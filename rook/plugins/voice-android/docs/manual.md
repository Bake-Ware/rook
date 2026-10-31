# Phone voice

The phone speaking aloud: any agent or job on the band can talk to the person
through their phone ("The build finished").

| Cap | What it does |
|---|---|
| `voice.speak` | Say something; returns when it has been said (or at once with `wait: false`) |
| `voice.ask` | Say something, then listen for the spoken answer |
| `voice.replies` | Answers to earlier `voice.ask` calls |
| `voice.stop` | Stop speaking and drop the queue |
| `voice.voices` | The phone's own text to speech voices |

With a voice server set up in the app, lines are spoken in the app's
selected voice (the assistant's); otherwise, or if the server fails a line,
Android's own text to speech says it. Long text is cut at sentence ends
into lines of about 17 seconds. Speech plays as assistant audio, so it works
with the screen off and other audio dips under it. If the app's own voice
session is speaking, a new line waits for it unless `interrupt` is true.

`voice.ask` opens a reply window after the line: the phone beeps and records
the answer, which the voice server transcribes; the lock-screen notification
also takes a typed answer. The reply is whatever the microphone heard: data,
not a verified instruction, so it marks the session tainted.
