"""Rook agent — the main brain with 3-tier memory kernel."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_TOOLCALL_RE = re.compile(r"</?tool_call>.*?(?:</tool_call>)?\s*", re.DOTALL)
_JSON_BLOB_RE = re.compile(r'^\s*\{["\'](?:name|type|function).*?\}\s*$', re.DOTALL | re.MULTILINE)

from .config import Config
from .router import Router
from ..tools.registry import ToolRegistry
from ..tools.base import ToolResult
from ..memory.compiler import compile_system_prompt
from ..memory.extractor import FactExtractor
from ..memory.curator import ContextCurator
from ..modules.loader import ModuleLoader
from .pipeline import PipelineConfig

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 15


class Conversation:
    """A single conversation thread with message history."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.messages: list[dict[str, Any]] = []

    def set_system(self, content: str) -> None:
        """Set or update the system prompt (always first message)."""
        if self.messages and self.messages[0]["role"] == "system":
            self.messages[0]["content"] = content
        else:
            self.messages.insert(0, {"role": "system", "content": content})

    def add_user(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})

    def add_assistant(self, content: str | None = None, tool_calls: list | None = None) -> None:
        msg: dict[str, Any] = {"role": "assistant"}
        if content:
            msg["content"] = content
        if tool_calls:
            msg["tool_calls"] = tool_calls
        self.messages.append(msg)

    def add_tool_result(self, tool_call_id: str, name: str, content: str) -> None:
        self.messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": name,
            "content": content,
        })

    @staticmethod
    def _estimate_tokens(msg: dict) -> int:
        content = msg.get("content", "")
        if isinstance(content, list):
            content = str(content)
        tokens = len(str(content)) / 3.5
        if msg.get("tool_calls"):
            tokens += len(str(msg["tool_calls"])) / 3.5
        return int(tokens)

    def conversation_tokens(self) -> int:
        """Total tokens in non-system messages."""
        return sum(
            self._estimate_tokens(m)
            for m in self.messages
            if m["role"] != "system"
        )

    def conversation_count(self) -> int:
        """Count of non-system messages."""
        return sum(1 for m in self.messages if m["role"] != "system")

    def trim(self, max_tokens: int = 128000) -> None:
        """Sliding window: keep system prompt + most recent messages that fit.

        Preserves tool_use/tool_result pairs — never splits them.
        Also ensures conversation starts with a user message (Anthropic requirement).
        """
        system = [m for m in self.messages if m["role"] == "system"]
        rest = [m for m in self.messages if m["role"] != "system"]

        system_tokens = sum(self._estimate_tokens(m) for m in system)
        budget = max_tokens - system_tokens

        # Walk backwards, keeping messages
        kept = []
        used = 0
        for msg in reversed(rest):
            t = self._estimate_tokens(msg)
            if used + t > budget:
                break
            kept.append(msg)
            used += t

        kept.reverse()

        # Fix broken tool pairs: if we have a tool_result without its tool_use,
        # or a tool_use assistant message without its tool_results, drop them
        kept = self._fix_tool_pairs(kept)

        # Ensure first message is a user message (Anthropic requirement)
        while kept and kept[0]["role"] not in ("user",):
            kept.pop(0)

        self.messages = system + kept

    @staticmethod
    def _fix_tool_pairs(messages: list[dict]) -> list[dict]:
        """Remove orphaned tool_use and tool_result messages."""
        # Collect all tool_call IDs from assistant messages
        tool_use_ids = set()
        for m in messages:
            if m["role"] == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    tc_id = tc.get("id") or tc.get("function", {}).get("id", "")
                    if tc_id:
                        tool_use_ids.add(tc_id)

        # Collect all tool_result IDs
        tool_result_ids = set()
        for m in messages:
            if m["role"] == "tool":
                tc_id = m.get("tool_call_id", "")
                if tc_id:
                    tool_result_ids.add(tc_id)

        # Find which IDs have both halves
        complete_ids = tool_use_ids & tool_result_ids

        # Filter: keep non-tool messages, and tool messages only if their pair is complete
        fixed = []
        for m in messages:
            if m["role"] == "tool":
                if m.get("tool_call_id", "") in complete_ids:
                    fixed.append(m)
            elif m["role"] == "assistant" and m.get("tool_calls"):
                # Keep if at least one tool call has a matching result
                has_match = any(
                    (tc.get("id") or tc.get("function", {}).get("id", "")) in complete_ids
                    for tc in m["tool_calls"]
                )
                if has_match:
                    # Filter to only matched tool calls
                    m = dict(m)
                    m["tool_calls"] = [
                        tc for tc in m["tool_calls"]
                        if (tc.get("id") or tc.get("function", {}).get("id", "")) in complete_ids
                    ]
                    fixed.append(m)
                elif m.get("content"):
                    # Keep as text-only if it had content
                    m = dict(m)
                    del m["tool_calls"]
                    fixed.append(m)
            else:
                fixed.append(m)

        return fixed

    def last_user_message(self) -> str:
        """Get the most recent user message content."""
        for msg in reversed(self.messages):
            if msg["role"] == "user":
                return msg.get("content", "")
        return ""

    def last_assistant_message(self) -> str:
        """Get the most recent assistant text response."""
        for msg in reversed(self.messages):
            if msg["role"] == "assistant" and msg.get("content"):
                return msg["content"]
        return ""


class Agent:
    """Core agent with 3-tier memory kernel."""

    def __init__(self, config: Config):
        self.config = config
        self.router = Router(config)
        self.tools = ToolRegistry(
            searxng_url=config.get("search.url", "https://searxng.bake.systems"),
            sqlite_path=config.get("memory.sqlite_path", "./data/rook.db"),
            graph_path=config.get("memory.graph_path", "./data/knowledge"),
            tier_size=config.get("memory.tier_size", 8000),
            promote_threshold=config.get("memory.promote_threshold", 3),
            concrete_threshold=config.get("memory.concrete_threshold", 6),
            remote_port=config.get("remote.port", 7005),
            remote_auth_token=config.get("remote.psk", ""),
            remote_domain=config.get("remote.domain", "rook.bake.systems"),
            remote_web_user=config.get("remote.web_user", ""),
            remote_web_pass=config.get("remote.web_pass", ""),
        )
        self.fact_store = self.tools.fact_store
        self.scheduler = self.tools.scheduler
        self.agent_pool = self.tools.agent_pool
        self.module_loader = ModuleLoader()
        self.conversations: dict[str, Conversation] = {}
        self.bot_name: str = "Rook"
        self.peers: list[str] = []
        self._notify_callback = None  # set by Discord interface
        self._tool_notify: dict[str, Any] = {}  # session_id -> async callback(msg)

        # Pipeline config — per-stage model selection, persisted to DB
        self.pipeline = PipelineConfig.from_config(config, db=self.tools.memory_store._db)

        # Set the global model from pipeline (persisted)
        if self.pipeline.main.model:
            self.router._global_model = self.pipeline.main.model
            log.info("Pipeline main model: %s", self.pipeline.main.model)

        # Extractor and curator use the router — works with any model type
        self.extractor = FactExtractor(
            router=self.router,
            model_name=self.pipeline.post_context.model,
            fact_store=self.fact_store,
        )
        self.curator = ContextCurator(
            router=self.router,
            model_name=self.pipeline.pre_context.model,
        )

        # Wire scheduler handler
        self.scheduler.set_handler(self._run_scheduled_job)

        # Wire agent pool handler + completion callback
        self.agent_pool.set_handler(self._run_sub_agent)
        self.agent_pool.set_on_complete(self._on_agent_complete)

    async def _run_scheduled_job(self, prompt: str, session_id: str, notify_channel: str | None) -> str:
        """Execute a scheduled job and optionally notify a Discord channel."""
        response = await self.handle_message(prompt, session_id=session_id)
        if notify_channel and self._notify_callback:
            try:
                await self._notify_callback(notify_channel, response)
            except Exception as e:
                log.error("Failed to notify channel %s: %s", notify_channel, e)
        return response

    async def _run_sub_agent(self, prompt: str, session_id: str) -> str:
        """Execute a sub-agent prompt in its own conversation."""
        # Set the agent model for this session
        agent_model = self.pipeline.agents.model
        if agent_model:
            self.router.set_active(session_id, agent_model)
        return await self.handle_message(prompt, session_id=session_id)

    def _inherit_notify(self, parent_session_id: str, child_session_id: str) -> None:
        """Copy tool notify callback from parent to child session."""
        cb = self._tool_notify.get(parent_session_id)
        if cb:
            self._tool_notify[child_session_id] = cb

    async def _on_agent_complete(self, sub_agent) -> None:
        """Called when a sub-agent finishes. Feeds results back through Rook to synthesize."""
        if not sub_agent.notify_channel or not self._notify_callback:
            return

        elapsed = (sub_agent.completed_at or 0) - sub_agent.started_at
        session_id = f"discord:{sub_agent.notify_channel}"

        if sub_agent.status == "completed":
            # Feed the agent's result back to Rook as a message, let Rook decide what to say
            synthesis_prompt = (
                f"[SYSTEM: Your sub-agent '{sub_agent.name}' just finished ({elapsed:.0f}s). "
                f"Here are its findings:\n\n{sub_agent.result}\n\n"
                f"Summarize the key findings and tell bake what's relevant. Be concise.]"
            )
        else:
            synthesis_prompt = (
                f"[SYSTEM: Your sub-agent '{sub_agent.name}' failed after {elapsed:.0f}s. "
                f"Error: {sub_agent.error}\n\nLet bake know briefly.]"
            )

        try:
            response = await self.handle_message(synthesis_prompt, session_id=session_id)
            if response:
                await self._notify_callback(sub_agent.notify_channel, response)
        except Exception as e:
            log.error("Agent synthesis failed: %s", e)

    async def start_services(self) -> None:
        """Start all modules. Call after event loop is running."""
        # Register Discord channel sender on the bridge
        bridge = self.tools.channel_bridge

        async def discord_send(channel_id: str, message: str) -> None:
            if self._notify_callback:
                await self._notify_callback(channel_id, message)
        bridge.register_sender("discord", discord_send)

        # Register module management tools
        from ..tools.modules import ListModulesTool, CreateModuleTool
        self.tools.register(ListModulesTool(self.module_loader))
        self.tools.register(CreateModuleTool(self.module_loader, self))

        # Load and start all modules
        await self.module_loader.load_all(self, self.config)
        log.info("All services started")

    def _on_worker_connect(self, name: str, platform: str, hostname: str, worker_id: str) -> None:
        """Register a remote worker as a communication channel."""
        self.tools.memory_store.register_channel(
            platform="worker",
            platform_id=name,  # use name, not UUID — prevents duplicates on reconnect
            session_id=f"worker:{name}",
            name=f"{name} ({hostname})",
            modality="shell",
        )
        log.info("Worker registered as channel: %s (%s/%s)", name, platform, hostname)

    def _on_worker_disconnect(self, name: str, worker_id: str) -> None:
        """Clean up channel on disconnect. Ephemeral channels (-cli) get deleted."""
        if name.endswith("-cli"):
            # CLI sessions are ephemeral — remove on disconnect
            self.tools.memory_store._db.execute(
                "DELETE FROM channels WHERE platform = ? AND platform_id = ?",
                ("worker", name),
            )
            self.tools.memory_store._db.commit()
            log.info("Worker channel removed (ephemeral): %s", name)
        else:
            self.tools.memory_store.touch_channel("worker", name)
            log.info("Worker channel disconnected: %s", name)

    async def _on_worker_chat(self, worker_name: str, content: str, worker_id: str) -> str:
        """Handle a chat message from a remote worker."""
        session_id = f"worker:{worker_name}"
        self.tools.memory_store.touch_channel("worker", worker_id)

        # Register tool notify for this worker session
        async def tool_notify(msg: str) -> None:
            worker = self.tools.remote_server.get_worker(worker_name)
            if worker and not worker.ws.closed:
                await worker.ws.send_json({"type": "tool_status", "content": msg})

        self._tool_notify[session_id] = tool_notify
        return await self.handle_message(content, session_id=session_id)

    async def _notify_tool(self, session_id: str, tool_name: str, args: dict) -> None:
        """Send a brief tool call notification to the originating channel."""
        cb = self._tool_notify.get(session_id)
        if not cb:
            return
        # Build a short summary
        summary = tool_name
        if tool_name == "spawn_agent":
            summary = f"♖ spawning agent: {args.get('name', '?')}"
        elif tool_name == "remote_exec":
            cmd = args.get("command", "")[:50]
            summary = f"♖ remote_exec on {args.get('worker', '?')}: {cmd}"
        elif tool_name == "web_search":
            summary = f"♖ searching: {args.get('query', '?')[:50]}"
        elif tool_name == "shell":
            summary = f"♖ shell: {args.get('command', '?')[:50]}"
        elif tool_name == "send_message":
            summary = f"♖ sending to {args.get('platform', '?')}:{args.get('channel', '?')[:10]}"
        elif tool_name == "remember":
            summary = f"♖ remembering: {args.get('key', '?')}"
        elif tool_name == "recall":
            summary = f"♖ recalling: {args.get('search', '?')}"
        elif tool_name == "schedule_job":
            summary = f"♖ scheduling: {args.get('name', '?')}"
        elif tool_name == "remote_update":
            summary = f"♖ updating worker: {args.get('worker', '?')}"
        elif tool_name.startswith("terminal_"):
            summary = f"♖ {tool_name}: {args.get('name', args.get('command', '?'))[:40]}"
        elif tool_name.startswith("memory_") or tool_name.startswith("graph_"):
            summary = f"♖ {tool_name}"
        else:
            summary = f"♖ {tool_name}"
        try:
            await cb(summary)
        except Exception:
            pass

    def update_pipeline(self, stage: str, **kwargs) -> str:
        """Update pipeline config at runtime."""
        result = self.pipeline.update(stage, **kwargs)

        if "model" in kwargs:
            if stage == "pre_context":
                self.curator.model_name = self.pipeline.pre_context.model
            elif stage == "post_context":
                self.extractor.model_name = self.pipeline.post_context.model
            elif stage == "main":
                self.router._global_model = self.pipeline.main.model
                log.info("Main model set globally: %s", self.pipeline.main.model)

        return result

    def set_identity(self, bot_name: str, peers: list[str] | None = None) -> None:
        self.bot_name = bot_name
        self.peers = peers or []
        log.info("Identity set: %s (peers: %s)", bot_name, self.peers)

    def _compile_system_prompt(self, conv: Conversation, session_id: str,
                               curated_facts: dict | None = None) -> str:
        """Build the system prompt with current memory state."""
        ctx_len = self.router.get_active(session_id).context_length
        return compile_system_prompt(
            bot_name=self.bot_name,
            fact_store=self.fact_store,
            conversation_tokens=conv.conversation_tokens(),
            conversation_messages=conv.conversation_count(),
            context_length=ctx_len,
            peers=self.peers if self.peers else None,
            session_id=session_id,
            recent_job_results=self.scheduler.recent_results(),
            recent_agent_results=self.agent_pool.recent_completed(),
            active_channels=self.tools.memory_store.list_channels(),
            anthropic_quota=self.router._anthropic_quota,
            active_goals=self.tools.goal_store.render_active(),
            curated_facts=curated_facts,
            pipeline_config=self.pipeline.to_dict(),
        )

    def get_conversation(self, session_id: str) -> Conversation:
        if session_id not in self.conversations:
            self.conversations[session_id] = Conversation(session_id)
        return self.conversations[session_id]

    async def handle_message(
        self,
        text: str,
        session_id: str = "default",
        **context: Any,
    ) -> str:
        """Process a user message and return the response.

        Main conversations delegate tool work to sub-agents.
        Sub-agent sessions (agent:*) run tools directly.
        """
        # Check for model switch — updates pipeline (persisted)
        if switch_to := self.router.detect_switch(text):
            entry = self.router.set_active(session_id, switch_to)
            if entry:
                self.pipeline.update("main", model=entry.name)
                switch_msg = f"Switched to **{entry.name}** ({entry.model})."
                stripped = text.strip()
                if re.match(r"^(use|switch\s+to|swap\s+to)\s+\S+[.!?]?$", stripped, re.I):
                    return switch_msg
                text = re.sub(
                    r"\b(?:use|switch\s+to|swap\s+to|change\s+to)\s+\S+[,.]?\s*",
                    "", text, flags=re.I,
                ).strip()
                if not text:
                    return switch_msg

        # Sub-agent sessions run tools directly (no delegation)
        is_sub_agent = session_id.startswith("agent:")
        if is_sub_agent:
            return await self._handle_direct(text, session_id)

        # Main conversations: delegate to a sub-agent for tool work
        return await self._handle_delegated(text, session_id)

    async def _handle_direct(self, text: str, session_id: str) -> str:
        """Direct mode — run tools inline. Used by sub-agents."""
        conv = self.get_conversation(session_id)

        self.fact_store.scan_for_references(text)
        system_prompt = self._compile_system_prompt(conv, session_id)
        conv.set_system(system_prompt)
        conv.add_user(text)

        response_text = await self._agent_loop(conv, session_id)

        ctx_len = self.router.get_active(session_id).context_length
        conv.trim(max_tokens=ctx_len)
        return response_text

    async def _handle_delegated(self, text: str, session_id: str) -> str:
        """Main conversation handler — full tool loop with notifications and goal self-stimulation."""
        conv = self.get_conversation(session_id)

        self.fact_store.scan_for_references(text)

        # Pre-context stage: curate memory if enabled
        curated = None
        if self.pipeline.pre_context.enabled:
            try:
                curated = await self.curator.curate(text, self.fact_store)
                log.info("Pre-context: curated %d/%d facts",
                         sum(len(v) for v in curated.values()),
                         len(self.fact_store.concrete) + len(self.fact_store.working) + len(self.fact_store.volatile))
            except Exception as e:
                log.error("Pre-context curation failed: %s", e)

        system_prompt = self._compile_system_prompt(conv, session_id, curated_facts=curated)
        conv.set_system(system_prompt)
        conv.add_user(text)

        # Log user message
        self.fact_store.log_conversation(session_id, "user", text)

        # Run the full agent loop with tool notifications
        response_text = await self._agent_loop(conv, session_id)

        # Log and maintain
        self.fact_store.log_conversation(session_id, "assistant", response_text)
        asyncio.create_task(self._maintenance(
            conv.last_user_message(), response_text, session_id,
        ))

        ctx_len = self.router.get_active(session_id).context_length
        conv.trim(max_tokens=ctx_len)

        # Self-stimulation: if there's an active goal with unchecked steps, keep going
        asyncio.create_task(self._goal_loop(session_id))

        return response_text

    async def _goal_loop(self, session_id: str) -> None:
        """Self-stimulation loop — keeps working toward active goals."""
        max_auto_rounds = 10  # safety limit

        for _ in range(max_auto_rounds):
            await asyncio.sleep(2)  # brief pause between rounds

            goal = self.tools.goal_store.get_active()
            if not goal or goal.is_complete or goal.status != "active":
                return

            next_step = goal.next_step
            if not next_step:
                return

            log.info("Goal self-stim: [%s] next step: %s", goal.id, next_step.description[:60])

            # Inject continuation prompt
            stimulus = (
                f"[SYSTEM: Continue working on your goal. "
                f"Next step: {next_step.description}. "
                f"Do it now, then call complete_step with the result.]"
            )

            try:
                response = await self.handle_message(stimulus, session_id=session_id)
                # Notify the channel
                if self._notify_callback and session_id.startswith("discord:"):
                    channel_id = session_id.split(":", 1)[1]
                    if response:
                        await self._notify_callback(channel_id, response)
                elif session_id.startswith("worker:"):
                    worker_name = session_id.split(":", 1)[1]
                    worker = self.tools.remote_server.get_worker(worker_name)
                    if worker and not worker.ws.closed and response:
                        await worker.ws.send_json({"type": "chat_response", "content": response})
            except Exception as e:
                log.error("Goal self-stim failed: %s", e)
                return

            # Check if goal was completed or paused during this round
            goal = self.tools.goal_store.get_active()
            if not goal or goal.status != "active":
                return

    async def _maintenance(self, user_msg: str, assistant_msg: str, session_id: str) -> None:
        """Background maintenance cycle — runs after response is sent."""
        try:
            # Post-context stage: extract facts if enabled
            if self.pipeline.post_context.enabled:
                await self.extractor.extract_and_store(user_msg, assistant_msg)

            # Scan assistant response for references to bump access counts
            self.fact_store.scan_for_references(assistant_msg)

            # Periodic channel cleanup (every maintenance cycle is fine, it's cheap)
            stale = self.tools.memory_store.cleanup_stale_channels(max_age_hours=24)
            if stale:
                log.info("Cleaned %d stale channels", stale)

            # Check for auto-promotions
            self.fact_store.check_promotions()

            # Flush all tiers to disk
            self.fact_store.flush_to_db()

            log.debug("Maintenance cycle complete")
        except Exception as e:
            log.error("Maintenance cycle failed: %s", e)

    async def _agent_loop(self, conv: Conversation, session_id: str) -> str:
        """Run the LLM with tool calling until it produces a text response."""

        for round_num in range(MAX_TOOL_ROUNDS):
            try:
                log.info("Agent loop round %d for session %s", round_num, session_id[:20])
                response = await asyncio.wait_for(
                    self.router.chat_with_tools(
                        messages=conv.messages,
                        tools=self.tools.openai_tools(),
                        session_id=session_id,
                    ),
                    timeout=180,
                )
            except asyncio.TimeoutError:
                entry = self.router.get_active(session_id)
                log.error("LLM call timed out (round %d, model=%s)", round_num, entry.name)
                return "The model took too long to respond. Try a simpler request or switch models."
            except Exception as e:
                entry = self.router.get_active(session_id)
                log.error("LLM call failed (round %d): %s", round_num, e)
                return f"Error calling {entry.name} ({entry.endpoint}): {e}"

            if response.get("tool_calls"):
                tool_calls = response["tool_calls"]

                conv.add_assistant(
                    content=response.get("content"),
                    tool_calls=[
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc["arguments"]) if isinstance(tc["arguments"], dict) else tc["arguments"],
                            },
                        }
                        for tc in tool_calls
                    ],
                )

                for tc in tool_calls:
                    name = tc["name"]
                    args = tc["arguments"]
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}

                    log.info("Tool call [%s]: %s(%s)", tc["id"][:8], name, args)
                    # Fire-and-forget notification (don't let it stall the loop)
                    asyncio.create_task(self._notify_tool(session_id, name, args))
                    # Execute tool with timeout
                    try:
                        result = await asyncio.wait_for(
                            self.tools.execute(name, args),
                            timeout=120,
                        )
                    except asyncio.TimeoutError:
                        log.error("Tool '%s' timed out after 120s", name)
                        result = ToolResult(success=False, output="", error=f"Tool '{name}' timed out")

                    output = result.output
                    if result.error:
                        output = f"ERROR: {result.error}\n{output}" if output else f"ERROR: {result.error}"

                    conv.add_tool_result(tc["id"], name, output)

                continue

            # Text response
            log.info("Agent loop round %d: text response received", round_num)
            text = response.get("content") or ""
            text = _JSON_BLOB_RE.sub("", _TOOLCALL_RE.sub("", _THINK_RE.sub("", text))).strip()
            conv.add_assistant(content=text)
            ctx_len = self.router.get_active(session_id).context_length
            conv.trim(max_tokens=ctx_len)
            return text

        # Tool loop limit
        log.warning("Tool loop limit reached (%d rounds), forcing text response", MAX_TOOL_ROUNDS)
        conv.add_user("You have enough information now. Summarize what you found and respond to the user. Do NOT call any more tools.")
        try:
            response = await self.router.chat_with_tools(
                messages=conv.messages,
                tools=None,
                session_id=session_id,
            )
            text = response.get("content") or "Sorry, I couldn't complete that request."
            text = _JSON_BLOB_RE.sub("", _TOOLCALL_RE.sub("", _THINK_RE.sub("", text))).strip()
            conv.add_assistant(content=text)
            ctx_len = self.router.get_active(session_id).context_length
            conv.trim(max_tokens=ctx_len)
            return text
        except Exception as e:
            log.error("Forced response failed: %s", e)
            return "I gathered some information but couldn't summarize it. Try a more specific question."
