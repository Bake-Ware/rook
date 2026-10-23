"""A fake ``claude`` for tests/test_agents_host.py: the stream-json protocol of
Claude Code 2.1 as the session host uses it (never the real thing in tests).

* ``-p --input-format stream-json``: reads user messages and control requests
  from stdin; each user message is a turn (``system/init`` first, then
  partial deltas with ``--include-partial-messages``, ``assistant``, ``result``).
* Turn scripts by the text of the message: ``slowtool <s>`` (a tool call that
  takes <s> seconds; messages written meanwhile are folded into the turn),
  ``pause <s>`` (waits before answering), ``crash`` (exits 3), ``/compact ...``.
* ``control_request`` interrupt: ``control_response``, then the turn ends with
  ``error_during_execution``.
* ``--resume <id>`` finds ``$CLAUDE_CONFIG_DIR/projects/*/<id>.jsonl`` or exits 1
  ("No conversation found"); ``--fork-session`` (with ``--resume-session-at``)
  copies it to a new id; ``--no-session-persistence`` writes nothing.
* Without ``--input-format``: one prompt from argv or stdin (forks, btw).

Every start is logged to ``$FAKE_CLAUDE_LOG`` (argv, cwd, a few env facts). If
the file ``$FAKE_CLAUDE_DIE`` exists, it fails at startup.
"""

import glob
import json
import os
import queue
import re
import sys
import threading
import time
import uuid

OUT_LOCK = threading.Lock()
VALUE_FLAGS = {"--input-format", "--output-format", "--setting-sources", "--settings",
               "--mcp-config", "--permission-mode", "--tools", "--model", "--effort",
               "--resume", "--resume-session-at", "--allowedTools"}


def out(obj):
    with OUT_LOCK:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def parse(argv):
    opts, flags, pos = {}, set(), []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in VALUE_FLAGS:
            opts[a] = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
            continue
        if a.startswith("-"):
            flags.add(a)
        else:
            pos.append(a)
        i += 1
    return opts, flags, pos


def config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def project_file(sid):
    d = os.path.join(config_dir(), "projects", os.getcwd().replace("/", "-"))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{sid}.jsonl")


def find(sid):
    hits = glob.glob(os.path.join(config_dir(), "projects", "*", f"{sid}.jsonl"))
    return hits[0] if hits else None


def load(path):
    msgs = []
    with open(path) as f:
        for line in f:
            if line.strip():
                msgs.append(json.loads(line))
    return msgs


class Session:
    def __init__(self, opts, flags):
        self.persist = "--no-session-persistence" not in flags
        self.msgs = []
        self.path = None
        resume = opts.get("--resume")
        if resume:
            found = find(resume)
            if not found:
                sys.stderr.write(f"No conversation found with session ID: {resume}\n")
                sys.stderr.flush()
                sys.exit(1)
            self.msgs = load(found)
            if "--fork-session" in flags:
                at = opts.get("--resume-session-at")
                if at:
                    idx = next((i for i, m in enumerate(self.msgs) if m.get("uuid") == at), None)
                    if idx is None:
                        sys.stderr.write(f"No message found with uuid {at}\n")
                        sys.exit(1)
                    self.msgs = self.msgs[:idx + 1]
                self.id = str(uuid.uuid4())
            else:
                self.id = resume
                self.path = found
        else:
            self.id = str(uuid.uuid4())

    def add(self, kind, text):
        m = {"type": kind, "uuid": str(uuid.uuid4()), "text": text}
        self.msgs.append(m)
        if self.persist:
            if self.path is None:
                self.path = project_file(self.id)
                with open(self.path, "w") as f:
                    for old in self.msgs[:-1]:
                        f.write(json.dumps(old) + "\n")
            with open(self.path, "a") as f:
                f.write(json.dumps(m) + "\n")
        return m

    def compact(self):
        self.msgs = [{"type": "user", "uuid": str(uuid.uuid4()), "text": "summary"}]
        if self.persist and self.path:
            with open(self.path, "w") as f:
                f.write(json.dumps(self.msgs[0]) + "\n")

    def context(self):
        return 100 * len(self.msgs) + 1000


def usage(sess, thinking):
    return {"input_tokens": 2, "cache_read_input_tokens": sess.context(),
            "cache_creation_input_tokens": 10, "output_tokens": 5}


def result_usage(sess):
    th = 0 if os.environ.get("MAX_THINKING_TOKENS") == "0" else 7
    return {"input_tokens": 2, "cache_read_input_tokens": sess.context(),
            "cache_creation_input_tokens": 10, "output_tokens": 5,
            "output_tokens_details": {"thinking_tokens": th}}


def stream_reply(sess, text, partial, model):
    msg_id = "msg_" + uuid.uuid4().hex[:12]
    if partial:
        out({"type": "stream_event", "event": {"type": "message_start", "message": {
            "id": msg_id, "usage": usage(sess, 0)}}, "parent_tool_use_id": None,
            "session_id": sess.id})
        out({"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
                                               "content_block": {"type": "text", "text": ""}},
             "parent_tool_use_id": None, "session_id": sess.id})
        for chunk in re.findall(r"\S+\s*", text):
            out({"type": "stream_event", "event": {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": chunk}},
                "parent_tool_use_id": None, "session_id": sess.id})
            time.sleep(0.005)
    m = sess.add("assistant", text)
    out({"type": "assistant", "uuid": m["uuid"], "parent_tool_use_id": None,
         "session_id": sess.id,
         "message": {"id": msg_id, "model": model, "role": "assistant",
                     "content": [{"type": "text", "text": text}], "usage": usage(sess, 0)}})
    if partial:
        out({"type": "stream_event", "event": {"type": "content_block_stop", "index": 0},
             "parent_tool_use_id": None, "session_id": sess.id})


def result(sess, text, t0, ok=True, num_turns=1, **extra):
    ev = {"type": "result", "subtype": "success" if ok else "error_during_execution",
          "is_error": not ok, "num_turns": num_turns,
          "duration_ms": round((time.monotonic() - t0) * 1000), "total_cost_usd": 0.001,
          "usage": result_usage(sess), "session_id": sess.id}
    if ok:
        ev["result"] = text
    ev.update(extra)
    out(ev)


def main():
    argv = sys.argv[1:]
    opts, flags, pos = parse(argv)
    log = os.environ.get("FAKE_CLAUDE_LOG")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps({
                "argv": argv, "cwd": os.getcwd(), "pid": os.getpid(), "t": time.time(),
                "env": {"MAX_THINKING_TOKENS": os.environ.get("MAX_THINKING_TOKENS"),
                        "DISABLE_AUTOUPDATER": os.environ.get("DISABLE_AUTOUPDATER"),
                        "ROOK_TURN_TOKEN": os.environ.get("ROOK_TURN_TOKEN"),
                        "has_link_token": "ROOK_AGENTS_LINK_TOKEN" in os.environ,
                        "has_api_key": "ANTHROPIC_API_KEY" in os.environ}}) + "\n")
    die = os.environ.get("FAKE_CLAUDE_DIE")
    if die and os.path.exists(die):
        sys.stderr.write("fake: startup failure\n")
        sys.exit(2)
    model = opts.get("--model") or "fake-model"
    if opts.get("--input-format") == "stream-json":
        return stream_mode(opts, flags, model)
    prompt = " ".join(pos) if pos else sys.stdin.read()
    sess = Session(opts, flags)
    seen = len(sess.msgs)
    t0 = time.monotonic()
    if opts.get("--output-format") == "json":
        q = prompt.strip().splitlines()[-1] if prompt.strip() else ""
        out({"type": "result", "subtype": "success", "is_error": False,
             "result": f"btw answer from {seen} messages: {q}", "session_id": sess.id,
             "num_turns": 1, "usage": result_usage(sess)})
        return 0
    out({"type": "system", "subtype": "init", "session_id": sess.id, "model": model})
    sess.add("user", prompt)
    m = re.search(r"slowfork ([\d.]+)", prompt)
    if m:
        time.sleep(float(m.group(1)))
    if "failfork" in prompt:
        result(sess, "", t0, ok=False, num_turns=1)
        return 1
    stream_reply(sess, f"fork did: {prompt.strip()} after {seen} messages", False, model)
    result(sess, f"fork did: {prompt.strip()} after {seen} messages", t0)
    return 0


def stream_mode(opts, flags, model):
    sess = Session(opts, flags)
    partial = "--include-partial-messages" in flags
    inbox = queue.Queue()
    interrupt = threading.Event()

    def reader():
        for line in sys.stdin:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("type") == "control_request":
                if (obj.get("request") or {}).get("subtype") == "interrupt":
                    interrupt.set()
                out({"type": "control_response", "response": {
                    "subtype": "success", "request_id": obj.get("request_id"), "response": {}}})
            elif obj.get("type") == "user":
                inbox.put(str((obj.get("message") or {}).get("content") or ""))
        inbox.put(None)

    threading.Thread(target=reader, daemon=True).start()

    def wait(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if interrupt.is_set():
                return False
            time.sleep(0.01)
        return not interrupt.is_set()

    def interrupted(t0, tool_id=None):
        interrupt.clear()
        if tool_id:
            out({"type": "user", "parent_tool_use_id": None, "session_id": sess.id,
                 "message": {"role": "user", "content": [{
                     "type": "tool_result", "tool_use_id": tool_id, "is_error": True,
                     "content": "The user doesn't want to proceed with this tool use."}]}})
        result(sess, "", t0, ok=False, num_turns=2,
               errors=["[ede_diagnostic] result_type=user"])

    while True:
        text = inbox.get()
        if text is None:
            return 0
        interrupt.clear()
        t0 = time.monotonic()
        out({"type": "system", "subtype": "init", "session_id": sess.id, "model": model,
             "cwd": os.getcwd(), "tools": (opts.get("--tools") or "").split(",")})
        if text.startswith("/compact"):
            pre = sess.context()
            out({"type": "system", "subtype": "status", "status": "compacting",
                 "session_id": sess.id})
            sess.compact()
            out({"type": "system", "subtype": "compact_boundary", "session_id": sess.id,
                 "compact_metadata": {"trigger": "manual", "pre_tokens": pre,
                                      "post_tokens": sess.context()}})
            out({"type": "result", "subtype": "success", "is_error": False, "result": "",
                 "num_turns": 0, "local_command": "compact", "session_id": sess.id,
                 "usage": result_usage(sess), "duration_ms": 1})
            continue
        sess.add("user", text)
        if "crash" in text:
            sys.stderr.write("fake: boom\n")
            sys.stderr.flush()
            os._exit(3)
        m = re.search(r"slowtool ([\d.]+)", text)
        if m:
            tool_id = "toolu_" + uuid.uuid4().hex[:8]
            am = sess.add("assistant", "(tool)")
            out({"type": "assistant", "uuid": am["uuid"], "parent_tool_use_id": None,
                 "session_id": sess.id,
                 "message": {"id": "msg_t", "model": model, "role": "assistant", "usage":
                             usage(sess, 0), "content": [{"type": "tool_use", "id": tool_id,
                                                          "name": "mcp__rook__wait",
                                                          "input": {}}]}})
            if not wait(float(m.group(1))):
                interrupted(t0, tool_id)
                continue
            folded = []
            while True:
                try:
                    nxt = inbox.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    inbox.put(None)
                    break
                folded.append(nxt)
                sess.add("user", nxt)
            reply = "done waiting" + (f"; folded: {' | '.join(folded)}" if folded else "")
        else:
            m = re.search(r"pause ([\d.]+)", text)
            if m and not wait(float(m.group(1))):
                interrupted(t0)
                continue
            reply = "echo: " + text.strip().splitlines()[-1]
        stream_reply(sess, reply, partial, model)
        result(sess, reply, t0)


if __name__ == "__main__":
    sys.exit(main())
