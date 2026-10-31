# Claude API

An `llm.complete` provider for the Claude API with an API key (the Messages
API, version 2026-10-21, over HTTPS; no SDK).

## Set up

Make an API key in the Claude Console, store it in the vault and set
`llm-anthropic.key` to the vault name (`secret.set {name: claude-api, value: ...}`,
then `claude-api`).

## The three systems

`model` is a model id or a system: `one` (default `claude-haiku-5-5`), `two`
(the default, `claude-opus-5-5`; `claude-sonnet-5-5` costs less) or `three`
(`claude-haiku-5-5`, the dream cycle). Change them with `setting.set`.

## The call

System messages become the request's system prompt; the rest are the turns.
`max_tokens` defaults to the `max_tokens` setting (16000). `stop` becomes stop
sequences. `temperature` is accepted for the contract but not sent: this API
version refuses sampling options. `stop_reason` is `end`, `length`, `stop` or
the API's own word (such as `refusal`). A rate limit or an overloaded API fails
with `retry`.
