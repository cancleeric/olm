"""olm 閘道 — 透明轉發 127.0.0.1:11434 → 127.0.0.1:11551（純 stdlib）。

架構：
  外機請求 ──► (127.0.0.1:11434，外網碰不到) ──► Ollama (127.0.0.1:11551)
這一輪只插門（透明轉發），政策（白名單/認證）留下一輪。
# ponytail: 純透明轉發，下輪加白名單/認證只需在 _Handler._forward() 前插政策層
"""
import argparse
import http.server
import ipaddress
import json
import os
import signal
import threading
import time
import urllib.error
import urllib.request
import uuid

GATEWAY_PIDFILE = "/tmp/olm-gateway.pid"
GATEWAY_LOGFILE = "/tmp/olm-gateway.log"

_CHUNK = 8192
# urllib 已解 chunked 編碼，不再需要 Transfer-Encoding；Connection 不跨 hop
_SKIP_RESP = frozenset({"transfer-encoding", "connection"})
# Host 由 urllib 自動補正確目標；Connection 不跨 hop
_SKIP_REQ = frozenset({"host", "connection"})


def _ip_allowed(ip: str, allowed: list[str]) -> bool:
    """True if ip is in any of the CIDR ranges in allowed."""
    if not allowed:
        return False
    try:
        addr = ipaddress.ip_address(ip)
        for cidr in allowed:
            try:
                net = ipaddress.ip_network(cidr, strict=False)
                if addr in net:
                    return True
            except ValueError:
                continue
    except ValueError:
        pass
    return False


def _make_handler(ollama_url: str, timeout: int, allowed_cidrs: list[str] | None = None):
    """閉包建立 ProxyHandler，帶入上游位址、timeout 與 IP 白名單。"""

    class _Handler(http.server.BaseHTTPRequestHandler):
        _up = ollama_url
        _to = timeout
        _allowed_cidrs: list[str] | None = allowed_cidrs

        def log_message(self, fmt, *args):  # type: ignore[override]
            pass  # 靜音，避免污染 stderr

        def _forward(self):
            # IP 白名單政策（_allowed_cidrs 非 None 時啟用）
            if self.__class__._allowed_cidrs is not None:
                client_ip = self.client_address[0]
                if not _ip_allowed(client_ip, self.__class__._allowed_cidrs):
                    self.send_error(403, f"IP not allowed: {client_ip}")
                    return
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

        # ── OpenAI 相容層 helpers ─────────────────────────────────────

        def _ollama_request(self, path: str, body: dict | None = None, method: str = "GET") -> dict:
            """對上游 Ollama 發一次完整請求並回傳解析後的 JSON dict。僅用於非 streaming。"""
            url = self.__class__._up.rstrip("/") + path
            data = json.dumps(body).encode() if body is not None else None
            headers = {"Content-Type": "application/json"} if data else {}
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            with urllib.request.urlopen(req, timeout=self.__class__._to) as r:
                return json.loads(r.read())

        def _send_json(self, obj: dict, status: int = 200) -> None:
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _handle_v1_models(self) -> None:
            data = self._ollama_request("/api/tags")
            models = data.get("models", [])
            self._send_json({
                "object": "list",
                "data": [
                    {"id": m["name"], "object": "model", "created": 0, "owned_by": "ollama"}
                    for m in models
                ],
            })

        def _handle_v1_chat(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length))

            model = req.get("model", "")
            messages = req.get("messages", [])
            stream = req.get("stream", False)

            options: dict = {}
            if "temperature" in req:
                options["temperature"] = req["temperature"]
            if "max_tokens" in req:
                options["num_predict"] = req["max_tokens"]
            if "top_p" in req:
                options["top_p"] = req["top_p"]
            if "seed" in req:
                options["seed"] = req["seed"]

            ollama_body: dict = {"model": model, "messages": messages, "stream": stream}
            if options:
                ollama_body["options"] = options

            if not stream:
                resp = self._ollama_request("/api/chat", ollama_body, "POST")
                msg = resp.get("message", {})
                self._send_json({
                    "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "message": {
                            "role": msg.get("role", "assistant"),
                            "content": msg.get("content", ""),
                        },
                        "finish_reason": "stop",
                    }],
                    "usage": {
                        "prompt_tokens": resp.get("prompt_eval_count", 0),
                        "completion_tokens": resp.get("eval_count", 0),
                        "total_tokens": resp.get("prompt_eval_count", 0) + resp.get("eval_count", 0),
                    },
                })
            else:
                self._handle_v1_chat_stream(model, ollama_body)

        def _handle_v1_chat_stream(self, model: str, ollama_body: dict) -> None:
            chat_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
            created = int(time.time())

            url = self.__class__._up.rstrip("/") + "/api/chat"
            data = json.dumps(ollama_body).encode()
            req = urllib.request.Request(
                url, data=data, method="POST",
                headers={"Content-Type": "application/json"},
            )

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            def send_sse(obj: dict) -> None:
                self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
                self.wfile.flush()

            try:
                with urllib.request.urlopen(req, timeout=self.__class__._to) as r:
                    for raw_line in r:
                        raw_line = raw_line.strip()
                        if not raw_line:
                            continue
                        try:
                            chunk = json.loads(raw_line)
                        except Exception:
                            continue

                        msg = chunk.get("message", {})
                        content = msg.get("content", "")
                        done = chunk.get("done", False)

                        send_sse({
                            "id": chat_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [{
                                "index": 0,
                                "delta": {"content": content} if content else {},
                                "finish_reason": "stop" if done else None,
                            }],
                        })

                        if done:
                            break
            except (ConnectionResetError, BrokenPipeError):
                return

            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        def _handle_v1_embeddings(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length))

            model = req.get("model", "")
            inp = req.get("input", "")

            resp = self._ollama_request("/api/embed", {"model": model, "input": inp}, "POST")
            embeddings = resp.get("embeddings", [[]])

            self._send_json({
                "object": "list",
                "data": [
                    {"object": "embedding", "index": i, "embedding": emb}
                    for i, emb in enumerate(embeddings)
                ],
                "model": model,
                "usage": {"prompt_tokens": 0, "total_tokens": 0},
            })

        # ── HTTP verb handlers ────────────────────────────────────────

        def do_OPTIONS(self):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.end_headers()

        def do_GET(self):
            if self.path == "/v1/models":
                self._handle_v1_models()
                return
            self._forward()

        def do_POST(self):
            if self.path == "/v1/chat/completions":
                self._handle_v1_chat()
                return
            if self.path == "/v1/embeddings":
                self._handle_v1_embeddings()
                return
            self._forward()

        do_DELETE = do_HEAD = do_PUT = _forward

    return _Handler


def serve(
    gateway_host: str,
    gateway_port: int,
    ollama_port: int,
    timeout: int = 21600,
    allowed_cidrs: list[str] | None = None,
) -> None:
    """啟動閘道，阻塞執行。由子程序（-m olm.gateway）呼叫。"""
    ollama_url = f"http://127.0.0.1:{ollama_port}"
    handler = _make_handler(ollama_url, timeout, allowed_cidrs)
    server = http.server.ThreadingHTTPServer((gateway_host, gateway_port), handler)

    # 定期重載 IP 白名單（每 60 秒從 DB 更新 handler class variable）
    if allowed_cidrs is not None:
        def _reload_acl():
            try:
                from .db import Settings
                handler._allowed_cidrs = Settings().gateway_load_cidrs()
            except Exception:
                pass
            t = threading.Timer(60, _reload_acl)
            t.daemon = True
            t.start()
        t0 = threading.Timer(60, _reload_acl)
        t0.daemon = True
        t0.start()

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
    ap.add_argument("--allow", action="append", default=None, dest="allow",
                    help="允許的 IP 或 CIDR（可多次，例如 --allow 192.168.0.0/24）")
    a = ap.parse_args()
    serve(a.gateway_host, a.gateway_port, a.ollama_port, a.timeout, a.allow)
