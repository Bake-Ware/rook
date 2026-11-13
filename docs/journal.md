# The journal

One flat table at the hub, `journal`, holds what happened (L1): calls, job runs, auth events, band changes, events on the bus, closed streams, and plugin worker parts a worker failed to load. Agents read it with the MCP door's `journal` tool and people on the Data page (L2). Each entry is written scrubbed of vault values and known secret shapes (L4), and `journal.redact` takes a value out of every entry afterwards (L5).

## An entry

| Field | Meaning |
|---|---|
| `at` | When it was written, in seconds. For a call or a job run, when the call **started** (see below); for everything else, when it happened. |
| `kind` | `call`, `job` (a job's run, J8), `auth`, `band`, `event`, `stream` (a stream closed) or `plugin` (a worker part that failed to load). |
| `user`, `token` | Who acted: the user, and the token they acted through, if any (L1). Empty for what the hub does by itself. |
| `worker` | The worker, device or `hub` it concerns. |
| `cap`, `why` | For a call: the cap and the caller's why. For a closed stream: why it was closed. |
| `job`, `run` | For a job's run: the job and the run ID its chain shares (J5). |
| `result` | JSON. For a call: its reply (below). For the other kinds: what happened. |

## Calls: written when they start, completed when they end

A call's entry (kind `call` or `job`) is written as the call starts, with the result `{"type": "running", "id": <call id>}`, so a reader can tell a call that is still running from one that has finished. If the client stopped waiting and was given a stream to follow (Bake 13 Nov), the running result also names that `stream`. When the call ends, the same entry holds its reply: `{"type": "result", "id", "result"}`, `{"type": "error", "id", "code", "message", "next"}` or, for a cap that opens a stream, `{"type": "stream_open", ...}`. So `at` is when the call started, not when it ended.

A call that was running when the hub stopped is ended when a hub next takes over the data directory: its result becomes the error `hub_restarted`, saying what to try next (C3), and so does the last entry of its stream if it had one. An entry that has ended is never changed again by the call it records.
