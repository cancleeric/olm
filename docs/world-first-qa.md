# olm — 世界第一缺口自問自答
> 每輪更新（2026-06-29 第五輪，v0.1.15）

## 已完成

- ✅ P1: `olm run` / Dashboard "5" 傳 OLLAMA_NUM_CTX env（v0.1.11）
- ✅ P2: OllamaClient.model_max_ctx() instance-level cache（v0.1.11）
- ✅ 儀表板標題顯示版號（v0.1.12）
- ✅ P3: `olm run` 無參數互動 _pick() 選模型（v0.1.13）
- ✅ P4: `olm pull` Rich Progress bar 進度條（v0.1.13）
- ✅ P_new: `olm delete` 指令 + Dashboard "d" 鍵（v0.1.15）
- ✅ P5: load 前 RAM headroom 警告（v0.1.15）
- ✅ `_sysmem()` 統一搬到 api.py 單一來源（v0.1.15）
- 15 CLI 指令、Rich 儀表板、bench、SQLite 設定、disk_models fallback、ctx 顯示

## 缺口分析（評分 1=差距最大）

### P6【3/10】Dashboard action "6" pull 無 Rich 進度條
- `dashboard.py:349-357`：action "6" 仍用 `input(...)` 收模型名 + `subprocess.run(["ollama", "pull", model])`
- `cli.py cmd_pull()` 已有 `pull_stream()` + Rich Progress bar（速度/剩餘時間）
- 兩者行為不一致：CLI 有進度條，Dashboard 黑盒
- 修正：dashboard action "6" 改用 `pull_stream()` + Rich Progress（參考 cli.py 實作）

### P7【4/10】`olm bench` 無參數不出選模型選單
- `cli.py:328-333`：`m = model or settings.default_model` — 無互動選單
- 對比：`olm run`、`olm load`、`olm unload` 全部有 `_pick()` 選單
- 用戶不記得 default_model 設什麼，`olm bench` 就默默跑錯模型
- 修正：`cmd_bench()` 無 model 參數時，從 `list_models()` 呼叫 `_pick()`

### P8【6/10】`config set` keep_alive/timeout 無格式驗證
- `cli.py:337-348`：只驗 `num_ctx`，其他 key 直接寫 SQLite
- `config set keep_alive abc` 靜默寫入無效值
- dashboard Settings 菜單有 `.isdigit()` 驗 timeout，但 CLI 沒有
- 修正：`keep_alive` 驗 `\d+[smhd]|-1`；`request_timeout`/`chat_timeout` 驗正整數

## 本輪聚焦（第五輪）

1. **P6 Dashboard action "6" 加 Rich 進度條** — 一致性，CLI 已有實作直接移植
2. **P7 `olm bench` 加互動選模型** — 與其他指令對齊

## 下輪候選

- P8: `config set` 格式驗證
- P9: `olm switch` 加互動選單（目前需兩個位置參數，無 picker）
- P10: `olm list --json` 輸出
- P11: `olm delete` 支援服務停止時刪除（目前 require_running，但 `ollama rm` 不需服務）
