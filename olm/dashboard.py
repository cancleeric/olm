"""Interactive dashboard / menu mode for olm."""
import os
import re
import subprocess
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from .api import OllamaClient, LOGFILE
from .db import Settings, parse_ctx, fmt_ctx

console = Console()


def _gb(b: int) -> float:
    return b / 1e9


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
    return default or options[0]


def _render_dashboard(client: OllamaClient, settings: Settings):
    os.system("clear")
    running = client.is_running()
    pid = client.server_pid() if running else None
    port = os.environ.get("OLLAMA_PORT", "11434")

    # ── Service status panel ──────────────────────────────────
    svc_color = "green" if running else "red"
    svc_label = "running" if running else "stopped"
    total_b, used_b, free_b = _sysmem()

    lines = [
        f"service: [{svc_color} bold]{svc_label}[/]   url=[bold]{client.base_url}[/]",
        f"pid: [bold]{pid or '?'}[/]   port: [bold]{port}[/]",
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

    console.print(Panel("\n".join(lines), title="[green bold]Ollama Dashboard[/]", border_style="green"))

    # ── Loaded Models ─────────────────────────────────────────
    loaded = client.list_loaded() if running else []
    loaded_table = Table(box=box.SIMPLE, show_header=True, header_style="green bold")
    loaded_table.add_column("Model", style="bold")
    loaded_table.add_column("RAM", justify="right")
    loaded_table.add_column("ctx(actual/max)")
    loaded_table.add_column("expires")

    if not loaded:
        console.print(Panel("（無）", title="[green bold]Loaded Models[/]", border_style="dim"))
    else:
        loaded_total = 0
        for m in loaded:
            sz = m.get("size", 0)
            loaded_total += sz
            actual = m.get("context_length")
            mx = client.model_max_ctx(m["name"])
            ctx_str = f"{fmt_ctx(actual)}/{fmt_ctx(mx)}"
            warn = ""
            if actual and actual < settings.effective_ctx(m["name"]):
                warn = " [yellow]⚠ 降載[/]"
            loaded_table.add_row(
                m["name"],
                f"{_gb(sz):.1f} GB",
                ctx_str + warn,
                m.get("expires_at", "?")[:19],
            )
        loaded_table.add_section()
        loaded_table.add_row(
            f"[dim]total ({len(loaded)} models)[/]",
            f"[bold]{_gb(loaded_total):.1f} GB[/]", "", "",
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

    hint = " [yellow](讀自磁碟)[/]" if from_disk else ""
    if not installed:
        console.print(Panel("（無）", title=f"[green bold]Installed Models[/]{hint}", border_style="dim"))
    else:
        for i, m in enumerate(installed, 1):
            ctx_col = fmt_ctx(client.model_max_ctx(m["name"])) if running else "?"
            inst_table.add_row(str(i), m["name"], f"{_gb(m.get('size', 0)):.1f} GB", ctx_col)
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
        ("s", "Settings", "調整設定（存 SQLite）"),
        ("r", "Refresh", "重新整理"),
        ("q", "Quit", "離開"),
    ]
    for key, cmd, desc in rows:
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
            else:
                console.print("[red]✗ 無效數值[/red]")
        elif choice == "2":
            v = input("  新的 keep_alive (24h / 30m / -1): ").strip()
            if v:
                settings.set("keep_alive", v)
                console.print(f"[green]✓ keep_alive = {v}[/green]")
        elif choice == "3":
            v = input("  新的 request_timeout (秒): ").strip()
            if v.isdigit():
                settings.set("request_timeout", v)
                console.print(f"[green]✓ request_timeout = {v}s[/green]")
            else:
                console.print("[red]✗ 需整數[/red]")
        elif choice == "4":
            v = input("  新的 chat_timeout (秒): ").strip()
            if v.isdigit():
                settings.set("chat_timeout", v)
                console.print(f"[green]✓ chat_timeout = {v}s[/green]")
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
        elif choice in ("b", ""):
            break
        else:
            console.print("[red]✗ 無效選擇[/red]")
        input("\n按 Enter 繼續…")


def run_dashboard(client: OllamaClient, settings: Settings):
    while True:
        settings._init()  # reload from DB
        _render_dashboard(client, settings)
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
                    console.print("[yellow]⚠ 服務已在運行[/yellow]")
                else:
                    port = int(os.environ.get("OLLAMA_PORT", "11434"))
                    pid = client.start_server(port, settings.num_ctx, settings.keep_alive)
                    console.print(f"[cyan]▶ 背景啟動 Ollama port={port}[/cyan]")
                    import time
                    for _ in range(15):
                        time.sleep(1)
                        if client.is_running():
                            console.print(f"[green]✓ 服務就緒 PID={pid}（尚未預載模型）[/green]")
                            break
                    else:
                        console.print("[red]✗ 服務啟動逾時[/red]")

            elif action == "2":
                if client.stop_server():
                    console.print("[green]✓ Ollama 已停止[/green]")
                else:
                    console.print("[yellow]⚠ 找不到 Ollama 進程[/yellow]")

            elif action == "3":
                client.stop_server()
                import time; time.sleep(1)
                port = int(os.environ.get("OLLAMA_PORT", "11434"))
                pid = client.start_server(port, settings.num_ctx, settings.keep_alive)
                for _ in range(15):
                    time.sleep(1)
                    if client.is_running():
                        console.print(f"[green]✓ 重啟完成 PID={pid}[/green]")
                        break

            elif action == "4":
                if not running:
                    console.print("[red]✗ 服務未啟動[/red]")
                else:
                    names = [m["name"] for m in client.list_models()]
                    model = _pick("載入哪個模型", names, settings.default_model)
                    ctx = settings.effective_ctx(model)
                    console.print(f"[cyan]▶ 載入 {model}  ctx={fmt_ctx(ctx)}[/cyan]")
                    ok = client.load(model, ctx, settings.keep_alive)
                    console.print(f"[{'green' if ok else 'red'}]{'✓ 已就緒' if ok else '✗ 載入失敗'}[/]")

            elif action == "5":
                if not running:
                    console.print("[red]✗ 服務未啟動[/red]")
                else:
                    names = [m["name"] for m in client.list_models()]
                    model = _pick("對話哪個模型", names, settings.default_model)
                    subprocess.run(["ollama", "run", model])

            elif action == "6":
                if not running:
                    console.print("[red]✗ 服務未啟動[/red]")
                else:
                    model = input("  輸入要下載的模型名稱: ").strip()
                    if model:
                        subprocess.run(["ollama", "pull", model])

            elif action == "7":
                if not running:
                    console.print("[red]✗ 服務未啟動[/red]")
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

            else:
                console.print(f"[red]✗ 無效選擇: {action}[/red]")

        except Exception as e:
            console.print(f"[red]✗ 錯誤: {e}[/red]")

        input("\n按 Enter 返回儀表板…")
