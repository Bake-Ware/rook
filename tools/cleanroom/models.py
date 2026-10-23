"""Model profiles on a clean-room machine.

- claude-subscription: install Claude Code on the agent-host worker and sign
  it in with a long-lived token (`claude setup-token`). A real person signs in
  through a browser: one human touch.
- local-only: a llama.cpp server with a small GGUF model on the laptop, as an
  OpenAI-compatible endpoint on localhost. The largest candidate that fits the
  machine's free memory wins; what fitted and how fast it runs go in the report.
"""

from __future__ import annotations

import json
import re
import shlex
import time

from .providers import Machine

LLAMA_RELEASES_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=10"


def install_claude_code(m: Machine, token: str) -> str:
    """Install Claude Code for the machine's user and sign it in with `token`.

    The token goes into ~/.config/rook-cleanroom/claude.env (mode 600). The
    worker's systemd user unit loads it (a drop-in), so agent-host turns and
    session terminals the worker starts see CLAUDE_CODE_OAUTH_TOKEN; login
    shells get it from ~/.profile. Returns `claude --version` and a one-word
    test reply.
    """
    m.sh("command -v claude >/dev/null || test -x ~/.local/bin/claude || "
         "(curl -fsSL https://claude.ai/install.sh | bash) >/tmp/claude-install.log 2>&1", timeout=900)
    m.sh("mkdir -p ~/.config/rook-cleanroom ~/.config/systemd/user/rook-band-worker.service.d")
    m.write(f"/home/{m.user}/.config/rook-cleanroom/claude.env", f"CLAUDE_CODE_OAUTH_TOKEN={token}\n")
    m.write(f"/home/{m.user}/.config/systemd/user/rook-band-worker.service.d/claude.conf",
            "[Service]\nEnvironmentFile=%h/.config/rook-cleanroom/claude.env\n"
            f"Environment=PATH=/home/{m.user}/.local/bin:/usr/local/bin:/usr/bin:/bin\n", mode="644")
    m.sh("grep -q rook-cleanroom/claude.env ~/.profile || "
         "printf '%s\\n' 'set -a; . ~/.config/rook-cleanroom/claude.env; set +a' "
         "'export PATH=$HOME/.local/bin:$PATH' >> ~/.profile")
    # A signed-in Claude Code skips its first-run screens when this says so.
    m.sh("test -f ~/.claude.json || echo '{\"hasCompletedOnboarding\": true}' > ~/.claude.json")
    m.sh("export XDG_RUNTIME_DIR=/run/user/$(id -u); systemctl --user daemon-reload; "
         "systemctl --user restart rook-band-worker.service 2>/dev/null || true", check=False)
    out = m.sh("bash -lc 'claude --version && claude -p \"Reply with the single word: ready\" --model haiku'",
               timeout=300, check=False)
    return out.stdout.strip() + (("\n" + out.stderr.strip()) if out.returncode else "")


def install_llama_server(m: Machine, cfg: dict, *, log=print) -> dict:
    """Install llama.cpp's prebuilt server and the biggest candidate model that fits.

    Returns {"model", "fits", "free_mb", "tokens_per_s", "base_url"} or raises.
    """
    port = int(cfg.get("port", 8080))
    ctx = int(cfg.get("ctx", 8192))
    m.sh("sudo DEBIAN_FRONTEND=noninteractive apt-get -qq install -y unzip curl libgomp1 >/dev/null", timeout=600)
    if m.sh("test -x ~/llama/llama-server", check=False).returncode != 0:
        # "latest" can be a tag with no binaries; take the newest build that has
        # the plain CPU Linux x64 archive (it picks the CPU variant at runtime).
        rels = json.loads(m.sh(f"curl -fsSL '{LLAMA_RELEASES_API}'", timeout=120).stdout)
        pick, rel = None, {}
        for rel in rels:
            pick = next((a["browser_download_url"] for a in rel.get("assets", [])
                         if re.search(r"-bin-ubuntu-x64\.(zip|tar\.gz)$", a["name"])), None)
            if pick:
                break
        if not pick:
            raise RuntimeError("no CPU Linux x64 llama.cpp build in the recent releases")
        log(f"[model] llama.cpp {rel.get('tag_name')}: {pick.rsplit('/', 1)[-1]}")
        unpack = "unzip -q -o /tmp/llama.zip -d ~/llama-unpack" if pick.endswith(".zip") else \
            "mkdir -p ~/llama-unpack && tar -xzf /tmp/llama.zip -C ~/llama-unpack"
        m.sh(f"curl -fsSL -o /tmp/llama.zip {shlex.quote(pick)} && {unpack} && "
             "d=$(dirname $(find ~/llama-unpack -name llama-server -type f | head -1)) && "
             "rm -rf ~/llama && mv \"$d\" ~/llama && rm -rf ~/llama-unpack /tmp/llama.zip && ~/llama/llama-server --version",
             timeout=900)
    free_mb = int(m.sh("awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo").stdout.strip())
    chosen = None
    for cand in cfg["candidates"]:
        if free_mb >= int(cand.get("min_free_mb", 0)):
            chosen = cand
            break
    if not chosen:
        raise RuntimeError(f"no candidate model fits in {free_mb} MB free")
    path = f"~/models/{chosen['name']}.gguf"
    m.sh(f"mkdir -p ~/models && (test -s {path} || curl -fsSL -o {path} {shlex.quote(chosen['url'])})", timeout=1800)
    unit = f"""[Unit]
Description=llama.cpp server (Rook clean room)
After=network-online.target
[Service]
ExecStart=/home/{m.user}/llama/llama-server -m /home/{m.user}/models/{chosen['name']}.gguf --host 127.0.0.1 --port {port} -c {ctx} --jinja --alias local -t {max(1, int(m.sh('nproc').stdout.strip()))}
Environment=LD_LIBRARY_PATH=/home/{m.user}/llama
Restart=on-failure
User={m.user}
[Install]
WantedBy=multi-user.target
"""
    m.sh("sudo tee /etc/systemd/system/llama-server.service >/dev/null", input=unit)
    m.sh("sudo systemctl daemon-reload && sudo systemctl enable --now llama-server >/dev/null 2>&1")
    deadline = time.time() + 300
    while time.time() < deadline:
        if m.sh(f"curl -fsS http://127.0.0.1:{port}/health", check=False).returncode == 0:
            break
        time.sleep(3)
    else:
        raise RuntimeError("llama-server did not become healthy: " +
                           m.sh("journalctl -u llama-server -n 20 --no-pager", check=False).stdout[-1500:])
    # One timed completion to record speed.
    body = json.dumps({"model": "local", "messages": [{"role": "user", "content": "Count from 1 to 20, comma separated."}],
                       "max_tokens": 80})
    t0 = time.time()
    out = m.sh(f"curl -fsS http://127.0.0.1:{port}/v1/chat/completions -H 'Content-Type: application/json' -d {shlex.quote(body)}",
               timeout=600)
    took = time.time() - t0
    j = json.loads(out.stdout)
    timings = j.get("timings") or {}
    tps = timings.get("predicted_per_second") or (j.get("usage", {}).get("completion_tokens", 0) / took if took else None)
    rss = m.sh("ps -o rss= -C llama-server | awk '{s+=$1} END {print int(s/1024)}'", check=False).stdout.strip()
    return {
        "model": chosen["name"], "free_mb_before": free_mb, "server_rss_mb": int(rss or 0),
        "tokens_per_s": round(tps, 1) if tps else None,
        "prompt_tokens_per_s": round(timings.get("prompt_per_second", 0), 1) or None,
        "base_url": f"http://127.0.0.1:{port}/v1",
    }
