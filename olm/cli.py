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

from .api import OllamaClient, LOGFILE, _sysmem
from .db import Settings, parse_ctx, fmt_ctx
from .dashboard import run_dashboard, _do_bench, _pick

app = typer.Typer(
    name="olm",
    help="Ollama model management CLI — dashboard, load, bench, config & more.",
    no_args_is_help=False,
    invoke_without_command=True,
)
console = Console()


def _client() -> OllamaClient:
    return OllamaClient()


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
@app.command("status", help="顯示已載入記憶體的模型（含 ctx 資訊）")
def cmd_status():
    client = _client()
    settings = _settings()
    _require_running(client)
    loaded = client.list_loaded()

    t = Table(box=box.SIMPLE, show_header=True, header_style="green bold")
    t.add_column("Model", style="bold")
    t.add_column("RAM", justify="right")
    t.add_column("ctx(actual/max)")
    t.add_column("expires")

    if not loaded:
        console.print(Panel("（無）", title="[green bold]Loaded Models[/]", border_style="dim"))
        return

    for m in loaded:
        sz = m.get("size", 0)
        actual = m.get("context_length")
        mx = client.model_max_ctx(m["name"])
        ctx_str = f"{fmt_ctx(actual)}/{fmt_ctx(mx)}"
        warn = ""
        if actual and actual < settings.effective_ctx(m["name"]):
            warn = " [yellow]⚠ 降載[/]"
        t.add_row(m["name"], f"{_gb(sz):.1f} GB", ctx_str + warn, m.get("expires_at", "?")[:19])

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
    from_model: str,
    to_model: str,
):
    client = _client()
    settings = _settings()
    _require_running(client)
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
    subprocess.run(["ollama", "run", m], env=env)


# ── start ─────────────────────────────────────────────────────
@app.command("start", help="背景啟動 Ollama 服務（不預載模型）")
def cmd_start():
    import time
    client = _client()
    settings = _settings()
    if client.is_running():
        console.print(f"[yellow]⚠ 服務已在 {client.base_url} 運行[/yellow]")
        return
    port = int(os.environ.get("OLLAMA_PORT", "11434"))
    console.print(f"[cyan]▶ 背景啟動 Ollama port={port}（不預載模型）[/cyan]")
    pid = client.start_server(port, settings.num_ctx, settings.keep_alive)
    for _ in range(15):
        time.sleep(1)
        if client.is_running():
            console.print(f"[green]✓ 服務就緒 PID={pid}（首次推論才載入模型）[/green]")
            return
    console.print("[red]✗ 服務啟動逾時[/red]")
    raise typer.Exit(1)


# ── stop ──────────────────────────────────────────────────────
@app.command("stop", help="停止 Ollama 服務")
def cmd_stop():
    client = _client()
    port = int(os.environ.get("OLLAMA_PORT", "11434"))
    if client.stop_server(port):
        console.print("[green]✓ Ollama 已停止[/green]")
    else:
        console.print("[yellow]⚠ 找不到 Ollama 進程[/yellow]")


# ── restart ───────────────────────────────────────────────────
@app.command("restart", help="重啟 Ollama 服務")
def cmd_restart():
    import time
    client = _client()
    settings = _settings()
    console.print("[cyan]▶ 停止服務…[/cyan]")
    client.stop_server()
    time.sleep(1)
    port = int(os.environ.get("OLLAMA_PORT", "11434"))
    console.print("[cyan]▶ 重啟服務…[/cyan]")
    pid = client.start_server(port, settings.num_ctx, settings.keep_alive)
    for _ in range(15):
        time.sleep(1)
        if client.is_running():
            console.print(f"[green]✓ 重啟完成 PID={pid}[/green]")
            return
    console.print("[red]✗ 重啟逾時[/red]")
    raise typer.Exit(1)


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
    _require_running(client)
    installed = [m["name"] for m in client.list_models()]
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
@app.command("logs", help="查看背景服務日誌")
def cmd_logs():
    subprocess.run(["tail", "-40", LOGFILE])


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
    for k in ["num_ctx", "keep_alive", "request_timeout", "chat_timeout", "default_model"]:
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
    settings = _settings()
    if key == "num_ctx":
        p = parse_ctx(value)
        if not p:
            console.print("[red]✗ 無效 ctx[/red]")
            raise typer.Exit(1)
        settings.set(key, str(p))
        console.print(f"[green]✓ {key} = {fmt_ctx(p)}[/green]")
    else:
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
