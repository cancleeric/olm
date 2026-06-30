"""Ollama HTTP API client (stdlib only — no httpx/requests)."""
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional
import re

LOGFILE = "/tmp/ollama-serve.log"
PIDFILE = "/tmp/ollama-serve.pid"

# 閘道常數（對應 gateway.py）
from .gateway import GATEWAY_PIDFILE, GATEWAY_LOGFILE


def _sysmem() -> tuple[Optional[int], Optional[int], Optional[int]]:
    """Return (total_bytes, used_bytes, free_bytes) matching Activity Monitor."""
    try:
        total = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"]).strip())
    except Exception:
        return None, None, None
    try:
        vm = subprocess.check_output(["vm_stat"]).decode()
    except Exception:
        return total, None, None
    pg = 4096
    m = re.search(r"page size of (\d+)", vm)
    if m:
        pg = int(m.group(1))

    def pages(name: str) -> int:
        mm = re.search(re.escape(name) + r":\s+(\d+)\.", vm)
        return int(mm.group(1)) * pg if mm else 0

    used = pages("Pages active") + pages("Pages wired down") + pages("Pages occupied by compressor")
    return total, used, total - used


class OllamaClient:
    def __init__(self, base_url: str | None = None):
        # 閘道輪後：olm client 直連私有埠 11551，繞過自己的門（免多一跳）
        port = os.environ.get("OLLAMA_PORT", "11551")
        host = os.environ.get("OLLAMA_HOST", f"http://127.0.0.1:{port}")
        self.base_url = (base_url or host).rstrip("/")
        self._ctx_cache: dict[str, int] = {}

    def _get(self, path: str, timeout: int = 5) -> Optional[dict]:
        try:
            with urllib.request.urlopen(self.base_url + path, timeout=timeout) as r:
                return json.load(r)
        except Exception:
            return None

    def _post(self, path: str, payload: dict, timeout: int = 10) -> Optional[dict]:
        try:
            req = urllib.request.Request(
                self.base_url + path,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception:
            return None

    def is_running(self) -> bool:
        return self._get("/api/tags") is not None

    def list_models(self) -> list[dict]:
        d = self._get("/api/tags")
        return (d or {}).get("models", [])

    def list_loaded(self) -> list[dict]:
        d = self._get("/api/ps")
        return (d or {}).get("models", [])

    def show(self, model: str) -> Optional[dict]:
        return self._post("/api/show", {"model": model}, timeout=10)

    def model_max_ctx(self, model: str) -> Optional[int]:
        if model in self._ctx_cache:
            return self._ctx_cache[model]
        result = self._fetch_max_ctx(model)
        if result is not None:
            self._ctx_cache[model] = result
        return result

    def _fetch_max_ctx(self, model: str) -> Optional[int]:
        d = self.show(model)
        if not d:
            return None
        mi = d.get("model_info", {}) or {}
        arch = mi.get("general.architecture", "")
        v = mi.get(f"{arch}.context_length")
        if v is None:
            for k, val in mi.items():
                if k.endswith(".context_length"):
                    v = val
                    break
        return v

    def clear_ctx_cache(self) -> None:
        self._ctx_cache.clear()

    def pull_stream(self, model: str):
        """Stream NDJSON from /api/pull, yield each parsed dict."""
        payload = json.dumps({"model": model, "stream": True}).encode()
        req = urllib.request.Request(
            self.base_url + "/api/pull",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=7200) as r:
            for raw_line in r:
                line = raw_line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue

    def chat_stream(
        self,
        model: str,
        messages: list[dict],
        options: dict | None = None,
        keep_alive: str = "24h",
        timeout: int = 21600,
        tools: list[dict] | None = None,
        fmt: str | None = None,
    ):
        """串流 /api/chat NDJSON，每次 yield 一個 chunk dict。
        容錯：空行跳過、JSONDecodeError 跳過；連線/逾時錯誤向上拋。
        """
        payload_dict: dict = {
            "model": model,
            "messages": messages,
            "stream": True,
            "keep_alive": keep_alive,
            "options": options or {},
        }
        if tools:
            payload_dict["tools"] = tools
        if fmt:
            payload_dict["format"] = fmt
        payload = json.dumps(payload_dict).encode()
        req = urllib.request.Request(
            self.base_url + "/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw_line in r:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def load(self, model: str, ctx: int, keep_alive: str = "24h", num_gpu: int | None = None) -> bool:
        opts: dict = {"num_ctx": ctx}
        if num_gpu is not None:
            opts["num_gpu"] = num_gpu
        d = self._post(
            "/api/generate",
            {"model": model, "keep_alive": keep_alive, "options": opts},
            timeout=300,
        )
        return d is not None and d.get("done") is True

    def unload(self, model: str) -> bool:
        d = self._post("/api/generate", {"model": model, "keep_alive": 0}, timeout=30)
        return d is not None

    def delete(self, model: str) -> bool:
        """從磁碟刪除模型（DELETE /api/delete）。"""
        try:
            req = urllib.request.Request(
                self.base_url + "/api/delete",
                data=json.dumps({"model": model}).encode(),
                headers={"Content-Type": "application/json"},
                method="DELETE",
            )
            with urllib.request.urlopen(req, timeout=30):
                return True
        except Exception:
            return False

    def bench(self, model: str, prompt: str = "Count from 1 to 20.") -> Optional[dict]:
        return self._post(
            "/api/generate",
            {"model": model, "prompt": prompt, "stream": False},
            timeout=300,
        )

    def embed(self, text: str, model: str = "nomic-embed-text") -> Optional[dict]:
        return self._post("/api/embed", {"model": model, "input": text}, timeout=60)

    def disk_models(self) -> list[dict]:
        """Read manifest files from disk when service is down."""
        base_dir = os.environ.get("OLLAMA_MODELS") or str(
            Path.home() / ".ollama" / "models"
        )
        mdir = os.path.join(base_dir, "manifests")
        out: list[dict] = []
        if not os.path.isdir(mdir):
            return out
        for root, _dirs, files in os.walk(mdir):
            for fn in files:
                if fn == ".DS_Store":
                    continue
                fp = os.path.join(root, fn)
                parts = os.path.relpath(fp, mdir).split(os.sep)
                if len(parts) < 2:
                    continue
                tag, model = parts[-1], parts[-2]
                ns = parts[-3] if len(parts) >= 3 else ""
                name = (
                    f"{model}:{tag}"
                    if ns in ("", "library")
                    else f"{ns}/{model}:{tag}"
                )
                try:
                    with open(fp) as f:
                        man = json.load(f)
                except Exception:
                    continue
                size = man.get("config", {}).get("size", 0)
                for layer in man.get("layers", []):
                    size += layer.get("size", 0)
                out.append({"name": name, "size": size})
        out.sort(key=lambda x: x["name"])
        return out

    def start_server(
        self,
        port: int = 11551,
        ctx: int = 262144,
        keep_alive: str = "24h",
        logfile: str = LOGFILE,
    ) -> Optional[int]:
        env = os.environ.copy()
        # 閘道輪：Ollama 只綁本機私有埠（127.0.0.1），外網無法直達
        env["OLLAMA_HOST"] = f"127.0.0.1:{port}"
        env["OLLAMA_NUM_CTX"] = str(ctx)
        env["OLLAMA_KEEP_ALIVE"] = keep_alive
        with open(logfile, "a") as log:
            proc = subprocess.Popen(
                ["ollama", "serve"],
                stdout=log,
                stderr=log,
                env=env,
                start_new_session=True,
            )
        with open(PIDFILE, "w") as f:
            f.write(str(proc.pid))
        return proc.pid

    def stop_server(self, port: int = 11551) -> bool:
        """停止 Ollama 服務（私有埠）。"""
        try:
            result = subprocess.check_output(
                ["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN", "-Pn"],
                stderr=subprocess.DEVNULL,
            ).decode().strip()
            if result:
                pids = result.split()
                for pid in pids:
                    subprocess.run(["kill", pid], check=False)
                return True
        except subprocess.CalledProcessError:
            pass
        try:
            subprocess.run(["pkill", "-f", "ollama serve"], check=False)
            return True
        except Exception:
            return False

    def server_pid(self, port: int = 11551) -> Optional[int]:
        """取得 Ollama 私有埠的 PID。"""
        try:
            result = subprocess.check_output(
                ["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN", "-Pn"],
                stderr=subprocess.DEVNULL,
            ).decode().strip()
            if result:
                return int(result.split()[0])
        except Exception:
            pass
        return None

    # ── 閘道管理 ─────────────────────────────────────────────────────────────

    def start_gateway(
        self,
        gateway_host: str = "127.0.0.1",
        gateway_port: int = 11434,
        ollama_port: int = 11551,
        timeout: int = 21600,
        logfile: str = GATEWAY_LOGFILE,
    ) -> Optional[int]:
        """背景啟動閘道子程序（-m olm.gateway），回傳子程序 PID。
        啟動前檢查埠是否已被佔用，友善提示（含 Ollama.app 偵測）。
        """
        try:
            occupant = subprocess.check_output(
                ["lsof", "-t", f"-iTCP:{gateway_port}", "-sTCP:LISTEN", "-Pn"],
                stderr=subprocess.DEVNULL,
            ).decode().strip()
        except subprocess.CalledProcessError:
            occupant = ""

        if occupant:
            pid_str = occupant.split()[0]
            # 查程序名，判斷是否 Ollama.app
            try:
                pname = subprocess.check_output(
                    ["ps", "-p", pid_str, "-o", "comm="],
                    stderr=subprocess.DEVNULL,
                ).decode().strip()
            except Exception:
                pname = "unknown"
            msg = f"埠 {gateway_port} 已被 PID={pid_str}({pname}) 佔用"
            if "ollama" in pname.lower():
                msg += (
                    f"\n偵測到 Ollama.app 正佔用 {gateway_port}，"
                    "請從選單列退出 Ollama.app 後再 olm start"
                )
            raise RuntimeError(msg)

        # 讀取 IP 白名單，加到閘道子程序參數
        try:
            from .db import Settings as _Settings
            cidrs = _Settings().gateway_load_cidrs()
        except Exception:
            cidrs = []
        args_extra: list[str] = []
        for c in cidrs:
            args_extra += ["--allow", c]

        with open(logfile, "a") as log:
            proc = subprocess.Popen(
                [
                    sys.executable, "-m", "olm.gateway",
                    "--gateway-host", gateway_host,
                    "--gateway-port", str(gateway_port),
                    "--ollama-port", str(ollama_port),
                    "--timeout", str(timeout),
                    *args_extra,
                ],
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        return proc.pid

    def stop_gateway(self, gateway_port: int = 11434) -> bool:
        """停止閘道：先試 pidfile，再試 lsof 掃埠。"""
        # 嘗試從 pidfile 讀 PID
        try:
            with open(GATEWAY_PIDFILE) as f:
                pid = f.read().strip()
            if pid:
                subprocess.run(["kill", pid], check=False)
                return True
        except FileNotFoundError:
            pass
        # fallback：掃 gateway_port
        try:
            result = subprocess.check_output(
                ["lsof", "-t", f"-iTCP:{gateway_port}", "-sTCP:LISTEN", "-Pn"],
                stderr=subprocess.DEVNULL,
            ).decode().strip()
            if result:
                for pid in result.split():
                    subprocess.run(["kill", pid], check=False)
                return True
        except subprocess.CalledProcessError:
            pass
        return False

    def gateway_pid(self, gateway_port: int = 11434) -> Optional[int]:
        """取得閘道進程 PID（優先 lsof 確認真的在監聽）。"""
        try:
            result = subprocess.check_output(
                ["lsof", "-t", f"-iTCP:{gateway_port}", "-sTCP:LISTEN", "-Pn"],
                stderr=subprocess.DEVNULL,
            ).decode().strip()
            if result:
                return int(result.split()[0])
        except Exception:
            pass
        return None


def search_models(keyword: str, limit: int = 20) -> list[dict]:
    """Scrape ollama.com/search?q= (HTMX fragment, HX-Request header required).
    Returns [{name, description, capabilities, sizes, pulls, tags, updated}].
    Raises ConnectionError on failure.
    """
    import html.parser
    import urllib.parse
    url = f"https://ollama.com/search?q={urllib.parse.quote_plus(keyword)}"
    req = urllib.request.Request(
        url,
        headers={"HX-Request": "true", "User-Agent": "olm/1"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            html_text = r.read().decode("utf-8", errors="replace")
    except Exception as exc:
        raise ConnectionError(f"ollama.com 連線失敗：{exc}") from exc

    class _P(html.parser.HTMLParser):
        def __init__(self):
            super().__init__()
            self.items: list[list[str]] = []
            self._in_li = False
            self._stack: list[str] = []
            self._li_depth = 0
            self._buf: list[str] = []

        def handle_starttag(self, tag, attrs):
            self._stack.append(tag)
            if tag == "li":
                self._in_li = True
                self._buf = []
                self._li_depth = len(self._stack)

        def handle_endtag(self, tag):
            if self._stack and self._stack[-1] == tag:
                self._stack.pop()
            if self._in_li and tag == "li" and len(self._stack) < self._li_depth:
                self._in_li = False
                self.items.append(self._buf[:])

        def handle_data(self, d):
            if self._in_li:
                s = d.strip()
                if s:
                    self._buf.append(s)

    p = _P()
    p.feed(html_text)

    _SKIP = frozenset({"Models", "Docs", "Pricing", "Download", "Sign In", "Log In", "Blog", "Enterprise"})
    _KNOWN_CAPS = frozenset({"tools", "vision", "embed", "code", "math", "audio"})
    _SIZE_RE = re.compile(r'^\d+\.?\d*[bBkKmMgGtT]$')
    _NUM_RE = re.compile(r'^[\d.,]+[KMBkmb]?$')

    results: list[dict] = []
    for buf in p.items:
        if len(buf) < 2:
            continue
        name = buf[0]
        if name in _SKIP or not re.match(r'^[a-z0-9._/:-]+$', name):
            continue
        desc = buf[1] if len(buf) > 1 else ""
        capabilities: list[str] = []
        sizes: list[str] = []
        pulls = "?"
        tags = "?"
        updated = ""
        i = 2
        while i < len(buf):
            v = buf[i]
            if v.lower() in _KNOWN_CAPS:
                capabilities.append(v.lower())
                i += 1
            elif _NUM_RE.match(v) and i + 1 < len(buf) and buf[i + 1] == "Pulls":
                pulls = v
                i += 2
            elif _NUM_RE.match(v) and i + 1 < len(buf) and buf[i + 1] == "Tags":
                tags = v
                i += 2
            elif v == "Pulls":
                i += 1
            elif _SIZE_RE.match(v):
                sizes.append(v)
                i += 1
            elif v == "Tags":
                i += 1
            elif v == "Updated" and i + 1 < len(buf):
                updated = buf[i + 1]
                i += 2
            else:
                i += 1
        results.append({
            "name": name,
            "description": desc,
            "capabilities": capabilities,
            "sizes": sizes,
            "pulls": pulls,
            "tags": tags,
            "updated": updated,
        })
        if len(results) >= limit:
            break
    return results


def fetch_model_tags(model_base: str) -> list[dict]:
    """從 ollama.com/library/<model>/tags 爬取可用 tag 和大小。回傳 [{"tag": "7b", "size": "4.1 GB"}, ...]"""
    import urllib.parse
    name = model_base.split(":")[0]  # 去掉 tag
    url = f"https://ollama.com/library/{urllib.parse.quote(name)}/tags"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "olm/0.1", "HX-Request": "true"})
        with urllib.request.urlopen(req, timeout=8) as r:
            html = r.read().decode("utf-8", errors="replace")
    except Exception:
        return []
    tags = []
    tag_re = re.compile(r'href="/library/' + re.escape(name) + r':([^"]+)"')
    size_re = re.compile(r'([\d.]+\s*(?:GB|MB|KB))', re.IGNORECASE)
    lines = html.split('\n')
    for i, line in enumerate(lines):
        m = tag_re.search(line)
        if m:
            tag = m.group(1)
            size = ""
            for j in range(i, min(i + 5, len(lines))):
                sm = size_re.search(lines[j])
                if sm:
                    size = sm.group(1)
                    break
            if tag and ":" not in tag:
                tags.append({"tag": tag, "size": size})
    return tags[:20]
