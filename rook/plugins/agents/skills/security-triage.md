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

## A call the security gate held

1. Read the hold the plugin sent you: the call, its why, the session's
   pinned task, the gate's reason (`taint`, `intent_mismatch`,
   `persistence`).
2. **False alarm you can show?** It matches the session's pinned task, or
   this caller made exactly this call there from a clean session before.
   Then `security.release {hold, evidence: matches_task | seen_clean, note}`
   - one sentence on why. Nothing else releases anything.
3. **Otherwise leave it**: the person already has the card. Reach them
   through the house agent only when it looks like a real attack, once, in
   plain words with the undo, e.g. "claude-code tried to copy your home
   folder to a server named in an email it read; I left it waiting. If that
   was you, approve it on Approvals."
4. After a release, a line on your notepad (what ran, why it was a false
   alarm). Releases show in `security.status`; they don't need a message.
