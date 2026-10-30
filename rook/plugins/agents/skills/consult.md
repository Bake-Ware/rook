---
description: Pull in another agent - ask, hand off or assemble a small team - instead of guessing or overstepping.
when: the work touches another role, you are unsure about something another agent owns, or a new idea needs several roles.
---
# Consulting other agents

Pulling in other agents is expected. It is not overstepping.

- **Ask** with `agents.chat.send` (agent: their name, text: your question).
  Give them what they need: the task, what you found, what you want back.
  `wait: false` if you don't need the answer to go on; read it later with
  `agents.chat.read`.
- **Who to ask:** your system prompt lists the agents on the band and what
  each is for. Examples: the code contradicts the ticket - ask the engineer
  who did the work; scope is creeping - tell the project manager; you need
  the person - ask the house agent.
- **Assemble a team** for a new idea: message each role with the same brief
  and what you want from them, then bring the answers together.
- **Don't relay chatter.** Ask one clear question; answer the same way.
