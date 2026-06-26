"""Bulk concept extraction — uses local 9B model to index conversations into the graph.

Reads conversations from the cloud sync DB, sends batched turns to the local
model for keyword/concept extraction, and writes results to the knowledge graph.

Zero Anthropic quota consumed. All extraction runs on localhost:1234 (LM Studio).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import sys
import time
from pathlib import Path

log = logging.getLogger("rook.extractor")

SYNC_DB = Path.home() / ".rook" / "cloud" / "cloud.db"
EXTRACT_STATE_DB = Path.home() / ".rook" / "graph" / "extract_state.db"

LM_STUDIO_URL = "http://localhost:1234/v1/chat/completions"
DEFAULT_MODEL = "qwen3.5-9b-claude-4.6-opus-reasoning-distilled"

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)

EXTRACTION_PROMPT = """Extract keywords and concepts from this conversation. Return ONLY a JSON object:
{
  "concepts": ["keyword1", "keyword2"],
  "project": "project name if this is about a specific project, otherwise empty string",
  "summary": "one-line summary of the conversation"
}

Rules:
- Concepts should be lowercase, use underscores for multi-word (e.g. "orthogonal_transforms")
- Include: techniques, tools, architectures, project names, technologies, specific terms
- Exclude: generic words (help, question, code, file, error), stop words, pleasantries
- Project should be a specific project name like "droga", "rook", "lisa", "messy_boi", or empty
- Keep concepts to 3-10 per conversation. Quality over quantity.
- Return ONLY the JSON. No explanation."""


def _init_state_db() -> sqlite3.Connection:
    EXTRACT_STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(EXTRACT_STATE_DB))
    db.execute("""
        CREATE TABLE IF NOT EXISTS extracted (
            conversation_uuid TEXT PRIMARY KEY,
            extracted_at REAL,
            concept_count INTEGER DEFAULT 0
        )
    """)
    db.commit()
    return db


def _call_local_model(conversation_text: str, model: str = DEFAULT_MODEL) -> dict | None:
    """Call local LM Studio model for extraction."""
    import urllib.request

    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": EXTRACTION_PROMPT},
            {"role": "user", "content": conversation_text},
        ],
        "temperature": 0.1,
        "max_tokens": 300,
    }).encode("utf-8")

    req = urllib.request.Request(LM_STUDIO_URL, data=payload, headers={
        "Content-Type": "application/json",
    })

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            raw = data["choices"][0]["message"]["content"]
            return _parse_extraction(raw)
    except Exception as e:
        log.error("Local model call failed: %s", e)
        return None


def _parse_extraction(raw: str) -> dict | None:
    """Parse JSON from model output, handling think tags, fences, and truncation."""
    # Strip think tags
    raw = _THINK_RE.sub("", raw).strip()

    # Strip markdown code fences
    raw = re.sub(r"^```\w*\n?", "", raw)
    raw = re.sub(r"\n?```\s*$", "", raw)
    raw = raw.strip()

    # Try direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Find JSON object in the text
    match = re.search(r"\{.*", raw, re.DOTALL)
    if not match:
        log.warning("No JSON found in response: %s", raw[:100])
        return None

    json_str = match.group()

    # Try to fix truncated JSON — close any open strings and brackets
    for suffix in ["", '"]}', '"}', "]}", "}"]:
        try:
            return json.loads(json_str + suffix)
        except json.JSONDecodeError:
            continue

    # Last resort: extract concepts with regex
    concepts = re.findall(r'"([a-z][a-z0-9_ ]+)"', json_str.lower())
    # Filter out JSON keys and generic words
    skip = {"concepts", "project", "summary", "true", "false", "null", "empty"}
    concepts = [c.replace(" ", "_") for c in concepts if c not in skip and len(c) > 2]

    if concepts:
        return {"concepts": concepts[:10], "project": "", "summary": ""}

    log.warning("Failed to parse extraction: %s", json_str[:150])
    return None


def _condense_turns(turns: list[dict], max_chars: int = 3000) -> str:
    """Condense conversation turns into a compact text block for extraction."""
    lines = []
    total = 0
    for t in turns:
        role = "Human" if t["role"] == "human" else "Assistant"
        content = t["content"][:500]  # Truncate long turns
        line = f"{role}: {content}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "\n\n".join(lines)


def extract_batch(limit: int = 50, force: bool = False,
                  model: str = DEFAULT_MODEL) -> dict:
    """Extract concepts from unprocessed conversations.

    Returns stats dict.
    """
    if not SYNC_DB.exists():
        return {"error": "No synced conversations. Run rook_cloud_sync first."}

    cloud_db = sqlite3.connect(str(SYNC_DB))
    cloud_db.row_factory = sqlite3.Row
    state_db = _init_state_db()

    from .graph import RookGraph
    graph = RookGraph()

    # Find conversations not yet extracted
    if force:
        convos = cloud_db.execute("""
            SELECT c.uuid, c.name, c.model, c.updated_at
            FROM conversations c
            ORDER BY c.updated_at DESC LIMIT ?
        """, (limit,)).fetchall()
    else:
        already = {r[0] for r in state_db.execute("SELECT conversation_uuid FROM extracted").fetchall()}
        convos = cloud_db.execute("""
            SELECT c.uuid, c.name, c.model, c.updated_at
            FROM conversations c
            ORDER BY c.updated_at DESC LIMIT ?
        """, (limit * 2,)).fetchall()  # fetch more to account for already-extracted
        convos = [c for c in convos if c["uuid"] not in already][:limit]

    stats = {"total": len(convos), "extracted": 0, "skipped": 0, "errors": 0,
             "concepts_total": 0, "model": model}

    for i, convo in enumerate(convos):
        uuid = convo["uuid"]
        name = convo["name"] or "(unnamed)"

        # Get turns
        turns = cloud_db.execute("""
            SELECT role, content FROM turns
            WHERE conversation_uuid = ?
            ORDER BY turn_index LIMIT 20
        """, (uuid,)).fetchall()

        if not turns or len(turns) < 2:
            state_db.execute(
                "INSERT OR REPLACE INTO extracted (conversation_uuid, extracted_at, concept_count) VALUES (?,?,?)",
                (uuid, time.time(), 0),
            )
            stats["skipped"] += 1
            continue

        # Condense for extraction
        text = _condense_turns([dict(t) for t in turns])
        if len(text) < 50:
            stats["skipped"] += 1
            continue

        # Call local model
        result = _call_local_model(text, model=model)
        if not result:
            stats["errors"] += 1
            continue

        concepts = result.get("concepts", [])
        project = result.get("project", "")
        summary = result.get("summary", "")

        # Clean concepts
        concepts = [re.sub(r'[^a-z0-9_]', '_', c.lower().strip()).strip('_')
                    for c in concepts if c and len(c) > 2]
        concepts = list(dict.fromkeys(concepts))[:10]  # dedup, cap at 10

        if concepts:
            # Index into graph
            graph.index_finding(
                concepts=concepts,
                source_type="conversation",
                source_location=uuid,
                source_title=name,
                project=project if project else "",
                weight=min(len(turns) / 5, 3.0),  # weight by conversation length
            )

            # If we have a project, update it
            if project:
                graph.index_project(project)
                if summary:
                    graph.add_project_event(
                        re.sub(r'[^a-z0-9_]', '_', project.lower().strip()).strip('_'),
                        summary,
                        event_type="extracted",
                        source_id=uuid,
                    )

        # Mark as extracted
        state_db.execute(
            "INSERT OR REPLACE INTO extracted (conversation_uuid, extracted_at, concept_count) VALUES (?,?,?)",
            (uuid, time.time(), len(concepts)),
        )
        state_db.commit()

        stats["extracted"] += 1
        stats["concepts_total"] += len(concepts)

        if (i + 1) % 10 == 0:
            log.info("Extracted %d/%d conversations (%d concepts so far)",
                     i + 1, len(convos), stats["concepts_total"])

    cloud_db.close()
    state_db.close()

    # Get final graph stats
    stats["graph"] = graph.stats()
    graph.close()

    return stats


def extract_single(conversation_uuid: str, model: str = DEFAULT_MODEL) -> dict:
    """Extract concepts from a single conversation."""
    if not SYNC_DB.exists():
        return {"error": "No synced conversations."}

    cloud_db = sqlite3.connect(str(SYNC_DB))
    cloud_db.row_factory = sqlite3.Row

    convo = cloud_db.execute("SELECT uuid, name FROM conversations WHERE uuid LIKE ?",
                             (f"{conversation_uuid}%",)).fetchone()
    if not convo:
        return {"error": f"Conversation {conversation_uuid} not found."}

    turns = cloud_db.execute("""
        SELECT role, content FROM turns
        WHERE conversation_uuid = ? ORDER BY turn_index LIMIT 20
    """, (convo["uuid"],)).fetchall()
    cloud_db.close()

    if not turns:
        return {"error": "No turns in conversation."}

    text = _condense_turns([dict(t) for t in turns])
    result = _call_local_model(text, model=model)
    if not result:
        return {"error": "Extraction failed."}

    from .graph import RookGraph
    graph = RookGraph()

    concepts = [re.sub(r'[^a-z0-9_]', '_', c.lower().strip()).strip('_')
                for c in result.get("concepts", []) if c and len(c) > 2]
    concepts = list(dict.fromkeys(concepts))[:10]
    project = result.get("project", "")

    if concepts:
        graph.index_finding(
            concepts=concepts,
            source_type="conversation",
            source_location=convo["uuid"],
            source_title=convo["name"] or "",
            project=project,
            weight=min(len(turns) / 5, 3.0),
        )

    graph.close()

    return {
        "conversation": convo["name"],
        "concepts": concepts,
        "project": project,
        "summary": result.get("summary", ""),
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    parser = argparse.ArgumentParser(description="Extract concepts from conversations using local model")
    parser.add_argument("--limit", type=int, default=50, help="Max conversations to process")
    parser.add_argument("--force", action="store_true", help="Re-extract already processed conversations")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="LM Studio model to use")
    parser.add_argument("--single", help="Extract a single conversation by UUID prefix")
    args = parser.parse_args()

    if args.single:
        result = extract_single(args.single, model=args.model)
        print(json.dumps(result, indent=2))
    else:
        print(f"Extracting from up to {args.limit} conversations using {args.model}...")
        stats = extract_batch(limit=args.limit, force=args.force, model=args.model)
        print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
