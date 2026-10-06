"""Jobs survive audio interruption and disconnect; their outcomes are always read."""
import asyncio
import json
import os
import re
from .identity import PolicyRefusal, current_identity
from .thinking import AGENT_TOOLS, READONLY_TOOLS, ThinkingAgent, UncertainToolOutcome

# Anything but an owner key gets short, few jobs so guests cannot starve owners.
GUEST_JOB_TIMEOUT = 60
GUEST_JOBS_PER_SESSION = 2
GUEST_JOBS_TOTAL = 8
GUEST_FAILURE = "That didn't work."
ERROR_LIMIT = 400

_SECRETS = [
    (re.compile(r"(?i)\b([a-z0-9_]*(?:api[_-]?key|token|secret|password|passwd|pwd|bearer|authorization))\b"
                r"(\s*[:=]\s*|\s+)(['\"]?)([^\s'\"]{6,})\3"), r"\1\2\3[redacted]\3"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[redacted]"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"), "[redacted]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "[redacted]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"), "[redacted]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "[redacted]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[redacted]"),
]


def scrub(text):
    """An owner's readable error: this service's credentials and known secret
    shapes removed, truncated."""
    text = str(text)
    for name in ('ROOK_MCP_TOKEN', 'LLM_PRIMARY_API_KEY', 'VOICE_TOKEN'):
        value = os.environ.get(name, '')
        if len(value) >= 6:
            text = text.replace(value, '[redacted]')
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text if len(text) <= ERROR_LIMIT else text[:ERROR_LIMIT] + '...'


class Jobs:
    def __init__(self, store, direct, acp_host, acp_port, notify, read_timeout=45, agent_timeout=600, agent=None):
        self.store, self.direct, self.notify = store, direct, notify
        self.host, self.port = acp_host, acp_port
        self.read_timeout, self.agent_timeout = read_timeout, agent_timeout
        self.agent = agent
        self.tasks = {}
        self.guest_tasks = {}

    def timeout(self, name, owner=None):
        owner = current_identity.get().owner if owner is None else owner
        timeout = self.agent_timeout if self.agent is not None or name in AGENT_TOOLS else self.read_timeout
        return timeout if owner else min(timeout, GUEST_JOB_TIMEOUT)

    def start(self, session, name, args, context):
        owner = current_identity.get().owner
        if sum(j["status"] == "running" for j in self.store.jobs(session)) >= 4 or len(self.tasks) >= 32:
            raise RuntimeError("Too many running jobs; wait for one to finish")
        if not owner and (sum(s == session for s in self.guest_tasks.values()) >= GUEST_JOBS_PER_SESSION
                          or len(self.guest_tasks) >= GUEST_JOBS_TOTAL):
            raise RuntimeError("Too many running jobs; wait for one to finish")
        jid = self.store.create_job(session, name, args)
        task = asyncio.create_task(self._run(session, jid, name, args, context))
        self.tasks[jid] = task
        if not owner:
            self.guest_tasks[jid] = session
        task.add_done_callback(lambda t: (self.tasks.pop(jid, None), self.guest_tasks.pop(jid, None),
                                          t.exception() if not t.cancelled() else None))
        return jid

    async def _run(self, session, jid, name, args, context):
        owner = current_identity.get().owner
        thinking = self.agent is not None or name in AGENT_TOOLS
        timeout = self.timeout(name, owner)
        result, status = "", "failed"
        try:
            if thinking:
                if name not in self.direct and name not in AGENT_TOOLS:
                    raise ValueError('Unsupported tool')
                agent = self.agent or ThinkingAgent(direct=self.direct)
                def on_event(update):
                    if update.get('trace') is not None:
                        self.store.record_job_progress(jid, json.dumps(update['trace']))
                    if update.get('progress'):
                        self.notify(session, {'type': 'tool', 'id': jid, 'status': 'running',
                            'title': 'Thinking', 'progress': str(update['progress'])[:240]})
                task = args.get('task', '') if name in AGENT_TOOLS else (
                    'Perform the requested ' + name + ' lookup and report its actual result. ' + json.dumps(args))
                # Only an owner's escalation gets the full, mutating toolset. A lookup
                # job reads untrusted text (web pages, files, messages) and must not be
                # steerable into changes, so it runs with read-only tools.
                tools = None if owner and name in AGENT_TOOLS else READONLY_TOOLS
                result = await asyncio.wait_for(agent.run(task, context, on_event,
                    initial=None if name in AGENT_TOOLS else {'name': name, 'arguments': args}, tools=tools), timeout)
            elif name in self.direct:
                result = await asyncio.wait_for(self.direct[name](args), timeout)
            else:
                raise ValueError("Unsupported tool")
            status = "completed"
        except asyncio.CancelledError:
            status, result = "cancel_requested", "Cancellation requested. External changes may already have occurred."
        except (TimeoutError, ConnectionError) as error:
            status = "unknown" if thinking or isinstance(error, UncertainToolOutcome) else "failed"
            detail = ": " + scrub(str(error) or type(error).__name__) if owner else ""
            result = "The tool timed out or disconnected" + detail + ". Check external state before retrying changes."
        except PolicyRefusal as error:
            result = str(error)
        except Exception as error:
            # Raw error text can carry worker output, paths or credentials. Only an
            # owner gets it (scrubbed); everyone else hears a generic failure.
            result = ("Tool failed: " + scrub(str(error) or type(error).__name__) + ". No success was confirmed."
                      if owner else GUEST_FAILURE)
        finally:
            if status != 'completed' and owner:
                progress = self.store.job(jid, session).get('result', '')
                if progress:
                    result += '\nRecorded tool progress: ' + progress
            result = str(result)[:16000]
            self.store.finish_job(jid, status, result)
            self.store.append(session, "tool", {"id": jid, "name": name, "args": args,
                                                "result": json.dumps({"status": status, "result": result})})
            self.notify(session, {"type": "tool", "id": jid, "title": name, "status": status,
                                  "result": result})

    def cancel(self, session, jid):
        if not self.store.job(jid, session):
            raise ValueError("No such job in this conversation")
        task = self.tasks.get(jid)
        if task:
            task.cancel()
        return "Cancellation requested; completed external actions are not undone."

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
