"""The dream pass (the paper's reflection, with Bake's additions).

1. Over the episodes not yet dreamed over, ask the model for the most salient questions.
2. For each question, retrieve memories (episodes and the beliefs that stand) the way a turn would,
   and ask the model for beliefs, each with a confidence, the memories that support it, and the held
   belief it contradicts or refines, if any. That one is kept and marked superseded, never deleted.
   Beliefs written for one question are there to be retrieved, and superseded, by the next.
3. Mark those episodes dreamed over, and condense the scratch pad: drop the word-for-word entries of
   all but the most recent sessions (the episodes still point at the source lines).
"""

import json
from string import Template

from . import model, recall, schemas


def due(store, knobs):
    return store.undreamed_importance() >= knobs["dream"]["when_importance_sum_reaches"]


def memory_line(item):
    if item.kind == "episode":
        return f"[{item.tag}] ({item.extra['happened_at'][:16].replace('T', ' ')}, importance {item.importance}) {item.text}"
    return f"[{item.tag}] (belief, confidence {item.extra['confidence']:.2f}) {item.text}"


def _ref(tag):
    tag = str(tag).strip().lower().strip("[]")
    kind = {"e": "episode", "b": "belief"}.get(tag[:1])
    if kind and tag[1:].isdigit():
        return kind, int(tag[1:])
    return None


def run(store, knobs, trigger="hand", log=print):
    d = knobs["dream"]
    new = store.undreamed()
    dream = store.start_dream(trigger)
    log(f"dream {dream} ({trigger}): {len(new)} new episodes, importance {sum(e['importance'] for e in new)}")
    questions, made, superseded = [], 0, 0
    if new:
        listing = "\n".join(
            f"[e{e['id']}] ({e['happened_at'][:16].replace('T', ' ')}, importance {e['importance']}) {e['text']}"
            for e in new)
        prompt = Template(knobs["prompts"]["questions"]).safe_substitute(count=d["questions"], episodes=listing)
        try:
            answer = model.ask(store, knobs, f"dream {dream} questions", knobs["model"]["dream_model"], prompt,
                               schemas.QUESTIONS)
            questions = [str(q).strip() for q in answer.get("questions") or [] if str(q).strip()][:d["questions"]]
        except model.ModelError as e:
            log(f"  no questions ({e}); the episodes stay new for the next dream")
            store.finish_dream(dream, json.dumps([]), [], 0, 0, 0)
            return {"dream": dream, "questions": [], "made": 0, "superseded": 0, "condensed": 0}
        for n, question in enumerate(questions, 1):
            log(f"  question {n}: {question}")
            items = recall.rank(recall.memories(store, knobs), question, knobs)[:d["retrieve_per_question"]]
            for kind in ("episode", "belief"):
                store.touch(kind, [i.id for i in items if i.kind == kind])
            prompt = Template(knobs["prompts"]["beliefs"]).safe_substitute(
                question=question, count=d["beliefs_per_question"],
                memories="\n".join(memory_line(i) for i in items))
            try:
                answer = model.ask(store, knobs, f"dream {dream} beliefs {n}", knobs["model"]["dream_model"], prompt,
                                   schemas.BELIEFS)
            except model.ModelError as e:
                log(f"    no beliefs ({e})")
                continue
            for b in (answer.get("beliefs") or [])[:d["beliefs_per_question"]]:
                text = str(b.get("belief") or "").strip()
                if not text:
                    continue
                because = []
                for tag in b.get("because") or []:
                    ref = _ref(tag)
                    table = {"episode": "episodes", "belief": "beliefs"}.get(ref[0]) if ref else None
                    if table and store.one(f"SELECT 1 FROM {table} WHERE id = ?", ref[1]):
                        because.append(ref)
                old = _ref(b.get("supersedes")) if b.get("supersedes") is not None else None
                old_id = old[1] if old and old[0] == "belief" else None
                how = b.get("how") if b.get("how") in ("contradicts", "refines") else None
                try:
                    confidence = float(b.get("confidence", 0.5))
                except (TypeError, ValueError):
                    confidence = 0.5
                try:
                    importance = max(1, min(10, int(round(float(b.get("importance", 5))))))
                except (TypeError, ValueError):
                    importance = 5
                new_id, replaced = store.add_belief(text, confidence, importance, dream=dream, question=question,
                                                    because=because, supersedes=old_id, how=how)
                made += 1
                note = f", supersedes b{replaced} ({how or 'refines'})" if replaced else ""
                log(f"    b{new_id} ({confidence:.2f}, {len(because)} supports{note}) {text}")
                if replaced:
                    superseded += 1
    condensed = store.condense_scratch(d["keep_scratch_of_last_sessions"])
    store.finish_dream(dream, json.dumps(questions), [e["id"] for e in new], made, superseded, condensed)
    log(f"  dream {dream} done: {made} beliefs, {superseded} superseded, {condensed} scratch entries condensed")
    return {"dream": dream, "questions": questions, "made": made, "superseded": superseded, "condensed": condensed}
