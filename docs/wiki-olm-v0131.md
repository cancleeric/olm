# olm — 終端機版 LM Studio

> Wiki 路徑：`/hurricanesoft/olm`
> 版本：v0.1.31（2026-06-30）
> Gitea：ssh://localhost:2230/hurricanesoft/olm.git

olm 是 HurricaneSoft 內部的 Ollama 管理 CLI，對標 LM Studio，以終端機 TUI 形式實現本地 LLM 管理。

## 安裝

```bash
pipx install -e ~/HurricaneSoft/olm
```

## 功能清單

| 功能 | 指令 |
|------|------|
| TUI Dashboard | `olm` |
| 多輪對話（進階） | `olm chat [model]` |
| 原生 REPL | `olm run [model]` |
| 搜尋模型 | `olm search <keyword>` |
| 模型資訊 | `olm info <model>` |
| 下載模型 | `olm pull <model>` |
| 速度測試 | `olm bench [model]` |
| 歷史記錄 | `olm history list/show/export/search/delete` |
| Preset 管理 | `olm preset save/list/delete` |
| 向量嵌入測試 | `olm embed <text>` |
| OpenAI 閘道 | `olm gateway start/stop/allow/deny/list` |
| MCP Server | `olm serve-mcp` |

## TUI Dashboard

執行 `olm` 進入互動式 Dashboard：

- **1/7**：啟動 Ollama 服務（帶 spinner 等待就緒）
- **2**：停止 Ollama
- **3**：重啟 Ollama（spinner 等待）
- **4**：載入模型到記憶體（picker + ctx 調整 + RAM 警告）
- **5**：開始對話（picker + ctx 調整）
- **6**：下載模型（Enter 開搜尋 → tag picker → 進度條下載）
- **7**：從記憶體卸載模型
- **f**：搜尋 ollama.com 模型庫（已安裝標記 ✓ + 直接下載）
- **g**：Gateway ACL 管理
- **h**：歷史記錄清單
- **p**：Preset 清單
- **s**：Settings

首次使用（無模型+服務未啟動）會顯示入門引導。

## Chat REPL 斜線指令

進入 `olm chat` 後可用：

| 指令 | 說明 |
|------|------|
| `/help` | 顯示所有指令 |
| `/ctx [n]` | 調整 context 視窗（無參數開 TUI picker） |
| `/clear` | 清除對話記錄（保留 system prompt） |
| `/model [名]` | 切換模型（無參數開 picker） |
| `/temp [0~2]` | 動態調整 temperature |
| `/system [p]` | 更新 system prompt（無參數多行輸入） |
| `/save [名]` | 開始儲存此對話到歷史記錄 |
| `/bye` | 離開對話 |

對話中顯示：
- ctx 視覺 bar：`[████░░░░░░] 40%  4,096/10,240`
- TPS（tokens/s）
- 已安裝模型的量化等級（Q4_K_M / FP16 / MLX 等）

## OpenAI 相容端點（Gateway）

```
GET  /v1/models
POST /v1/chat/completions   # streaming SSE 支援
POST /v1/embeddings
```

任何 OpenAI SDK 工具指向 `http://localhost:11434/v1` 即可直用（VS Code Continue、LangChain、Open WebUI 等）。

LAN 白名單管理：
```bash
olm gateway allow 192.168.0.0/24
olm gateway list
olm gateway deny 192.168.0.0/24
```

## MCP Server

```bash
olm serve-mcp   # stdio JSON-RPC 2.0
```

Claude Desktop 設定：
```json
{"mcpServers":{"olm":{"command":"olm","args":["serve-mcp"]}}}
```

11 個工具：
`list_models`, `loaded_models`, `load_model`, `unload_model`, `chat`, `get_status`, `pull_model`, `get_model_info`, `bench`, `list_presets`, `search_models`

## Tag Picker（Pull 前選量化版本）

執行 `olm pull llama3.2` 或 Dashboard 下載時，自動爬取 ollama.com 可用 tag 列表：

```
#   Tag                           Size
1   3b-instruct-q4_K_M            2.0 GB
2   3b-instruct-q8_0              3.3 GB
3   3b-instruct-fp16              6.4 GB
...
選擇 tag（Enter 使用 latest）: _
```

## 版本歷史

| 版本 | 重點 |
|------|------|
| v0.1.22 | OpenAI 相容端點（/v1/models, /v1/chat/completions, /v1/embeddings）、CORS |
| v0.1.27 | MCP Server（serve-mcp），11 個工具 |
| v0.1.28 | TUI Dashboard 升級（ctx picker, wait spinner, GGUF ctx reader, fits? 欄） |
| v0.1.29 | /clear, /model, ctx bar, expires 相對時間, 搜尋直接 pull |
| v0.1.30 | /temp, /save, /system, Q 量化欄, tag picker, action 6 搜尋流程 |
| v0.1.31 | Onboarding 提示, /help, fetch_model_tags, restart spinner, _pick 反饋 |

## 架構

```
olm/
├── cli.py          # typer CLI 入口（所有子指令）
├── dashboard.py    # TUI Dashboard + Chat REPL
├── api.py          # OllamaClient HTTP wrapper + fetch_model_tags
├── gateway.py      # OpenAI 相容閘道（BaseHTTPRequestHandler）
├── mcp_server.py   # MCP Server（stdio JSON-RPC 2.0）
├── db.py           # SQLite（settings, history, presets, bench, gateway_acl）
└── __init__.py     # 版本號
```

## 開發埠

| 服務 | 埠 |
|------|-----|
| Ollama（私有） | 127.0.0.1:11551 |
| olm Gateway（公開） | 127.0.0.1:11434 |
| MCP Server | stdio |
