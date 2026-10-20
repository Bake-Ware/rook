"""Speak a clip's lines with Rook's voice server -> voices/<id>.wav and voices/durations.json.

    VOICE_URL=https://kaiju:8900 VOICE_KEY=... python voices.py N [N...]
Rook speaks in the server's default voice; 'you' (the person) uses a Kokoro voice.
"""
import json, os, ssl, sys, urllib.request, wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
URL = os.environ.get('VOICE_URL', 'https://192.168.1.64:8900') + '/api/voice'
KEY = os.environ.get('VOICE_KEY', '')
VOICES = {'rook': None, 'you': 'am_michael', 'reviewer': 'bm_george'}
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
(HERE / 'voices').mkdir(exist_ok=True)
dpath = HERE / 'voices' / 'durations.json'
durs = json.loads(dpath.read_text()) if dpath.exists() else {}
for n in sys.argv[1:]:
    meta = json.loads((HERE / 'out' / f'c{n}.json').read_text())
    for v in meta['voice']:
        out = HERE / 'voices' / f"{v['id']}.wav"
        txt_file = out.with_suffix('.txt')
        if not (out.exists() and txt_file.exists() and txt_file.read_text() == v['text'] + '|' + v['who']):
            body = {'text': v['text']}
            if VOICES.get(v['who']): body['voice'] = VOICES[v['who']]
            req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {KEY}'})
            for attempt in range(4):
                try:
                    out.write_bytes(urllib.request.urlopen(req, context=ctx, timeout=60).read()); break
                except Exception as e:
                    print('retry', v['id'], e); import time; time.sleep(3)
            txt_file.write_text(v['text'] + '|' + v['who'])
        with wave.open(str(out)) as w:
            durs[v['id']] = round(w.getnframes() / w.getframerate(), 3)
        print(v['id'], durs[v['id']], v['text'])
dpath.write_text(json.dumps(durs, indent=1))
