"""Front: the fast voice of the front_background pipeline.

One streaming chat completion per turn to the local model (VLLM_URL /
VLLM_MODEL), with no tools and thinking disabled. The prompt is laid out for
prefix caching: a fixed block (persona, mode, rules) that never changes within
a conversation, then the context board, then the last N turns, then the
utterance. Nothing that changes per turn sits before the fixed block.
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


def followup_note(facts):
    return ('[Internal note, not from the user] Your background work just finished: ' + facts +
            '\nTell the user the result now, in the first person, in one or two short sentences. '
            'Do not greet and do not repeat what you already said.')


def payload(messages):
    return {'model': providers.FRONT_MODEL, 'messages': messages, 'stream': True,
            'max_tokens': int(os.environ.get('VOICE_FRONT_MAX_TOKENS', '200')), 'temperature': 0.5,
            'reasoning_effort': 'none', 'chat_template_kwargs': {'enable_thinking': False}}


async def stream(messages, on_clause, on_token=None, transport=None, url=None, client=None):
    """Stream one Front reply. Calls ``on_token()`` at the first content token
    and ``on_clause(text)`` per complete clause. Returns the full text.
    ``client``: a caller-owned httpx.AsyncClient to reuse (kept open)."""
    body = payload(messages)
    content, pending, first = '', '', True
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
                pending += piece
                done, pending = providers.split_sentences(pending)
                for clause in done:
                    if len(clause.strip()) >= 2:
                        await on_clause(clause)
    if pending.strip():
        await on_clause(pending.strip())
    return content.strip()
