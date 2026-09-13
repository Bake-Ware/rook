import json
from pathlib import Path

from rook.worker.agent_activity import active_sessions

SID = '12345678-1234-1234-1234-123456789abc'


def process(root, pid, name, argv, log=None, state='S'):
    p = root / str(pid)
    (p / 'fd').mkdir(parents=True)
    (p / 'comm').write_text(name)
    (p / 'cmdline').write_bytes('\0'.join(argv).encode())
    (p / 'stat').write_text(f'{pid} ({name}) {state} 1 2 3')
    if log:
        (p / 'fd' / '8').symlink_to(log)
    return p


def test_exact_open_file_detects_independent_codex_and_ignores_readers(tmp_path):
    log = tmp_path / ('rollout-' + SID + '.jsonl')
    log.touch()
    root = tmp_path / 'proc'
    p = process(root, 123, 'codex', ['/bin/codex'], log)
    paths, ids = active_sessions('codex', root)
    assert str(log) in paths
    (p / 'comm').write_text('tail')
    (p / 'cmdline').write_bytes(b'tail\0-f\0session.jsonl')
    assert active_sessions('codex', root) == (set(), set())


def test_resume_arguments_and_claude_live_pid_markers(tmp_path):
    root = tmp_path / 'proc'
    p = process(root, 456, 'node', ['/usr/bin/node', '/bin/claude'])
    home = tmp_path / 'claude'
    (home / 'sessions').mkdir(parents=True)
    (home / 'sessions' / '456.json').write_text(json.dumps({'pid': 456, 'sessionId': SID}))
    assert SID in active_sessions('claude', root, home)[1]
    (home / 'sessions' / '456.json').write_text(json.dumps({'pid': 456, 'sessionId': SID, 'procStart': 'old-process'}))
    assert SID not in active_sessions('claude', root, home)[1]
    # Stale marker survives, but a dead process is never considered active.
    (p / 'stat').write_text('456 (node) Z 1 2 3')
    assert SID not in active_sessions('claude', root, home)[1]
    process(root, 789, 'codex', ['/bin/codex', 'resume', SID])
    assert SID in active_sessions('codex', root)[1]
    assert active_sessions('claude', tmp_path / 'missing') == (set(), set())
