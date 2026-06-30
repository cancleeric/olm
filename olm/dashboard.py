"""Interactive dashboard / menu mode for olm."""
import os
import subprocess
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from . import __version__
from .api import OllamaClient, LOGFILE, _sysmem
from .db import Settings, parse_ctx, fmt_ctx

console = Console()


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
                m.get("expires_at", "?")[:19],
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
        ("d", "Delete model", "從磁碟刪除模型"),
        ("f", "Search models", "搜尋 Ollama 模型庫"),
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
    settings.add_bench_result(model, gen_tps, pr_tps, td / 1e9, ec)
    console.print(f"  [dim]已記錄到歷史（olm bench --history 查看）[/dim]")


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

    console.print(f"\n[cyan]▶ 對話：[bold]{model}[/bold]  exit 或 /bye 離開，Ctrl-D 結束[/cyan]")
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
            user_input = input("\n[你] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]結束對話[/yellow]")
            break
        if not user_input:
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
                        console.print(f"  [{color}]ctx: {used_ctx} / {ctx_limit} ({pct:.0f}%)[/{color}]")
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
                    console.print("[yellow]⚠ Ollama 已在運行[/yellow]")
                else:
                    import time
                    ollama_port = settings.ollama_port
                    gw_host = settings.gateway_host
                    gw_port = settings.gateway_port
                    pid = client.start_server(ollama_port, settings.num_ctx, settings.keep_alive)
                    console.print(f"[cyan]▶ 啟動 Ollama port={ollama_port}（私有埠）[/cyan]")
                    for _ in range(15):
                        time.sleep(1)
                        if client.is_running():
                            console.print(f"[green]✓ Ollama 就緒 PID={pid}[/green]")
                            break
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
                for _ in range(15):
                    time.sleep(1)
                    if client.is_running():
                        break
                client.start_gateway(gw_host, gw_port, ollama_port, settings.chat_timeout)
                time.sleep(1)
                gw_pid = client.gateway_pid(gw_port)
                console.print(f"[green]✓ 重啟完成 Ollama PID={pid} / 閘道 PID={gw_pid}[/green]")

            elif action == "4":
                if not running:
                    console.print("[red]✗ 服務未啟動[/red]")
                else:
                    models_list = client.list_models()
                    names = [m["name"] for m in models_list]
                    model = _pick("載入哪個模型", names, settings.default_model)
                    minfo = next((mo for mo in models_list if mo["name"] == model), {})
                    model_sz = minfo.get("size", 0)
                    _, _, free_b = _sysmem()
                    if free_b is not None and model_sz and free_b < model_sz + 2_000_000_000:
                        console.print(
                            f"[yellow]⚠ RAM 可能不足：可用 {free_b/1e9:.1f} GB，"
                            f"模型約 {model_sz/1e9:.1f} GB（建議保留 2 GB 餘裕）[/yellow]"
                        )
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
                    sys_prompt = input("  System prompt（留空略過）: ").strip() or None
                    _do_chat_repl(client, settings, model, system=sys_prompt)

            elif action == "6":
                if not running:
                    console.print("[red]✗ 服務未啟動[/red]")
                else:
                    model = input("  輸入要下載的模型名稱: ").strip()
                    if model:
                        subprocess.run(["ollama", "pull", model])
                        client.clear_ctx_cache()

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

            elif action == "d":
                if not running:
                    console.print("[red]✗ 服務未啟動[/red]")
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
                            st = Table(box=box.SIMPLE, show_header=True, header_style="green bold")
                            st.add_column("#", justify="right", style="cyan")
                            st.add_column("Model", style="bold")
                            st.add_column("Pulls", justify="right")
                            st.add_column("Sizes")
                            st.add_column("Description", style="dim", max_width=50)
                            for i, r in enumerate(results[:15], 1):
                                st.add_row(
                                    str(i), r["name"], r["pulls"],
                                    " ".join(r["sizes"]) or "-", r["description"][:60],
                                )
                            console.print(Panel(
                                st,
                                title=f"[green bold]搜尋：{keyword}[/]",
                                border_style="green",
                            ))

            else:
                console.print(f"[red]✗ 無效選擇: {action}[/red]")

        except Exception as e:
            console.print(f"[red]✗ 錯誤: {e}[/red]")

        input("\n按 Enter 返回儀表板…")
