# QueryCraft — llama.cpp edition

Local multilingual voice assistant for English, Japanese, and Korean.

```text
Microphone -> faster-whisper -> llama.cpp -> MCP tools -> TTS -> sounddevice
```

This version uses llama.cpp's OpenAI-compatible `/v1/chat/completions` API instead of Ollama.

## Requirements

Before running QueryCraft, install:

- Python 3.11 recommended.
- A recent llama.cpp build containing `llama-server`.
- A Qwen3.5 GGUF model.
- Git, if required by pip for Git-based dependencies.
- Working microphone and audio output devices.

Python 3.9-3.11 is supported by the current MeloTTS dependency set. Python 3.12 is not recommended while MeloTTS remains enabled.

## Quick start

### 1. Configure QueryCraft

Copy the example files:

Windows:

```bat
copy .env.example .env
copy mcp_config.json.example mcp_config.json
```

Linux/macOS:

```bash
cp .env.example .env
cp mcp_config.json.example mcp_config.json
```

Edit `.env` as needed.

Default llama.cpp connection:

```env
LLAMA_CPP_URL=http://127.0.0.1:8080/v1/chat/completions
LLAMA_CPP_MODEL=querycraft
```

The `querycraft` model name must match `--alias querycraft` when starting `llama-server`.

### 2. Start llama-server

Run llama.cpp in a separate terminal before starting QueryCraft.

Example using a local GGUF:

Windows:

```bat
llama-server.exe ^
  -m C:\path\to\Qwen3.5-4B-Q4_K_M.gguf ^
  --alias querycraft ^
  --host 127.0.0.1 ^
  --port 8080 ^
  --jinja ^
  --reasoning off ^
  --no-webui ^
  --parallel 1 ^
  -c 8192 ^
  -ngl auto ^
  -fa auto
```

Linux/macOS:

```bash
llama-server \
  -m /path/to/Qwen3.5-4B-Q4_K_M.gguf \
  --alias querycraft \
  --host 127.0.0.1 \
  --port 8080 \
  --jinja \
  --reasoning off \
  --no-webui \
  --parallel 1 \
  -c 8192 \
  -ngl auto \
  -fa auto
```

Use a recent llama.cpp build. Tool calling requires Jinja chat-template support.

`llama-server` should remain running while QueryCraft is active.

### 3. Run QueryCraft

The launcher scripts handle Python setup automatically.

Windows:

```bat
run.bat
```

Linux/macOS:

```bash
chmod +x run.sh
./run.sh
```

On first run, the launcher:

1. Creates `.venv`.
2. Prefers Python 3.11.
3. Upgrades `pip`, `setuptools`, and `wheel`.
4. Installs `requirements.txt`.
5. Installs `requirements_mcp.txt`.
6. Installs `requirements_korean.txt`.
7. Downloads the UniDic dictionary.
8. Creates `.venv/.querycraft_setup_complete`.
9. Starts `assistant.py`.

Later runs skip dependency installation and start the assistant directly.

If first-run setup fails, fix the reported error and run the launcher again. The setup marker is created only after every setup step succeeds.

## Reset the Python environment

Delete `.venv` to force a complete reinstall.

Windows PowerShell:

```powershell
Remove-Item -Recurse -Force .venv
.\run.bat
```

Windows Command Prompt:

```bat
rmdir /s /q .venv
run.bat
```

Linux/macOS:

```bash
rm -rf .venv
./run.sh
```

To reinstall dependencies without deleting the environment, remove only the setup marker:

Windows PowerShell:

```powershell
Remove-Item .venv\.querycraft_setup_complete
.\run.bat
```

Linux/macOS:

```bash
rm .venv/.querycraft_setup_complete
./run.sh
```

## Manual run

The launchers are recommended, but an existing environment can run QueryCraft directly:

Windows:

```bat
.venv\Scripts\python.exe assistant.py
```

Linux/macOS:

```bash
.venv/bin/python assistant.py
```

Text-only testing:

```bash
python assistant.py --no-tts
```

Override the llama.cpp model alias:

```bash
python assistant.py --model querycraft
```

## FreshRSS

FreshRSS is accessed through the RSS MCP server.

Configure `.env` with either the FreshRSS base URL:

```env
FRESHRSS_URL=http://192.168.0.3:8380
FRESHRSS_USER=admin
FRESHRSS_API_PASSWORD=YOUR_SPECIAL_API_PASSWORD
```

or the exact Google Reader API endpoint:

```env
FRESHRSS_API_URL=http://192.168.0.3:8380/api/greader.php
FRESHRSS_USER=admin
FRESHRSS_API_PASSWORD=YOUR_SPECIAL_API_PASSWORD
```

`FRESHRSS_API_PASSWORD` is the separate API password configured in the FreshRSS user profile.

Do not set the API URL as the password.

The old `FRESHRSS_API_KEY` variable remains accepted for compatibility, but `FRESHRSS_API_PASSWORD` is preferred.

Ensure RSS is enabled in `mcp_config.json`.

At startup, verify that RSS tools appear:

```text
[mcp] Tools: ... check_freshrss_connection, get_unread_articles, get_articles_from_feed, ...
```

Test FreshRSS independently with:

```bash
python diagnose_freshrss.py
```

A successful check should show HTTP `200` for login and unread requests.

The assistant prefers FreshRSS for requests about:

- unread items;
- RSS feeds;
- subscribed news;
- headlines from feeds.

Explicit requests to search the web still use `web_search`.

## TTS

QueryCraft detects the language of the generated reply before selecting TTS.

For example:

```text
[you(en)] Say something in Japanese.
[assistant] こんにちは。何かお手伝いできますか？
[tts] Reply language: ja
```

### English and Japanese

Kokoro handles English and Japanese.

### Korean

The current Korean backend uses MeloTTS when configured.

```env
KOREAN_TTS_BACKEND=melo
```

`run.bat` and `run.sh` install `requirements_korean.txt` and UniDic automatically on first run.

On Windows, a local Korean Windows speech voice can also be used:

```env
KOREAN_TTS_BACKEND=windows
```

If MeloTTS dependency installation fails under Python 3.12, recreate `.venv` using Python 3.11.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `LLAMA_CPP_URL` | `http://127.0.0.1:8080/v1/chat/completions` | llama.cpp chat endpoint |
| `LLAMA_CPP_MODEL` | `querycraft` | llama.cpp model alias |
| `LLAMA_CPP_MAX_TOKENS` | `256` | Maximum response tokens |
| `LLAMA_CPP_TEMPERATURE` | `0.7` | Sampling temperature |
| `LLAMA_CPP_TOP_P` | `0.8` | Nucleus sampling |
| `LLAMA_CPP_TOP_K` | `20` | Top-k sampling |
| `LLM_HISTORY_TURNS` | `12` | Conversation turns retained |
| `WHISPER_BEAM_SIZE` | `1` | STT beam size |
| `WHISPER_MODEL` | `base` | Whisper model |
| `WHISPER_DEVICE` | `cpu` | Whisper device |
| `WHISPER_COMPUTE` | `int8` | Whisper compute type |
| `KOREAN_TTS_BACKEND` | `auto` | Korean TTS backend |
| `FRESHRSS_URL` | — | FreshRSS base URL |
| `FRESHRSS_API_URL` | — | Optional exact Google Reader API URL |
| `FRESHRSS_USER` | — | FreshRSS username |
| `FRESHRSS_API_PASSWORD` | — | FreshRSS special API password |

## MCP agents

`mcp_config.json` controls MCP subprocesses.

Included agents include:

- document tools;
- FreshRSS tools;
- web search;
- weather;
- other configured MCP agents.

MCP uses stdio subprocesses. It does not require a separate MCP HTTP server.

## llama.cpp tuning

Recommended starting command options:

```text
--jinja
--reasoning off
--no-webui
--parallel 1
-c 8192
-ngl auto
-fa auto
```

For this voice assistant:

- Keep reasoning disabled for normal conversations.
- Keep only one inference slot unless multiple clients need the model.
- Keep context close to actual needs.
- Use GPU offload when available.
- Enable Flash Attention when supported.
- Keep output limits small.
- Let llama-server remain resident so model weights and prompt cache survive between turns.

`llama-server` is preferred over `llama-cli` because QueryCraft uses the OpenAI-compatible HTTP API and structured tool calling.

## Troubleshooting

### QueryCraft cannot connect to the model

Confirm `llama-server` is running:

```text
http://127.0.0.1:8080
```

Confirm `.env` contains:

```env
LLAMA_CPP_URL=http://127.0.0.1:8080/v1/chat/completions
LLAMA_CPP_MODEL=querycraft
```

Confirm the server was started with:

```text
--alias querycraft
```

### RSS tools are missing

Check startup output for:

```text
[mcp] Tools:
```

If FreshRSS tools are missing:

1. Check `mcp_config.json`.
2. Confirm the RSS server has `"enabled": true`.
3. Check `.env`.
4. Run `python diagnose_freshrss.py`.

### Dependencies reinstall every run

The successful first-run marker is:

```text
.venv/.querycraft_setup_complete
```

If it is never created, one of the installation or UniDic steps is failing.

### Need to reinstall after editing requirements

Delete:

```text
.venv/.querycraft_setup_complete
```

Then run `run.bat` or `run.sh` again.

### `fugashi`, `tokenizers`, or MeloTTS fails to build

Use Python 3.11.

The current MeloTTS dependency tree is not a good fit for Python 3.12 on Windows.

## Main changes from the Ollama version

- Replaced Ollama `/api/chat` with llama.cpp `/v1/chat/completions`.
- Uses OpenAI-compatible tool calls and `tool_call_id`.
- Uses Qwen3.5-friendly non-reasoning configuration.
- Uses persistent HTTP sessions.
- Improves RSS intent routing.
- Loads `.env` relative to the project directory.
- Makes `.env` override MCP placeholder configuration.
- Fixes MCP startup and shutdown handling.
- Detects TTS language from the generated reply.
- Supports Japanese and Korean reply routing.
- Uses lower-latency Whisper defaults.
- Reduces default conversation history.
- Adds first-run launcher scripts.

## License

GPL-3.0, matching the upstream QueryCraft repository.
