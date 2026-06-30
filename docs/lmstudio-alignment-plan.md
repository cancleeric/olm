# olm × LM Studio 缺口分析與開發路線圖

> 版本：v0.1.15 基準 | 分析日：2026-06-30 | 作者：CPO (HurricaneEdge)

---

## CEO 審核裁示（2026-06-30，覆寫下方原排程）

CPO 原分析通過，但 CEO 親自驗證後做以下調整，**路線以本節為準**：

1. **第一輪 = `olm chat`（P1-a）+ VRAM 顯示（G-3）**。VRAM 從原 P2 拉進第一輪（純贏、十幾行）。已派 CTO 開發中（分支 `feat/lmstudio-r1-chat-vram`）。
2. **`olm search`（G-2）降到第二輪，且方法要改**。CEO 親測證偽 CPO 原案：`ollama search` 子指令不存在、`ollama.com/api/search` 回 404、`registry.ollama.ai/v2/library/.../tags/list` 回 404。**唯一可行 = 爬 `https://ollama.com/search?q=` 的 HTML（已驗 200）**，用 stdlib `html.parser`，並接受 HTML 改版的脆弱性（要加防呆與失敗 fallback 訊息）。
3. **MCP / tool-calling 升為旗艦差異化**（原被 CPO 低估埋在 P4）。定位為 olm 的世界第一槓桿：「**第一個能在終端讓本機模型（Ollama）在對話中呼叫 MCP 工具的管理器**」。LM Studio 2026 已將 MCP host 做成標配；Ollama 原生支援 tool calling。排為 **旗艦階段（第一輪 chat 落地後接著做）**，凌駕原 P3 preset / P4 開發者工具之上。

**調整後路線順序**：R1 chat+VRAM（開發中）→ R2 `olm search`（HTML 爬，修正法）→ **R-旗艦 MCP tool-calling chat** → preset / embeddings / JSON / 日誌結構化（原 P3–P5 順位下移）。

---

## 前置說明：依賴鐵律釐清

任務描述中的「純 stdlib / 零外部依賴」在現行程式碼（pyproject.toml）中的實際定義是：

- **已允許**：`typer[all]` (CLI 框架)、`rich` (終端 UI) — 兩者已是既成依賴
- **HTTP 層**：嚴格使用 `urllib`（stdlib），不引入 httpx / requests
- **ML/資料科學層**：禁止（numpy / torch / sentence-transformers 等）
- **新依賴**：任何超出上述範圍的依賴都需獨立評估，本計劃不引入新依賴

---

## 1. 能力對照矩陣

| LM Studio 能力面 | olm 現況 | 狀態 | 半套時缺哪半 |
|---|---|---|---|
| 模型探索與下載（HuggingFace 瀏覽/搜尋、quant 變體、相容性提示） | `pull` 可下載，需知確切名稱；`list` 列本機 | **半套** | 無關鍵字搜尋、無 quant 變體比對、無「是否塞得下」相容性試算 |
| 豐富對話（system prompt、temperature/top_p/top_k、preset、多輪、停止序列） | `run` → subprocess `ollama run`（Ollama CLI 原生多輪） | **半套** | 無 system prompt 控制、無取樣參數（全交 ollama run 預設）、無 preset 存取、無停止序列設定 |
| 本機 server（OpenAI 相容 API、連線數/狀態） | start/stop/restart + 儀表板顯示服務 UP/DOWN + pid | **半套** | 無連線數、無請求吞吐量/延遲統計、無即時 API 使用量監控 |
| 模型載入控制（context length、GPU offload、keep-alive） | ctx（全域/per-model）+ keep-alive 完整 | **半套** | GPU offload 層數（num_gpu）無法從 olm 設定/顯示 |
| 硬體洞察（RAM/VRAM/CPU 即時用量、相容性預估） | RAM total/used/free + 載入前 RAM 不足警示 | **半套** | VRAM 分離顯示缺（/api/ps 回傳 size_vram 欄位但未使用）、CPU 使用率缺、儀表板手動 refresh 非自動 |
| 結構化輸出與 tool/function calling 測試 | 無 | **缺** | — |
| 文件對話/RAG 與 embeddings | 無（Ollama /api/embed 存在但 olm 未包） | **缺** | — |
| 多模型同時載入與切換 | `switch` + `status` 顯示全部載入模型、dashboard load/unload | **半套** | 無多模型並行對話路由（單次只能和一個模型對話） |
| 請求日誌/檢視 | `logs` → `tail -40 /tmp/ollama-serve.log` | **半套** | 無結構化 API 請求紀錄、無關鍵字過濾、無請求延遲分佈 |

**矩陣結論：已有 0 / 半套 7 / 缺 2**（共 9 個能力面）

---

## 2. 缺口清單（優先排序）

排序依據：使用者日常價值（高/中/低）× 實作成本（低/中/高）× 是否違反鐵律（Y=違反即排除）

| 優先 | 缺口代號 | 缺口描述 | 價值 | 成本 | 違反鐵律 | 備注 |
|---|---|---|---|---|---|---|
| 1 | G-1 | 對話取樣參數控制（system prompt / temp / top_p / top_k / stop） | 高 | 中 | 否 | 需自實作 /api/chat 串流，取代 subprocess ollama run |
| 2 | G-2 | Ollama Library 模型搜尋（keyword 查可下載模型） | 高 | 低 | 否 | 走 urllib 呼叫 ollama.com/api/search 端點；或解析 subprocess `ollama search`（若 CLI 版本支援） |
| 3 | G-3 | VRAM 分離顯示（/api/ps size_vram 欄位） | 中 | 低 | 否 | API 資料已有，只差 UI 渲染；對獨顯機使用者洞察顯著 |
| 4 | G-4 | 取樣參數 preset 儲存/套用 | 中 | 低 | 否 | SQLite 新增 presets 表（漸進加欄）；常用「創意模式」「嚴謹模式」可存 |
| 5 | G-5 | Embeddings 測試指令（`olm embed`） | 中 | 低 | 否 | /api/embed 直接呼叫，輸出向量維度與前 N 值 |
| 6 | G-6 | GPU offload 層數顯示與設定（num_gpu 參數） | 中 | 中 | 否 | `olm load --gpu-layers N`；config 加全域 gpu_layers；適用獨顯/混合推論場景 |
| 7 | G-7 | 結構化輸出測試（JSON schema → /api/chat format=json） | 中 | 中 | 否 | 需 JSON schema 輸入流程；開發者測試 structured output 時常用 |
| 8 | G-8 | API 請求日誌結構化（過濾/搜尋/延遲統計） | 中 | 中 | 否 | parse Ollama serve log，加關鍵字過濾與延遲欄位提取 |

**刻意排後 / 不進 P1 的缺口：**
- 多模型並行對話路由（複雜度高，使用頻率低，終端 UX 不易實現）
- 即時自動刷新儀表板（需 loop 輪詢，干擾終端交互，Rich Live 可做但代入侵性改動）

---

## 3. 分階段路線圖

### P1：對話升級 + 模型搜尋（目標 2 週）

**一句目標**：讓使用者從 olm 就能控制對話品質，不必手打 ollama 指令查找模型。

**具體工作：**

#### P1-a：新增 `olm chat` 指令

- 位置：`olm/cli.py` 新增 `chat` 指令，`olm/api.py` 新增 `chat_stream()` 方法
- API 路徑：POST `/api/chat`（NDJSON 串流，message role = user/assistant/system）
- 支援選項：
  - `--system TEXT`：system prompt
  - `--temp FLOAT`：temperature（預設 Ollama 預設值，不傳即不覆蓋）
  - `--top-p FLOAT`：top_p
  - `--top-k INT`：top_k
  - `--stop TEXT`：停止序列（可多次），轉成 options.stop[]
  - `--no-stream`：關閉串流，等完整回應
- 多輪：session 內以 `messages[]` list 累積，`exit` / Ctrl-C 結束
- 保留 `olm run` 不動（仍代理 subprocess `ollama run`，作為 raw/快速模式）

實作細節：
- `api.py` 新增 `chat_stream(model, messages, options, timeout)` → urllib POST + NDJSON 逐行 yield
- `dashboard.py` 動作 5（Run model chat）改為呼叫新 chat 邏輯，並提示 --system 可設定

#### P1-b：新增 `olm search <keyword>` 指令

- 優先嘗試 `ollama search <keyword>`（subprocess），若 CLI 不支援則 fallback 到 urllib GET `https://ollama.com/api/search?q=<keyword>&limit=20`
- Rich 表格顯示：model name / pulls / tags 數量 / 描述
- 儀表板動作加入 `f) Search models`

**P1 驗收（CEO 可親手執行）：**

```bash
# 驗 1：system prompt 生效
olm chat llama3.2 --system "Only reply in Traditional Chinese"
# 輸入：Hello → 驗：終端輸出繁中回應（非 shell 轉接視窗）

# 驗 2：temperature 生效（高 temp 輸出有創意變化）
olm chat qwen3.6:27b --temp 1.2 --top-p 0.95
# 輸入：寫一首詩 → 驗：每次輸出不同

# 驗 3：模型搜尋
olm search llama
# 驗：Rich 表格顯示可下載模型列表，含 pulls 數字

# 驗 4：API 層確認（直接 curl，不走 olm）
curl -s http://localhost:11434/api/chat \
  -d '{"model":"llama3.2","messages":[{"role":"system","content":"只說「是」或「否」"},{"role":"user","content":"你好嗎"}],"stream":false}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['message']['content'])"
# 驗：輸出為「是」或「否」，確認 /api/chat 有效
```

---

### P2：硬體洞察強化（目標 1 週）

**一句目標**：在儀表板顯示 VRAM 用量與 GPU 層資訊，load 時可指定 GPU offload 層數。

**具體工作：**

- `api.py`：`list_loaded()` 結果新增 `size_vram` 欄位解析（/api/ps 已回傳）
- `dashboard.py` + `cli.py status`：Loaded Models 表格加 VRAM 欄
- `cli.py load` 加 `--gpu-layers INT` 選項（對應 Ollama options.num_gpu）
- `db.py`：`settings` 表新增 `gpu_layers` 欄（DEFAULT NULL = 讓 Ollama 自動決定），漸進加欄用 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`
- `config set gpu_layers N` / `config get gpu_layers`

**P2 驗收：**

```bash
olm status
# 驗：Loaded Models 表格出現 VRAM 欄（Apple Silicon 會顯示非零值）

olm load llama3.2 --gpu-layers 0
# 驗：載入後 VRAM 顯示 0（全 CPU 推論）

olm config set gpu_layers 35
olm config list
# 驗：gpu_layers = 35 出現在設定列表
```

---

### P3：取樣 Preset 系統（目標 1 週）

**一句目標**：常用參數組合可命名儲存，`olm chat --preset <name>` 一鍵套用。

**具體工作：**

- `db.py` 新增 `presets` 表：`(name TEXT PRIMARY KEY, model TEXT, params_json TEXT)`
- 新增子指令群 `olm preset`：
  - `olm preset save <name> [--model M] [--system TEXT] [--temp F] [--top-p F] [--top-k I]`
  - `olm preset list`
  - `olm preset delete <name>`
- `olm chat --preset <name>` → 載入 preset，可被 CLI 選項覆蓋（CLI 優先）
- 儀表板 `p) Presets` 顯示 preset 列表供選擇

**P3 驗收：**

```bash
olm preset save strict --system "只回答是或否" --temp 0.1
olm preset save creative --temp 1.2 --top-p 0.95
olm preset list
# 驗：表格顯示兩條 preset

olm chat llama3.2 --preset strict
# 輸入：你喜歡音樂嗎 → 驗：只回「是」或「否」
```

---

### P4：開發者工具（目標 2 週）

**一句目標**：讓 olm 成為 Ollama API 開發者的測試終端，支援 embeddings 與結構化輸出。

**具體工作：**

- `olm embed <TEXT> [--model M]`：POST /api/embed，顯示向量維度 + 前 8 個數值 + 計算耗時
- `olm chat --format json [--schema PATH]`：發 /api/chat 加 `format: "json"`；若提供 schema 則自動在 system 中注入「請輸出以下 JSON schema...」
- 儀表板新增：`e) Embed test`、`j) JSON output test`

**P4 驗收：**

```bash
olm embed "Hello world" --model nomic-embed-text
# 驗：顯示「維度：768  [0.123, -0.456, 0.789, ...]  耗時 XXms」

echo '{"type":"object","properties":{"name":{"type":"string"},"age":{"type":"integer"}}}' \
  > /tmp/schema.json
olm chat llama3.2 --format json --schema /tmp/schema.json
# 輸入：介紹張三，30 歲 → 驗：輸出合法 JSON {"name":"張三","age":30}
```

---

### P5：日誌結構化（目標 1 週）

**一句目標**：`olm logs` 支援過濾，讓使用者快速定位 API 問題。

**具體工作：**

- `olm logs [--tail N] [--grep PATTERN] [--level error|warn|info]`
  - stdlib re 過濾、ANSI 著色
- 儀表板動作 9 加 grep 輸入欄

**P5 驗收：**

```bash
olm logs --tail 200 --grep "/api/chat"
# 驗：只顯示含 /api/chat 的行，無其他雜訊
```

---

## 4. 明確排除項

| 排除項 | 理由 |
|---|---|
| GUI 視窗（Qt / Tk / Electron） | 違反終端 UX 鐵律；olm 定位是純 CLI 工具 |
| 自帶推論引擎（llama.cpp 直接呼叫） | 必須走 Ollama HTTP API；自帶引擎引入大量 C++ 依賴，超出工具定位 |
| HuggingFace 直接整合（模型瀏覽、GGUF 直接下載） | HuggingFace API 需解析複雜分頁 + quant 版本比對邏輯；Ollama Library 已覆蓋 95% 主流模型；CP 比低 |
| RAG / 文件向量索引 | 真正 RAG 需向量 DB（sqlite-vec 是外部 dep）或 FAISS（ML dep）；純 TF-IDF stdlib 實作品質不足；超出「終端模型管理」定位 |
| 多租戶 / 雲端設定同步 | 單人本機工具，無跨機同步需求；雲端同步需引入認證機制 |
| OpenAI 相容 proxy 自建 | Ollama 本身就是 OpenAI 相容 API（/v1/chat/completions）；olm 再包一層無附加價值 |
| 儀表板自動刷新（Live TUI） | Rich Live 需 altscreen + 持續輪詢，會干擾互動輸入；當前 press-Enter-to-refresh 模式對單人工具已足夠 |
| 多模型並行對話路由 | 終端 UX 難以優雅支援多模型同時輸入；需要並行 thread 管理；對個人使用頻率極低 |

---

## 附錄：API 端點能力對照

| 功能 | Ollama API 端點 | olm 現況 |
|---|---|---|
| 列模型 | GET /api/tags | 已用 |
| 已載入模型 | GET /api/ps | 已用（size_vram 未解析） |
| 模型資訊 | POST /api/show | 已用（max ctx 取值） |
| 預熱/卸載 | POST /api/generate (keep_alive) | 已用 |
| 下載 | POST /api/pull (stream) | 已用 |
| 刪除 | DELETE /api/delete | 已用 |
| **多輪對話** | **POST /api/chat** | **P1 新增** |
| **Embeddings** | **POST /api/embed** | **P4 新增** |
| OpenAI 相容 | POST /v1/chat/completions | 不包（Ollama 原生即可） |
