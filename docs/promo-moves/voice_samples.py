"""Voice audition: the same few lines in each candidate voice -> out/voicesamples/ (+ index.json),
shown on the clip list page (director.py serves it).
    VOICE_URL=https://kaiju:8900 VOICE_KEY=... python voice_samples.py
Only the voices marked public are cleared for the published films; sojourn is there for comparison."""
import json, os, ssl, sys, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
URL = os.environ.get('VOICE_URL', 'https://192.168.1.64:8900') + '/api/voice'
KEY = os.environ.get('VOICE_KEY', '')
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE

LINES = [
    'Morning. Two meetings today, the first at nine.',
    'Intrusion on the NAS. Unknown login at two fourteen. I’ve blocked the address and cut it off from the band.',
    'Folio’s done and on the bed. Hinge stops at flat, and the pen locks it shut.',
    'Push the two o’clock to tomorrow.',
]
VOICES = [
    ('jenny', 'Jenny (Dioco) · Rook', 'Chatterbox', True, 'Jenny TTS dataset: commercial use permitted; apps that speak in her voice must credit “Jenny (Dioco)”. Rendered clips need no credit.'),
    ('ljspeech', 'LJ Speech', 'Chatterbox', True, 'LibriVox reading, public domain.'),
    ('default', 'Chatterbox built-in · women', 'Chatterbox', True, 'The model’s own voice (MIT).'),
    ('m_hifi6097', 'Hi-Fi TTS 6097 (man)', 'Chatterbox', True, 'Real man’s voice: LibriVox reader (public domain) via NVIDIA Hi-Fi TTS, CC BY 4.0 — credit “Hi-Fi TTS (NVIDIA)” where credits run.'),
    ('m_hifi9017', 'Hi-Fi TTS 9017 (man)', 'Chatterbox', True, 'Real man’s voice: LibriVox reader (public domain) via NVIDIA Hi-Fi TTS, CC BY 4.0 — credit “Hi-Fi TTS (NVIDIA)” where credits run.'),
    ('m_michael', 'Michael (Chatterbox)', 'Chatterbox', True, 'Chatterbox from a Kokoro am_michael reference (Apache 2.0): rights-clean.'),
    ('m_onyx', 'Onyx (Chatterbox)', 'Chatterbox', True, 'Chatterbox from a Kokoro am_onyx reference (Apache 2.0): rights-clean.'),
    ('m_eric', 'Eric (Chatterbox)', 'Chatterbox', True, 'Chatterbox from a Kokoro am_eric reference (Apache 2.0): rights-clean.'),
    ('m_liam', 'Liam (Chatterbox)', 'Chatterbox', True, 'Chatterbox from a Kokoro am_liam reference (Apache 2.0): rights-clean.'),
    ('am_michael', 'Michael (US)', 'Kokoro', True, 'Kokoro voice, Apache 2.0. The person in the current films.'),
    ('af_heart', 'Heart (US)', 'Kokoro', True, 'Kokoro voice, Apache 2.0.'),
    ('bm_george', 'George (UK)', 'Kokoro', True, 'Kokoro voice, Apache 2.0. The reviewer in clip 3.'),
    ('sojourn', 'Sojourn (current Rook)', 'Chatterbox', False, 'Personal voice: for comparison only, not for public use.'),
]

out = HERE / 'out' / 'voicesamples'; out.mkdir(parents=True, exist_ok=True)
index = {'lines': LINES, 'voices': []}
for vid, label, engine, public, note in VOICES:
    files = []
    for i, text in enumerate(LINES):
        f = out / f'{vid}__{i}.wav'
        if not f.exists() or '--force' in sys.argv:
            req = urllib.request.Request(URL, data=json.dumps({'text': text, 'voice': vid}).encode(),
                                         headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {KEY}'})
            f.write_bytes(urllib.request.urlopen(req, context=ctx, timeout=120).read())
        files.append(f'out/voicesamples/{f.name}')
        print(vid, i, f.stat().st_size)
    index['voices'].append({'id': vid, 'label': label, 'engine': engine, 'public': public, 'note': note, 'files': files})
(out / 'index.json').write_text(json.dumps(index, indent=1, ensure_ascii=False))
