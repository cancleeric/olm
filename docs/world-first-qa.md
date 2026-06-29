# olm — 世界第一缺口自問自答
> 每輪更新（2026-06-29 第三輪，v0.1.13）

## 已完成

- ✅ P1: `olm run` / Dashboard "5" 傳 OLLAMA_NUM_CTX env（v0.1.11）
- ✅ P2: OllamaClient.model_max_ctx() instance-level cache（v0.1.11）
- ✅ 儀表板標題顯示版號（v0.1.12）
- ✅ P3: `olm run` 無參數互動 _pick() 選模型（v0.1.13）
- ✅ P4: `olm pull` Rich Progress bar 進度條（v0.1.13）
- 14 CLI 指令、Rich 儀表板、bench、SQLite 設定、disk_models fallback、ctx 顯示

## 缺口分析（評分 1=差距最大）

### P5【5/10】Load 前無 RAM headroom 警告
- Dashboard action "4" / `cli.py cmd_load()`：直接呼叫 `client.load()`，沒有 RAM 檢查
- 若模型 size > free RAM，Ollama 會 swap，幾分鐘無回應用戶不知道原因
- `_sysmem()` 已有 free_b；`list_models()` 有 model size
- 修正：load 前對比 model_size vs free_b，若 model_size > 0.8 × free_b 顯示 ⚠️ 警告（仍讓用戶選擇繼續）

### P6【6/10】`config set keep_alive/timeout` 無格式驗證
- `cli.py:304-316`：`config set keep_alive "abc"` 無驗證直接寫 SQLite
- 修正：keep_alive 驗證 `\d+[smhd]|-1`；timeout 驗證正整數

### P7【6/10】shell completion 未文件化
- olm 靠 Typer 支援 `--install-completion`，但 README 沒提
- 修正：README 加一節說明 + `olm --install-completion zsh`

### P8【7/10】`olm list` 無 --json 輸出
- 無法 `olm list --json | jq '.[] | .name'`
- 修正：加 `--json` flag，輸出 raw JSON

## 下輪候選

1. **P5 Load 前 RAM headroom 警告** — 最高優先，防 swap 黑盒
2. **P6 config set 格式驗證** — 防誤寫
3. P7 shell completion 文件
4. P8 `olm list --json`
