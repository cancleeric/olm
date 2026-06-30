"""SQLite settings persistence — shares ~/.config/run-ollama/settings.db with run-ollama.sh."""
import json
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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS presets (
                    name TEXT PRIMARY KEY,
                    model TEXT,
                    system_prompt TEXT,
                    temperature REAL,
                    top_p REAL,
                    top_k INTEGER,
                    stop_seqs TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    model TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conv_id INTEGER REFERENCES conversations(id),
                    role TEXT,
                    content TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS gateway_acl (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cidr TEXT UNIQUE NOT NULL,
                    note TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bench_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model TEXT NOT NULL,
                    gen_tps REAL,
                    prompt_tps REAL,
                    total_s REAL,
                    eval_count INTEGER,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            try:
                conn.execute("ALTER TABLE presets ADD COLUMN extra TEXT")
            except Exception:
                pass  # already exists
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

    # ── Preset CRUD ───────────────────────────────────────────────────────────

    def save_preset(
        self,
        name: str,
        model: str | None,
        system_prompt: str | None,
        temperature: float | None,
        top_p: float | None,
        top_k: int | None,
        stop_seqs: list[str] | None,
        extra: dict | None = None,
    ):
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO presets"
                "(name,model,system_prompt,temperature,top_p,top_k,stop_seqs,extra)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    name, model, system_prompt, temperature, top_p, top_k,
                    json.dumps(stop_seqs) if stop_seqs else None,
                    json.dumps(extra) if extra else None,
                ),
            )

    def get_preset(self, name: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT name,model,system_prompt,temperature,top_p,top_k,stop_seqs,extra"
                " FROM presets WHERE name=?",
                (name,),
            ).fetchone()
        if not row:
            return None
        extra = json.loads(row[7]) if row[7] else {}
        return {
            "name": row[0],
            "model": row[1],
            "system_prompt": row[2],
            "temperature": row[3],
            "top_p": row[4],
            "top_k": row[5],
            "stop_seqs": json.loads(row[6]) if row[6] else None,
            "repeat_penalty": extra.get("repeat_penalty"),
            "min_p": extra.get("min_p"),
            "seed": extra.get("seed"),
        }

    def list_presets(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT name,model,system_prompt,temperature,top_p,top_k,created_at"
                " FROM presets ORDER BY created_at DESC"
            ).fetchall()
        return [
            {
                "name": r[0],
                "model": r[1],
                "system_prompt": r[2],
                "temperature": r[3],
                "top_p": r[4],
                "top_k": r[5],
                "created_at": r[6],
            }
            for r in rows
        ]

    def delete_preset(self, name: str) -> bool:
        with self._conn() as conn:
            c = conn.execute("DELETE FROM presets WHERE name=?", (name,))
            return c.rowcount > 0

    # ── Gateway ACL ────────────────────────────────────────────────────────────

    def gateway_add_allow(self, cidr: str, note: str = "") -> bool:
        """Add CIDR to whitelist. Returns False if already exists."""
        try:
            import ipaddress
            ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            raise ValueError(f"無效的 IP/CIDR：{cidr}")
        try:
            with self._conn() as conn:
                conn.execute("INSERT INTO gateway_acl(cidr,note) VALUES(?,?)", (cidr, note))
            return True
        except Exception:
            return False

    def gateway_remove_allow(self, cidr: str) -> bool:
        with self._conn() as conn:
            c = conn.execute("DELETE FROM gateway_acl WHERE cidr=?", (cidr,))
            return c.rowcount > 0

    def gateway_list_allow(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT cidr,note,created_at FROM gateway_acl ORDER BY created_at"
            ).fetchall()
        return [{"cidr": r[0], "note": r[1], "created_at": r[2]} for r in rows]

    def gateway_load_cidrs(self) -> list[str]:
        """Load current whitelist CIDRs for runtime use."""
        return [r["cidr"] for r in self.gateway_list_allow()]

    # ── Bench History ──────────────────────────────────────────────────────────

    def add_bench_result(self, model: str, gen_tps: float, prompt_tps: float, total_s: float, eval_count: int):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO bench_history(model,gen_tps,prompt_tps,total_s,eval_count) VALUES(?,?,?,?,?)",
                (model, gen_tps, prompt_tps, total_s, eval_count),
            )

    def list_bench_history(self, model: str | None = None, limit: int = 10) -> list[dict]:
        with self._conn() as conn:
            if model:
                rows = conn.execute(
                    "SELECT model,gen_tps,prompt_tps,total_s,eval_count,created_at"
                    " FROM bench_history WHERE model=? ORDER BY created_at DESC LIMIT ?",
                    (model, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT model,gen_tps,prompt_tps,total_s,eval_count,created_at"
                    " FROM bench_history ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [
            {"model": r[0], "gen_tps": r[1], "prompt_tps": r[2],
             "total_s": r[3], "eval_count": r[4], "created_at": r[5]}
            for r in rows
        ]

    # ── Conversation history CRUD ────────────────────────────────────────────

    def create_conversation(self, name: str | None, model: str) -> int:
        with self._conn() as conn:
            c = conn.execute(
                "INSERT INTO conversations(name,model) VALUES(?,?)", (name, model)
            )
            return c.lastrowid

    def add_message(self, conv_id: int, role: str, content: str):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO messages(conv_id,role,content) VALUES(?,?,?)", (conv_id, role, content)
            )
            conn.execute(
                "UPDATE conversations SET updated_at=datetime('now') WHERE id=?", (conv_id,)
            )

    def list_conversations(self, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT c.id,c.name,c.model,c.created_at,c.updated_at,COUNT(m.id) as msg_count "
                "FROM conversations c LEFT JOIN messages m ON m.conv_id=c.id "
                "GROUP BY c.id ORDER BY c.updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {"id": r[0], "name": r[1], "model": r[2],
             "created_at": r[3], "updated_at": r[4], "msg_count": r[5]}
            for r in rows
        ]

    def get_conversation_messages(self, conv_id: int) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT role,content,created_at FROM messages WHERE conv_id=? ORDER BY id", (conv_id,)
            ).fetchall()
        return [{"role": r[0], "content": r[1], "created_at": r[2]} for r in rows]

    def delete_conversation(self, conv_id: int) -> bool:
        with self._conn() as conn:
            conn.execute("DELETE FROM messages WHERE conv_id=?", (conv_id,))
            c = conn.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
            return c.rowcount > 0

    def export_conversation_md(self, conv_id: int) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT name,model,created_at FROM conversations WHERE id=?", (conv_id,)
            ).fetchone()
        if not row:
            return None
        name, model, created_at = row
        messages = self.get_conversation_messages(conv_id)
        lines = [f"# {name or f'Conversation {conv_id}'}", f"Model: {model} | Created: {created_at}", ""]
        for m in messages:
            role_label = {
                "user": "**你**", "assistant": "**AI**",
                "system": "**System**", "tool": "**Tool**",
            }.get(m["role"], m["role"])
            lines.append(f"{role_label}: {m['content']}")
            lines.append("")
        return "\n".join(lines)


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
