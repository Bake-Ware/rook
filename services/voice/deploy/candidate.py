"""Run a voice release as a throwaway candidate beside the live service.

The candidate gets the live service's exact environment (read from the running
process, so drop-ins and EnvironmentFiles are already resolved) with these
overrides, and nothing it writes touches live state:

* loopback bind on a spare port;
* a private scratch VOICE_MODEL_DIR whose model files are symlinks to the live
  ones (so voice.state, rejected-plan logs and the default DB paths are private);
* its own empty VOICE_STATE_DB and VOICE_ADMIN_DB in that scratch directory;
* DECISION_URL empty (no decision-engine traffic or feedback writes);
* an extra, temporary owner key for smoke tests, merged into a private copy of
  VOICE_IDENTITIES_FILE. The key is written only to <scratch>/smoke-token (0600).
* optionally WHISPER_DEVICE=cpu (``--cpu-stt``) so the candidate does not load a
  second speech model onto the live GPU.

Secrets are never printed. Usage (on the voice host, as the service user):

    python3 candidate.py --release DIR --port 8931 --scratch DIR [--cpu-stt]
        [--unit voice-agent.service] [--python /path/to/venv/bin/python]

It execs the server in the foreground; release.sh backgrounds it.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys

MODEL_FILES = ('kokoro-v1.0.onnx', 'voices-v1.0.bin', 'smart-turn-v3.2-cpu.onnx', 'static', 'voice.state')


def service_environment(unit):
    pid = subprocess.run(['systemctl', 'show', '-p', 'MainPID', '--value', unit],
                         capture_output=True, text=True, check=True).stdout.strip()
    if not pid or pid == '0':
        raise SystemExit(f'{unit} is not running; cannot copy its environment')
    raw = subprocess.run(['sudo', '-n', 'cat', f'/proc/{pid}/environ'], capture_output=True, check=True).stdout
    env = {}
    for item in raw.split(b'\0'):
        if b'=' in item:
            key, value = item.split(b'=', 1)
            env[key.decode()] = value.decode()
    for key in ('INVOCATION_ID', 'JOURNAL_STREAM', 'SYSTEMD_EXEC_PID', 'MEMORY_PRESSURE_WATCH', 'MEMORY_PRESSURE_WRITE'):
        env.pop(key, None)
    return env


def port_free(port):
    with socket.socket() as sock:
        return sock.connect_ex(('127.0.0.1', port)) != 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--release', required=True, type=Path)
    parser.add_argument('--port', required=True, type=int)
    parser.add_argument('--scratch', required=True, type=Path)
    parser.add_argument('--unit', default='voice-agent.service')
    parser.add_argument('--python', default=None, help='interpreter; default: the live service ExecStart python')
    parser.add_argument('--cpu-stt', action='store_true')
    args = parser.parse_args()

    if not (args.release / 'services' / 'voice' / 'server.py').exists():
        raise SystemExit(f'{args.release} is not a voice release')
    if not port_free(args.port):
        raise SystemExit(f'port {args.port} is in use')
    env = service_environment(args.unit)
    live_dir = Path(env.get('VOICE_MODEL_DIR', '.'))
    scratch = args.scratch
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(scratch, 0o700)
    for name in MODEL_FILES:
        source, target = live_dir / name, scratch / name
        if source.exists() and not target.exists():
            if name == 'voice.state':
                target.write_text(source.read_text())
            else:
                target.symlink_to(source)

    identities = {}
    if env.get('VOICE_IDENTITIES_FILE') and Path(env['VOICE_IDENTITIES_FILE']).exists():
        identities = json.loads(Path(env['VOICE_IDENTITIES_FILE']).read_text())
    token_file = scratch / 'smoke-token'
    if not token_file.exists():
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write(secrets.token_urlsafe(32))
    token = token_file.read_text().strip()
    identities[hashlib.sha256(token.encode()).hexdigest()] = {'principal': 'candidate-smoke', 'owner': True}
    ids_file = scratch / 'identities.json'
    fd = os.open(ids_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(identities, handle)

    env.update({
        'VOICE_BIND': '127.0.0.1', 'VOICE_PORT': str(args.port), 'VOICE_MODEL_DIR': str(scratch),
        'VOICE_STATE_DB': str(scratch / 'voice-state.sqlite3'), 'VOICE_ADMIN_DB': str(scratch / 'voice-admin.sqlite3'),
        'VOICE_IDENTITIES_FILE': str(ids_file), 'DECISION_URL': '', 'DECISION_GATE_THRESHOLD': '',
    })
    if args.cpu_stt:
        env.update({'WHISPER_DEVICE': 'cpu', 'WHISPER_COMPUTE': 'int8'})
    python = args.python
    if not python:
        execstart = subprocess.run(['systemctl', 'show', '-p', 'ExecStart', '--value', args.unit],
                                   capture_output=True, text=True, check=True).stdout
        python = execstart.split('path=', 1)[1].split(' ;', 1)[0].strip() if 'path=' in execstart else sys.executable
    print(f'candidate: release={args.release} port={args.port} scratch={scratch} python={python}', flush=True)
    os.chdir(args.release)
    os.execve(python, [python, '-m', 'services.voice.server'], env)


if __name__ == '__main__':
    main()
