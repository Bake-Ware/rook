"""A minimal service manager for the e2e tests: run the command, start it
again whenever it exits (like systemd's Restart=always), and pass SIGTERM on.
Core updates need one: a release that crashes is rolled back by the next
start (rook.core2.worker.boot)."""
import json
import signal
import subprocess
import sys
import time

child = None
stopping = False


def stop(*_):
    global stopping
    stopping = True
    if child is not None and child.poll() is None:
        child.send_signal(signal.SIGTERM)


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
while True:
    child = subprocess.Popen(sys.argv[1:])
    rc = child.wait()
    if stopping:
        sys.exit(rc)
    print(json.dumps({"msg": "supervisor restart", "rc": rc}), flush=True)
    time.sleep(0.3)
