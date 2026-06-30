"""olm — Ollama model management CLI (Typer + Rich)."""
import os
import subprocess
import sys
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from .api import OllamaClient, LOGFILE, GATEWAY_LOGFILE, _sysmem
from .db import Settings, parse_ctx, fmt_ctx
from .dashboard import run_dashboard, _do_bench, _pick, _do_chat_repl

app = typer.Typer(
    name="olm",
    help="Ollama model management CLI — dashboard, load, bench, config & more.",
    no_args_is_help=False,
    invoke_without_command=True,
)
console = Console()


def _client() -> OllamaClient:
    # 閘道輪：直連 Ollama 私有埠，繞過閘道（olm 自身不需過自己的門）
    s = _settings()
    return OllamaClient(f"http://127.0.0.1:{s.ollama_port}")


def _settings() -> Settings:
    return Settings()


def _require_running(client: OllamaClient):
    if not client.is_running():
        console.print(f"[red]✗ Ollama 服務未啟動 ({client.base_url})[/red]")
        console.print("  請先執行：[bold]olm start[/bold]")
        raise typer.Exit(1)


def _gb(b: int) -> float:
    return b / 1e9


# ── Default (no subcommand) → Dashboard ─────────────────────
@app.callback(invoke_without_command=True)
def main(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        run_dashboard(_client(), _settings())


# ── list ─────────────────────────────────────────────────────
@app.command("list", help="列出所有本機模型（服務停止時讀磁碟）")
def cmd_list():
    client = _client()
    settings = _settings()
    running = client.is_running()

    if running:
        models = client.list_models()
        from_disk = False
    else:
        models = client.disk_models()
        from_disk = True

    hint = " [yellow](讀自磁碟)[/]" if from_disk else ""
    t = Table(box=box.SIMPLE, show_header=True, header_style="green bold")
    t.add_column("#", justify="right", style="cyan")
    t.add_column("Model", style="bold")
    t.add_column("Size", justify="right")
    t.add_column("支援 ctx")

    for i, m in enumerate(models, 1):
        ctx_col = fmt_ctx(client.model_max_ctx(m["name"])) if running else "?"
        t.add_row(str(i), m["name"], f"{_gb(m.get('size', 0)):.1f} GB", ctx_col)

    console.print(Panel(t, title=f"[green bold]Installed Models[/]{hint}", border_style="green"))


# ── status ────────────────────────────────────────────────────
@app.command("status", help="顯示閘道/Ollama 進程狀態與已載入記憶體的模型")
def cmd_status():
    client = _client()
    settings = _settings()

    # ── 閘道輪：顯示「門 + Ollama」兩程序狀態 ────────────────────
    gw_pid = client.gateway_pid(settings.gateway_port)
    ol_pid = client.server_pid(settings.ollama_port)
    gw_label = f"[green]運行中[/] PID={gw_pid}" if gw_pid else "[red]未啟動[/]"
    ol_label = f"[green]運行中[/] PID={ol_pid}" if ol_pid else "[red]未啟動[/]"
    console.print(Panel(
        f"閘道   127.0.0.1:{settings.gateway_port}  {gw_label}\n"
        f"Ollama 127.0.0.1:{settings.ollama_port}  {ol_label}",
        title="[green bold]進程狀態[/]",
        border_style="green" if (gw_pid and ol_pid) else "yellow",
    ))

    _require_running(client)
    loaded = client.list_loaded()

    t = Table(box=box.SIMPLE, show_header=True, header_style="green bold")
    t.add_column("Model", style="bold")
    t.add_column("RAM", justify="right")
    t.add_column("VRAM", justify="right")
    t.add_column("ctx(actual/max)")
    t.add_column("expires")

    if not loaded:
        console.print(Panel("（無）", title="[green bold]Loaded Models[/]", border_style="dim"))
        return

    for m in loaded:
        sz = m.get("size", 0)
        sv = m.get("size_vram", 0)
        vram_col = "-" if not sv else f"{_gb(sv):.1f} GB"
        actual = m.get("context_length")
        mx = client.model_max_ctx(m["name"])
        ctx_str = f"{fmt_ctx(actual)}/{fmt_ctx(mx)}"
        warn = ""
        if actual and actual < settings.effective_ctx(m["name"]):
            warn = " [yellow]⚠ 降載[/]"
        t.add_row(m["name"], f"{_gb(sz):.1f} GB", vram_col, ctx_str + warn, m.get("expires_at", "?")[:19])

    console.print(Panel(t, title="[green bold]Loaded Models[/]", border_style="green"))


# ── load ─────────────────────────────────────────────────────
@app.command("load", help="預熱模型到記憶體")
def cmd_load(
    model: Annotated[Optional[str], typer.Argument()] = None,
    ctx: Annotated[Optional[str], typer.Option("--ctx", "-c", help="num_ctx (256K / 65536)")] = None,
    keep: Annotated[Optional[str], typer.Option("--keep", "-k", help="keep_alive (24h / -1)")] = None,
):
    client = _client()
    settings = _settings()
    _require_running(client)
    m = model or settings.default_model
    _, _, free_b = _sysmem()
    if free_b is not None:
        minfo = next((mo for mo in client.list_models() if mo["name"] == m), {})
        model_sz = minfo.get("size", 0)
        if model_sz and free_b < model_sz + 2_000_000_000:
            console.print(
                f"[yellow]⚠ RAM 可能不足：系統可用 {free_b/1e9:.1f} GB，"
                f"模型約 {model_sz/1e9:.1f} GB（建議保留 2 GB 餘裕）[/yellow]"
            )
    c = parse_ctx(ctx) if ctx else settings.effective_ctx(m)
    k = keep or settings.keep_alive
    console.print(f"[cyan]▶ 載入 [bold]{m}[/bold]  ctx={fmt_ctx(c)}  keep_alive={k}[/cyan]")
    ok = client.load(m, c, k)
    if ok:
        console.print(f"[green]✓ {m} 已就緒[/green]")
    else:
        console.print(f"[red]✗ 載入失敗[/red]")
        raise typer.Exit(1)


# ── unload ───────────────────────────────────────────────────
@app.command("unload", help="從記憶體卸載模型（只列已載入的）")
def cmd_unload(model: Annotated[Optional[str], typer.Argument()] = None):
    client = _client()
    _require_running(client)
    loaded = [m["name"] for m in client.list_loaded()]
    if not loaded:
        console.print("[yellow]⚠ 目前沒有已載入的模型[/yellow]")
        return
    m = model or _pick("卸載哪個模型", loaded, loaded[0])
    console.print(f"[yellow]▶ 卸載 [bold]{m}[/bold][/yellow]")
    ok = client.unload(m)
    console.print(f"[{'green' if ok else 'yellow'}]{'✓ 已卸載' if ok else '⚠ 可能未載入'}[/]  {m}")


# ── switch ────────────────────────────────────────────────────
@app.command("switch", help="卸載 from_model，載入 to_model")
def cmd_switch(
    from_model: Annotated[Optional[str], typer.Argument()] = None,
    to_model: Annotated[Optional[str], typer.Argument()] = None,
):
    client = _client()
    settings = _settings()
    _require_running(client)
    loaded = [m["name"] for m in client.list_loaded()]
    all_models = [m["name"] for m in client.list_models()]
    from_model = from_model or _pick("卸載哪個模型", loaded, loaded[0] if loaded else "")
    to_model = to_model or _pick("載入哪個模型", all_models, settings.default_model)
    if not from_model or not to_model:
        console.print("[red]✗ 請指定來源與目標模型[/red]")
        raise typer.Exit(1)
    console.print(f"[yellow]▶ 卸載 {from_model}[/yellow]")
    client.unload(from_model)
    ctx = settings.effective_ctx(to_model)
    console.print(f"[cyan]▶ 載入 {to_model}  ctx={fmt_ctx(ctx)}[/cyan]")
    ok = client.load(to_model, ctx, settings.keep_alive)
    console.print(f"[{'green' if ok else 'red'}]{'✓ 完成' if ok else '✗ 失敗'}[/]")


# ── run ───────────────────────────────────────────────────────
@app.command("run", help="互動對話模式")
def cmd_run(model: Annotated[Optional[str], typer.Argument()] = None):
    client = _client()
    settings = _settings()
    if model is None:
        running = client.is_running()
        names = [e["name"] for e in (client.list_models() if running else client.disk_models())]
        m = _pick("選擇對話模型", names, settings.default_model) if names else settings.default_model
    else:
        m = model
    ctx = settings.effective_ctx(m)
    console.print(f"[cyan]▶ 互動模式：[bold]{m}[/bold]  ctx={fmt_ctx(ctx)}[/cyan]")
    env = os.environ.copy()
    env["OLLAMA_NUM_CTX"] = str(ctx)
    # 直連 Ollama 私有埠，繞過閘道（閘道掛了也不影響 run）
    env["OLLAMA_HOST"] = f"http://127.0.0.1:{settings.ollama_port}"
    subprocess.run(["ollama", "run", m], env=env)


# ── chat ──────────────────────────────────────────────────────
@app.command("chat", help="多輪對話（自控 system prompt 與取樣參數）")
def cmd_chat(
    model: Annotated[Optional[str], typer.Argument()] = None,
    system: Annotated[Optional[str], typer.Option("--system", "-s", help="System prompt")] = None,
    temp: Annotated[Optional[float], typer.Option("--temp", help="Temperature")] = None,
    top_p: Annotated[Optional[float], typer.Option("--top-p", help="top_p 取樣")] = None,
    top_k: Annotated[Optional[int], typer.Option("--top-k", help="top_k 取樣")] = None,
    stop: Annotated[Optional[list[str]], typer.Option("--stop", help="停止序列（可多次）")] = None,
    no_stream: Annotated[bool, typer.Option("--no-stream", help="等完整回應再印")] = False,
    preset: Annotated[Optional[str], typer.Option("--preset", "-p", help="載入已存 preset")] = None,
    mcp: Annotated[Optional[str], typer.Option("--mcp", help="MCP server spec（npx:pkg/cmd:exe/python:mod）")] = None,
    fmt: Annotated[Optional[str], typer.Option("--format", help="輸出格式（json）")] = None,
    save: Annotated[Optional[str], typer.Option("--save", help="儲存對話歷史（給定名稱）")] = None,
):
    client = _client()
    settings = _settings()
    _require_running(client)

    # 載入 preset（CLI 選項優先）
    if preset:
        p = settings.get_preset(preset)
        if not p:
            console.print(f"[red]preset '{preset}' 不存在，用 olm preset list 查看[/red]")
            raise typer.Exit(1)
        model = model or p.get("model")
        system = system or p.get("system_prompt")
        if temp is None and p.get("temperature") is not None:
            temp = p["temperature"]
        if top_p is None and p.get("top_p") is not None:
            top_p = p["top_p"]
        if top_k is None and p.get("top_k") is not None:
            top_k = p["top_k"]
        if not stop and p.get("stop_seqs"):
            stop = p["stop_seqs"]
        console.print(f"[dim]已載入 preset：{preset}[/dim]")

    m = model or settings.default_model

    # 只把「有給的」取樣參數放進 options
    options: dict = {}
    if temp is not None:
        options["temperature"] = temp
    if top_p is not None:
        options["top_p"] = top_p
    if top_k is not None:
        options["top_k"] = top_k
    if stop:
        options["stop"] = list(stop)

    mcp_client = None
    if mcp:
        from .mcp import MCPClient, parse_mcp_spec
        cmd_args = parse_mcp_spec(mcp)
        console.print(f"[cyan]啟動 MCP server：{' '.join(cmd_args)}[/cyan]")
        try:
            mcp_client = MCPClient(cmd_args)
            mcp_client.initialize()
        except Exception as e:
            console.print(f"[red]MCP 啟動失敗：{e}[/red]")
            raise typer.Exit(1)

    try:
        _do_chat_repl(
            client, settings, m,
            system=system,
            options=options or None,
            no_stream=no_stream,
            mcp_client=mcp_client,
            fmt=fmt,
            save_name=save,
        )
    finally:
        if mcp_client:
            mcp_client.close()


# ── start ─────────────────────────────────────────────────────
@app.command("start", help="背景啟動 Ollama 服務（不預載模型）")
def cmd_start():
    import time
    client = _client()
    settings = _settings()
    ollama_port = settings.ollama_port
    gw_host = settings.gateway_host
    gw_port = settings.gateway_port

    # 分別判斷 Ollama（私有埠）與閘道，允許部分恢復
    ol_running = client.is_running()
    gw_pid = client.gateway_pid(gw_port)

    if ol_running and gw_pid:
        console.print(f"[yellow]⚠ Ollama(:{ollama_port}) 與閘道(:{gw_port}) 皆已運行[/yellow]")
        return

    if not ol_running:
        # 1. 起 Ollama（私有埠，只綁 127.0.0.1）
        console.print(f"[cyan]▶ 背景啟動 Ollama port={ollama_port}（私有，只綁 127.0.0.1）[/cyan]")
        pid = client.start_server(ollama_port, settings.num_ctx, settings.keep_alive)
        for _ in range(15):
            time.sleep(1)
            if client.is_running():
                break
        else:
            console.print("[red]✗ Ollama 啟動逾時[/red]")
            raise typer.Exit(1)
        console.print(f"[green]✓ Ollama 就緒 PID={pid}（首次推論才載入模型）[/green]")
    else:
        console.print(f"[yellow]Ollama 已在 :{ollama_port} 運行，跳過啟動[/yellow]")

    if not gw_pid:
        # 2. 起閘道（127.0.0.1:11434，外網打不進來）
        console.print(f"[cyan]▶ 啟動閘道 {gw_host}:{gw_port} → Ollama:{ollama_port}[/cyan]")
        client.start_gateway(gw_host, gw_port, ollama_port, settings.chat_timeout)
        time.sleep(1)  # 等 pidfile 寫入
        gw_pid = client.gateway_pid(gw_port)
        if gw_pid:
            console.print(f"[green]✓ 閘道就緒 PID={gw_pid}  127.0.0.1:{gw_port}（外網即斷）[/green]")
        else:
            console.print("[red]✗ 閘道啟動失敗[/red]")
            raise typer.Exit(1)
    else:
        console.print(f"[yellow]閘道已在 :{gw_port} 運行，跳過啟動[/yellow]")


# ── stop ──────────────────────────────────────────────────────
@app.command("stop", help="停止閘道與 Ollama 服務")
def cmd_stop():
    client = _client()
    settings = _settings()
    # 先停閘道（11434），再停 Ollama（11551）
    gw_ok = client.stop_gateway(settings.gateway_port)
    ol_ok = client.stop_server(settings.ollama_port)
    if gw_ok:
        console.print(f"[green]✓ 閘道已停止 (:{settings.gateway_port})[/green]")
    else:
        console.print(f"[yellow]⚠ 找不到閘道進程 (:{settings.gateway_port})[/yellow]")
    if ol_ok:
        console.print(f"[green]✓ Ollama 已停止 (:{settings.ollama_port})[/green]")
    else:
        console.print(f"[yellow]⚠ 找不到 Ollama 進程 (:{settings.ollama_port})[/yellow]")


# ── restart ───────────────────────────────────────────────────
@app.command("restart", help="重啟閘道與 Ollama 服務")
def cmd_restart():
    import time
    client = _client()
    settings = _settings()
    ollama_port = settings.ollama_port
    gw_host = settings.gateway_host
    gw_port = settings.gateway_port

    console.print("[cyan]▶ 停止閘道與 Ollama…[/cyan]")
    client.stop_gateway(gw_port)
    client.stop_server(ollama_port)
    time.sleep(1)

    console.print(f"[cyan]▶ 重啟 Ollama port={ollama_port}…[/cyan]")
    pid = client.start_server(ollama_port, settings.num_ctx, settings.keep_alive)
    for _ in range(15):
        time.sleep(1)
        if client.is_running():
            break
    else:
        console.print("[red]✗ Ollama 重啟逾時[/red]")
        raise typer.Exit(1)
    console.print(f"[green]✓ Ollama 就緒 PID={pid}[/green]")

    console.print(f"[cyan]▶ 重啟閘道 {gw_host}:{gw_port}…[/cyan]")
    client.start_gateway(gw_host, gw_port, ollama_port, settings.chat_timeout)
    time.sleep(1)
    gw_pid = client.gateway_pid(gw_port)
    if not gw_pid:
        console.print("[red]✗ 閘道重啟失敗[/red]")
        raise typer.Exit(1)
    console.print(f"[green]✓ 重啟完成 閘道 PID={gw_pid} / Ollama PID={pid}[/green]")


# ── pull ──────────────────────────────────────────────────────
@app.command("pull", help="下載模型")
def cmd_pull(model: str):
    from rich.progress import (
        Progress, BarColumn, DownloadColumn,
        TransferSpeedColumn, TimeRemainingColumn, TextColumn,
    )
    client = _client()
    _require_running(client)
    console.print(f"[cyan]▶ 下載 [bold]{model}[/bold][/cyan]")
    try:
        with Progress(
            TextColumn("[bold cyan]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task("pulling manifest", total=None)
            for chunk in client.pull_stream(model):
                status = chunk.get("status", "")
                kwargs: dict = {"description": status}
                if "total" in chunk:
                    kwargs["total"] = chunk["total"]
                    kwargs["completed"] = chunk.get("completed", 0)
                progress.update(task, **kwargs)
        console.print(f"[green]✓ {model} 下載完成[/green]")
        client.clear_ctx_cache()
    except Exception:
        console.print("[yellow]▶ 串流異常，改用 subprocess 下載…[/yellow]")
        subprocess.run(["ollama", "pull", model])
        client.clear_ctx_cache()


# ── delete ────────────────────────────────────────────────────
@app.command("delete", help="從磁碟刪除模型（不可復原）")
def cmd_delete(
    model: Annotated[Optional[str], typer.Argument()] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="跳過確認")] = False,
):
    client = _client()
    running = client.is_running()
    installed = [m["name"] for m in (client.list_models() if running else client.disk_models())]
    if not installed:
        console.print("[yellow]⚠ 無已安裝的模型[/yellow]")
        return
    m = model or _pick("刪除哪個模型", installed, "")
    if not m:
        return
    if not yes:
        confirm = input(f"  確定刪除 {m}？此操作不可復原 [y/N] ").strip().lower()
        if confirm != "y":
            console.print("[yellow]已取消[/yellow]")
            return
    console.print(f"[red]▶ 刪除 [bold]{m}[/bold][/red]")
    if not running:
        # fallback: 用 ollama CLI
        result = subprocess.run(["ollama", "rm", m], capture_output=True, text=True)
        if result.returncode == 0:
            console.print(f"[green]✓ {m} 已刪除（離線模式）[/green]")
        else:
            console.print(f"[red]✗ {result.stderr.strip()}[/red]")
            raise typer.Exit(1)
        return
    ok = client.delete(m)
    if ok:
        console.print(f"[green]✓ {m} 已刪除[/green]")
        client.clear_ctx_cache()
    else:
        console.print("[red]✗ 刪除失敗（模型不存在或服務異常）[/red]")
        raise typer.Exit(1)


# ── info ──────────────────────────────────────────────────────
@app.command("info", help="查看模型詳細資訊")
def cmd_info(model: str):
    client = _client()
    _require_running(client)
    console.print(f"[cyan]▶ 模型資訊：[bold]{model}[/bold][/cyan]")
    subprocess.run(["ollama", "show", model])


# ── logs ──────────────────────────────────────────────────────
@app.command("logs", help="查看背景服務日誌（Ollama + 閘道）")
def cmd_logs(
    gateway: Annotated[bool, typer.Option("--gateway", "-g", help="只看閘道日誌")] = False,
):
    if gateway:
        console.print(f"[dim]── 閘道日誌 {GATEWAY_LOGFILE} ──[/dim]")
        subprocess.run(["tail", "-40", GATEWAY_LOGFILE])
    else:
        console.print(f"[dim]── Ollama 日誌 {LOGFILE} ──[/dim]")
        subprocess.run(["tail", "-30", LOGFILE])
        console.print(f"\n[dim]── 閘道日誌 {GATEWAY_LOGFILE} ──[/dim]")
        subprocess.run(["tail", "-10", GATEWAY_LOGFILE])


# ── search ────────────────────────────────────────────────────
@app.command("search", help="搜尋 Ollama 模型庫（爬 ollama.com/search）")
def cmd_search(
    keyword: Annotated[str, typer.Argument(help="搜尋關鍵字")] = "",
    limit: Annotated[int, typer.Option("--limit", "-n", help="最多顯示幾筆")] = 20,
):
    from .api import search_models
    if not keyword:
        console.print("[yellow]請輸入搜尋關鍵字[/yellow]")
        raise typer.Exit(1)
    console.print(f"[cyan]搜尋 ollama.com：[bold]{keyword}[/bold][/cyan]")
    try:
        results = search_models(keyword, limit=limit)
    except ConnectionError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    if not results:
        console.print("[yellow]無結果（ollama.com 可能已改版，或關鍵字無符合）[/yellow]")
        return
    t = Table(box=box.SIMPLE, show_header=True, header_style="green bold")
    t.add_column("#", justify="right", style="cyan")
    t.add_column("Model", style="bold")
    t.add_column("Pulls", justify="right")
    t.add_column("Tags", justify="right")
    t.add_column("Sizes")
    t.add_column("Caps", style="dim")
    t.add_column("Updated", style="dim")
    t.add_column("Description", style="dim", no_wrap=False, max_width=40)
    for i, r in enumerate(results, 1):
        sizes_str = " ".join(r["sizes"]) or "-"
        caps_str = " ".join(r["capabilities"]) or "-"
        t.add_row(
            str(i), r["name"], r["pulls"], r["tags"],
            sizes_str, caps_str, r["updated"], r["description"][:80],
        )
    console.print(Panel(t, title=f"[green bold]搜尋結果：{keyword}[/]", border_style="green"))
    console.print(f"  [dim]提示：olm pull <model>:<tag> 下載模型[/dim]")


# ── embed ─────────────────────────────────────────────────────
@app.command("embed", help="測試 embeddings（向量維度/前8值/耗時）")
def cmd_embed(
    text: Annotated[str, typer.Argument(help="要嵌入的文字")] = "",
    model: Annotated[Optional[str], typer.Option("--model", "-m", help="Embedding 模型")] = None,
):
    import time as _time
    client = _client()
    _require_running(client)
    if not text:
        console.print("[yellow]請輸入要嵌入的文字[/yellow]")
        raise typer.Exit(1)
    m = model or "nomic-embed-text"
    console.print(f"[cyan]embed：[bold]{m}[/bold][/cyan]")
    t0 = _time.monotonic()
    result = client.embed(text, m)
    elapsed = _time.monotonic() - t0
    if not result:
        console.print("[red]失敗（模型未安裝或服務異常）[/red]")
        raise typer.Exit(1)
    embeddings = result.get("embeddings", [])
    if not embeddings or not embeddings[0]:
        console.print("[red]回應無嵌入向量[/red]")
        raise typer.Exit(1)
    vec = embeddings[0]
    dim = len(vec)
    preview = [f"{v:.4f}" for v in vec[:8]]
    console.print(f"  維度：[bold]{dim}[/bold]")
    console.print(f"  前8值：[dim]{', '.join(preview)}[/dim]")
    console.print(f"  耗時：[bold]{elapsed:.3f}s[/bold]")


# ── bench ─────────────────────────────────────────────────────
@app.command("bench", help="測試推論速度（tok/s）")
def cmd_bench(model: Annotated[Optional[str], typer.Argument()] = None):
    client = _client()
    settings = _settings()
    _require_running(client)
    m = model or settings.default_model
    _do_bench(client, settings, m)


# ── config ────────────────────────────────────────────────────
_config_app = typer.Typer(help="查詢/改動 SQLite 設定")
app.add_typer(_config_app, name="config")


@_config_app.callback(invoke_without_command=True)
def config_default(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        _config_list()


@_config_app.command("list", help="列出所有設定")
def _config_list():
    settings = _settings()
    t = Table(box=box.SIMPLE, show_header=True, header_style="green bold")
    t.add_column("key", style="bold")
    t.add_column("value")
    for k in ["num_ctx", "keep_alive", "request_timeout", "chat_timeout", "default_model",
               "ollama_port", "gateway_host", "gateway_port"]:
        v = settings.get(k)
        display = fmt_ctx(int(v)) if k == "num_ctx" else v
        t.add_row(k, display)
    console.print(Panel(t, title="[green bold]全域設定[/]", border_style="green"))

    ovs = settings.list_model_ctx()
    if ovs:
        t2 = Table(box=box.SIMPLE, show_header=True, header_style="green bold")
        t2.add_column("model", style="bold")
        t2.add_column("num_ctx")
        for m, c in ovs:
            t2.add_row(m, fmt_ctx(c))
        console.print(Panel(t2, title="[green bold]per-model ctx[/]", border_style="green"))


@_config_app.command("get", help="取得某個設定值")
def _config_get(key: str):
    settings = _settings()
    console.print(settings.get(key))


@_config_app.command("set", help="改動設定值（num_ctx 支援 256K/1M）")
def _config_set(key: str, value: str):
    import re
    settings = _settings()
    if key == "num_ctx":
        p = parse_ctx(value)
        if not p:
            console.print("[red]✗ 無效 ctx[/red]")
            raise typer.Exit(1)
        settings.set(key, str(p))
        console.print(f"[green]✓ {key} = {fmt_ctx(p)}[/green]")
    elif key == "keep_alive":
        if not re.match(r'^\d+[smhd]?$', value) and value != "-1":
            console.print(f"[red]✗ keep_alive 格式無效（範例：24h、30m、3600s、-1）[/red]")
            raise typer.Exit(1)
        settings.set(key, value)
        console.print(f"[green]✓ {key} = {value}[/green]")
    else:
        # 非 loopback 的 gateway_host 安全警告（仍允許寫入，但醒目提示）
        if key == "gateway_host" and value not in ("127.0.0.1", "::1", "localhost"):
            console.print(
                f"[bold yellow]⚠ 警告：gateway_host={value} 為非 loopback 位址。\n"
                "  LAN 白名單政策未啟用前，外機將可直達閘道，破壞本機隔離。\n"
                "  下一輪 LAN 政策輪完成前，強烈建議維持 127.0.0.1。[/bold yellow]"
            )
        settings.set(key, value)
        console.print(f"[green]✓ {key} = {value}[/green]")


@_config_app.command("model", help="設定/清除某模型的專屬 ctx")
def _config_model(
    name: str,
    ctx_or_clear: str = typer.Argument(..., help="ctx 值 (256K/65536) 或 'clear'"),
):
    settings = _settings()
    if ctx_or_clear.lower() == "clear":
        settings.del_model_ctx(name)
        console.print(f"[green]✓ 已清除 {name} 專屬設定[/green]")
    else:
        p = parse_ctx(ctx_or_clear)
        if not p:
            console.print("[red]✗ 無效 ctx[/red]")
            raise typer.Exit(1)
        settings.set_model_ctx(name, p)
        console.print(f"[green]✓ {name} ctx = {fmt_ctx(p)}[/green]")


# ── preset ────────────────────────────────────────────────────
_preset_app = typer.Typer(help="對話 preset 儲存/載入")
app.add_typer(_preset_app, name="preset")


@_preset_app.callback(invoke_without_command=True)
def preset_default(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        _preset_list()


@_preset_app.command("save", help="儲存 preset（取樣參數 + system prompt）")
def _preset_save(
    name: str,
    model: Annotated[Optional[str], typer.Option("--model", "-m")] = None,
    system: Annotated[Optional[str], typer.Option("--system", "-s")] = None,
    temp: Annotated[Optional[float], typer.Option("--temp")] = None,
    top_p: Annotated[Optional[float], typer.Option("--top-p")] = None,
    top_k: Annotated[Optional[int], typer.Option("--top-k")] = None,
    stop: Annotated[Optional[list[str]], typer.Option("--stop")] = None,
):
    settings = _settings()
    settings.save_preset(name, model, system, temp, top_p, top_k, list(stop) if stop else None)
    console.print(f"[green]✓ preset [bold]{name}[/bold] 已儲存[/green]")


@_preset_app.command("list", help="列出所有 preset")
def _preset_list():
    settings = _settings()
    presets = settings.list_presets()
    if not presets:
        console.print("[yellow]（無 preset）[/yellow]  提示：olm preset save <name> --system '...' --temp 0.7")
        return
    t = Table(box=box.SIMPLE, show_header=True, header_style="green bold")
    t.add_column("Name", style="bold")
    t.add_column("Model")
    t.add_column("temp", justify="right")
    t.add_column("top_p", justify="right")
    t.add_column("top_k", justify="right")
    t.add_column("System", style="dim", max_width=40)
    t.add_column("Created")
    for p in presets:
        t.add_row(
            p["name"], p["model"] or "-", str(p["temperature"] or "-"),
            str(p["top_p"] or "-"), str(p["top_k"] or "-"),
            (p["system_prompt"] or "-")[:40], (p["created_at"] or "")[:16],
        )
    console.print(Panel(t, title="[green bold]Presets[/]", border_style="green"))


@_preset_app.command("delete", help="刪除 preset")
def _preset_delete(name: str):
    settings = _settings()
    if settings.delete_preset(name):
        console.print(f"[green]✓ preset [bold]{name}[/bold] 已刪除[/green]")
    else:
        console.print(f"[red]✗ preset '{name}' 不存在[/red]")
        raise typer.Exit(1)


# ── history ───────────────────────────────────────────────────
_history_app = typer.Typer(help="對話歷史管理")
app.add_typer(_history_app, name="history")


@_history_app.callback(invoke_without_command=True)
def history_default(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        _history_list()


@_history_app.command("list", help="列出對話歷史")
def _history_list(limit: int = 20):
    settings = _settings()
    convs = settings.list_conversations(limit)
    if not convs:
        console.print("[yellow]（無歷史對話）[/yellow]  提示：olm chat --save '對話名稱'")
        return
    t = Table(box=box.SIMPLE, show_header=True, header_style="green bold")
    t.add_column("ID", justify="right", style="cyan")
    t.add_column("名稱", style="bold")
    t.add_column("模型")
    t.add_column("訊息數", justify="right")
    t.add_column("最後更新")
    for c in convs:
        t.add_row(
            str(c["id"]), c["name"] or "-", c["model"] or "-",
            str(c["msg_count"]), (c["updated_at"] or "")[:16],
        )
    console.print(Panel(t, title="[green bold]對話歷史[/]", border_style="green"))


@_history_app.command("show", help="顯示對話內容")
def _history_show(conv_id: int):
    settings = _settings()
    messages = settings.get_conversation_messages(conv_id)
    if not messages:
        console.print(f"[red]✗ 對話 #{conv_id} 不存在[/red]")
        raise typer.Exit(1)
    for m in messages:
        role = m["role"]
        label = {
            "user": "[bold cyan][你][/]",
            "assistant": "[bold green][AI][/]",
            "system": "[dim][System][/]",
            "tool": "[yellow][Tool][/]",
        }.get(role, f"[{role}]")
        console.print(f"{label} {m['content']}")


@_history_app.command("export", help="匯出對話為 Markdown")
def _history_export(
    conv_id: int,
    output: Annotated[Optional[str], typer.Option("--output", "-o")] = None,
):
    settings = _settings()
    md = settings.export_conversation_md(conv_id)
    if not md:
        console.print(f"[red]✗ 對話 #{conv_id} 不存在[/red]")
        raise typer.Exit(1)
    if output:
        with open(output, "w") as f:
            f.write(md)
        console.print(f"[green]✓ 已匯出到 {output}[/green]")
    else:
        console.print(md)


@_history_app.command("search", help="搜尋對話歷史")
def _history_search(keyword: str):
    settings = _settings()
    with settings._conn() as conn:
        rows = conn.execute(
            "SELECT c.id,c.name,c.model,m.role,m.content,m.created_at "
            "FROM messages m JOIN conversations c ON c.id=m.conv_id "
            "WHERE m.content LIKE ? ORDER BY m.created_at DESC LIMIT 20",
            (f"%{keyword}%",),
        ).fetchall()
    if not rows:
        console.print(f"[yellow]⚠ 找不到含「{keyword}」的訊息[/yellow]")
        return
    for r in rows:
        role_label = {"user": "[cyan][你][/]", "assistant": "[green][AI][/]"}.get(r[3], f"[{r[3]}]")
        match = r[4][:100].replace(keyword, f"[bold yellow]{keyword}[/]")
        console.print(f"#{r[0]} {r[1] or '無名'} ({r[2]}) {role_label}: {match}")


@_history_app.command("delete", help="刪除對話歷史")
def _history_delete(
    conv_id: int,
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
):
    settings = _settings()
    if not yes:
        confirm = input(f"確定刪除對話 #{conv_id}？[y/N] ").strip().lower()
        if confirm != "y":
            console.print("[yellow]已取消[/yellow]")
            return
    if settings.delete_conversation(conv_id):
        console.print(f"[green]✓ 對話 #{conv_id} 已刪除[/green]")
    else:
        console.print(f"[red]✗ 對話 #{conv_id} 不存在[/red]")
        raise typer.Exit(1)
