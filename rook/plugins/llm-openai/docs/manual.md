# OpenAI-compatible models

An `llm.complete` provider for any endpoint that speaks the OpenAI chat
completions API: a local model server (llama.cpp, vLLM, Ollama, LM Studio), a
front door such as llmanifold (lanes, queueing, fallbacks) or a hosted API.
One endpoint per hub.

## Set up

Set `llm-openai.url` to the API base up to `/v1` (for example
`http://kaiju:8080/v1`). If the endpoint needs a key, store it in the vault and
set `llm-openai.key` to the vault name (`secret.set` stores it). `llm.status` says whether it
answers; `llm.models {provider: llm-openai}` lists what it serves.

## The three systems

`model` is a model id, or a system: `one` (the fast voice talker), `two` (an
agent's normal brain, the default) or `three` (the dream cycle's cleanup).
`model_one`, `model_two` and `model_three` map them; blank One falls back to
Two, blank Three to One, and blank Two lets the endpoint pick.

## The call

`llm.complete {messages, model?, max_tokens?, temperature?, stop?}` becomes one
`POST /chat/completions`. The reply's `stop_reason` is `end`, `length` or
`refusal` (a content filter); `usage` counts `input` and `output` tokens when
the endpoint reports them.
