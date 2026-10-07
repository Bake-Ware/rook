# Voice replies: answering what an agent says through the phone

Status: approved (2026-10-07), step 1 being built. Tracking: rook project
`rook-oct-2026-batch`, tasks "Voice replies 1" … "Voice replies 3".

## Why

Agents talk to Bake through `voice.speak` on bakephone ("Build finished, want
me to deploy?"). Today that is one-way: the answer has to go through a chat
window or a terminal. Bake wants to just answer out loud and have the answer
reach the agent that asked. Since anything may be going on around the phone
when a message arrives (kids, the car, a TV), the agent also needs to know
whether the answer really came from Bake.

## Step 1: a reply window

An agent asks for an answer with `voice.speak(text, reply=true)`.

On the phone, after the (last part of the) line has played:

1. A short beep marks the window. The phone records with a plain energy
   endpointer: it waits up to `reply_timeout` seconds (default 8, 2–30) for
   speech, then stops after 1.2 s of silence, at most 20 s in total. Nothing
   heard means no reply.
2. The audio (16 kHz mono PCM) goes to the voice server's new
   `POST /api/transcribe` (same keys/guest rule as `/api/voice`), which runs
   the server's Whisper and returns `{text, seconds}`.
3. The text is attached to that speech job: `reply: {text, via: "voice",
   at, seconds}`; or `reply_state: "none"` (silence), `"error"` (mic or
   server failure, with the reason).
4. While the window is open the phone stays out of wake standby and nothing
   else is spoken over it.

A notification ("<line> — Reply") is posted for every line that asks for a
reply. Its **Reply** action takes typed text from the lock screen, for when
talking isn't possible. A typed reply is `via: "text"`, and wins if it lands
while the voice window is still open. It is accepted for 10 minutes.

Getting the reply to the caller:

- `wait=true` (the default): the call returns after the window closes, with
  `reply` (or `reply_state`) in its result. The caller's timeout should cover
  the line plus the window.
- `wait=false`: the agent reads it later with `voice.speak_status(id)` or
  `voice.replies(since)`, which lists recent replies with their speech ids.

A reply is data, not an instruction: it is whatever the microphone heard.
Agents treat it like chat text from the user, and until step 2 lands they
cannot know who said it.

## Step 2: who said it (speaker verification)

The voice server gets a speaker-embedding model (ECAPA-TDNN class, small,
on GPU0 beside Chatterbox). Bake enrolls once with about a minute of speech.
Every reply (and later every voice-chat turn) is scored against the
enrollment: `speaker: {id: "bake"|null, score, verified, voices}`.

- `verified` needs the score above a tuned threshold and at least ~1.5 s of
  speech; short replies ("yeah") come back unverified with the score.
- `voices > 1` flags overlapping speakers.
- It is a confidence signal, not security: a recording or a cloned voice
  (this server clones voices) can pass it. Agents may act on a verified reply
  to their own question, and must ask again (or ask for a typed reply) before
  anything risky when the reply is unverified.

## Step 3: replying later, and replies into Rook chat

- "Hey Sojourn, tell Claude the deploy can wait": the assistant keeps recent
  agent messages on its board and can route an answer to the sender.
- Replies to `wait=false` messages are also posted to the calling agent's
  Rook chat by the hub, so `_unread_chat` surfaces them without polling.

## Not doing

- Always-on listening for replies: only the window after a line that asked.
- Treating a voice reply as approval for destructive actions.
