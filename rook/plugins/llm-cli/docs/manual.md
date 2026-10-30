# Models through Claude Code or Codex

An `llm.complete` provider that uses a CLI subscription already signed in on a
worker: Claude Code (`tool: claude`, the default) or Codex (`tool: codex`).
Most people start here: no API key, the subscription's tokens.

Install it on the worker where the CLI is signed in. Each call runs one
completion with tools off, through core's launch path, in a temp folder:

| Tool | Command (prompt on stdin) |
|---|---|
| claude | `claude -p --output-format json --tools "" --no-session-persistence --model M --system-prompt S` |
| codex | `codex exec --ephemeral --skip-git-repo-check -s read-only -o FILE -m M -` |

A conversation longer than one message goes in as a labelled transcript;
system messages become Claude Code's system prompt, or lead the Codex prompt.

## The three systems

`model` is a model id or `one`, `two` (the default) or `three`. For Claude
Code the defaults are `haiku`, `opus` and `haiku`; for Codex, blank means the
CLI's own default. Set `model_one`, `model_two`, `model_three` to change them.

## Limits

`max_tokens` and `temperature` can't be passed to a CLI and are ignored.
`stop` sequences cut the reply here (`stop_reason: stop`). `llm.models` runs
`<program> --version` to show the CLI works on this worker.
