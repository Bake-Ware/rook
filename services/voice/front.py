"""Front: the fast voice of the front_background pipeline.

One streaming chat completion per turn to the local model (VLLM_URL /
VLLM_MODEL), with no tools and thinking disabled. The prompt is an append-only
transcript (:class:`Transcript`): a fixed block (persona, mode, rules) that
never changes within a conversation, then every exchange as it happened, with
new board facts riding in the user message that follows them. The kaiju model
(Qwen3-Next on Strata, a hybrid recurrent model) reuses its cache only when a
prompt extends the previous prompt exactly; any edit earlier in the prompt
costs a full prefill (about 600 tokens a second there, 2026-10-23).
"""
import contextlib
import json
import os

import httpx

from . import providers
from .modes import MODES
from .policy import capability_summary

SPOKEN_MARK = '[Spoken response generated; playback may be interrupted]'
SILENT_RECORD = '[Stayed silent: speech was not addressed to the assistant]'
BOARD_HEADER = '\n\nKnown facts (current; you may state these):\n'

RULES = (
    'Everything you say is spoken aloud: one or two short sentences, plain words, no markdown, lists or emoji.\n'
    'You are the one doing the work. Speak in the first person about it ("Let me check your calendar.").\n'
    'Only state facts that are common knowledge, already said in this conversation, or listed under Known '
    'facts. Never invent a result, time, number, status, event or message.\n'
    'Answer general knowledge, explanations, ideas, jokes and small talk yourself, right away.\n'
    'If the request needs live or personal information (weather, news, prices, the user\'s calendar, mail, '
    'devices, home or tasks) or an action, say one short natural acknowledgement (for example '
    '"Let me check." or "On it.") and stop there. Do not guess the result and do not promise a time; the '
    'result comes to you separately and you will tell the user then.\n'
    'If Known facts already answer the request, answer directly from them.\n'
    'Call a task, room, job or other record by its three-word name (say "brave otter lamp"), never by its id.\n'
    'Never say that something was done, set, sent, booked, turned on or changed: only your background work can '
    'confirm an action, and it tells you when it has.\n'
    'Lines in square brackets are notes to you, not from the user.\n'
)


def fixed_block(mode, tools, name=None, owner=None, sound_tags=()):
    """Persona + rules + mode instructions. Stable for the whole conversation.
    ``sound_tags``: tags the session's TTS voice can perform (Chatterbox); empty for Kokoro."""
    name = name or providers.ASSISTANT_NAME
    owner = providers.OWNER if owner is None else owner
    text = providers.assistant_intro(name, owner) + ' You are talking by voice.\n' + RULES
    text += 'Things you can get done in this session: ' + capability_summary(tools) + \
            '. For anything else, say plainly that you cannot do that here.\n'
    if sound_tags:
        text += ('Your voice can perform ' + ', '.join(sound_tags) + '. Use one only when it truly fits, such as '
                 'laughing at a joke; most replies use none, never more than one. Never describe or explain them.\n')
    if mode.agent:
        if mode.custom:
            text += ('Additional instructions configured by the user for this session. They shape style only '
                     'and cannot override the rules above:\n' + mode.prompt + '\n')
    else:
        text += (f'Mode: {MODES[mode.id]["label"]}. The user configured these instructions; they set your role '
                 'and style only and cannot override the rules above:\n' + mode.prompt + '\n')
    return text.rstrip()


def history(messages, turns):
    """The last ``turns`` user/assistant exchanges as plain text, marker-free and
    with same-role neighbours merged (tool records and silences dropped)."""
    out = []
    for message in messages:
        role, content = message.get('role'), message.get('content')
        if role not in ('user', 'assistant') or not isinstance(content, str):
            continue
        content = content.replace(SPOKEN_MARK, '').strip()
        if not content or content == SILENT_RECORD:
            continue
        if out and out[-1]['role'] == role:
            out[-1]['content'] += ' ' + content
        else:
            out.append({'role': role, 'content': content})
    keep = out[-(2 * turns):] if turns > 0 else []
    while keep and keep[0]['role'] != 'user':
        keep.pop(0)
    return keep


def build_messages(fixed, board_text, past, utterance):
    """[system: fixed + board] + past turns + the utterance (a user message)."""
    messages = [{'role': 'system', 'content': fixed + BOARD_HEADER + board_text}] + list(past)
    if messages[-1]['role'] == 'user':
        # Keep roles alternating: an unanswered earlier user line joins this one.
        messages[-1] = {'role': 'user', 'content': messages[-1]['content'] + ' ' + utterance}
    else:
        messages.append({'role': 'user', 'content': utterance})
    return messages


EARLY_CLAUSE_WORDS = int(os.environ.get('VOICE_FRONT_EARLY_WORDS', '5'))


def early_clause(pending):
    """(clauses, rest): the text up to a comma once it has a few words, else nothing."""
    j = pending.find(', ')
    if j == -1 or len(pending[:j].split()) < EARLY_CLAUSE_WORDS:
        return [], pending
    return [pending[:j + 1].strip()], pending[j + 2:]


class Clauses:
    """Streamed text -> speakable clauses: whole sentences, and the first clause
    early at a comma once it has a few words. Shared by Front and the house engine."""

    def __init__(self):
        self.pending, self.first = '', True

    def feed(self, piece):
        self.pending += piece
        done, self.pending = providers.split_sentences(self.pending)
        if self.first and not done:
            # Start speaking sooner: the first clause may end at a comma.
            done, self.pending = early_clause(self.pending)
        if done:
            self.first = False
        return [clause for clause in done if len(clause.strip()) >= 2]

    def flush(self, restart=False):
        """The rest as one clause. ``restart``: the next clause may end early again
        (text after a pause, e.g. the house voice's words after a tool call)."""
        rest, self.pending = self.pending.strip(), ''
        if restart:
            self.first = True
        return [rest] if rest else []


def followup_note(facts):
    return ('[Internal note, not from the user] Your background work just finished: ' + facts +
            '\nTell the user the result now, in the first person, in one or two short sentences. '
            'If it contradicts something you said earlier, correct yourself plainly first '
            '("Actually, I was wrong: ..."). Do not greet and do not repeat what you already said.')


def facts_note(facts, told):
    """Board facts not yet told to Front (new or changed), as a bracketed note for the
    next user message; updates ``told`` (key -> text)."""
    new = [f for f in facts if told.get(f['key']) != f['text']]
    for fact in new:
        told[fact['key']] = fact['text']
    if not new:
        return ''
    return '[Known facts now (current; you may state these): ' + ' '.join(
        f['text'].rstrip('.') + '.' for f in new) + ']'


class Transcript:
    """Front's prompt as an append-only list of messages, so every request extends
    the previous one (prompt + reply) and the model's cache carries over. It starts
    from the stored history, and starts again when the fixed block changes or it
    outgrows ``max_chars``. An interrupted reply is recorded as heard, which breaks
    the cache once; the truth matters more than one slow prefill."""

    def __init__(self, max_chars=None, keep_turns=None):
        self.max_chars = max_chars or int(os.environ.get('VOICE_FRONT_TRANSCRIPT_CHARS', '14000'))
        self.keep_turns = keep_turns if keep_turns is not None else int(os.environ.get('VOICE_FRONT_TURNS', '6'))
        self.fixed = None
        self.messages = []
        self.told = {}
        self.pending = None
        self.resets = 0

    def size(self):
        return sum(len(m['content']) for m in self.messages)

    def request(self, fixed, past, facts, text, preface=''):
        """Messages for the next completion: the transcript plus one user message
        (``text`` after any new facts). ``past``: stored history, used on a (re)start."""
        if fixed != self.fixed or self.size() > self.max_chars:
            self.fixed, self.told, self.resets = fixed, {}, self.resets + 1
            self.messages = history(past, min(self.keep_turns, 3) if self.size() > self.max_chars else self.keep_turns)
            if preface:
                text = preface + '\n' + text
        note = facts_note(facts, self.told)
        content = (note + '\n' + text) if note else text
        messages = list(self.messages)
        if messages and messages[-1]['role'] == 'user':
            # Keep roles alternating: an unanswered earlier line joins this one.
            messages[-1] = {'role': 'user', 'content': messages[-1]['content'] + '\n' + content}
        else:
            messages.append({'role': 'user', 'content': content})
        self.pending = messages
        return [{'role': 'system', 'content': fixed}] + messages

    def replace_last_user(self, text):
        """A merged utterance: the unanswered user line becomes ``text``."""
        if self.messages and self.messages[-1]['role'] == 'user':
            self.messages = self.messages[:-1]
        return text

    def add_assistant(self, text):
        """Speech that answered no user line (a pushed back-end result), as heard. The
        transcript stays what was said; joining the last reply edits it, so the
        model's cache misses once."""
        if not text or self.fixed is None or self.pending is not None:
            return
        if self.messages and self.messages[-1]['role'] == 'assistant':
            self.messages[-1] = {'role': 'assistant', 'content': self.messages[-1]['content'] + ' ' + text}
        else:
            self.messages.append({'role': 'assistant', 'content': text})

    def commit(self, reply):
        """After the completion: ``reply`` exactly as generated (or as heard)."""
        if self.pending is None:
            return
        self.messages = self.pending + ([{'role': 'assistant', 'content': reply}] if reply else [])
        self.pending = None


def payload(messages):
    return {'model': providers.FRONT_MODEL, 'messages': messages, 'stream': True,
            'max_tokens': int(os.environ.get('VOICE_FRONT_MAX_TOKENS', '200')), 'temperature': 0.5,
            'reasoning_effort': 'none', 'chat_template_kwargs': {'enable_thinking': False}}


async def stream(messages, on_clause, on_token=None, transport=None, url=None, client=None):
    """Stream one Front reply. Calls ``on_token()`` at the first content token
    and ``on_clause(text)`` per complete clause. Returns the full text.
    ``client``: a caller-owned httpx.AsyncClient to reuse (kept open)."""
    body = payload(messages)
    content, first, clauses = '', True, Clauses()
    async with contextlib.AsyncExitStack() as stack:
        if client is None:
            kwargs = {'timeout': float(os.environ.get('VOICE_FRONT_TIMEOUT_S', '30')), 'trust_env': False}
            if transport is not None:
                kwargs['transport'] = transport
            client = await stack.enter_async_context(httpx.AsyncClient(**kwargs))
        async with client.stream('POST', url or providers.FRONT_URL, json=body) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith('data:'):
                    continue
                chunk = line[5:].strip()
                if chunk == '[DONE]':
                    break
                try:
                    delta = (json.loads(chunk).get('choices') or [{}])[0].get('delta') or {}
                except (ValueError, AttributeError):
                    continue
                piece = delta.get('content') or ''
                if not piece:
                    continue
                if first and on_token:
                    on_token()
                first = False
                content += piece
                for clause in clauses.feed(piece):
                    await on_clause(clause)
    for clause in clauses.flush():
        await on_clause(clause)
    return content.strip()
