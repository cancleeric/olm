# olm — Ollama Model Management CLI

Python + Typer + Rich  
Gitea: hurricanesoft/olm · GitHub: cancleeric/olm

## Install

```bash
pip install -e .
```

## Usage

```bash
olm              # interactive dashboard (menu mode)
olm list         # list installed models
olm status       # show loaded models (with ctx info)
olm load [model] # warm model into RAM
olm bench        # measure tok/s
olm start        # start Ollama service (no preload)
olm stop
olm config list  # show SQLite settings
olm config set num_ctx 256K
olm config model qwen3.5:9b 128K
```

## Settings

Shares `~/.config/run-ollama/settings.db` with `run-ollama.sh`.
test
test2
test3
test4
test5
test6
