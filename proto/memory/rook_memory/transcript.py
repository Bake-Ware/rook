"""Reading a closed Claude Code session file (.jsonl) into plain entries.

Each entry is one thing that happened, kept word for word: what the user wrote, what the agent
wrote, a tool the agent ran (its input cut to a knob's length), and what came back (also cut).
The agent's hidden thinking is not in the files and is not read. Every entry keeps the line of the
file it came from, so an episode can point back at its source.
"""

import json
from dataclasses import dataclass


@dataclass
class Entry:
    line: int    # line number in the .jsonl file, from 1
    at: str      # when, as the file gives it (ISO 8601, UTC)
    role: str    # user, agent, tool, result, summary
    text: str


def _clip(text, chars):
    text = text.strip()
    if chars and len(text) > chars:
        return text[:chars] + f" [... {len(text) - chars} more characters]"
    return text


def _result_text(content):
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif isinstance(block, dict):
            parts.append(f"[{block.get('type')}]")
    return "\n".join(parts)


def read(path, knobs, after_line=0):
    """The entries of the session file at `path`, from the line after `after_line`."""
    tool_input_chars = knobs["ingest"]["tool_input_chars"]
    tool_result_chars = knobs["ingest"]["tool_result_chars"]
    skip = tuple(knobs["ingest"]["skip_user_text_starting_with"])
    entries = []
    with open(path, encoding="utf-8") as f:
        for line, raw in enumerate(f, 1):
            if line <= after_line:
                continue
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            kind = record.get("type")
            if kind not in ("user", "assistant") or record.get("isMeta"):
                continue
            at = record.get("timestamp", "")
            content = (record.get("message") or {}).get("content")
            if kind == "user":
                role = "summary" if record.get("isCompactSummary") else "user"
                if isinstance(content, str):
                    if content.strip() and not content.lstrip().startswith(skip):
                        entries.append(Entry(line, at, role, content.strip()))
                    continue
                for block in content or []:
                    if block.get("type") == "text":
                        text = block.get("text", "")
                        if text.strip() and not text.lstrip().startswith(skip):
                            entries.append(Entry(line, at, role, text.strip()))
                    elif block.get("type") == "tool_result":
                        text = _clip(_result_text(block.get("content")), tool_result_chars)
                        if block.get("is_error"):
                            text = "(error) " + text
                        entries.append(Entry(line, at, "result", text))
            else:
                for block in content or []:
                    if block.get("type") == "text" and block.get("text", "").strip():
                        entries.append(Entry(line, at, "agent", block["text"].strip()))
                    elif block.get("type") == "tool_use":
                        args = json.dumps(block.get("input"), ensure_ascii=False)
                        entries.append(Entry(line, at, "tool", f"{block.get('name')} {_clip(args, tool_input_chars)}"))
    return entries


def render(entry):
    """One entry as a line of text for the model and for people: `L123 2026-11-12 21:03 agent: ...`."""
    return f"L{entry.line} {entry.at[:16].replace('T', ' ')} {entry.role}: {entry.text}"


def chunks(entries, chars):
    """The entries in runs whose rendered text is about `chars` long; an entry is never split."""
    run, size = [], 0
    for entry in entries:
        n = len(render(entry)) + 1
        if run and size + n > chars:
            yield run
            run, size = [], 0
        run.append(entry)
        size += n
    if run:
        yield run
