"""Jobs survive audio interruption and disconnect; their outcomes are always read."""
import asyncio
import json
from .thinking import AGENT_TOOLS, ThinkingAgent, UncertainToolOutcome


class Jobs:
    def __init__(self, store, direct, acp_host, acp_port, notify, read_timeout=45, agent_timeout=600, agent=None):
        self.store, self.direct, self.notify = store, direct, notify
        self.host, self.port = acp_host, acp_port
        self.read_timeout, self.agent_timeout = read_timeout, agent_timeout
        self.agent = agent
        self.tasks = {}

    def timeout(self, name):
        return self.agent_timeout if self.agent is not None or name in AGENT_TOOLS else self.read_timeout

    def start(self, session, name, args, context):
        if sum(j["status"] == "running" for j in self.store.jobs(session)) >= 4 or len(self.tasks) >= 32:
            raise RuntimeError("Too many running jobs; wait for one to finish")
        jid = self.store.create_job(session, name, args)
        task = asyncio.create_task(self._run(session, jid, name, args, context))
        self.tasks[jid] = task
        task.add_done_callback(lambda t: (self.tasks.pop(jid, None), t.exception() if not t.cancelled() else None))
        return jid

    async def _run(self, session, jid, name, args, context):
        thinking = self.agent is not None or name in AGENT_TOOLS
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
                result = await asyncio.wait_for(agent.run(task, context, on_event,
                    initial=None if name in AGENT_TOOLS else {'name': name, 'arguments': args}), self.agent_timeout)
            elif name in self.direct:
                result = await asyncio.wait_for(self.direct[name](args), self.read_timeout)
            else:
                raise ValueError("Unsupported tool")
            status = "completed"
        except asyncio.CancelledError:
            status, result = "cancel_requested", "Cancellation requested. External changes may already have occurred."
        except (TimeoutError, ConnectionError) as error:
            status = "unknown" if thinking or isinstance(error, UncertainToolOutcome) else "failed"
            result = "The tool timed out or disconnected: " + (str(error) or type(error).__name__) + ". Check external state before retrying changes."
        except Exception as error:
            result = "Tool failed: " + (str(error) or type(error).__name__) + ". No success was confirmed."
        finally:
            if status != 'completed':
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
