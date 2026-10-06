"""Background tool policy: caller identity x conversation mode.

Front has no tools at all; this table is the only place a front_background turn
gets its tools (docs/design/voice-front-background.md, "Modes").

| mode / identity              | Background tools                                   |
|------------------------------|----------------------------------------------------|
| assistant, owner key         | everything                                         |
| assistant, device-mapped key | timers, web_search, weather, own-device reads      |
| guest (no mapping)           | timers, web_search, weather                        |
| conversation, brainstorm,    | timers, web_search, weather                        |
|   roleplay, listen           |                                                    |
| dictate                      | none                                               |
"""
TIMER_TOOLS = frozenset({'timer_set', 'timer_list', 'timer_cancel'})
BASE_TOOLS = TIMER_TOOLS | {'web_search', 'weather'}
# Own-device reads; identity.authorize_read still pins them to the mapped device.
DEVICE_TOOLS = BASE_TOOLS | {'rook_read', 'calendar_list', 'mail_list'}
OWNER_ONLY = frozenset({'rook_devices', 'rook_describe', 'rook_call', 'rook_mcp_describe', 'rook_mcp',
                        'tasks_deck', 'task_get', 'music', 'ha_list', 'ha_call'})
ALL_TOOLS = DEVICE_TOOLS | OWNER_ONLY
CHAT_MODES = frozenset({'conversation', 'brainstorm', 'roleplay', 'listen'})


def background_tools(identity, mode_id):
    """Tool names Background may offer and run for this caller in this mode."""
    if mode_id == 'dictate':
        return frozenset()
    if identity.owner:
        by_identity = ALL_TOOLS
    elif identity.worker:
        by_identity = DEVICE_TOOLS
    else:
        by_identity = BASE_TOOLS
    if mode_id == 'assistant':
        return frozenset(by_identity)
    # Kids/chat modes, and anything unrecognised, get the base set at most.
    return frozenset(by_identity & BASE_TOOLS)


def capability_summary(tools):
    """Plain words for the Front prompt: what the assistant can have done this session."""
    parts = []
    if TIMER_TOOLS & tools:
        parts.append('timers')
    if 'weather' in tools:
        parts.append('the weather')
    if 'web_search' in tools:
        parts.append('web searches')
    if 'calendar_list' in tools:
        parts.append('your calendar')
    if 'mail_list' in tools:
        parts.append('recent mail notifications')
    if 'rook_read' in tools:
        parts.append('reading your own device')
    if 'tasks_deck' in tools:
        parts.append('your Rook tasks')
    if 'music' in tools:
        parts.append('music')
    if 'ha_call' in tools:
        parts.append('home lights, switches, scenes and media players')
    if 'rook_call' in tools:
        parts.append('work on your Rook devices')
    return ', '.join(parts) if parts else 'nothing beyond conversation'
