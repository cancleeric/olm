"""Interactive dashboard / menu mode for olm."""
import os
import json as _json_mod
import subprocess
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from . import __version__
from .api import OllamaClient, LOGFILE, GATEWAY_LOGFILE, _sysmem
from .db import Settings, parse_ctx, fmt_ctx

console = Console()


def _gguf_read_ctx(path: str) -> Optional[int]:
    """從 GGUF binary 讀 context_length（最小解析，不依賴外部套件）。"""
    import struct
    _GGUF_TYPES = {
        0: ("B", 1), 1: ("b", 1), 2: ("H", 2), 3: ("h", 2),
        4: ("I", 4), 5: ("i", 4), 6: ("f", 4), 7: ("?", 1),
        10: ("Q", 8), 11: ("q", 8), 12: ("d", 8),
    }
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"GGUF":
                return None
            version = struct.unpack("<I", f.read(4))[0]
            if version not in (2, 3):
                return None
            _n_tensors = struct.unpack("<Q", f.read(8))[0]
            n_kv = struct.unpack("<Q", f.read(8))[0]

            def read_str() -> str:
                ln = struct.unpack("<Q", f.read(8))[0]
                return f.read(ln).decode("utf-8", errors="replace")

            def skip_value(vtype: int) -> None:
                if vtype in _GGUF_TYPES:
                    f.read(_GGUF_TYPES[vtype][1])
                elif vtype == 8:  # string
                    ln = struct.unpack("<Q", f.read(8))[0]
                    f.read(ln)
                elif vtype == 9:  # array
                    elem_type = struct.unpack("<I", f.read(4))[0]
                    count = struct.unpack("<Q", f.read(8))[0]
                    for _ in range(count):
                        skip_value(elem_type)

            for _ in range(n_kv):
                key = read_str()
                vtype = struct.unpack("<I", f.read(4))[0]
                if key.endswith(".context_length") or key == "context_length":
                    if vtype in _GGUF_TYPES:
                        fmt, sz = _GGUF_TYPES[vtype]
                        val = struct.unpack(f"<{fmt}", f.read(sz))[0]
                        return int(val)
                skip_value(vtype)
    except Exception:
        pass
    return None


def _read_model_ctx_from_disk(model_name: str) -> str:
    """從 OLLAMA_MODELS manifest 找 GGUF model layer，讀 context_length；不需 Ollama 在跑。"""
    base = os.environ.get("OLLAMA_MODELS") or os.path.expanduser("~/.ollama/models")
    if ":" in model_name:
        name, tag = model_name.rsplit(":", 1)
    else:
        name, tag = model_name, "latest"

    if "/" not in name:
        name = f"library/{name}"

    manifest_path = os.path.join(base, "manifests", "registry.ollama.ai", name, tag)
    if not os.path.exists(manifest_path):
        return "?"

    try:
        with open(manifest_path) as f:
            manifest = _json_mod.load(f)

        # 找 model layer（mediaType 以 .model 結尾）
        model_digest = None
        for layer in manifest.get("layers", []):
            mt = layer.get("mediaType", "")
            if mt.endswith(".model"):
                model_digest = layer.get("digest", "")
                break
        if not model_digest:
            return "?"

        blob_hash = model_digest.replace("sha256:", "")
        blob_path = os.path.join(base, "blobs", f"sha256-{blob_hash}")
        if not os.path.exists(blob_path):
            return "?"

        ctx = _gguf_read_ctx(blob_path)
        if not ctx:
            return "?"
        if ctx >= 1024:
            return f"{ctx // 1024}K"
        return str(ctx)
    except Exception:
        return "?"


def _gb(b: int) -> float:
    return b / 1e9


def _vram_str(size_vram: int) -> str:
    """size_vram bytes → 顯示字串；0 或缺值顯示 '-'。"""
    if not size_vram:
        return "-"
    return f"{size_vram / 1e9:.1f} GB"


def _pick(prompt: str, options: list[str], default: str = "") -> str:
    """Simple numbered picker. Returns selected value."""
    if not options:
        return default
    if len(options) == 1:
        return options[0]
    console.print()
    for i, opt in enumerate(options, 1):
        console.print(f"  [cyan]{i:2})[/cyan] {opt}")
    choice = input(f"  {prompt} [1-{len(options)}] (Enter={default or options[0]}): ").strip()
    if not choice:
        return default or options[0]
    try:
        idx = int(choice)
        if 1 <= idx <= len(options):
            return options[idx - 1]
    except ValueError:
        pass
    if choice and not choice.isdigit():
        console.print(f"[yellow]無效選擇，使用預設：{default or options[0]}[/yellow]")
    return default or options[0]


def _render_dashboard(client: OllamaClient, settings: Settings):
    os.system("clear")
    running = client.is_running()
    ol_pid = client.server_pid(settings.ollama_port) if running else None
    gw_pid = client.gateway_pid(settings.gateway_port)

    # ── Service status panel ──────────────────────────────────
    svc_color = "green" if running else "red"
    svc_label = "running" if running else "stopped"
    gw_color = "green" if gw_pid else "red"
    gw_label = "running" if gw_pid else "stopped"
    total_b, used_b, free_b = _sysmem()

    lines = [
        f"閘道(gate): [{gw_color} bold]{gw_label}[/]  127.0.0.1:{settings.gateway_port}  pid=[bold]{gw_pid or '?'}[/]",
        f"Ollama:     [{svc_color} bold]{svc_label}[/]  127.0.0.1:{settings.ollama_port}  pid=[bold]{ol_pid or '?'}[/]",
        f"default_model: [bold]{settings.default_model}[/]",
        f"context_length: [bold]{fmt_ctx(settings.num_ctx)}[/]",
        f"request_timeout: [bold]{settings.request_timeout}s[/]   chat_timeout: [bold]{settings.chat_timeout}s[/]",
        f"keep_alive: [bold]{settings.keep_alive}[/]",
    ]
    if total_b:
        if used_b is not None:
            pct = used_b / total_b * 100
            lines.append(
                f"ram: total=[bold]{_gb(total_b):.1f}[/] GB  "
                f"used=[bold]{_gb(used_b):.1f}[/] GB ([bold]{pct:.1f}%[/])  "
                f"free=[bold]{_gb(free_b):.1f}[/] GB"
            )
        else:
            lines.append(f"ram: total={_gb(total_b):.1f} GB")

    console.print(Panel("\n".join(lines), title=f"[green bold]Ollama Dashboard v{__version__}[/]", border_style="green"))

    # ── Loaded Models ─────────────────────────────────────────
    loaded = client.list_loaded() if running else []
    loaded_table = Table(box=box.SIMPLE, show_header=True, header_style="green bold")
    loaded_table.add_column("Model", style="bold")
    loaded_table.add_column("RAM", justify="right")
    loaded_table.add_column("VRAM", justify="right")
    loaded_table.add_column("ctx(actual/max)")
    loaded_table.add_column("expires")

    if not loaded:
        console.print(Panel("（無）", title="[green bold]Loaded Models[/]", border_style="dim"))
    else:
        loaded_total = 0
        loaded_vram_total = 0
        for m in loaded:
            sz = m.get("size", 0)
            sv = m.get("size_vram", 0)
            loaded_total += sz
            loaded_vram_total += sv
            actual = m.get("context_length")
            mx = client.model_max_ctx(m["name"])
            ctx_str = f"{fmt_ctx(actual)}/{fmt_ctx(mx)}"
            warn = ""
            if actual and actual < settings.effective_ctx(m["name"]):
                warn = " [yellow]⚠ 降載[/]"
            loaded_table.add_row(
                m["name"],
                f"{_gb(sz):.1f} GB",
                _vram_str(sv),
                ctx_str + warn,
                _fmt_expires(m.get("expires_at", "?")),
            )
        loaded_table.add_section()
        loaded_table.add_row(
            f"[dim]total ({len(loaded)} models)[/]",
            f"[bold]{_gb(loaded_total):.1f} GB[/]",
            f"[bold]{_vram_str(loaded_vram_total)}[/]",
            "", "",
        )
        console.print(Panel(loaded_table, title="[green bold]Loaded Models[/]", border_style="green"))

    # ── Installed Models ─────────────────────────────────────
    from_disk = False
    if running:
        installed = client.list_models()
    else:
        installed = client.disk_models()
        from_disk = bool(installed)

    inst_table = Table(box=box.SIMPLE, show_header=True, header_style="green bold")
    inst_table.add_column("#", justify="right", style="cyan")
    inst_table.add_column("Model", style="bold")
    inst_table.add_column("Size", justify="right")
    inst_table.add_column("支援 ctx")
    inst_table.add_column("Q", style="dim", justify="center")
    inst_table.add_column("fits?", justify="center")

    hint = " [yellow](讀自磁碟)[/]" if from_disk else ""
    if not installed:
        console.print(Panel("（無）", title=f"[green bold]Installed Models[/]{hint}", border_style="dim"))
    else:
        for i, m in enumerate(installed, 1):
            if running:
                mx = client.model_max_ctx(m["name"])
                ctx_col = fmt_ctx(mx) if mx else _read_model_ctx_from_disk(m["name"])
            else:
                ctx_col = _read_model_ctx_from_disk(m["name"])
            # #3 MLX 格式標注
            if ctx_col == "?" and "mlx" in m["name"].lower():
                ctx_col = "[dim]?(MLX)[/dim]"
            # #2 fits? 欄：與可用 RAM 比較
            size_b = m.get("size", 0)
            if not size_b or not free_b:
                fits_str = "[dim]?[/dim]"
            elif size_b < free_b * 0.7:
                fits_str = "[green]✓[/green]"
            elif size_b < free_b:
                fits_str = "[yellow]![/yellow]"
            else:
                fits_str = "[red]✗[/red]"
            inst_table.add_row(str(i), m["name"], f"{m.get('size', 0) / 1e9:.1f} GB", ctx_col, _quant_label(m["name"]), fits_str)
        console.print(Panel(inst_table, title=f"[green bold]Installed Models[/]{hint}", border_style="green"))

    # ── Actions ───────────────────────────────────────────────
    actions = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    actions.add_column("key", style="cyan", justify="right")
    actions.add_column("cmd")
    actions.add_column("desc", style="dim")
    rows = [
        ("1", "Start service", "啟動 Ollama（不預載）"),
        ("2", "Stop service", "停止 Ollama"),
        ("3", "Restart service", "重啟服務"),
        ("4", "Load model", "預熱到記憶體"),
        ("5", "Run model chat", "互動對話"),
        ("6", "Pull model", "下載新模型"),
        ("7", "Unload model", "從記憶體卸載"),
        ("8", "Show model info", "查模型詳細資訊"),
        ("9", "Logs", "查背景服務日誌"),
        ("0", "Benchmark", "測試 tok/s 推論速度"),
        ("d", "Delete model", "從磁碟刪除模型"),
        ("f", "Search models", "搜尋 Ollama 模型庫"),
        ("g", "Gateway", "閘道 IP 白名單管理"),
        ("h", "History", "對話歷史"),
        ("p", "Presets", "預設集管理"),
        ("s", "Settings", "調整設定（存 SQLite）"),
        ("r", "Refresh", "重新整理"),
        ("q", "Quit", "離開"),
    ]
    for key, cmd, desc in rows:
        if not running and key in ("4", "5", "7"):
            actions.add_row(
                f"[dim]{key})[/dim]",
                f"[dim]{cmd}[/dim]",
                f"[dim]{desc}  (需先啟動)[/dim]",
            )
        else:
            actions.add_row(f"{key})", cmd, desc)
    console.print(actions)


def _do_bench(client: OllamaClient, settings: Settings, model: str):
    console.print(f"\n[cyan]▶ Benchmark: [bold]{model}[/][/cyan]")
    console.print("  傳送固定 prompt，等待回應…")
    result = client.bench(model)
    if not result:
        console.print("[red]✗ Benchmark 失敗（服務未啟動或模型載入逾時）[/red]")
        return
    ec = result.get("eval_count", 0)
    ed = result.get("eval_duration", 0)
    pc = result.get("prompt_eval_count", 0)
    pd_ns = result.get("prompt_eval_duration", 0)
    td = result.get("total_duration", 0)
    gen_tps = ec / (ed / 1e9) if ed else 0
    pr_tps = pc / (pd_ns / 1e9) if pd_ns else 0
    console.print()
    console.print(f"  prompt_eval : {pc} tokens, {pd_ns/1e9:.3f}s → {pr_tps:.1f} tok/s")
    console.print(f"  generation  : {ec} tokens, {ed/1e9:.3f}s → [green bold]{gen_tps:.1f} tok/s[/green bold]")
    console.print(f"  total       : {td/1e9:.3f}s")
    settings.add_bench_result(model, gen_tps, pr_tps, td / 1e9, ec)
    # 顯示與前次的 delta
    history = settings.list_bench_history(model, limit=2)
    if len(history) >= 2:
        prev_tps = history[1]["gen_tps"]  # 倒序：0=本次, 1=前次
        delta = gen_tps - prev_tps
        pct = delta / prev_tps * 100 if prev_tps else 0
        sign = "+" if delta >= 0 else ""
        color = "green" if delta >= 0 else "red"
        console.print(f"  [{color}]前次：{prev_tps:.1f} tok/s → 本次：{gen_tps:.1f} tok/s（{sign}{pct:.1f}%）[/{color}]")
    console.print(f"  [dim]已記錄到歷史（olm bench --history 查看）[/dim]")


def _estimate_kv_gb(model_name: str, num_ctx: int) -> float:
    """粗估 KV cache 大小（GB），q4 量化模型用。
    基準：7B 模型 32K context ≈ 0.5 GB KV cache。
    """
    import re
    m = re.search(r'(\d+\.?\d*)\s*b', model_name.lower())
    params_b = float(m.group(1)) if m else 7.0
    # 7B @ 32K = 0.5 GB；線性 scale with params and context
    return (params_b / 7.0) * 0.5 * (num_ctx / 32768)


def _free_ram_gb() -> float:
    """讀系統可用 RAM（GB）。"""
    try:
        _total, _used, free = _sysmem()
        return (free or 0) / 1e9
    except Exception:
        return 0.0


def _ctx_picker(current: int, model: str, free_ram_gb: float) -> Optional[int]:
    """互動式 context 長度 picker（raw 模式 TUI）。回傳選定值或 None（取消）。"""
    import sys
    import io
    import math
    import select
    import termios
    import tty
    from rich.panel import Panel as RPanel
    from rich.console import Console as RConsole

    PRESETS = [4096, 8192, 16384, 32768, 65536, 131072, 262144]
    PRESET_LABELS = ["4K", "8K", "16K", "32K", "64K", "128K", "256K"]
    MIN_CTX = 512
    MAX_CTX = 1048576  # 1M

    val = current

    def _fmt_ctx(n: int) -> str:
        if n >= 1024:
            return f"{n // 1024}K ({n:,})"
        return str(n)

    def _render(v: int) -> str:
        kv_gb = _estimate_kv_gb(model, v)
        ok = kv_gb <= free_ram_gb * 0.8
        ram_color = "green" if ok else "red"
        ram_icon = "✓" if ok else "✗"

        pct = math.log(max(v, 1)) / math.log(MAX_CTX)
        bar_len = 30
        filled = int(pct * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)

        preset_row = "  ".join(
            f"[[bold]{i + 1}[/bold]]{PRESET_LABELS[i]}" for i in range(len(PRESETS))
        )

        content = (
            f"[bold cyan]{_fmt_ctx(v)}[/bold cyan]\n\n"
            f"[dim]◀ ←/- 減少 1K    增加 1K →/+ ▶    h/l 跳 8K[/dim]\n\n"
            f"  [{bar}] {int(pct * 100)}%\n\n"
            f"  KV Cache 預估：[bold]~{kv_gb:.2f} GB[/bold]\n"
            f"  可用 RAM：[bold]{free_ram_gb:.1f} GB[/bold]  [{ram_color}]{ram_icon} "
            f"{'可載入' if ok else '可能不足'}[/{ram_color}]\n\n"
            f"  {preset_row}\n\n"
            f"  [dim][Enter] 確認   [Esc/q] 取消[/dim]"
        )

        buf = io.StringIO()
        rc = RConsole(file=buf, highlight=False)
        rc.print(RPanel(content, title=f"[bold]Context Length — {model}[/bold]", border_style="cyan", width=60))
        return buf.getvalue()

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    lines_printed = 0

    def _clear_lines(n: int) -> None:
        sys.stdout.write(f"\033[{n}A\033[J")
        sys.stdout.flush()

    def _print_panel() -> None:
        nonlocal lines_printed
        if lines_printed:
            _clear_lines(lines_printed)
        out = _render(val)
        sys.stdout.write(out)
        sys.stdout.flush()
        lines_printed = out.count("\n") + 1

    try:
        tty.setraw(fd)
        _print_panel()

        while True:
            ch = sys.stdin.read(1)

            if ch in ("\r", "\n"):  # Enter
                return val
            elif ch in ("\x1b", "q"):
                if ch == "\x1b":
                    r, _, _ = select.select([sys.stdin], [], [], 0.05)
                    if r:
                        next1 = sys.stdin.read(1)
                        if next1 == "[":
                            r2, _, _ = select.select([sys.stdin], [], [], 0.05)
                            if r2:
                                arrow = sys.stdin.read(1)
                                if arrow == "C":  # →
                                    val = min(val + 1024, MAX_CTX)
                                elif arrow == "D":  # ←
                                    val = max(val - 1024, MIN_CTX)
                                _print_panel()
                                continue
                return None
            elif ch in ("+", "="):
                val = min(val + 1024, MAX_CTX)
            elif ch == "-":
                val = max(val - 1024, MIN_CTX)
            elif ch == "l":
                val = min(val + 8192, MAX_CTX)
            elif ch == "h":
                val = max(val - 8192, MIN_CTX)
            elif ch in "1234567":
                val = PRESETS[int(ch) - 1]

            _print_panel()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\n")
        sys.stdout.flush()

    return None


def _fmt_expires(ts: str) -> str:
    """將 ISO 時間戳轉成 'in 23m' 或 'expired'。"""
    if not ts or ts == "?":
        return "?"
    try:
        import datetime
        ts_clean = ts.replace("Z", "+00:00")
        exp = datetime.datetime.fromisoformat(ts_clean)
        now = datetime.datetime.now(datetime.timezone.utc)
        diff = exp - now
        secs = int(diff.total_seconds())
        if secs < 0:
            return "[dim]expired[/dim]"
        if secs < 60:
            return f"in {secs}s"
        if secs < 3600:
            return f"in {secs//60}m"
        return f"in {secs//3600}h{(secs%3600)//60}m"
    except Exception:
        return ts[:16]


def _quant_label(name: str) -> str:
    """從模型名稱解析量化等級簡稱。"""
    import re
    n = name.lower()
    if "mlx" in n:
        return "MLX"
    m = re.search(r'(q\d+_k_[sml]|q\d+_[0-9]+|fp16|bf16|int4|int8|f32)', n)
    if m:
        return m.group(1).upper()
    return ""


def _show_tag_picker(model_base: str) -> str:
    """顯示可用 tag 列表，讓用戶選擇，回傳完整 model:tag。"""
    from .api import fetch_model_tags
    base = model_base.split(":")[0]
    if ":" in model_base:
        return model_base  # 已有 tag，直接用
    console.print(f"[dim]查詢 {base} 可用 tag…[/dim]")
    tags = fetch_model_tags(base)
    if not tags:
        return model_base  # 無法取得，原樣回傳
    t = Table(box=box.SIMPLE, show_header=True, header_style="cyan bold")
    t.add_column("#", justify="right", style="cyan")
    t.add_column("Tag", style="bold")
    t.add_column("Size", justify="right")
    for i, tg in enumerate(tags, 1):
        t.add_row(str(i), tg["tag"], tg["size"])
    console.print(t)
    choice = input("  選擇 tag（Enter 使用 latest）: ").strip()
    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(tags):
            return f"{base}:{tags[idx]['tag']}"
    return f"{base}:latest"


def _dash_pull(client: "OllamaClient", model: str) -> None:
    """Dashboard 用的 pull，帶 Rich 進度條。"""
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, DownloadColumn, TransferSpeedColumn
    last_status = ""
    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            transient=True,
        ) as prog:
            task = prog.add_task(f"下載 {model}", total=None)
            for chunk in client.pull_stream(model):
                status = chunk.get("status", "")
                total = chunk.get("total")
                completed = chunk.get("completed", 0)
                if total and prog.tasks[task].total != total:
                    prog.update(task, total=total)
                if completed:
                    prog.update(task, completed=completed, description=status or last_status)
                if status:
                    last_status = status
                if chunk.get("status") == "success":
                    break
        console.print(f"[green]✓ 下載完成：{model}[/green]")
        client.clear_ctx_cache()
    except Exception:
        console.print("[yellow]▶ 串流異常，改用 subprocess 下載…[/yellow]")
        import subprocess as _sp
        _sp.run(["ollama", "pull", model])
        client.clear_ctx_cache()


def _wait_ollama(client: "OllamaClient", timeout: int = 20) -> bool:
    """等候 Ollama 就緒，顯示 spinner + 倒計時。回傳 True=成功。"""
    import time
    import sys
    spinners = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    for i in range(timeout):
        sp = spinners[i % len(spinners)]
        sys.stdout.write(f"\r  {sp} 等待 Ollama 就緒… ({i + 1}/{timeout}s)  ")
        sys.stdout.flush()
        if client.is_running():
            sys.stdout.write(f"\r  ✓ 就緒（{i + 1}s）                    \n")
            sys.stdout.flush()
            return True
        time.sleep(1)
    sys.stdout.write(f"\r  ✗ 超時（{timeout}s）                    \n")
    sys.stdout.flush()
    return False


def _do_chat_repl(
    client: OllamaClient,
    settings: Settings,
    model: str,
    system: Optional[str] = None,
    options: Optional[dict] = None,
    no_stream: bool = False,
    mcp_client=None,
    fmt: str | None = None,
    save_name: str | None = None,
) -> None:
    """多輪對話 REPL with optional MCP tool-calling。輸入 exit / /bye 或 Ctrl-D 結束。"""
    import json as _json

    messages: list[dict] = []
    tools: list[dict] = []

    if system:
        messages.append({"role": "system", "content": system})

    if mcp_client:
        try:
            tools = mcp_client.list_tools()
            tool_names = ", ".join(t["function"]["name"] for t in tools)
            console.print(f"  [dim]MCP 工具 ({len(tools)} 個)：{tool_names}[/dim]")
        except Exception as e:
            console.print(f"[yellow]MCP 工具載入失敗：{e}[/yellow]")
            tools = []

    conv_id = None
    if save_name:
        conv_id = settings.create_conversation(save_name, model)
        console.print(f"  [dim]歷史記錄中：#{conv_id} {save_name}[/dim]")

    console.print(f"\n[cyan]▶ 對話：[bold]{model}[/bold]  /bye 離開 · /ctx 調整 context · /clear 清除對話 · /model 切換模型 · /temp 取樣溫度 · /system 更新提示 · /save 儲存[/cyan]")
    if fmt:
        console.print(f"  [dim]輸出格式：{fmt}[/dim]")
    if options:
        console.print(f"  [dim]取樣參數：{options}[/dim]")
    if system:
        console.print(f"  [dim]system：{system[:80]}{'…' if len(system) > 80 else ''}[/dim]")

    # 取模型 context 上限（G-B）
    ctx_limit = client.model_max_ctx(model) or 0

    timeout = settings.chat_timeout

    while True:
        try:
            raw = input("\n[你] ")
            # 多行模式：輸入 """ 或 <<< 開頭進入
            if raw.strip() in ('"""', "<<<"):
                console.print('  [dim]多行模式：輸入完後單獨一行 """ 或 --- 結束[/dim]')
                lines = []
                try:
                    while True:
                        line = input()
                        if line.strip() in ('"""', "---"):
                            break
                        lines.append(line)
                except EOFError:
                    pass
                user_input = "\n".join(lines).strip()
                if not user_input:
                    continue
            else:
                user_input = raw.strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]結束對話[/yellow]")
            break
        if not user_input:
            continue
        if user_input.startswith("/ctx"):
            parts = user_input.split()
            if len(parts) == 1:
                # 無參數 → 開啟互動 picker
                result = _ctx_picker(
                    current=ctx_limit or 4096,
                    model=model,
                    free_ram_gb=_free_ram_gb(),
                )
                if result is not None:
                    if options is None:
                        options = {}
                    options["num_ctx"] = result
                    ctx_limit = result
                    console.print(f"  [green]✓ Context 已設為 {result:,} tokens[/green]")
                else:
                    console.print("  [dim]取消，保持原設定[/dim]")
            elif len(parts) == 2:
                try:
                    val = parts[1].upper()
                    if val.endswith("K"):
                        new_ctx = int(float(val[:-1]) * 1024)
                    elif val.endswith("M"):
                        new_ctx = int(float(val[:-1]) * 1024 * 1024)
                    else:
                        new_ctx = int(val)
                    if options is None:
                        options = {}
                    options["num_ctx"] = new_ctx
                    ctx_limit = new_ctx
                    console.print(f"  [green]Context 已更新：{new_ctx:,} tokens（下一輪生效）[/green]")
                except ValueError:
                    console.print("  [red]格式錯誤，例：/ctx 32768 或 /ctx 128K[/red]")
            else:
                console.print(f"  [dim]目前 ctx_limit = {ctx_limit:,}[/dim]  用法：/ctx 32768 或 /ctx 128K")
            continue
        if user_input.startswith("/temp"):
            parts_t = user_input.split(None, 1)
            if len(parts_t) == 2:
                try:
                    val = float(parts_t[1])
                    if 0.0 <= val <= 2.0:
                        if options is None:
                            options = {}
                        options["temperature"] = val
                        console.print(f"[green]✓ temperature = {val}[/green]")
                    else:
                        console.print("[red]範圍：0.0 ~ 2.0[/red]")
                except ValueError:
                    console.print("[red]格式：/temp 0.7[/red]")
            else:
                cur = options.get("temperature", "（未設定）") if options else "（未設定）"
                console.print(f"  temperature = {cur}  用法：/temp 0.7")
            continue
        if user_input.strip() == "/clear":
            messages.clear()
            if system:
                messages.append({"role": "system", "content": system})
            console.print("[yellow]✓ 對話已清除（system prompt 保留）[/yellow]")
            continue
        if user_input.startswith("/model"):
            parts = user_input.split(None, 1)
            if len(parts) == 2:
                new_model = parts[1].strip()
                messages.clear()
                if system:
                    messages.append({"role": "system", "content": system})
                model = new_model
                ctx_limit = client.model_max_ctx(model) or 0
                console.print(f"[green]✓ 已切換：{model}（對話已清除）[/green]")
            else:
                all_models = [m["name"] for m in client.list_models()]
                if all_models:
                    picked = _pick("切換模型", all_models, model)
                    if picked and picked != model:
                        messages.clear()
                        if system:
                            messages.append({"role": "system", "content": system})
                        model = picked
                        ctx_limit = client.model_max_ctx(model) or 0
                        console.print(f"[green]✓ 已切換：{model}（對話已清除）[/green]")
                else:
                    console.print("  [dim]無可用模型[/dim]")
            continue
        if user_input.startswith("/system"):
            parts_sys = user_input.split(None, 1)
            if len(parts_sys) == 2:
                new_system = parts_sys[1].strip()
            else:
                console.print("  [dim]輸入新的 system prompt，Ctrl-D 結束：[/dim]")
                lines = []
                try:
                    while True:
                        lines.append(input())
                except EOFError:
                    pass
                new_system = "\n".join(lines).strip()
            if new_system:
                messages[:] = [m for m in messages if m["role"] != "system"]
                messages.insert(0, {"role": "system", "content": new_system})
                system = new_system
                console.print("[green]✓ System prompt 已更新[/green]")
            continue
        if user_input.startswith("/preset"):
            parts_p = user_input.split(None, 1)
            if len(parts_p) == 2:
                pname = parts_p[1].strip()
                preset = settings.get_preset(pname)
                if preset:
                    if preset.get("system_prompt"):
                        messages[:] = [m for m in messages if m["role"] != "system"]
                        messages.insert(0, {"role": "system", "content": preset["system_prompt"]})
                        system = preset["system_prompt"]
                    # 重置取樣參數後套用 preset（避免舊 preset 殘留）
                    if options is None:
                        options = {}
                    for k in ("temperature", "top_p", "top_k", "repeat_penalty", "min_p", "seed", "stop"):
                        options.pop(k, None)
                    for k in ("temperature", "top_p", "top_k", "repeat_penalty", "min_p", "seed"):
                        v = preset.get(k)
                        if v is not None:
                            options[k] = v
                    if preset.get("stop_seqs"):
                        options["stop"] = preset["stop_seqs"]
                    if preset.get("model") and preset["model"] != model:
                        console.print(f"  [dim]Preset 建議模型：{preset['model']}（目前：{model}）[/dim]")
                    console.print(f"[green]✓ 已載入 preset：{pname}[/green]")
                    clear_ans = input("  是否清除目前對話？[y/N] ").strip().lower()
                    if clear_ans in ("y", "yes"):
                        messages.clear()
                        if system:
                            messages.insert(0, {"role": "system", "content": system})
                        conv_id = None  # 重置歷史記錄
                        if save_name:
                            conv_id = settings.create_conversation(save_name, model)
                            console.print(f"  [dim]已建新記錄：#{conv_id} {save_name}[/dim]")
                else:
                    presets = settings.list_presets()
                    if presets:
                        console.print("  可用 preset：")
                        for p in presets:
                            console.print(f"    [cyan]{p['name']}[/cyan]  {p.get('model', '') or ''}  {(p.get('system_prompt') or '')[:40]}")
                    else:
                        console.print("  [dim]尚無 preset，用 olm preset save 建立[/dim]")
            else:
                presets = settings.list_presets()
                if presets:
                    console.print("  可用 preset：")
                    for p in presets:
                        console.print(f"    [cyan]{p['name']}[/cyan]  {p.get('model', '') or ''}  {(p.get('system_prompt') or '')[:40]}")
                    console.print("  用法：/preset <名稱>")
                else:
                    console.print("  [dim]尚無 preset，用 olm preset save 建立[/dim]")
            continue

        if user_input.startswith("/save"):
            parts_s = user_input.split(None, 1)
            save_label = parts_s[1].strip() if len(parts_s) == 2 else f"{model}-saved"
            if conv_id is None:
                conv_id = settings.create_conversation(save_label, model)
                save_name = save_label  # 讓 /preset clear 也能重建記錄
                for msg in messages:
                    if msg["role"] != "system":
                        settings.add_message(conv_id, msg["role"], msg["content"])
                console.print(f"[green]✓ 已建立記錄：#{conv_id} {save_label}[/green]")
            else:
                console.print(f"[dim]已在記錄中：#{conv_id}（後續訊息自動儲存）[/dim]")
            continue
        if user_input.strip() in ("/help", "/?"):
            console.print(Panel(
                "\n".join([
                    "[bold]/bye[/bold]         離開對話",
                    '[bold]"""[/bold]          進入多行輸入模式（單獨 """ 或 --- 結束）',
                    "[bold]/ctx[/bold] [n]     調整 context 視窗（無參數開 picker）",
                    "[bold]/clear[/bold]       清除對話記錄（保留 system prompt）",
                    "[bold]/model[/bold] [名]  切換模型（無參數開 picker）",
                    "[bold]/temp[/bold] [0~2]  調整 temperature",
                    "[bold]/system[/bold] [p]  更新 system prompt（無參數多行輸入）",
                    "[bold]/preset[/bold] [名] 載入 preset（system prompt + 取樣參數）",
                    "[bold]/save[/bold] [名]   開始儲存此對話到歷史記錄",
                    "[bold]/help[/bold]        顯示此說明",
                ]),
                title="[green]Chat 指令說明[/]",
                border_style="green",
            ))
            continue
        if user_input.lower() in ("exit", "/bye"):
            console.print("[yellow]再見[/yellow]")
            break

        messages.append({"role": "user", "content": user_input})

        # Tool call loop：model 可能連續呼叫多個工具
        turn_failed = False
        full_content = ""
        while True:
            try:
                full_content = ""
                tool_calls: list = []
                final_msg: dict = {}
                done_chunk: dict = {}

                if not no_stream and fmt != "json":
                    console.print("\n[bold green][AI][/bold green] ", end="", highlight=False)

                for chunk in client.chat_stream(
                    model, messages, options=options,
                    tools=tools if tools else None,
                    timeout=timeout,
                    fmt=fmt,
                ):
                    msg = chunk.get("message", {})
                    if chunk.get("done"):
                        final_msg = msg
                        done_chunk = chunk
                        break
                    content = msg.get("content", "")
                    if content:
                        full_content += content
                        if not no_stream and fmt != "json":
                            print(content, end="", flush=True)
                    # tool_calls 可能在 done=False 的 chunk 中出現
                    tc = msg.get("tool_calls")
                    if tc:
                        tool_calls = tc

                # 也查 done chunk（部分模型放這裡）
                if not tool_calls:
                    tool_calls = final_msg.get("tool_calls", [])

                if not no_stream:
                    if fmt != "json":
                        print()
                else:
                    if full_content and fmt != "json":
                        console.print(f"\n[bold green][AI][/bold green] {full_content}")

                # JSON format: 語法高亮顯示
                if fmt == "json" and full_content:
                    console.print("\n[bold green][AI][/bold green]")
                    try:
                        from rich.syntax import Syntax
                        parsed = _json.loads(full_content)
                        pretty = _json.dumps(parsed, ensure_ascii=False, indent=2)
                        console.print(Syntax(pretty, "json", theme="monokai"))
                    except Exception:
                        console.print(full_content)

                # TPS 顯示
                eval_count = done_chunk.get("eval_count", 0)
                eval_duration = done_chunk.get("eval_duration", 0)
                if eval_count and eval_duration:
                    tps = eval_count / (eval_duration / 1e9)
                    secs = eval_duration / 1e9
                    console.print(f"  [dim]⏱ {tps:.1f} tok/s · {eval_count} tokens · {secs:.2f}s[/dim]")

                # Context 用量顯示（G-B）
                prompt_eval = done_chunk.get("prompt_eval_count", 0) or 0
                used_ctx = prompt_eval + (eval_count or 0)
                if used_ctx:
                    if ctx_limit:
                        pct = used_ctx / ctx_limit * 100
                        color = "red" if pct > 80 else "yellow" if pct > 60 else "dim"
                        bar_len = 10
                        filled = int(pct / 100 * bar_len)
                        bar = "█" * filled + "░" * (bar_len - filled)
                        console.print(f"  [{color}][{bar}] {pct:.0f}%  {used_ctx:,}/{ctx_limit:,}[/{color}]")
                    else:
                        console.print(f"  [dim]ctx: {used_ctx} tokens[/dim]")

                if tool_calls and mcp_client:
                    # 有工具呼叫，執行後繼續對話
                    messages.append({
                        "role": "assistant",
                        "content": full_content,
                        "tool_calls": tool_calls,
                    })
                    for tc in tool_calls:
                        fn = tc.get("function", {})
                        tool_name = fn.get("name", "")
                        tool_args = fn.get("arguments", {})
                        if isinstance(tool_args, str):
                            try:
                                tool_args = _json.loads(tool_args)
                            except Exception:
                                tool_args = {}
                        console.print(
                            f"\n[yellow]  {tool_name}"
                            f"({_json.dumps(tool_args, ensure_ascii=False)})[/yellow]"
                        )
                        try:
                            result = mcp_client.call_tool(tool_name, tool_args)
                            preview = result[:300] + ("…" if len(result) > 300 else "")
                            console.print(f"[dim]   -> {preview}[/dim]")
                        except Exception as e:
                            result = f"工具呼叫失敗：{e}"
                            console.print(f"[red]   -> {e}[/red]")
                        messages.append({"role": "tool", "content": result})
                    # 繼續迴圈讓 model 處理工具結果
                    if not no_stream and fmt != "json":
                        console.print("\n[bold green][AI][/bold green] ", end="", highlight=False)
                else:
                    # 無工具呼叫，這一輪結束
                    messages.append({"role": "assistant", "content": full_content})
                    break

            except Exception as e:
                console.print(f"\n[red]✗ 對話錯誤：{e}[/red]")
                turn_failed = True
                break

        if turn_failed:
            messages.pop()
        elif conv_id and full_content:
            settings.add_message(conv_id, "user", user_input)
            settings.add_message(conv_id, "assistant", full_content)  # 移除未得到回應的 user message


def _settings_menu(client: OllamaClient, settings: Settings):
    while True:
        os.system("clear")
        ovs = settings.list_model_ctx()
        console.print(Panel(
            "\n".join([
                f"  [cyan]1)[/] 全域 num_ctx       [bold]{fmt_ctx(settings.num_ctx)}[/]",
                f"  [cyan]2)[/] keep_alive         [bold]{settings.keep_alive}[/]",
                f"  [cyan]3)[/] request_timeout    [bold]{settings.request_timeout}s[/]",
                f"  [cyan]4)[/] chat_timeout       [bold]{settings.chat_timeout}s[/]",
                f"  [cyan]5)[/] default_model      [bold]{settings.default_model}[/]",
                f"  [cyan]6)[/] per-model ctx (新增/改動)",
                f"  [cyan]7)[/] per-model ctx (清除)",
                *(
                    ["\n  [bold]現有 per-model ctx:[/]"]
                    + [f"    [cyan]{m}[/] = {fmt_ctx(c)}" for m, c in ovs]
                    if ovs else []
                ),
                "",
                "  [cyan]b)[/] 返回儀表板",
            ]),
            title="[green bold]Settings[/]",
            border_style="green",
        ))
        choice = input("選擇: ").strip().lower()
        if choice == "1":
            v = input("  新的 num_ctx (256K / 131072 / 1M): ").strip()
            p = parse_ctx(v)
            if p:
                settings.set("num_ctx", str(p))
                console.print(f"[green]✓ num_ctx = {fmt_ctx(p)}[/green]")
                continue
            else:
                console.print("[red]✗ 無效數值[/red]")
        elif choice == "2":
            v = input("  新的 keep_alive (24h / 30m / -1): ").strip()
            if v:
                settings.set("keep_alive", v)
                console.print(f"[green]✓ keep_alive = {v}[/green]")
                continue
        elif choice == "3":
            v = input("  新的 request_timeout (秒): ").strip()
            if v.isdigit():
                settings.set("request_timeout", v)
                console.print(f"[green]✓ request_timeout = {v}s[/green]")
                continue
            else:
                console.print("[red]✗ 需整數[/red]")
        elif choice == "4":
            v = input("  新的 chat_timeout (秒): ").strip()
            if v.isdigit():
                settings.set("chat_timeout", v)
                console.print(f"[green]✓ chat_timeout = {v}s[/green]")
                continue
            else:
                console.print("[red]✗ 需整數[/red]")
        elif choice == "5":
            names = [m["name"] for m in client.list_models()] if client.is_running() else []
            if names:
                model = _pick("選預設模型", names, settings.default_model)
            else:
                model = input("  輸入預設模型名稱: ").strip()
            if model:
                settings.set("default_model", model)
                console.print(f"[green]✓ default_model = {model}[/green]")
                continue
        elif choice == "6":
            names = [m["name"] for m in client.list_models()] if client.is_running() else []
            if names:
                model = _pick("哪個模型", names, "")
            else:
                model = input("  輸入模型名稱: ").strip()
            if model:
                v = input(f"  {model} 的專屬 num_ctx (128K / 65536): ").strip()
                p = parse_ctx(v)
                if p:
                    settings.set_model_ctx(model, p)
                    console.print(f"[green]✓ {model} ctx = {fmt_ctx(p)}[/green]")
                    continue
                else:
                    console.print("[red]✗ 無效數值[/red]")
        elif choice == "7":
            ovs2 = settings.list_model_ctx()
            if not ovs2:
                console.print("[yellow]無 per-model 設定[/yellow]")
            else:
                names2 = [m for m, _ in ovs2]
                model = _pick("清除哪個模型的專屬設定", names2, "")
                if model:
                    settings.del_model_ctx(model)
                    console.print(f"[green]✓ 已清除 {model} 專屬設定[/green]")
                    continue
        elif choice in ("b", ""):
            break
        else:
            console.print("[red]✗ 無效選擇[/red]")
        input("\n按 Enter 繼續…")


def run_dashboard(client: OllamaClient, settings: Settings):
    while True:
        settings._init()  # reload from DB
        _render_dashboard(client, settings)
        # P4: onboarding — 首次/空白狀態提示
        _r = client.is_running()
        _ml = client.list_models() if _r else client.disk_models()
        if not _r and len(_ml) == 0:
            console.print(Panel(
                "[bold]入門步驟：[/bold]\n"
                "  [cyan]1[/cyan] 或 [cyan]7[/cyan]  啟動 Ollama 服務\n"
                "  [cyan]f[/cyan]       搜尋並下載模型\n"
                "  [cyan]5[/cyan]       開始對話",
                title="[yellow]首次使用[/]",
                border_style="yellow",
            ))
        action = input("\nSelect action: ").strip().lower()

        if action == "q":
            console.print("[green]再見[/green]")
            break
        elif action in ("r", ""):
            continue

        # wrap each action so errors don't crash the loop
        try:
            running = client.is_running()
            if action == "1":
                if running:
                    console.print("[yellow]⚠ Ollama 已在運行[/yellow]")
                else:
                    import time
                    ollama_port = settings.ollama_port
                    gw_host = settings.gateway_host
                    gw_port = settings.gateway_port
                    pid = client.start_server(ollama_port, settings.num_ctx, settings.keep_alive)
                    console.print(f"[cyan]▶ 啟動 Ollama port={ollama_port}（私有埠）[/cyan]")
                    if _wait_ollama(client, 20):
                        console.print(f"[green]✓ Ollama 就緒 PID={pid}[/green]")
                    else:
                        console.print("[red]✗ Ollama 啟動逾時[/red]")
                        break
                    client.start_gateway(gw_host, gw_port, ollama_port, settings.chat_timeout)
                    time.sleep(1)
                    gw_pid = client.gateway_pid(gw_port)
                    console.print(f"[green]✓ 閘道就緒 PID={gw_pid}  127.0.0.1:{gw_port}[/green]")

            elif action == "2":
                import time
                client.stop_gateway(settings.gateway_port)
                if client.stop_server(settings.ollama_port):
                    console.print("[green]✓ 閘道與 Ollama 已停止[/green]")
                else:
                    console.print("[yellow]⚠ 找不到 Ollama 進程[/yellow]")

            elif action == "3":
                import time
                ollama_port = settings.ollama_port
                gw_host = settings.gateway_host
                gw_port = settings.gateway_port
                client.stop_gateway(gw_port)
                client.stop_server(ollama_port)
                time.sleep(1)
                pid = client.start_server(ollama_port, settings.num_ctx, settings.keep_alive)
                if _wait_ollama(client, timeout=15):
                    console.print("[green]✓ Ollama 已就緒[/green]")
                else:
                    console.print("[red]✗ 重啟逾時[/red]")
                client.start_gateway(gw_host, gw_port, ollama_port, settings.chat_timeout)
                time.sleep(1)
                gw_pid = client.gateway_pid(gw_port)
                console.print(f"[green]✓ 重啟完成 Ollama PID={pid} / 閘道 PID={gw_pid}[/green]")

            elif action == "4":
                if not running:
                    console.print("[yellow]⚠ Ollama 未啟動[/yellow]")
                    start_ans = input("  是否立即啟動？[y/N] ").strip().lower()
                    if start_ans in ("y", "yes"):
                        client.start_server(settings.ollama_port, settings.num_ctx, settings.keep_alive)
                        if _wait_ollama(client, timeout=20):
                            running = True
                            console.print("[green]✓ Ollama 已就緒[/green]")
                        else:
                            console.print("[red]✗ 啟動失敗，請查 olm logs[/red]")
                if running:
                    models_list = client.list_models()
                    names = [m["name"] for m in models_list]
                    if not names:
                        console.print("[red]✗ 無可用模型，請先 pull[/red]")
                    else:
                        default = settings.default_model if settings.default_model in names else names[0]
                        model = _pick("載入哪個模型", names, default)
                        minfo = next((mo for mo in models_list if mo["name"] == model), {})
                        model_sz = minfo.get("size", 0)
                        _, _, free_b = _sysmem()
                        if free_b is not None and model_sz and free_b < model_sz + 2_000_000_000:
                            console.print(
                                f"[yellow]⚠ RAM 可能不足：可用 {free_b/1e9:.1f} GB，"
                                f"模型約 {model_sz/1e9:.1f} GB（建議保留 2 GB 餘裕）[/yellow]"
                            )
                        cur_ctx = settings.effective_ctx(model)
                        console.print(f"  [dim]Context：{cur_ctx:,} tokens  （Enter 維持，[bold]c[/bold] 開啟調整器）[/dim]")
                        ans = input("  > ").strip().lower()
                        num_ctx_override = None
                        if ans == "c":
                            picked = _ctx_picker(cur_ctx, model, _free_ram_gb())
                            if picked:
                                num_ctx_override = picked
                                console.print(f"  [green]✓ 此次 context：{picked:,} tokens[/green]")
                        ctx = num_ctx_override or cur_ctx
                        console.print(f"[cyan]▶ 載入 {model}  ctx={fmt_ctx(ctx)}[/cyan]")
                        ok = client.load(model, ctx, settings.keep_alive)
                        console.print(f"[{'green' if ok else 'red'}]{'✓ 已就緒' if ok else '✗ 載入失敗'}[/]")

            elif action == "5":
                if not running:
                    console.print("[yellow]⚠ Ollama 未啟動[/yellow]")
                    start_ans = input("  是否立即啟動？[y/N] ").strip().lower()
                    if start_ans in ("y", "yes"):
                        client.start_server(settings.ollama_port, settings.num_ctx, settings.keep_alive)
                        if _wait_ollama(client, timeout=20):
                            running = True
                            console.print("[green]✓ Ollama 已就緒[/green]")
                        else:
                            console.print("[red]✗ 啟動失敗，請查 olm logs[/red]")
                if running:
                    names = [m["name"] for m in client.list_models()]
                    if not names:
                        console.print("[red]✗ 無可用模型，請先 pull[/red]")
                        running = False  # 跳過後續邏輯
                    else:
                        default = settings.default_model if settings.default_model in names else names[0]
                        model = _pick("對話哪個模型", names, default)
                if running and names:
                    cur_ctx = settings.effective_ctx(model)
                    console.print(f"  [dim]Context：{cur_ctx:,} tokens  （Enter 維持，[bold]c[/bold] 開啟調整器）[/dim]")
                    ans = input("  > ").strip().lower()
                    num_ctx_override = None
                    if ans == "c":
                        picked = _ctx_picker(cur_ctx, model, _free_ram_gb())
                        if picked:
                            num_ctx_override = picked
                            console.print(f"  [green]✓ 此次 context：{picked:,} tokens[/green]")
                    sys_prompt = input("  System prompt（留空略過）: ").strip() or None
                    _do_chat_repl(client, settings, model, system=sys_prompt,
                                  options={"num_ctx": num_ctx_override or cur_ctx})

            elif action == "6":
                if not running:
                    console.print("[yellow]⚠ Ollama 未啟動[/yellow]")
                    start_ans = input("  是否立即啟動？[y/N] ").strip().lower()
                    if start_ans in ("y", "yes"):
                        client.start_server(settings.ollama_port, settings.num_ctx, settings.keep_alive)
                        if _wait_ollama(client, timeout=20):
                            running = True
                            console.print("[green]✓ Ollama 已就緒[/green]")
                        else:
                            console.print("[red]✗ 啟動失敗，請查 olm logs[/red]")
                if running:
                    console.print("[dim]（直接 Enter 可開啟搜尋，或輸入模型名稱）[/dim]")
                    model = input("  輸入模型名稱: ").strip()
                    if not model:
                        keyword = input("  搜尋關鍵字: ").strip()
                        if keyword:
                            from .api import search_models
                            try:
                                results = search_models(keyword, 15)
                            except ConnectionError as e:
                                console.print(f"[red]{e}[/red]")
                                results = []
                            if results:
                                installed_names = {m["name"].split(":")[0] for m in client.list_models()}
                                st = Table(box=box.SIMPLE, show_header=True, header_style="green bold")
                                st.add_column("#", justify="right", style="cyan")
                                st.add_column("Model", style="bold")
                                st.add_column("Pulls")
                                st.add_column("Sizes")
                                for i, r in enumerate(results, 1):
                                    rname = r["name"]
                                    name_col = rname + (" [green]✓已安裝[/green]" if rname.split(":")[0] in installed_names else "")
                                    st.add_row(str(i), name_col, r["pulls"], " ".join(r["sizes"]) or "-")
                                console.print(Panel(st, title=f"[green bold]搜尋：{keyword}[/]", border_style="green"))
                                choice = input("  輸入編號下載（Enter 略過）: ").strip()
                                if choice.isdigit():
                                    idx = int(choice) - 1
                                    if 0 <= idx < len(results):
                                        model = results[idx]["name"]
                            else:
                                console.print("[yellow]無結果[/yellow]")
                    if model:
                        model = _show_tag_picker(model)
                        _dash_pull(client, model)

            elif action == "7":
                if not running:
                    console.print("[yellow]服務未啟動，記憶體中無模型可卸載[/yellow]")
                else:
                    loaded = [m["name"] for m in client.list_loaded()]
                    if not loaded:
                        console.print("[yellow]⚠ 目前沒有已載入的模型[/yellow]")
                    else:
                        model = _pick("卸載哪個模型", loaded, loaded[0])
                        ok = client.unload(model)
                        console.print(f"[{'green' if ok else 'yellow'}]{'✓ 已卸載' if ok else '⚠ 可能未載入'}[/]  {model}")

            elif action == "8":
                if not running:
                    console.print("[red]✗ 服務未啟動[/red]")
                else:
                    names = [m["name"] for m in client.list_models()]
                    model = _pick("查看哪個模型", names, settings.default_model)
                    subprocess.run(["ollama", "show", model])

            elif action == "9":
                subprocess.run(["tail", "-40", LOGFILE])
                # 加閘道日誌
                if os.path.exists(GATEWAY_LOGFILE):
                    console.print("\n[dim]── 閘道日誌（最後 10 行）──[/dim]")
                    subprocess.run(["tail", "-10", GATEWAY_LOGFILE])

            elif action == "0":
                if not running:
                    console.print("[red]✗ 服務未啟動[/red]")
                else:
                    names = [m["name"] for m in client.list_models()]
                    model = _pick("Benchmark 哪個模型", names, settings.default_model)
                    _do_bench(client, settings, model)

            elif action == "s":
                _settings_menu(client, settings)
                continue

            elif action == "d":
                if not running:
                    # 離線模式：仍可嘗試刪除（ollama rm 不需服務）
                    names = [m["name"] for m in client.disk_models()]
                    if not names:
                        console.print("[yellow]無本機模型可刪除[/yellow]")
                    else:
                        model = _pick("刪除哪個模型（離線）", names, "")
                        if model:
                            confirm = input(f"  確定刪除 {model}？[y/N] ").strip().lower()
                            if confirm == "y":
                                import subprocess as _sp
                                import os as _os
                                _env = _os.environ.copy()
                                _env["OLLAMA_HOST"] = f"http://127.0.0.1:{settings.ollama_port}"
                                result = _sp.run(["ollama", "rm", model], capture_output=True, text=True, env=_env)
                                if result.returncode == 0:
                                    console.print(f"[green]✓ 已刪除：{model}[/green]")
                                else:
                                    console.print(f"[red]✗ 刪除失敗：{result.stderr.strip()}[/red]")
                            else:
                                console.print("[yellow]已取消[/yellow]")
                else:
                    names = [m["name"] for m in client.list_models()]
                    if not names:
                        console.print("[yellow]⚠ 無已安裝的模型[/yellow]")
                    else:
                        model = _pick("刪除哪個模型", names, "")
                        if model:
                            confirm = input(f"  確定刪除 {model}？[y/N] ").strip().lower()
                            if confirm == "y":
                                ok = client.delete(model)
                                if ok:
                                    console.print(f"[green]✓ {model} 已刪除[/green]")
                                    client.clear_ctx_cache()
                                else:
                                    console.print("[red]✗ 刪除失敗[/red]")
                            else:
                                console.print("[yellow]已取消[/yellow]")

            elif action == "f":
                keyword = input("  搜尋關鍵字: ").strip()
                if keyword:
                    from .api import search_models
                    console.print(f"[cyan]搜尋：{keyword}[/cyan]")
                    try:
                        results = search_models(keyword)
                    except ConnectionError as e:
                        console.print(f"[red]{e}[/red]")
                    else:
                        if not results:
                            console.print("[yellow]無結果[/yellow]")
                        else:
                            installed_names = {m["name"].split(":")[0] for m in client.list_models()} if running else set()
                            st = Table(box=box.SIMPLE, show_header=True, header_style="green bold")
                            st.add_column("#", justify="right", style="cyan")
                            st.add_column("Model", style="bold")
                            st.add_column("Pulls", justify="right")
                            st.add_column("Sizes")
                            st.add_column("Description", style="dim", max_width=50)
                            for i, r in enumerate(results[:15], 1):
                                rname = r["name"]
                                name_col = rname + (" [green]✓已安裝[/green]" if rname.split(":")[0] in installed_names else "")
                                st.add_row(
                                    str(i), name_col, r["pulls"],
                                    " ".join(r["sizes"]) or "-", r["description"][:60],
                                )
                            console.print(Panel(
                                st,
                                title=f"[green bold]搜尋：{keyword}[/]",
                                border_style="green",
                            ))
                            console.print("\n[dim]輸入編號直接下載，或 Enter 返回[/dim]")
                            choice = input("  > ").strip()
                            if choice.isdigit():
                                idx = int(choice) - 1
                                if 0 <= idx < len(results[:15]):
                                    model_name = results[idx]["name"]
                                    console.print(f"[cyan]▶ 下載 {model_name}...[/cyan]")
                                    if not running:
                                        start_ans = input("  Ollama 未啟動，是否立即啟動？[y/N] ").strip().lower()
                                        if start_ans in ("y", "yes"):
                                            client.start_server(settings.ollama_port, settings.num_ctx, settings.keep_alive)
                                            if _wait_ollama(client, timeout=20):
                                                running = True
                                            else:
                                                console.print("[red]✗ 啟動失敗[/red]")
                                                model_name = ""
                                        else:
                                            model_name = ""
                                    if model_name:
                                        model_name = _show_tag_picker(model_name)
                                        _dash_pull(client, model_name)

            elif action == "g":
                acl = settings.gateway_list_allow()
                if not acl:
                    console.print("[yellow]（白名單為空——閘道僅 localhost 可用）[/yellow]")
                    console.print("提示：在 shell 用 [bold]olm gateway allow 192.168.0.176[/bold] 加入")
                else:
                    t = Table(box=box.SIMPLE)
                    t.add_column("CIDR", style="bold")
                    t.add_column("備注")
                    t.add_column("加入時間")
                    for r in acl:
                        t.add_row(r["cidr"], r["note"] or "-", (r["created_at"] or "")[:16])
                    console.print(Panel(t, title="[green bold]閘道 IP 白名單[/]", border_style="green"))
                input("[dim]按 Enter 返回[/dim]")
                continue

            elif action == "h":
                convs = settings.list_conversations(limit=10)
                if not convs:
                    console.print("[yellow]（無對話歷史）[/yellow]  用 olm chat --save <名稱> 儲存對話")
                else:
                    t = Table(box=box.SIMPLE)
                    t.add_column("#", style="dim")
                    t.add_column("名稱", style="bold")
                    t.add_column("模型")
                    t.add_column("更新")
                    for i, c in enumerate(convs, 1):
                        t.add_row(str(i), c["name"], c["model"], (c["updated_at"] or "")[:16])
                    console.print(Panel(t, title="[cyan bold]對話歷史（最近 10 筆）[/]", border_style="cyan"))
                    console.print("[dim]詳細操作：olm history list/show/export[/dim]")
                input("[dim]按 Enter 返回[/dim]")
                continue

            elif action == "p":
                presets = settings.list_presets()
                if not presets:
                    console.print("[yellow]（無 Preset）[/yellow]  用 olm preset save <名稱> 儲存")
                else:
                    t = Table(box=box.SIMPLE)
                    t.add_column("名稱", style="bold")
                    t.add_column("模型")
                    t.add_column("Temp", justify="right")
                    t.add_column("建立")
                    for pr in presets:
                        t.add_row(
                            pr["name"], pr["model"] or "-",
                            str(pr.get("temperature") or "-"),
                            (pr.get("created_at") or "")[:16],
                        )
                    console.print(Panel(t, title="[magenta bold]Presets[/]", border_style="magenta"))
                    console.print("[dim]使用：olm chat --preset <名稱>[/dim]")
                input("[dim]按 Enter 返回[/dim]")
                continue

            else:
                console.print(f"[red]✗ 無效選擇: {action}[/red]")

        except Exception as e:
            console.print(f"[red]✗ 錯誤: {e}[/red]")

        input("\n按 Enter 返回儀表板…")
