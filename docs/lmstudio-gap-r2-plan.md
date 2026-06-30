# olm 下一輪產品計劃 — LM Studio 缺口分析 + Top 5 優先順序

基準版本：olm v0.1.18 | 分析日：2026-06-30 | 作者：CPO

---

## 1. LM Studio 2026 現況（知識基線至 2025-08）

LM Studio 0.3.x（2024–2025）主要能力清單：

| # | 功能 | 說明 |
|---|---|---|
| 1 | 圖形化模型瀏覽器 | HuggingFace 整合，含篩選/量化/架構標籤 |
| 2 | 多輪聊天 + session 持久化 | 自動儲存每段對話，側欄顯示歷史列表，可重新載入 |
| 3 | 多模型並行載入 | 同時預熱多個模型，VRAM 精確分配顯示 |
| 4 | 取樣參數全控 | system/temp/top-p/top-k/repeat-penalty/min-p/seed 一次全設 |
| 5 | Preset（Character 卡） | 命名 preset，含完整取樣參數 + system prompt |
| 6 | JSON 結構化輸出 | 對話中可強制要求 JSON 格式，含 schema 注入 |
| 7 | OpenAI-compatible server | /v1/chat/completions，供第三方工具接線 |
| 8 | 串流即時 TPS 顯示 | 推論過程中每 token 都更新顯示速度（tok/s） |
| 9 | 聊天歷史搜尋與匯出 | 依關鍵字搜歷史，匯出為 markdown 或 JSON |
| 10 | Embedding 測試 | 送文字取向量，顯示維度與耗時 |
| 11 | 本機 RAG（0.3.4+） | 上傳文件自動切塊 + 語意搜尋注入 context |
| 12 | 模型比較模式 | 同一 prompt 送兩個模型，並排顯示輸出 |
| 13 | MLX 後端支援 | Apple Silicon 自動選 MLX runtime（延遲更低） |
| 14 | GPU 層數手動調整 | --gpu-layers 暴露給使用者調整 offload 比例 |
| 15 | bench 跨時段歷史 | 保留每次 benchmark 結果，供趨勢比較 |

---

## 2. 差距矩陣（olm v0.1.18 基準）

### 凡例
- 價值：使用者日常感受到的差異（高/中/低）
- 成本：實作工時（低=1 天內 / 中=2–3 天 / 高=1 週+）
- 純 stdlib：typer+rich 以外不新增 pip 套件

| 缺口代號 | LM Studio 功能 | olm 現況 | 價值 | 成本 | 純 stdlib | 備注 |
|---|---|---|---|---|---|---|
| **G-A** | 聊天歷史持久化（儲存/載入） | 完全缺 | **高** | 低 | 是 | SQLite 已在，只差加表 |
| **G-B** | 串流即時 TPS 顯示 | 僅 bench 事後顯示，chat 全盲 | **高** | 低 | 是 | done chunk 已有 eval_count/eval_duration |
| **G-C** | 聊天歷史匯出（markdown/JSON） | 完全缺 | 中 | 低 | 是 | 依賴 G-A，建議同輪 |
| **G-D** | JSON 結構化輸出（--format json） | 完全缺 | 中 | 低 | 是 | API 原生支援 `format: json` |
| **G-E** | LAN 白名單閘道政策 | 閘道已鎖 localhost，無白名單 | **高** | 中 | 是 | gateway.py 已留 ponytail 標記 |
| **G-F** | HuggingFace 直接搜尋/下載 | 只搜 ollama.com | 中 | 中 | 是 | urllib 爬 hf.co/models API |
| **G-G** | repeat-penalty / min-p / seed 參數 | 缺（olm chat 只有 temp/top-p/top-k） | 低 | 低 | 是 | 直接加 CLI option |
| **G-H** | GPU 層數調整（--gpu-layers） | 缺 | 低 | 低 | 是 | ollama run --num-gpu 已支援 |
| **G-I** | bench 跨時段歷史追蹤 | 缺 | 低 | 低 | 是 | SQLite 一張表 |
| **G-J** | 本機 RAG | 完全缺 | 高 | 高 | 否（需向量算相似度） | 需 numpy 或手寫點積；延後 |
| **G-K** | 多模型並排比較 | 缺 | 低 | 高 | 是 | 終端 UX 笨重；延後 |

### 排除清單（本輪不做）

| 功能 | 排除理由 |
|---|---|
| 本機 RAG | 需外部向量庫或手寫向量算法，成本高且非差異化 |
| 多模型並排比較 | 終端 UX 天然限制，價值低 |
| OpenAI-compatible server | Ollama 原生已相容 /v1/，閘道直接透明轉發即可 |
| MLX 後端支援 | Ollama 0.3+ 已自動選用 MLX，無需 olm 介入 |
| 多人協作/雲端同步 | 設計定位為單人本機工具 |

---

## 3. 下一輪 Top 5 優先順序

### P1 — 聊天歷史持久化（G-A + G-C）

**一句話說明**
每段 `olm chat` 對話自動存入 SQLite，可列表、查看、搜尋、匯出 markdown，終結「聊完即忘」的最大 UX 缺口。

**實作範圍**

`db.py`：新增 `chat_sessions` + `chat_messages` 兩張表

```sql
CREATE TABLE IF NOT EXISTS chat_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    model TEXT NOT NULL,
    preset TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES chat_sessions(id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    ts TEXT DEFAULT (datetime('now'))
);
```

`dashboard.py:_do_chat_repl()`：對話結束前問「是否儲存？輸入名稱或 Enter 略過」，呼叫 `Settings.save_session()`

`cli.py`：新增 `olm chat --save <name>` flag（帶名稱直接儲存，不問）

`cli.py`：新增 `olm history` 子命令群組

```
olm history list [--limit 20]      # 列最近 N 條 session
olm history show <id>              # 顯示完整對話
olm history export <id> [--json]   # 預設 markdown，--json 輸出 JSON
olm history search <keyword>       # 全文搜尋 messages.content
olm history delete <id>            # 刪除單條 session
```

**預估成本**：低（2 天，SQLite schema + CRUD + CLI 子命令）

**驗收條件（CEO 親手測）**

```bash
# 1. 開始對話並儲存
olm chat qwen3:latest --save "測試session"
# 輸入：你好，介紹一下你自己
# 輸入 exit 結束
# 預期：輸出「session 已儲存 id=1」

# 2. 列出歷史
olm history list
# 預期：表格顯示 id=1 "測試session" 模型名稱 建立時間

# 3. 查看內容
olm history show 1
# 預期：角色/內容逐行顯示

# 4. 搜尋
olm history search "介紹"
# 預期：找到該 session，顯示命中摘要

# 5. 匯出
olm history export 1 > /tmp/chat.md
cat /tmp/chat.md
# 預期：markdown 格式，含 # Session 標題、## User / ## Assistant 段落
```

---

### P2 — 串流即時 TPS 顯示（G-B）

**一句話說明**
`olm chat` 每次 AI 回應結束後，在訊息下方顯示本輪 tok/s 與 token 數，告別推論速度全盲。

**實作範圍**

`dashboard.py:_do_chat_repl()`：解析串流 `done=True` chunk 的 `eval_count`、`eval_duration`（ns），計算 `gen_tps = eval_count / (eval_duration / 1e9)`，在每輪 AI 回應後印一行：

```
[dim]  28.4 tok/s · 152 tokens · 5.34s[/dim]
```

無新 API 呼叫，僅改動 `_do_chat_repl()` 約 10 行。

**預估成本**：低（半天）

**驗收條件（CEO 親手測）**

```bash
olm chat qwen3:latest
# 輸入：寫一首五言絕句
# 預期：AI 回應後緊接一行灰色文字，含 tok/s 數字（大於 0）
```

---

### P3 — JSON 結構化輸出（G-D）

**一句話說明**
`olm chat --format json` 強制模型輸出 JSON，Rich 自動語法高亮，讓 olm 成為開發者測試 structured output 的首選終端工具。

**實作範圍**

`cli.py:cmd_chat()`：新增 `--format` option（值：`json` 或留空）

`dashboard.py:_do_chat_repl()`：
- 參數新增 `output_format: str | None = None`
- 組 API payload 時加 `"format": output_format`（若非 None）
- 收到 AI 回應後，若 `output_format == "json"`，嘗試 `json.loads()` + `rich.syntax.Syntax(json.dumps(..., indent=2), "json")` 高亮顯示；解析失敗則 fallback 純文字

無新套件：`rich.syntax` 已在 rich 包內。

**預估成本**：低（半天）

**驗收條件（CEO 親手測）**

```bash
olm chat qwen3:latest --format json
# 輸入：列出三個台灣城市，含人口，輸出 JSON array
# 預期：
#   - 終端顯示語法高亮 JSON（關鍵字藍色，字串綠色）
#   - 結構為合法 JSON（可 echo 輸出 | python3 -m json.tool 驗證）
```

---

### P4 — LAN 白名單閘道政策（G-E）

**一句話說明**
在閘道層插入 IP 白名單，讓使用者可指定哪些 LAN 機器可存取 Ollama，實現「辦公室安全分享」。

**實作範圍**

`db.py`：新增 `gateway_allowlist` 表（value TEXT = CIDR 或精確 IP）

`gateway.py:_Handler._forward()`：在轉發前取 `client_address[0]`，對比 allowlist（localhost 永遠通過）；不在名單回 403。allowlist 每 60 秒 reload 一次（避免每請求都查 SQLite）。

`cli.py`：新增 `olm gateway` 子命令群組

```
olm gateway allow <IP>         # 加入白名單（支援 CIDR，如 192.168.0.0/24）
olm gateway deny <IP>          # 從白名單移除
olm gateway list               # 顯示目前白名單
```

修改 `olm config set gateway_host`：允許 0.0.0.0（搭配白名單使用），並自動提示「請先設定白名單再開放」。

**預估成本**：中（2 天，CIDR 比對用 Python 3.11 `ipaddress` 模組，stdlib 內建）

**驗收條件（CEO 親手測）**

```bash
# 情境：本機 + 辦公室另一台機器 192.168.0.176

# 1. 開放白名單
olm gateway allow 192.168.0.176
olm gateway list
# 預期：表格顯示 192.168.0.176 + 127.0.0.1（預設）

# 2. 調整閘道綁定（允許 LAN）
olm config set gateway_host 0.0.0.0
olm restart

# 3. 從 192.168.0.176 測試存取（CEO 要去那台機器執行）
curl http://192.168.0.70:11434/api/tags
# 預期：回 200 + 模型列表

# 4. 從未授權 IP 測試（可用 curl --interface 指定 source IP）
curl http://192.168.0.70:11434/api/tags （從第三台機器）
# 預期：回 403 Forbidden

# 5. 移除白名單
olm gateway deny 192.168.0.176
olm gateway list
# 預期：列表只剩 127.0.0.1
```

---

### P5 — UX 一致性修補（三小項合一）（P8 + P9 + P11）

**一句話說明**
補齊三個已知 UX 不一致點：`config set` 格式驗證、`olm switch` 互動選單、`olm delete` 離線模式，每項 1–2 小時。

**實作範圍**

**P5-a：`config set` 格式驗證**（`cli.py:_config_set()`）
- `keep_alive`：驗 `\d+[smhd]` 或 `-1`，否則報錯拒絕寫入
- `request_timeout`、`chat_timeout`：驗正整數，否則報錯
- `gateway_port`、`ollama_port`：驗 1024–65535 範圍

**P5-b：`olm switch` 互動選單**（`cli.py:cmd_switch()`）
- 當 `from_model` 或 `to_model` 為空時，從已載入/已安裝列表呼叫 `_pick()`
- 改簽名：兩個參數改為 `Optional[str]`，預設 `None`

**P5-c：`olm delete` 離線模式**（`cli.py:cmd_delete()`）
- 移除 `_require_running(client)` 強制
- 服務未啟動時，fallback 呼叫 `subprocess.run(["ollama", "rm", m])`
- 若 ollama CLI 不在 PATH 才報錯

**預估成本**：低（1 天，三個小改動）

**驗收條件（CEO 親手測）**

```bash
# P5-a：格式驗證
olm config set keep_alive abc123
# 預期：報錯「無效格式，範例：24h / 30m / -1」，拒絕寫入

olm config set chat_timeout 3600
# 預期：成功，顯示 chat_timeout = 3600

# P5-b：switch 互動選單
olm switch
# 預期：出現「卸載哪個（已載入）」選單 → 選後出現「載入哪個（已安裝）」選單

# P5-c：delete 離線模式
olm stop
olm delete
# 預期：列出所有已安裝模型，選擇後成功刪除（不要求 Ollama 服務在跑）
```

---

## 4. 本輪規格摘要

| 代號 | 功能 | 新命令/修改 | 預估成本 | 價值 |
|---|---|---|---|---|
| P1 | 聊天歷史持久化 + 匯出 | `olm history list/show/export/search/delete` + `olm chat --save` | 低 | 高 |
| P2 | 串流即時 TPS 顯示 | 改 `_do_chat_repl()`（10 行） | 低 | 高 |
| P3 | JSON 結構化輸出 | `olm chat --format json`，改 `_do_chat_repl()` | 低 | 中 |
| P4 | LAN 白名單閘道政策 | `olm gateway allow/deny/list` + 改 `gateway.py` | 中 | 高 |
| P5 | UX 一致性修補（×3） | 改 `cli.py`（三處） | 低 | 中 |

**總估算：低成本 3 項（3 天）+ 中成本 1 項（2 天）= 約 5 天開發**

---

## 5. 保留下輪候選（本輪不納入）

| 代號 | 功能 | 原因 |
|---|---|---|
| G-F | HuggingFace 搜尋/下載 | 中等成本，且 olm pull + search 已可用；HuggingFace HTML 結構更複雜 |
| G-G | repeat-penalty / min-p / seed | 低價值，使用場景少，下輪打包 |
| G-H | GPU 層數調整 | 只是透傳參數，技術瑣碎，下輪打包 |
| G-I | bench 歷史追蹤 | SQLite 容易做，但使用頻率低 |
| G-J | 本機 RAG | 需向量算法，不符純 stdlib 限制 |
| G-K | 多模型並排比較 | 終端 UX 天然限制，低性價比 |

---

## 6. 世界第一差異化盤點

| 角度 | 現況 | 本輪後 |
|---|---|---|
| MCP tool-calling in terminal chat | 已是世界首創（v0.1.16） | 維持 |
| 本機 LLM 閘道安全隔離（localhost-only） | 已完成（v0.1.16） | P4 後升級為白名單可控 LAN 開放 |
| 終端 chat session 持久化搜尋 | 缺 | P1 後補齊，超越 ollama CLI（官方無此功能） |
| 推論即時 TPS（chat 中） | 缺 | P2 後補齊，對齊 LM Studio |

---

*本文件供 CEO 審核後派 CTO 開發，實作細節（class/method 精確簽名）由 CTO 決定。*
