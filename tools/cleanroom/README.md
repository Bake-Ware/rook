# Clean-room runs

Fresh installs of Rook on disposable machines, driven end to end with no
person at the keyboard, to measure what a new user meets: how long until the
first useful result, how many things they must do by hand ("human touches"),
which steps fail, and what the models cost.

A run is one cell of the matrix in `matrix.json`:

- **hardware**: `bachelor` (one laptop hosts the hub and is the only desktop
  worker, plus a phone running the APK), `homelab` (a hub VM plus two worker
  VMs), `gpu-box` (planned).
- **models**: `claude-subscription` (Claude Code signed in with a token),
  `local-only` (a small CPU model behind llama.cpp's OpenAI-compatible server
  on the laptop), `mixed` (the local model as the house agent, Claude Code for
  coding sessions).
- **trust**: `locked-down`, `balanced`, `yolo`. These are mapped onto the
  hub's real knob: the trust map's level for targets not on the map.

Scenarios (`scenarios.py`, in order): install the hub with the one-command
installer; pair the laptop with a dashboard pairing code; install the APK and
pair the phone through its UI; apply the trust preset; set up the model
profile; ask the house agent a first question and a quick action; an agent's
admin call is held, approved and run once; "fix this bug" in a toy repo,
ending in a pull request; teach the house agent a preference and check it
holds; kill the worker mid-call and take the phone's network away, then
check both recover; voice (skipped: a fresh hub has no voice server).

Every machine is created for the run and destroyed afterwards:

- Proxmox VMs come from a cloud image, in a configured VMID range, tagged
  `rook-cleanroom` and named `cleanroom-*`. The provider refuses to stop or
  destroy anything else.
- The phone is an Android emulator AVD started with `-wipe-data`.

Each test hub makes its own band key, so nothing here ever uses a live band's
key or tokens.

## Site config

Everything specific to a site lives outside the repo, in
`~/.config/rook-cleanroom/site.json` (or `--site PATH`):

```json
{
  "proxmox": {
    "host": "root@<hypervisor>",
    "image": "/var/lib/vz/template/debian-12-genericcloud-amd64.qcow2",
    "storage": "local-lvm", "bridge": "vmbr0",
    "vmid_range": [900, 919],
    "ssh_key": "~/.ssh/rook_cleanroom_ed25519",
    "subnet_scan": "192.168.1"
  },
  "emulator": {"sdk": "~/Android/Sdk", "avd": "rook-cleanroom", "port": 5584, "memory_mb": 2048},
  "apk": "~/path/to/rook-worker.apk",
  "toy_repo": "<owner>/<private throwaway repo>",
  "secrets": {"claude_oauth_token": {"file": "~/.config/rook-cleanroom/claude-token"}},
  "reports_dir": "~/rook-cleanroom-reports"
}
```

- `ssh_key`: a key pair the harness owns. Its public half goes into each VM
  through cloud-init.
- `subnet_scan` (optional): the /24 the VMs get DHCP addresses on. The
  hypervisor pings it to learn a new VM's address.
- `claude_oauth_token`: a long-lived Claude Code token (`claude setup-token`).
  `{"env": "NAME"}` and `{"cmd": [...]}` work too.
- `toy_repo`: a GitHub repo the harness force-resets to `toy_seed.py` before
  each run. Coding sessions open their pull request there. `gh` on the
  controller must be able to push to it, and its token is handed to the
  laptop VM for the session.

One-time setup on the controller:

```sh
ssh-keygen -t ed25519 -N '' -C rook-cleanroom -f ~/.ssh/rook_cleanroom_ed25519
# the cloud image, on the hypervisor:
curl -fLo /var/lib/vz/template/debian-12-genericcloud-amd64.qcow2 \
  https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2
# an AVD of its own (any recent x86_64 google_apis image):
echo no | avdmanager create avd -n rook-cleanroom -k "system-images;android-35;google_apis;x86_64" -d pixel_5
```

## Run

From the repo root:

```sh
python3 -m tools.cleanroom list                       # the matrix
python3 -m tools.cleanroom run --hardware bachelor --model claude-subscription --trust balanced
python3 -m tools.cleanroom run --model local-only --only first-request,learn-preference
python3 -m tools.cleanroom run --ref my-branch --keep # keep the machines to poke at
python3 -m tools.cleanroom sweep                      # destroy leftovers after a crash
```

- `--ref` is any git ref or sha that is pushed to GitHub. The installer and
  the hub both come from it.
- `--only` limits the scenarios. Installing and pairing always run.

Each run writes `<reports_dir>/<run id>/`, containing:

- `run.log`;
- `hub-install.log`;
- `<run id>.json`, every step with its status, duration, human touches,
  tokens and notes;
- `<run id>.md`, a readable summary with the findings.

A bachelor run with Claude takes about 25-40 minutes, most of it the coding
session.

## Counting human touches

A touch is anything the system makes a person do besides asking for what they
want. Examples:

- paste an install command;
- type a password or pairing code;
- click a dashboard button;
- sign in to Claude;
- approve a hold;
- answer a Yes/No ask.

Each scenario records its touches where a real user would hit them, even
though the harness performs them itself.
