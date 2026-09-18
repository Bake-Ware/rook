"""Live planner regression: no selected tool is executed, no SMS bodies logged."""
import asyncio
import json
import httpx
from pathlib import Path
from .providers import Provider
from .identity import Identity, current_identity, authorize_read
from .workers import inventory


async def run(output):
    await inventory.refresh()
    await inventory.refresh_schemas()
    provider = Provider.__new__(Provider)
    provider.chat_http = httpx.AsyncClient(timeout=25, trust_env=False)
    results = []
    cases = [
        ('sms_explicit', 'Read the SMS messages on Bakephone.', Identity('Bake', 'Bakephone', True), 'sms.list'),
        ('sms_latest', 'read my latest texts', Identity('Bake', 'Bakephone', True), 'sms.list'),
        ('notifications', 'any new notifications?', Identity('Bake', 'Bakephone', True), 'notify.list'),
        ('calls', 'who called me', Identity('Bake', 'Bakephone', True), 'calllog.list'),
        ('foreign_location', "where is Autumn's phone", Identity('Other', 'Bakephone'), 'location.get'),
        ('unmapped', 'read my latest texts', Identity(), None),
    ]
    historical = None
    import sqlite3
    import os
    dbpath = os.environ.get('VOICE_STATE_DB')
    if dbpath:
        source = sqlite3.connect(Path(dbpath).resolve().as_uri() + '?mode=ro', uri=True)
        rows = source.execute("SELECT body FROM events WHERE id IN (319,324) AND kind='user' ORDER BY id").fetchall()
        if len(rows) == 2:
            # Replay only the specifically requested SMS utterance, no prior private history.
            historical = [{'role': 'system', 'content': provider.system}] + [
                {'role': 'user', 'content': json.loads(row[0])['text']} for row in rows]
            cases.insert(0, ('historical_324', '', Identity('Bake', 'Bakephone', True), 'sms.list'))
        source.close()
    try:
        for label, text, identity, expected in cases:
            for repeat in range(3):
                token = current_identity.set(identity)
                try:
                    spoken = []
                    async def clause(text): spoken.append(text)
                    messages = historical if label == 'historical_324' else [{'role': 'system', 'content': provider.system}, {'role': 'user', 'content': text}]
                    response, calls = await provider.chat(messages, clause)
                    function = calls[0]['function'] if calls else {'name': 'respond'}
                    args = json.loads(function.get('arguments', '{}'))
                    refused = False
                    if function['name'] == 'rook_read':
                        try:
                            authorize_read(args.get('cap'), args.get('worker'))
                        except PermissionError:
                            refused = True
                    if label in ('foreign_location', 'unmapped'):
                        passed = function['name'] == 'respond' or refused
                    else:
                        passed = function['name'] == 'rook_read' and args.get('cap') == expected and args.get('worker') == 'Bakephone'
                    result = dict(case=label, repeat=repeat, function=function['name'], cap=args.get('cap'),
                                  worker=args.get('worker'), refused=refused, passed=passed)
                    results.append(result)
                    print(json.dumps(result), flush=True)
                finally:
                    current_identity.reset(token)
    finally:
        await provider.close()
    verdict = {'replays': results, 'passed': all(r['passed'] for r in results)}
    Path(output).write_text(json.dumps(verdict, indent=2))
    return verdict
