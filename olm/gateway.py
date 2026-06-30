"""olm 閘道 — 透明轉發 127.0.0.1:11434 → 127.0.0.1:11551（純 stdlib）。

架構：
  外機請求 ──► (127.0.0.1:11434，外網碰不到) ──► Ollama (127.0.0.1:11551)
這一輪只插門（透明轉發），政策（白名單/認證）留下一輪。
# ponytail: 純透明轉發，下輪加白名單/認證只需在 _Handler._forward() 前插政策層
"""
import argparse
import http.server
import os
import signal
import threading
import urllib.error
import urllib.request

GATEWAY_PIDFILE = "/tmp/olm-gateway.pid"
GATEWAY_LOGFILE = "/tmp/olm-gateway.log"

_CHUNK = 8192
# urllib 已解 chunked 編碼，不再需要 Transfer-Encoding；Connection 不跨 hop
_SKIP_RESP = frozenset({"transfer-encoding", "connection"})
# Host 由 urllib 自動補正確目標；Connection 不跨 hop
_SKIP_REQ = frozenset({"host", "connection"})


def _make_handler(ollama_url: str, timeout: int):
    """閉包建立 ProxyHandler，帶入上游位址與 timeout。"""

    class _Handler(http.server.BaseHTTPRequestHandler):
        _up = ollama_url
        _to = timeout

        def log_message(self, fmt, *args):  # type: ignore[override]
            pass  # 靜音，避免污染 stderr

        def _forward(self):
            # SSRF 守衛：路徑必須以 / 開頭，防 GET @evil.com/ 被 urllib 解析成 userinfo@host
            if not self.path.startswith("/"):
                self.send_error(400, "Bad path")
                return
            target = self._up + self.path

            # 只在有 Content-Length 時讀 body（GET/HEAD 通常無 body，避免 blocking）
            cl = self.headers.get("Content-Length")
            body = self.rfile.read(int(cl)) if cl else None

            fwd_headers = {
                k: v for k, v in self.headers.items()
                if k.lower() not in _SKIP_REQ
            }
            req = urllib.request.Request(
                target, data=body, headers=fwd_headers, method=self.command
            )

            try:
                with urllib.request.urlopen(req, timeout=self._to) as resp:
                    self.send_response(resp.status)
                    for k, v in resp.headers.items():
                        if k.lower() not in _SKIP_RESP:
                            self.send_header(k, v)
                    self.end_headers()
                    # 逐 chunk 串流轉發（chat/generate/pull 是長 NDJSON，不可整包緩存）
                    if self.command != "HEAD":
                        while chunk := resp.read(_CHUNK):
                            self.wfile.write(chunk)
                            self.wfile.flush()

            except urllib.error.HTTPError as exc:
                body_err = exc.read()
                self.send_response(exc.code)
                self.send_header("Content-Length", str(len(body_err)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body_err)

            except (ConnectionResetError, BrokenPipeError):
                pass  # 客戶端主動中斷，正常現象

            except Exception as exc:
                try:
                    self.send_error(502, f"上游連線失敗：{exc}")
                except Exception:
                    pass

        do_GET = do_POST = do_DELETE = do_HEAD = do_PUT = _forward

    return _Handler


def serve(
    gateway_host: str,
    gateway_port: int,
    ollama_port: int,
    timeout: int = 21600,
) -> None:
    """啟動閘道，阻塞執行。由子程序（-m olm.gateway）呼叫。"""
    ollama_url = f"http://127.0.0.1:{ollama_port}"
    handler = _make_handler(ollama_url, timeout)
    server = http.server.ThreadingHTTPServer((gateway_host, gateway_port), handler)

    # 寫 pidfile，讓 olm stop 可找到並終止
    with open(GATEWAY_PIDFILE, "w") as f:
        f.write(str(os.getpid()))

    def _quit(sig, frame):
        # 背景執行緒 shutdown，避免 SIGTERM 死鎖
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _quit)
    signal.signal(signal.SIGINT, _quit)

    try:
        server.serve_forever()
    finally:
        try:
            os.unlink(GATEWAY_PIDFILE)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="olm 閘道子程序（由 olm start 管理，勿直接呼叫）")
    ap.add_argument("--gateway-host", default="127.0.0.1")
    ap.add_argument("--gateway-port", type=int, default=11434)
    ap.add_argument("--ollama-port", type=int, default=11551)
    ap.add_argument("--timeout", type=int, default=21600)
    a = ap.parse_args()
    serve(a.gateway_host, a.gateway_port, a.ollama_port, a.timeout)
