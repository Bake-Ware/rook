# Voice on the hub

The voice service (`services/voice`: speech recognition, spoken replies and
the voice agent) is served by the hub itself through the **voice** hub
plugin (`rook/hub/plugins/voice`). Phones, the desktop app and browsers use
the hub's address; nothing else needs to be exposed.

| Path on the hub | What |
| --- | --- |
| `/voice/ws` | The voice WebSocket (protocol 2), what the apps connect to |
| `/voice/api/transcribe`, `/voice/api/voice` | Speech to text, text to speech |
| `/voice/voices`, `/voice/modes`, `/voice/health` | Catalogs and health |
| `/voice/` | The bundled browser client |
| `/voice/admin` | Voice keys (its own login, HTTPS only) |
| `/api/transcribe`, `/api/voice`, `/voices` | The same, at the root, for the Android app, which builds these paths from the host of its voice URL alone (`voice.root_aliases`, on by default) |

These paths are served by `rook hub serve` (the single-process hub). They
carry their own authentication (voice keys), like `/mcp`, so the dashboard
login does not apply to them. A path the dashboard itself routes always stays
the dashboard's, and a path with a `.` or `..` segment (`/voice/../x`) is
never handed to the plugin.

In the older two-process layout (`rook-dashboard` + `rook-mcp`) nothing
serves these paths, so the plugin does not load (no models are loaded) and
Manage > Plugins and Manage > Voice say "needs the single-process hub: run it
with `rook hub serve`". Keep running the standalone server there.

## Where speech runs: `voice.engine`

**`here`** runs the voice service inside the hub process. It needs:

1. the voice extras: `pip install 'rook[voice]'` (FastAPI, numpy, webrtcvad,
   faster-whisper, kokoro-onnx, onnxruntime; about a gigabyte installed);
2. the model files in the model folder (`voice.model_dir`, default the
   plugin's data folder, `<hub data>/plugins/voice`): `kokoro-v1.0.onnx`,
   `voices-v1.0.bin`, `smart-turn-v3.2-cpu.onnx` (URLs and digests in
   `services/voice/models.json` and `services/voice/README.md`);
3. the voice service code: a source checkout has it in `services/voice`; an
   installed hub carries it as `rook/_voice_engine`.

The models load in a thread at start, so the hub keeps answering; until they
are loaded the voice paths answer 503 "starting". The voice keys and history
databases live in the model folder too.

**`remote`** forwards every voice path, WebSocket included, to a standalone
voice server on another machine (`voice.remote_url`, for example
`https://gpu.example.com:8900`). Close codes pass through unchanged (4401
for a bad key, 4409 for a conversation already connected). Keys are the voice
server's: make them on its `/admin` page (reachable through the hub at
`/voice/admin`). Turn off `voice.remote_verify_tls` only for a self-signed
certificate on a private network. The address must not carry a user name or
password (`https://user:pass@...` is refused).

What the hub forwards: the request as the client sent it (path encoding
included), addressed to the voice server by its own name (`Host` is the voice
server's, so a proxy in front of it routes as usual), with
`X-Forwarded-For`, `-Proto`, `-Host` (the hub's name) and `-Prefix`. A
browser `Origin` equal to the hub's own `https://` origin is presented as the
voice server's origin, so voice admin sign-in works through the hub; any
other origin goes as sent and the voice server refuses it. The dashboard's
session cookie and Cloudflare Access headers (`Cf-Access-*`) never leave the
hub: the only cookie forwarded is the voice admin's (`voice_admin`), and the
only cookie passed back is that one, with its path moved under
`/voice/admin` and no domain. One client address may hold at most 16 voice
WebSockets through the hub (429 beyond that); behind a local tunnel the
address is the tunnel's `X-Forwarded-For`, and the hub passes that on without
adding its own loopback hop. The voice admin counts failed sign-ins per client
address: for that address to be the client's rather than the hub's, start the
voice server with `FORWARDED_ALLOW_IPS=<the hub's address>` (uvicorn trusts
only loopback by default).

Without what the chosen engine needs the plugin does not load, and
Manage > Plugins shows the reason ("Needs the voice extras ...", "Speech model
files are missing from ...", "its address is not set"). A small hub that
forwards to a GPU box never imports any of the speech stack.

## Settings

All `voice.*` settings are on Settings > Voice, with the same keys and
environment variables as before (`VOICE_TOKEN`, `WHISPER_MODEL`, `VOICE`,
`VLLM_URL`, `ROOK_MCP_URL`, ...). The hub's own: `voice.engine`
(`ROOK_VOICE_ENGINE`), `voice.remote_url` (`ROOK_VOICE_REMOTE_URL`),
`voice.remote_verify_tls`, `voice.model_dir` (`VOICE_MODEL_DIR`),
`voice.root_aliases`. With engine `here`, the values an operator stored (and
secrets set in the vault) are handed to the voice service as its environment
variables; defaults stay the service's own, and a variable already set in the
hub's environment always wins. A value the hub's environment gives under
another name (the automatic `ROOK_VOICE_<NAME>`, e.g. `ROOK_VOICE_HASS_URL`)
is handed on under the variable the service reads (`VOICE_HASS_URL`), unless
that variable is set itself. Changing one restarts the plugin (and reloads
the models; a load still in progress is waited for first, so two never run at
once). If that load is still running after three minutes, the new engine
waits for it to finish and be cleared away before it starts, and the Voice
page says so, rather than start on the old settings.

The settings a GPU voice server is usually configured with are there too,
each under the variable the voice service reads (defaults are the service's
own; see `services/voice/README.md`):

| Group | Keys (variable) |
| --- | --- |
| Speech | `voice.tts_default` (`VOICE_TTS_DEFAULT`: the starting voice from either engine, `kokoro:`/`chatterbox:` names accepted); advanced `voice.tts_target_dbfs` (`VOICE_TTS_TARGET_DBFS`, -20), `voice.tts_threads` (`VOICE_TTS_THREADS`, 8) |
| Chatterbox | `voice.chatterbox_device` (`VOICE_CHATTERBOX_DEVICE`, e.g. `cuda:0`; blank keeps Chatterbox off), `voice.chatterbox_python` (`VOICE_CHATTERBOX_PYTHON`, a venv python with torch and chatterbox-tts), `voice.chatterbox_voices_dir` (`VOICE_CHATTERBOX_VOICES_DIR`); advanced `voice.chatterbox_start_timeout_s` (`VOICE_CHATTERBOX_START_TIMEOUT_S`, 180) |
| Agent (advanced) | `voice.tool_reasoning_effort` (`VOICE_TOOL_REASONING_EFFORT`, high), `voice.tool_max_tokens` (`VOICE_TOOL_MAX_TOKENS`, 4096), `voice.tool_max_steps` (`VOICE_TOOL_MAX_STEPS`, 24), `voice.tool_model_timeout_s` (`VOICE_TOOL_MODEL_TIMEOUT_S`, 180) |
| Models | `voice.front_url`/`voice.front_model` (`VOICE_FRONT_URL`/`VOICE_FRONT_MODEL`), `voice.background_url`/`voice.background_model` (`VOICE_BACKGROUND_URL`/`VOICE_BACKGROUND_MODEL`), blank meaning `VLLM_URL`/`VLLM_MODEL`; `voice.primary_url`, `voice.primary_model`, `voice.primary_api_key` (secret) (`LLM_PRIMARY_URL`, `LLM_PRIMARY_MODEL`, `LLM_PRIMARY_API_KEY`: a hosted model tried first, falling back to `VLLM_*`); advanced: `voice.front_timeout_s`, `voice.front_max_tokens`, `voice.front_turns`, `voice.background_effort`, `voice.background_max_steps`, `voice.background_timeout_s`, `voice.primary_timeout_s` (`VOICE_FRONT_TIMEOUT_S`, `VOICE_FRONT_MAX_TOKENS`, `VOICE_FRONT_TURNS`, `VOICE_BACKGROUND_EFFORT`, `VOICE_BACKGROUND_MAX_STEPS`, `VOICE_BACKGROUND_TIMEOUT_S`, `LLM_PRIMARY_TIMEOUT_S`) |
| Home | `voice.hass_url`, `voice.hass_token` (secret), `voice.hass_verify_tls` (`VOICE_HASS_URL`, `VOICE_HASS_TOKEN`, `VOICE_HASS_VERIFY_TLS`), `voice.pianobar_worker` (`VOICE_PIANOBAR_WORKER`) |
| Location | `voice.home_lat`, `voice.home_lon` (`VOICE_HOME_LAT`, `VOICE_HOME_LON`), `voice.timezone` (`VOICE_TZ`), `voice.weather_units` (`VOICE_WEATHER_UNITS`, celsius); advanced `voice.weather_url` (`VOICE_WEATHER_URL`) |

Secrets (`voice.token`, `voice.mcp_token`, `voice.hass_token`,
`voice.primary_api_key`) go to the vault; the settings database and history
keep only a reference and a fingerprint. Not settings (set them in the hub's
environment if you need them): `LLM_PRIMARY_EXTRA`, `VOICE_FOLLOWUP_WAIT_S`,
`VOICE_PREFETCH_*`, `DECISION_*`, `ONNX_PROVIDER`, the database and TLS paths.

All of this applies to engine `here` only. In `remote` mode none of these
reach the voice server (the hub only presents `voice.token` for its own
checks): that server keeps its own environment, so configure Chatterbox, Home
Assistant and the models there.

`voice.token` is an owner voice key. In `remote` mode the hub also uses it for
the Voice page's microphone check, so the voice server must accept it.

## The Voice page

Manage > Voice shows whether voice is ready, where speech runs, the address to
give the apps (`wss://<hub>/voice/ws`), a choice between "On this hub" and "On
another machine", and a microphone check: it records a few seconds in the
browser (16 kHz mono) and shows what the hub transcribed. The recording goes
to the hub once and is not kept. The browser needs HTTPS (or localhost) for
the microphone.

## Moving an existing voice server

Nothing has to move at once: the standalone server (`python -m
services.voice.server`, the GPU host's `release.sh` / `voice-select` flow in
`services/voice/deploy`) runs exactly as before.

To put it behind the hub's address:

1. On the hub: Manage > Voice > "On another machine", the voice server's
   address, Save (or `ROOK_VOICE_ENGINE=remote` and
   `ROOK_VOICE_REMOTE_URL=...`). Use Check connection.
2. In the Rook app: set the voice server to `wss://<hub>/voice/ws`. Keys are
   unchanged (they belong to the voice server).
3. Once nothing uses the voice server's own public address, stop exposing it
   (for example, remove its tunnel route).

To run speech on the hub instead (a home server with spare CPU or a GPU):
install the extras, copy the model files into the model folder, copy the
voice server's `voice-admin.sqlite3` and `voice-state.sqlite3` there too to
keep keys and history, then choose "On this hub".
