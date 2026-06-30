# olm 閘道輪 — olm 接管 11434,Ollama 縮私有埠

> 提案日：2026-06-30 | CEO 拍板 | 對應 LM Studio 對齊路線的「存取控制」基礎

## 目標(這一輪只做「插門」,不做政策)

把 olm 變成 11434 的唯一門面,Ollama 縮到只給本機的私有埠,外網碰不到。
政策(白名單/認證)留下一輪加在門上。

```
早先:  外網 ──► Ollama (0.0.0.0:11434)              ← 誰都能直接切模型
改後:  本機 ──► olm 門 (127.0.0.1:11434) ──► Ollama (127.0.0.1:11551)
       外網 ──► (打不到 127.0.0.1,被擋)             ← .32 立即斷,不需改它的設定
```

## CEO 拍板

1. **門綁 `127.0.0.1:11434`**(localhost-only)。純從 Mac 這邊鎖,外網(含 .32)即刻擋掉;別台機器 URL 不用改,照舊打 `:11434`,只是進不來。
2. **Ollama 私有埠 = `127.0.0.1:11551`**(olm 設定可改)。
3. 門這一輪**純透明轉發**,行為跟現在一致,只是中間多一道 olm 的門。
4. 日後要放行特定機器,就把政策加在門上(門可改綁 0.0.0.0 + 白名單),屆時別台機器仍打 `:11434` 不用換。

## 實作範圍(守 olm 純 stdlib 鐵律)

- **新模組 `olm/gateway.py`**：`ThreadingHTTPServer` 監聽 `127.0.0.1:11434`,把所有路徑(`/api/*`、`/v1/*`、其他)原樣轉給 `127.0.0.1:11551`。
  - 必須**串流轉發**(chat/generate/pull 是長 NDJSON,逐 chunk 寫回,不可整包緩存)
  - 支援 GET / POST / DELETE / HEAD,轉發 request body、status、回應 header
  - 長 timeout(沿用 settings.chat_timeout 量級);上游連不到回 502 友善訊息
  - 純 stdlib：`http.server` + `urllib`,零新依賴
- **`olm/api.py`**：
  - `start_server`：`OLLAMA_HOST` 從 `0.0.0.0:11434` 改 `127.0.0.1:{ollama_port}`(預設 11551)
  - olm 自己的 client `base_url` 改直接走 `127.0.0.1:{ollama_port}`(繞過自己的門,免多一跳)
  - `stop_server`：要能同時停 Ollama(11551)與門(11434);`server_pid` 對應調整
  - 新增 `start_gateway()` / `stop_gateway()` / `gateway_pid()`(門以子程序背景跑,寫獨立 pidfile)
- **`olm/db.py`**：設定加 `ollama_port`(預設 11551)、`gateway_host`(預設 127.0.0.1)、`gateway_port`(預設 11434)。漸進加,不破壞既有 schema。
- **`olm/cli.py`**：`start` 先起 Ollama(11551)再起門(11434);`stop`/`restart` 兩個都收;`status`/dashboard 顯示「門 + Ollama」兩個程序狀態與埠。

## 親測(CTO 必須真跑貼證據)

- `olm start` 後：`lsof -iTCP:11434` 應是 olm 門(綁 127.0.0.1)、`lsof -iTCP:11551` 應是 ollama 且綁 127.0.0.1
- 本機 `curl http://localhost:11434/api/tags` → 200(透過門轉發成功)
- 本機 `curl http://localhost:11434/api/chat -d '{"model":"...","messages":[...],"stream":true}'` → 串流即時回(驗證沒被緩存)
- 外網模擬：從 Mac 的區網 IP 打(`curl http://<lan-ip>:11434/api/tags`)→ 連線被拒(門綁 localhost)
- `ollama ps`(CLI 預設打 11434)→ 經門轉發仍正常
- `olm stop` → 兩個程序都收乾淨,11434/11551 都釋放

## 排程

- **依賴 R1(`olm chat`+VRAM)先合 main**(共改 api.py/cli.py/dashboard.py,避免並行 worktree git 競態)
- R1 落地 → 立刻派 CTO 做本輪
- 安全屬性：改的是監聽位址與轉發,屬存取邊界 → 合前跑 eye + 安全雙審(CISO + CPO)
