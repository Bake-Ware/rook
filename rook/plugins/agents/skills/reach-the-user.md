---
description: Get word to the person, or a decision from them, in the right way at the right time.
when: another agent asks you to reach the person, or something you hold is worth their attention.
---
# Reaching the person

You are their assistant; other agents come to you instead of notifying them
directly. Your job is to pick the channel, the device and the moment.

1. **Is it worth it now?** Weigh it against what you know of them. Decisions
   that block work, things with a deadline and anything they asked to hear
   about go now; the rest waits for a natural moment or a digest.
2. **Where are they and what are they doing?** Use the presence skill.
3. **Pick the form that fits the moment:**
   - driving or walking: one short voice message on the phone ("when you
     park...");
   - at the desk: a desktop notification, or open the thing itself and show
     what needs attention;
   - asleep or busy with people: hold it until a good moment, unless it is
     worth waking them for;
   - away from devices: a chat message (`notify.send`) they will see later.
4. **Say it in one or two sentences**, with the answer or the decision you
   need, not the raw data. For a decision, say the options and what you
   recommend.
5. **Follow through.** Note what you sent and when; if the decision comes
   back, relay it to the agent that asked with the person's own words. A
   question that came as `approval.ask` (a `q_...` id) is answered with
   `approval.decide {hold, answer}`; the person can also answer it on the
   Approvals page themselves.
6. **An agent needs a decision from the person?** It asks with
   `approval.ask`, and the question reaches you; never answer one yourself.

What they told you about how they want to be reached beats all of this; keep
it in your notepad.
