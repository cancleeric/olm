"""Ollama HTTP API client (stdlib only — no httpx/requests)."""
import json
import os
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

LOGFILE = "/tmp/ollama-serve.log"
PIDFILE = "/tmp/ollama-serve.pid"


class OllamaClient:
    def __init__(self, base_url: str | None = None):
        port = os.environ.get("OLLAMA_PORT", "11434")
        host = os.environ.get("OLLAMA_HOST", f"http://localhost:{port}")
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

    def load(self, model: str, ctx: int, keep_alive: str = "24h") -> bool:
        d = self._post(
            "/api/generate",
            {"model": model, "keep_alive": keep_alive, "options": {"num_ctx": ctx}},
            timeout=300,
        )
        return d is not None and d.get("done") is True

    def unload(self, model: str) -> bool:
        d = self._post("/api/generate", {"model": model, "keep_alive": 0}, timeout=30)
        return d is not None

    def bench(self, model: str, prompt: str = "Count from 1 to 20.") -> Optional[dict]:
        return self._post(
            "/api/generate",
            {"model": model, "prompt": prompt, "stream": False},
            timeout=300,
        )

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
        port: int = 11434,
        ctx: int = 262144,
        keep_alive: str = "24h",
        logfile: str = LOGFILE,
    ) -> Optional[int]:
        env = os.environ.copy()
        env["OLLAMA_HOST"] = f"0.0.0.0:{port}"
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

    def stop_server(self, port: int = 11434) -> bool:
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

    def server_pid(self, port: int = 11434) -> Optional[int]:
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
