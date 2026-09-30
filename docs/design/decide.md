# decide: one-pass decision models and fast remote control

Status: beta. The hub plugin `decide` (`rook/hub/plugins/decide/`) is
implemented and tested against a fake decision server and fake
screenshot/hid caps (`tests/test_decide_plugin.py`). It has not yet driven a
real screen.

## 1. What it is for

A one-pass decision model answers a whole batch of typed questions about one
state in a **single forward pass**. There is no text generation. Examples are
a masked-diffusion model ("diffucision": LLaDA-style, one `[MASK]` per answer
slot, logits restricted to the option letters) and the typed decision engine.
A batch of 2 questions returns in tens of milliseconds, and 60 questions cost
barely more than 2. That makes the model a good *motor cortex*: an LLM plans
("export last month's invoices"), and the decision model makes the many small
per-frame choices (which element, click or type, is the page ready, is this
irreversible) at control-loop speed, handing back when it is done, stuck or
unsure.

The plugin gives Rook two things:

1. `decide.run`: the model as a band capability. Any agent or plugin can ask a
   batch of questions in one call.
2. `decide.drive`: a control loop that drives a screen. Each frame is a
   screenshot, then one decision pass, then `hid.*` input, repeated behind
   safety gates.

## 2. Placement: on the hub, orchestrating caps across workers

`decide` is a hub plugin (`place("is_hub", run="one")`). It is not a worker
plugin next to `screenshot.*` and `hid.*`. The reasons:

- **Screen and input are often on different machines.** A remote PC may be
  seen by one worker (a capture device, a KVM or its own `screenshot.*`) and
  driven by another (a USB HID dongle bridge, or its own `hid.*`). The target
  may not run Rook at all. Only the hub can combine caps from several workers
  into one loop (`screen_worker` and `input_worker` are separate arguments).
- **One place for safety state.** The kill switch (setting `halt`,
  `decide.stop`), pending confirmations, the journal and the per-worker
  "one live drive" lock all live in one process. Every agent sees the same
  runs with `decide.runs`, and an operator can stop them from the Settings
  page.
- **One place for credentials and settings.** The model endpoint and its
  token live in the hub settings store and the vault, not on every worker.
- **No fleet change.** Workers keep their existing caps, so build-167 workers
  are drivable as they are and nothing new ships in the worker bundle.
- **The latency cost is small.** A frame is about three band round trips
  (screenshot, optional `ui.text`, input) plus one model call. A band hop is
  tens of milliseconds and a model pass is 30–150 ms, so a step is
  sub-second. That is dominated by the screen settling after input
  (`settle_ms`), not by routing. If a local loop ever matters, the same
  `DriveRun` can be hosted by a worker plugin, because it only sees the band
  through injected callables.

## 3. Capabilities (worker `rook`)

| Cap | Risk | Does |
|---|---|---|
| `decide.run(state, questions, temperature?)` | read | One batch of typed questions, answered in one pass (split into several when larger than the model's limit). No side effects. |
| `decide.health()` | read | Is the configured model reachable and loaded? Reports `configured: false` when no endpoint is set. |
| `decide.info()` | read | Model, adapter, calibration status, limits. |
| `decide.drive(goal, screen_worker, input_worker?, dry_run?, max_steps?, max_seconds?, texts?, keys?, screen_size?, wait?)` | exec, `physical` | Starts a drive and returns `run_id` at once, or after `wait` seconds. |
| `decide.runs(run_id?, steps?)` | read | Recent drives, or one drive with its journaled frames and any pending confirmation. |
| `decide.confirm(run_id, approve, note?)` | exec, `physical` | Approve or refuse the action a drive is waiting on. A refusal stops the drive. |
| `decide.stop(run_id?)` | write | Kill switch for one drive or all of them. |

`decide.run` is `read`, so it is reachable within the hub's band risk
ceiling. The drive caps are `exec` and need the MCP bridge (or a raised
`ROOK_HUB_BAND_MAX_RISK`). The hub calls `screenshot.*`, `ui.*` and `hid.*`
on workers from the drive task. That task inherits the context of the
`decide.drive` call, so the permissions layer authorizes those calls as the
principal that **started** the drive (the envelope identity reads
`system:rook-hub`). The journal records the starter's identity, and who
answered each confirmation.

### 3.1 Questions and answers

```json
{"state": {"utterance": "turn off the lights"},
 "questions": [
   {"id": "intent", "type": "choice", "question": "What does the user want?",
    "options": ["device_control", "question", "chit_chat"]},
   {"id": "urgent", "type": "noul", "question": "Is it urgent?"},
   {"id": "effort", "type": "score", "question": "How much effort?",
    "levels": ["none", "some", "lots"]}]}
```

- `type` is `choice`, `score` or `noul` (yes/no; `yes-no`, `yesno` and
  `bool` are accepted). The text may be given as `question`, `instructions`
  or `text`. `options` / `levels` is a list or a `{label: description}` map.
- Limits are checked before sending: 26 options per question for
  diffucision, 52 for the typed engine. A batch over the question limit (64 or
  24) is split into several passes, and `passes` in the reply says how many.

Answers come back in one shape whichever service answered:

| Type | Fields |
|---|---|
| choice | `id, type, choice, probabilities {label: p}, confidence` |
| noul | `id, type, p` (P(yes)), `probabilities {true, false}, confidence` |
| score | `id, type, level, score` (expected value), `probabilities, confidence` |

`confidence` is the model's top-two margin. **Probabilities are currently
uncalibrated** (temperature 1.0, no calibration fit). Treat them as a ranking,
not a promise. Everything that gates on them defaults towards asking a human.

## 4. Pointing it at a model

Open **Settings → Decide (decision model)** in the dashboard (or use
`settings.set` on worker `rook`, or the `ROOK_DECIDE_*` environment variables
of the MCP process):

| Setting | For diffucision | For the typed decision engine |
|---|---|---|
| `decide.endpoint` | `http://gpu-box:8911` | `http://gpu-box:8910`, or `cap://gpu-box/cmd.decide-run` |
| `decide.adapter` | `diffucision` | `decision-engine` |
| `decide.token` (vault) | only if the service sets `DECISION_TOKEN` | same |

Endpoint forms:

- `http(s)://host:port`. `/decide`, `/healthz` and `/info` are appended, and
  a trailing `/decide` is tolerated. A query string is kept on `/decide` and
  `/info`. A serve-and-train diffucision can pick a named adapter that way
  (`http://gpu-box:8911/decide?adapter=web`), so a computer-use adapter can be
  served next to the Minecraft one without touching it.
- `cap://<worker>/<cap>` reaches the model through a band cap when the hub
  cannot reach the service directly. A worker custom cap under `cmd.` that
  wraps a CLI (`customcap.add name=decide-run command="decide-cli run
  {payload}"`) receives `{"payload": "<request JSON>"}`, and its stdout is
  parsed. Any other cap receives the request as its arguments and returns the
  response. `decide.health` and `decide.info` call the sibling caps
  (`cmd.decide-health`, `cmd.decide-info`).

Then check it: `rook_call(cap="decide.health", worker="rook")` and
`rook_call(cap="decide.info", worker="rook")`. `decide.health` makes one
light GET request. Neither sends decisions, so neither adds load to a service
that is busy with an experiment.

## 5. The drive loop

```
 screen_worker                     hub (decide)                          input_worker
 screenshot.capture_preview ──▶ frame (JPEG, size from SOF)
 ui.text (Android) ───────────▶ observation ◀── perceiver (optional OCR/UI parser)
                                 questions ──▶ decision model (one pass)
                                 interpret + gates ──▶ decide.confirm? (human/agent)
                                 hid.mouse.click / hid.type / hid.key_combo / hid.mouse.drag ──▶
                                 journal (drive.db), settle, repeat
```

### 5.1 Perception

The decision models are text-only, so a frame has to become text first. In
order:

1. **`decide.perceiver`**, an optional `http(s)://` or `cap://` service. It is
   sent `{image: <base64 JPEG>, format, width, height, goal}` and returns
   `{text, elements: [{label, x, y, w, h}]}` in the image's pixel space. An
   OCR, accessibility-tree or small vision model can fill this slot.
2. **Android `ui.text`**, when the screen worker has it (the accessibility
   service's visible text).
3. **Nothing.** The run is *blind*: the model still picks grid cells, but
   every action needs confirmation (gate `blind`). Blind mode is only useful
   for supervised exploration.

When the perceiver lists elements, the target question chooses among them (up
to the model's option limit). Otherwise it is two questions, a grid row and a
grid column (`decide.grid`, default 8x8), and the action goes to the cell's
centre.

### 5.2 One pass per frame

The state is JSON: the goal, step and budget, the screen size and grid, the
observation source, the screen text (4000 characters at most), the element
labels, and the last five actions with their results. The questions are:

| id | type | Asks |
|---|---|---|
| `action` | choice | click, double_click, right_click (desktop), type (if `texts`), key (if `keys`), scroll_down, scroll_up, wait |
| `target` or `target_row` + `target_col` | choice | where to act |
| `text` | choice | which caller-supplied text to type (the model cannot write text) |
| `key` | choice | which allowed key to press |
| `done` | noul | is the goal already achieved? |
| `ready` | noul | is the screen settled? |
| `needs_confirmation` | noul | is the next action irreversible or risky? |
| `abort` | noul | is something wrong (error, unexpected dialog, logged out)? |

### 5.3 Decision and gates

- `abort` ≥ `abort_p` stops the run (`aborted`). `done` ≥ `done_p` finishes it.
  `ready` below 0.5 waits one settle period, and that counts as a step.
- Otherwise the action runs, unless a gate fires. A gated action waits in
  state `awaiting_confirmation` until `decide.confirm` (by any MCP caller)
  or `confirm_timeout_s` (which stops the run). The gates:
  - `policy`: `decide.confirm = always`.
  - `blind`: there was no observation.
  - `low_confidence`: the smallest margin among the answers the action used
    (action, target, text, key) is below `min_confidence`. The default is 0.9,
    because probabilities are uncalibrated, so most live actions ask until the
    model is calibrated and the floor is lowered.
  - `model_flagged`: P(`needs_confirmation`) ≥ `confirm_p` (default 0.2).
  - `destructive`: the target label, the text or the key contains one of
    `destructive_words` (send, delete, pay, submit, …), or the key is a
    destructive combo (Delete, Alt+F4, Ctrl+W, …).
- A refused confirmation stops the run and hands back. It does not skip the
  action and carry on.

### 5.4 Safety

- **Dry-run is the default** (`decide.dry_run`, on). A dry run takes
  screenshots and asks the model, but never sends input. It journals the
  decision, the gates that would have fired and the exact `hid.*` calls it
  would have made, and it stops after one frame unless `max_steps` is given.
  Pass `dry_run=false` to drive for real.
- **Kill switch.** `decide.stop()` (all drives) or `decide.stop(run_id)`, or
  the `decide.halt` setting, which is read live on every step and while
  waiting for a confirmation. With `halt` on, new drives are refused and
  approvals are rejected.
- **Budgets.** `max_steps` (default 20) and `max_seconds` (default 120) per
  drive. Waits and dry-run frames count as steps.
- **One live input drive per input worker.** Dry runs are exempt.
- **Failure stops.** A failed screenshot, model call or input call ends the
  run (`failed`) with the error recorded.
- **Journal.** Every frame is a row in `<hub state>/plugins/decide/drive.db`:
  the frame summary (size, bytes, hash, never the pixels), the observation
  source, every answer with its probabilities, the decision and its gates,
  the confirmation (approved or not, by whom, note), the executed calls and
  their results, and timings. Runs left open by a restart are marked
  `stopped` / `hub restarted`. `decide.runs` reads it.

### 5.5 Input backends

The input worker's `hid.backend` is read once per drive.

| Action | Desktop (`xdotool`, `ydotool`, `wtype`, `win32`) | Android accessibility |
|---|---|---|
| click / double / right | `hid.mouse.click(button, x, y)` | `hid.mouse.click(x, y)` (tap); no double/right |
| type | `hid.type(text)` | `hid.type(text)` |
| key | `hid.key_combo(key, modifiers)`, key names mapped to X keysyms or Windows names | `hid.key_combo(keys)`: back, home, recents (escape maps to back) |
| scroll | Page Down / Page Up (hid has no wheel) | `hid.mouse.drag` swipe |

Coordinates are in screen pixels. Screenshots can be downscaled on the way
(large captures are shrunk to keep band payloads small). When they are, set
`decide.screen_size` (or pass `screen_size`) so perceiver boxes and grid cells
are scaled to the real screen.

## 6. Settings

All settings are hub scope, applied live, and declared in the plugin, so they
appear on the unified Settings page and in
[settings-reference.md](../operations/settings-reference.md).

| Group | Keys |
|---|---|
| Model | `endpoint` (resource), `adapter`, `token` (secret), `timeout_s`, `temperature` (advanced) |
| Drive | `dry_run` (on), `max_steps`, `max_seconds`, `settle_ms`, `screenshot_cap`, `perceiver` (resource), `grid`, `screen_size` (advanced) |
| Safety | `halt`, `confirm` (gated/always), `min_confidence`, `confirm_p`, `done_p`, `abort_p`, `confirm_timeout_s`, `destructive_words` |

## 7. Limits and next steps

- **Perception is the bottleneck.** Without a perceiver, a desktop drive is
  blind. The next piece is a perceiver service (OCR plus element boxes, or the
  browser DOM or accessibility tree), or a vision-capable decision adapter
  that accepts the frame directly.
- **More than 26 elements.** Only the first N elements are offered. A two-pass
  shortlist (pass 1 narrows, pass 2 picks) would lift that.
- **Surprise detection.** Predicting the expected screen change and escalating
  when the actual change differs would catch misclicks and popups cheaply. It
  would add one noul/choice question per frame and a frame diff.
- **More input backends.** A PiKVM or USB HID dongle bridge as the input side
  (the `input_calls` table is the one place to add them).
- **Calibration.** Once the model is calibrated on logged drive frames (the
  journal is the dataset), lower `min_confidence` and `confirm_p` so that
  confident, non-destructive steps run unattended.
