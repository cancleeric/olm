"""SQLite settings persistence — shares ~/.config/run-ollama/settings.db with run-ollama.sh."""
import sqlite3
import os
from pathlib import Path

DEFAULTS: dict[str, str] = {
    "num_ctx": "262144",
    "keep_alive": "24h",
    "request_timeout": "28800",
    "chat_timeout": "21600",
    "default_model": "qwen3.6:27b",
    # 閘道輪新增：Ollama 私有埠（11551）與閘道公開埠（11434）
    "ollama_port": "11551",
    "gateway_host": "127.0.0.1",
    "gateway_port": "11434",
}


class Settings:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or os.environ.get(
            "OLLAMA_RUN_DB",
            str(Path.home() / ".config" / "run-ollama" / "settings.db"),
        )
        self._init()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS model_ctx(model TEXT PRIMARY KEY, num_ctx INTEGER)"
            )
            for k, v in DEFAULTS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v)
                )

    def get(self, key: str) -> str:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
            return row[0] if row else DEFAULTS.get(key, "")

    def set(self, key: str, value: str):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_model_ctx(self, model: str) -> int | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT num_ctx FROM model_ctx WHERE model=?", (model,)
            ).fetchone()
            return row[0] if row else None

    def set_model_ctx(self, model: str, ctx: int):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO model_ctx(model,num_ctx) VALUES(?,?) "
                "ON CONFLICT(model) DO UPDATE SET num_ctx=excluded.num_ctx",
                (model, ctx),
            )

    def del_model_ctx(self, model: str):
        with self._conn() as conn:
            conn.execute("DELETE FROM model_ctx WHERE model=?", (model,))

    def list_model_ctx(self) -> list[tuple[str, int]]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT model, num_ctx FROM model_ctx ORDER BY model"
            ).fetchall()

    def effective_ctx(self, model: str) -> int:
        v = self.get_model_ctx(model)
        return v if v is not None else self.num_ctx

    @property
    def num_ctx(self) -> int:
        return int(self.get("num_ctx"))

    @property
    def keep_alive(self) -> str:
        return self.get("keep_alive")

    @property
    def request_timeout(self) -> int:
        return int(self.get("request_timeout"))

    @property
    def chat_timeout(self) -> int:
        return int(self.get("chat_timeout"))

    @property
    def default_model(self) -> str:
        return self.get("default_model")

    @property
    def ollama_port(self) -> int:
        return int(self.get("ollama_port"))

    @property
    def gateway_host(self) -> str:
        return self.get("gateway_host")

    @property
    def gateway_port(self) -> int:
        return int(self.get("gateway_port"))


def parse_ctx(s: str) -> int | None:
    """'256K' → 262144, '128k', '1M', '65536' → int. None if invalid."""
    s = s.strip().lower()
    try:
        if s.endswith("k"):
            v = int(s[:-1]) * 1024
        elif s.endswith("m"):
            v = int(s[:-1]) * 1024 * 1024
        else:
            v = int(s)
        return v if v > 0 else None
    except ValueError:
        return None


def fmt_ctx(n: int | None) -> str:
    """65536 → '65536(64K)', None/'0' → '?'"""
    if not n:
        return "?"
    if n % 1024 == 0:
        return f"{n}({n // 1024}K)"
    return str(n)
