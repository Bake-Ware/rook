"""whiteboard.read/write/clear: one board record per task or project, made
on first write. Last write wins; append rereads so a line is never lost."""

MAX = 16000      # a whiteboard holds essentials, not logs
TRIES = 5


def _which(ctx, task, project):
    if (task is None) == (project is None):
        raise ctx.Error("bad args: give task or project (one)")
    return ("task", task) if task is not None else ("project", project)


def _find(ctx, key, ref):
    rows = ctx.records.board.list(where={key: ref}, limit=1)
    return rows[0] if rows else None


def _view(key, ref, b):
    if b is None:
        return {key: ref, "text": "", "by": None, "updated": None}
    return {key: ref, "text": b["text"] or "", "by": b["by"], "updated": b["updated"]}


def read(ctx, task=None, project=None):
    key, ref = _which(ctx, task, project)
    return _view(key, ref, _find(ctx, key, ref))


def _put(ctx, key, ref, new_text):
    """Set the board's text to ``new_text(old_text)``."""
    me = ctx.caller.actor
    for attempt in range(TRIES):
        b = _find(ctx, key, ref)
        text = new_text(b["text"] or "" if b else "")
        if len(text) > MAX:
            raise ctx.Error(f"a whiteboard holds essentials: {len(text)} characters is over {MAX}; "
                            "summarize, and link the details to the task instead")
        try:
            if b is None:
                return _view(key, ref, ctx.records.board.create(**{key: ref}, text=text, by=me))
            return _view(key, ref, ctx.records.board.update(b["id"], revision=b["revision"], text=text, by=me))
        except ctx.Error:
            # Someone else wrote first (a revision race, or both creating the
            # board); a bad task or project id fails the same way again.
            if attempt == TRIES - 1 or (b is None and _find(ctx, key, ref) is None):
                raise


def write(ctx, task=None, project=None, text=None, append=None):
    key, ref = _which(ctx, task, project)
    if (text is None) == (append is None):
        raise ctx.Error("bad args: give text (the whole board) or append (one)")
    if text is not None:
        return _put(ctx, key, ref, lambda old: text)
    return _put(ctx, key, ref, lambda old: f"{old.rstrip()}\n{append}".lstrip("\n"))


def clear(ctx, task=None, project=None):
    key, ref = _which(ctx, task, project)
    if _find(ctx, key, ref) is None:
        return _view(key, ref, None)
    return _put(ctx, key, ref, lambda old: "")
