---
description: Triage a security event - blocked call, odd hold, tainted session, exposed secret - into noise or a ranked finding.
when: a gate blocks a call, a session is tainted, or you spot a secret or private detail where it shouldn't be.
---
# Security triage

1. **What happened?** The call, who made it, why they said they made it, the
   gate's reason, what came before it in the journal.
2. **Noise or real?** Most blocks are a confused agent or a bad guess. Noise:
   one line on your notepad, move on.
3. **Real:** rank it - what an attacker could do with it, how exposed it is
   (public beats internal), how easy the fix is.
4. **Exposed secret:** it is compromised. Never repeat it; say where it is
   and which secret it looks like; the fix is rotation.
5. **Report** real findings to the person through the house agent: what,
   how bad, the fix you recommend - a safer way, not a new guardrail.
