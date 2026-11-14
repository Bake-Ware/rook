"""Start a worker: `python -m rook_worker HUB_URL` (R1, the part before an installer).

The hub URL, as the hub prints it when it starts, is the only input. The worker keeps its state in
`~/.rook-worker` (W7, R5): the keys it generates on the first start and the identity the hub mints
when a person or agent approves it. It knocks under this machine's hostname with the caps it has
and waits for approval (R2, R4), saying so; then it links and serves (H1) until Ctrl-C or SIGTERM,
and exits with code 0. Started again, it reuses that state and links at once: no second knock.

What happens is printed as one plain line each, from the worker's `rook_worker` log. A hub that
cannot be reached, a URL that is not a hub, and a key the hub refuses end the command with one
line saying what happened and what to do, and exit code 1; once approved, a hub that cannot be
reached is waited for, as the worker's loop decides (link design 3.2), with one line saying why.
"""

import argparse
import asyncio
import http.client
import logging
import pathlib
import signal
import sys
import urllib.error
import urllib.parse

from nacl.exceptions import BadSignatureError

import rook_worker
from rook_protocol import ProtocolError


def state_dir():
    """Where this machine's worker keeps its keys and identity: no directory 0.4 or 0.5 used."""
    return pathlib.Path.home() / ".rook-worker"


async def serve(worker):
    for signum in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(signum, worker.stop)
    await worker.run()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m rook_worker", description="Start a Rook worker on this machine.")
    parser.add_argument("hub_url", metavar="HUB_URL", help="the hub's address, as the hub prints it when it starts")
    url = parser.parse_args(sys.argv[1:] if argv is None else argv).hub_url
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return f"rook worker: {url} is not a hub address. Give the address the hub prints, such as http://127.0.0.1:8700/."

    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(logging.Formatter("%(message)s"))
    rook_worker.log.addHandler(out)
    rook_worker.log.setLevel(logging.INFO)
    rook_worker.log.propagate = False
    logging.getLogger("telesthete").setLevel(logging.ERROR)   # its warnings repeat what those lines say

    state = state_dir()
    worker = rook_worker.Worker(state, url)
    print(f"Rook worker {worker.name}, state in {state}", flush=True)
    if worker.identity is None:
        print(f"  Knocking at {worker.hub_url} with the key in {state / 'key'}.", flush=True)
    else:
        print(f"  Approved before as {worker.identity['moniker']}: linking to {worker.hub_url}.", flush=True)
    try:
        asyncio.run(serve(worker))
    except rook_worker.Refused as e:
        return (f"rook worker: the hub refused this worker's key. {e.message['message']} {e.message['next']} "
                f"This worker's keys and identity are in {state}.")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        return (f"rook worker: cannot reach the hub at {worker.hub_url} ({rook_worker.unreachable(e)}). "
                "Check the address and that the hub is running, then start again.")
    except (ProtocolError, http.client.HTTPException):
        return (f"rook worker: {worker.hub_url} did not answer as a Rook hub. "
                "Check the address: it is the one the hub prints when it starts.")
    except BadSignatureError:
        return (f"rook worker: the approval from {worker.hub_url} does not verify under the hub's own key, so it "
                "was not kept. Check that the address is your hub, then start again.")
    print("Stopped.", flush=True)
    return 0
