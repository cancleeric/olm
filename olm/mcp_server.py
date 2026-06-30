"""MCP server — olm 以 stdio JSON-RPC 2.0 對外當 MCP server。

讓 Claude Desktop / Cursor / 其他 AI 工具透過 MCP 管理 Ollama。

用法（Claude Desktop / claude_desktop_config.json）：
{
  "mcpServers": {
    "olm": {
      "command": "olm",
      "args": ["serve-mcp"]
    }
  }
}
"""
import json
import sys

TOOLS = [
    {
        "name": "list_models",
        "description": "列出本機所有已安裝的 Ollama 模型（含大小、支援 ctx）",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "loaded_models",
        "description": "列出目前已載入記憶體的模型（含 VRAM 用量）",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "load_model",
        "description": "將模型預熱到記憶體",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "模型名稱，例 qwen2.5:7b"},
                "num_ctx": {"type": "integer", "description": "Context 長度，例 32768"},
                "num_gpu": {"type": "integer", "description": "GPU layers，-1=全 GPU，0=CPU only"},
            },
            "required": ["model"],
        },
    },
    {
        "name": "unload_model",
        "description": "從記憶體卸載模型",
        "inputSchema": {
            "type": "object",
            "properties": {"model": {"type": "string"}},
            "required": ["model"],
        },
    },
    {
        "name": "chat",
        "description": "與 Ollama 模型單輪對話（非串流，回傳完整回應）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string"},
                "messages": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string"},
                            "content": {"type": "string"},
                        },
                    },
                },
                "temperature": {"type": "number"},
                "num_ctx": {"type": "integer"},
            },
            "required": ["model", "messages"],
        },
    },
    {
        "name": "get_status",
        "description": "取得 Ollama 服務狀態、RAM 用量、閘道狀態",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "pull_model",
        "description": "從 ollama.com 下載模型（同步等待完成）",
        "inputSchema": {
            "type": "object",
            "properties": {"model": {"type": "string"}},
            "required": ["model"],
        },
    },
    {
        "name": "get_model_info",
        "description": "查詢模型詳細資訊（prompt template 格式、參數數量等）",
        "inputSchema": {
            "type": "object",
            "properties": {"model": {"type": "string"}},
            "required": ["model"],
        },
    },
    {
        "name": "bench",
        "description": "測試模型推論速度（tok/s）",
        "inputSchema": {
            "type": "object",
            "properties": {"model": {"type": "string"}},
            "required": ["model"],
        },
    },
    {
        "name": "list_presets",
        "description": "列出所有已儲存的 preset",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_models",
        "description": "搜尋 ollama.com 模型庫",
        "inputSchema": {
            "type": "object",
            "properties": {"keyword": {"type": "string"}},
            "required": ["keyword"],
        },
    },
]


def _send(msg: dict):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _send_error(req_id, code: int, message: str):
    _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def _send_result(req_id, result):
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _send_tool_result(req_id, text: str, is_error: bool = False):
    _send_result(req_id, {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    })


def _handle_tool(req_id, name: str, args: dict, client, settings):
    """分派工具呼叫，回傳結果文字。"""
    try:
        if name == "list_models":
            models = client.list_models()
            lines = [f"已安裝 {len(models)} 個模型："]
            for m in models:
                sz = m.get("size", 0) / 1e9
                lines.append(f"  {m['name']}  {sz:.1f}GB")
            _send_tool_result(req_id, "\n".join(lines))

        elif name == "loaded_models":
            loaded = client.list_loaded()
            if not loaded:
                _send_tool_result(req_id, "目前無已載入模型")
            else:
                lines = [f"已載入 {len(loaded)} 個模型："]
                for m in loaded:
                    vram = m.get("size_vram", 0) / 1e9
                    lines.append(f"  {m['name']}  VRAM={vram:.1f}GB")
                _send_tool_result(req_id, "\n".join(lines))

        elif name == "load_model":
            model = args["model"]
            num_ctx = args.get("num_ctx")
            num_gpu = args.get("num_gpu")
            ok = client.load(
                model,
                num_ctx if num_ctx is not None else settings.num_ctx,
                settings.keep_alive,
                num_gpu=num_gpu,
            )
            _send_tool_result(req_id, f"{'✓ 載入成功' if ok else '✗ 載入失敗'}：{model}")

        elif name == "unload_model":
            model = args["model"]
            ok = client.unload(model)
            _send_tool_result(req_id, f"{'✓ 已卸載' if ok else '✗ 卸載失敗'}：{model}")

        elif name == "chat":
            model = args["model"]
            messages = args["messages"]
            options: dict = {}
            if "temperature" in args:
                options["temperature"] = args["temperature"]
            if "num_ctx" in args:
                options["num_ctx"] = args["num_ctx"]

            full_content = ""
            for chunk in client.chat_stream(model, messages, options=options or None):
                msg = chunk.get("message", {})
                full_content += msg.get("content", "")
                if chunk.get("done"):
                    break
            _send_tool_result(req_id, full_content)

        elif name == "get_status":
            from .api import _sysmem
            running = client.is_running()
            total, used, free = _sysmem()
            status = {
                "ollama": "running" if running else "stopped",
                "ram_free_gb": round((free or 0) / 1e9, 1),
                "ram_used_gb": round((used or 0) / 1e9, 1),
            }
            if running:
                loaded = client.list_loaded()
                status["loaded_models"] = [m["name"] for m in loaded]
            _send_tool_result(req_id, json.dumps(status, ensure_ascii=False, indent=2))

        elif name == "pull_model":
            model = args["model"]
            last_statuses: list[str] = []
            for chunk in client.pull_stream(model):
                s = chunk.get("status", "")
                if s and (not last_statuses or last_statuses[-1] != s):
                    last_statuses.append(s)
            _send_tool_result(req_id, f"✓ 下載完成：{model}\n" + "\n".join(last_statuses[-3:]))

        elif name == "get_model_info":
            model = args["model"]
            info = client.show(model)
            if not info:
                _send_tool_result(req_id, f"找不到模型：{model}", is_error=True)
                return
            tmpl = info.get("template", "")
            t = tmpl.lower()
            if "<|im_start|>" in t:
                fmt = "ChatML"
            elif "<|start_header_id|>" in t:
                fmt = "Llama3"
            elif "[inst]" in t:
                fmt = "Llama2"
            elif "### instruction" in t:
                fmt = "Alpaca"
            elif "<|user|>" in t:
                fmt = "Phi"
            else:
                fmt = "Unknown"

            mi = info.get("model_info", {}) or {}
            ctx = mi.get("llm.context_length", "?")
            params = mi.get("general.parameter_count", "?")
            _send_tool_result(req_id, f"模型：{model}\n格式：{fmt}\nContext：{ctx}\n參數：{params}")

        elif name == "bench":
            model = args["model"]
            messages = [{"role": "user", "content": "Count from 1 to 20."}]
            eval_count = 0
            eval_duration = 0
            for chunk in client.chat_stream(model, messages):
                if chunk.get("done"):
                    eval_count = chunk.get("eval_count", 0)
                    eval_duration = chunk.get("eval_duration", 0)
                    break
            if eval_count and eval_duration:
                tps = eval_count / (eval_duration / 1e9)
                _send_tool_result(req_id, f"{model}: {tps:.1f} tok/s ({eval_count} tokens)")
            else:
                _send_tool_result(req_id, "無法取得 bench 數據（模型是否在跑？）", is_error=True)

        elif name == "list_presets":
            presets = settings.list_presets()
            if not presets:
                _send_tool_result(req_id, "（無 preset）")
            else:
                lines = [f"找到 {len(presets)} 個 preset："]
                for p in presets:
                    lines.append(f"  {p['name']}  model={p.get('model') or '-'}  temp={p.get('temperature') or '-'}")
                _send_tool_result(req_id, "\n".join(lines))

        elif name == "search_models":
            from .api import search_models
            keyword = args["keyword"]
            results = search_models(keyword, limit=10)
            if not results:
                _send_tool_result(req_id, f"找不到 '{keyword}'")
            else:
                lines = [f"搜尋 '{keyword}' 結果（{len(results)} 筆）："]
                for r in results:
                    lines.append(f"  {r['name']}  {r.get('pulls', '')}  {r.get('description', '')[:50]}")
                _send_tool_result(req_id, "\n".join(lines))

        else:
            _send_error(req_id, -32601, f"Unknown tool: {name}")

    except Exception as e:
        _send_tool_result(req_id, f"錯誤：{e}", is_error=True)


def run_server():
    """主迴圈：從 stdin 讀 JSON-RPC，處理，回 stdout。"""
    from .api import OllamaClient
    from .db import Settings

    settings = Settings()
    client = OllamaClient(f"http://127.0.0.1:{settings.ollama_port}")

    import olm as _olm

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = req.get("method", "")
        req_id = req.get("id")
        params = req.get("params", {})

        if method == "initialize":
            _send_result(req_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "olm", "version": _olm.__version__},
            })

        elif method == "notifications/initialized":
            pass  # no response for notifications

        elif method == "tools/list":
            _send_result(req_id, {"tools": TOOLS})

        elif method == "tools/call":
            name = params.get("name", "")
            tool_args = params.get("arguments", {})
            _handle_tool(req_id, name, tool_args, client, settings)

        elif req_id is not None:
            _send_error(req_id, -32601, f"Method not found: {method}")


if __name__ == "__main__":
    run_server()
