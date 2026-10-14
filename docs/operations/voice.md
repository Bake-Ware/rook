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
login does not apply to them. In the older two-process layout
(`rook-dashboard` + `rook-mcp`) keep running the standalone server.

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
certificate on a private network.

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
hub's environment always wins. Changing one restarts the plugin (and reloads
the models).

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
