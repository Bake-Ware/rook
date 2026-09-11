# Voice quality upgrade

Research and implementation audit: 2026-09-10. Voice quality is the first
priority; Android's default-assistant integration follows it. This is a proposed
design. Implementation and validation status are recorded in
[the voice service README](../services/voice/README.md) and the deployment record.
Acoustic performance targets below are not measured results.

## Recommendation

Keep the current model services and Rook/Hermes capabilities initially. Replace
the ad hoc conversation orchestration with a tested pipeline that separates
audio capture, wake detection, turn detection, playback, conversation state and
background work. Evaluate Pipecat first for this deployment; LiveKit Agents is
the other credible candidate. Neither framework removes the need to test the
Android audio path.

The primary sources converge on these mechanisms, not on a universally best
framework:

* Detect speech separately from the wake phrase. openWakeWord explicitly supports
  a Silero VAD gate to reduce activation on nonspeech noise. Threshold tuning and
  an optional speaker verifier are additional controls, with false-rejection
  tradeoffs. A speech gate alone cannot reject a TV saying the actual wake phrase.
  [openWakeWord](https://github.com/dscripka/openWakeWord/blob/main/README.md)
* Separate speech start/stop from deciding whether a person has finished their
  thought. Pipecat combines local Silero VAD with Smart Turn and advises testing
  actual audio conditions instead of blindly raising confidence thresholds.
  [Pipecat speech input](https://docs.pipecat.ai/pipecat/learn/speech-input)
* Treat interruptions as recoverable events. LiveKit documents adaptive
  interruptions and resuming after a false interruption. Its timing defaults
  are starting points, not measurements of Rook's environment.
  [LiveKit tuning](https://docs.livekit.io/agents/logic/turns/tuning/)
* Give long tools their own lifecycle. Pipecat supports tool timeouts and a
  per-tool interruption policy; LiveKit describes background tools that leave
  conversation responsive. Cancelling speech must not imply a completed external
  action was undone.
  [Pipecat tools](https://docs.pipecat.ai/pipecat/learn/function-calling),
  [LiveKit async tools](https://docs.livekit.io/agents/logic/tools/async/)
* Preserve conversation and valid tool-call/result relationships across agent
  handoffs. A short spoken summary is not an adequate durable record of work.
  [LiveKit context](https://docs.livekit.io/agents/logic/chat-context/)

## What the current implementation actually does

Audited Android source at `4854c9a` and the running service's source through Rook
on kaiju (`/home/bake/voice-agent/server.py`, `voice-agent.service`). Backend
source is outside this Git repository. No live conversations were exercised or
private transcripts collected for this audit.

| Reported problem | Observed implementation | Consequence |
| --- | --- | --- |
| Wake word triggers on noise | `WakeWordDetector` accepts one classifier score at or above 0.5; no speech gate or confirmation window. Reset leaves raw/pending audio intact. | A single erroneous score can wake the app. Real recordings are still needed to measure the false-activation rate. |
| Interruptions feel broken | `VoiceClient.pushFrame` drops microphone audio during queued playback plus 400 ms. The backend independently drops microphone frames until its estimated playback deadline. | Ordinary spoken barge-in cannot reach the conversation pipeline during playback. |
| Assistant may wake itself | The shared microphone feeds wake detection even during assistant playback. AEC objects are not retained and effectiveness is not checked. | Residual playback can enter the wake classifier. Hardware AEC availability is not proof of effective cancellation. |
| Conversation loses context | APK closes after 20 seconds of listening inactivity; backend creates a fresh history and ACP session for every WebSocket. | A quiet pause can destroy conversation continuity. |
| Old connection affects a new one | VoiceService callbacks and delayed goodbye handlers have no connection-generation guard. | A late close/bye can clear or close a newer session. |
| Tool calls stall or appear to succeed | ACP requests wait up to 180 seconds; read-loop EOF does not promptly fail pending requests. Hermes supervisor does not retrieve its child task's result/exception. | Disconnects can resemble slow tools; a failed task can reach the completion-summary path. |
| Follow-up questions lack details | Direct tool results exist only in the temporary model request; Hermes output is reduced to a short spoken summary in history. | Later turns may lack facts that were already fetched. |
| Stop is inconsistent | Backend stop signals an event and ACP cancellation but does not cancel the active turn task. Audio is untagged and client playback queue is unbounded. | Late output and task completion can cross turn boundaries; queued speech complicates interruption. |

These are code findings, not proof that every reported incident has the same
cause. Latency could also come from STT, inference, TTS or network load; those
stages need separate timing measurements.

## Implementation order

### 1. Establish a reproducible backend and fix lifecycle failures

Bring the backend under version control with credentials in runtime configuration
and model files managed separately. Preserve the current deployment as rollback.
Build an isolated harness with fake STT/LLM/TTS and controllable ACP/tool servers.

Fail pending ACP requests promptly on EOF and JSON-RPC errors. Bound connection,
tool and model waits independently; retrieve every child-task outcome and clean
up tasks on cancellation. Report failed/unknown results honestly. Do not retry
state-changing work automatically after a transport error.

Give Android callbacks a connection generation. Retain/release audio effects,
reset all wake buffers, and repair mic/player shutdown ownership. Remove the
20-second transport-bound context expiration as part of session persistence.

### 2. Make sessions and background work independent of audio turns

Use an opaque conversation ID scoped to the authenticated identity. Persist
user turns, tool jobs and outcomes independently of the WebSocket, with bounded
retention and explicit new-conversation behavior. Handle competing connections
deliberately. Do not use a shared global conversation for all clients.

Store valid tool-call/result pairs and enough result detail for follow-ups.
Compact context by complete turns and token budget, preserving unresolved jobs
and user constraints. Track what was played separately from what was generated.

Long Hermes jobs return a job ID immediately and publish progress/completion.
The user can keep speaking while a job runs. A barge-in stops speech generation
and playback; an explicit request to cancel work follows the job's cancellation
policy. Completed actions remain recorded even when their narration is cut off.

### 3. Fix the input and interruption path together

Add a local neural speech gate to wake detection and tune against recorded wake
phrases and negative audio. Run wake detection in standby; during a conversation,
normal speech controls turn-taking. Keep a short pre-roll so the first words
following a wake aren't lost.

Establish and test echo cancellation on the actual phone, including speakerphone
and Bluetooth routes. Do not merely delete both microphone-muting guards: that
could turn assistant playback into repeated self-interruptions. A WebRTC media
transport is worth evaluating for its audio processing and congestion handling;
the existing WebSocket can remain a control channel or a compatibility path.

Use local speech-start detection for prompt playback pause, then confirm the
interruption. Brief noises/backchannels should allow playback to resume. Use
semantic/acoustic turn completion to tolerate natural pauses. Keep an explicit
interrupt control available if a route cannot support reliable full duplex.

Tag output with turn/response IDs, bound buffered audio, and acknowledge actual
playback position. Discard stale output after cancellation. Decouple model
stream consumption from TTS while keeping both queues bounded.

### 4. Prove voice quality before adding entry points

Measure a baseline and the candidate with identical fixtures. Proposed release
criteria below are targets, not observed results:

| Test | Required evidence |
| --- | --- |
| Silence, fan, keyboard, clatter, music and TV | Record false wakes per hour; compare with baseline. No self-wake during repeated assistant playback. |
| Wake phrase at varied distances and speaking volumes | Record wake recall and delay alongside false wakes; do not improve one by making the other unusable. |
| Speak over assistant; cough; say a brief acknowledgement | Prompt audible stop for real interruption, recovery for false interruption, no old audio after the new answer. Aim for p95 stop latency below 500 ms after confirmed speech onset. |
| Pause mid-sentence and continue | One coherent user turn; no premature response that consumes the rest of the utterance. |
| Slow, failed and disconnected tools | Conversation remains responsive; one accurate terminal job state; no fabricated success or duplicate external action. |
| Pause over 20 seconds; reconnect; restart client | Same conversation and completed job results available to follow-up questions. |
| Interrupt at STT, LLM, TTS and tool boundaries | No stale callback/state/audio, orphaned tool result or leaked background task. |
| Speakerphone, headset, Bluetooth and screen off | Repeat acoustic tests on physical Android; emulator tests are insufficient. |

Log stage timings and event/job IDs by default, not raw microphone recordings,
credentials or full private transcripts. Record audio for evaluation only through
an explicit diagnostic flow.

### 5. Android default assistant

Once the quality gates pass, add `VoiceInteractionService` and
`VoiceInteractionSessionService`, plus a Settings entry for the system assistant
selector. Invocation should open the existing conversation and use the same
voice engine, with normal permission and lock-screen handling. Selection must
remain a user action; do not assume it grants unrestricted microphone access or
Google's proprietary hardware hotword support.
[Android VoiceInteractionService](https://developer.android.com/reference/android/service/voice/VoiceInteractionService)

## Rollout

Keep old clients working during protocol migration. Test the candidate backend
on a separate endpoint and use a development APK before moving the production
voice service or publishing an OTA. Promote only after the acoustic and failure
tests above, and record APK/backend versions together. Worker fleet updates are
not necessary for a voice-only change unless a capability contract changes.
