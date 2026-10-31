"""notify.send through a real 0.5 hub (see harness.py): the hub runs the
notify and telegram plugins from its library; the phone channel goes over
the band to notification.post on the worker (ctx.call_on, a ticket the
worker verifies), and the Telegram channel to a fake Bot API, with the bot
token from the hub's environment (the vault stand-in)."""
from __future__ import annotations

import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from harness import REPO, WHY, until

TOKEN = "123456:e2e-telegram-token"

PHONE = """\
manifest: 1
plugin: phone
version: 1.0.0
doc: A fake phone that takes notifications.
caps:
  notification.post:
    doc: Show a notification.
    risk: write
    scope: local
    run: scripts/phone.py:post
    args:
      text: { type: str, required: true }
      title: { type: str }
provides:
  notification.post: phone.notification.post
"""
PHONE_PY = """\
def post(ctx, text, title=None):
    return {"shown": text, "title": title, "for": ctx.person}
"""


class BotAPI(BaseHTTPRequestHandler):
    sent: list = []
    updates: list = []          # what the next getUpdates hands the bot (then cleared)

    def do_POST(self):          # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        method = self.path.rsplit("/", 1)[-1]
        ok = self.path.startswith(f"/bot{TOKEN}/")
        if ok and method == "sendMessage":
            BotAPI.sent.append(body)
        updates, BotAPI.updates = (BotAPI.updates, []) if ok and method == "getUpdates" else ([], BotAPI.updates)
        result = {"getMe": {"id": 1, "username": "rook_bot"}, "getUpdates": updates,
                  "sendMessage": {"message_id": 9}}.get(method)
        data = json.dumps({"ok": ok, "result": result} if ok else
                          {"ok": False, "error_code": 401, "description": "Unauthorized"}).encode()
        self.send_response(200 if ok else 401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def test_notify_send_reaches_a_worker_and_telegram(fresh, tmp_path):
    api = ThreadingHTTPServer(("127.0.0.1", 0), BotAPI)
    threading.Thread(target=api.serve_forever, daemon=True).start()
    try:
        for name in ("notify", "telegram"):
            shutil.copytree(REPO / "rook" / "plugins" / name, fresh.library / name)
        hub_yaml = fresh.hub_dir / "hub.yaml"
        cfg = json.loads(hub_yaml.read_text())
        cfg["settings"] = {"telegram": {"token": "tg-bot", "api": f"http://127.0.0.1:{api.server_port}", "poll": 1}}
        hub_yaml.write_text(json.dumps(cfg))
        phone = tmp_path / "phone"
        (phone / "scripts").mkdir(parents=True)
        (phone / "plugin.yaml").write_text(PHONE)
        (phone / "scripts" / "phone.py").write_text(PHONE_PY)
        worker_yaml = fresh.worker_dir / "worker.yaml"
        wcfg = json.loads(worker_yaml.read_text())
        wcfg["plugins"].append(str(phone))
        worker_yaml.write_text(json.dumps(wcfg))
        fresh.hub_cli("secret", "set", "tg-bot", "--description", "the e2e bot", input=TOKEN)
        assert fresh.hub_cli("secret", "list") == "tg-bot\tthe e2e bot"

        fresh.start_hub()
        fresh.start_worker()
        until(lambda: (c := fresh.caps()).get("online") and "phone.notification.post" in c.get("caps", {}),
              30, "the worker online with its phone cap")
        hub = fresh.owner
        # bake links Telegram the way a person does: a code, then /link CODE to the bot.
        out = hub.call("telegram.link", {}, worker="rook")
        assert out["ok"], out
        BotAPI.updates.append({"update_id": 1, "message": {
            "message_id": 1, "chat": {"id": 42, "type": "private"}, "from": {"id": 42, "first_name": "bk"},
            "text": f"/link {out['result']['code']}"}})
        until(lambda: any(m.get("text", "").startswith("Linked:") for m in BotAPI.sent), 30, "bake linked")
        assert hub.call("setting.set", {"plugin": "notify", "key": "phone", "value": "e2e-worker"},
                        worker="rook")["ok"]

        out = hub.call("notify.send", {"text": "The build is green.", "title": "CI"}, worker="rook")
        assert out["ok"] and out["result"] == {"sent": [{"to": "person:bake", "channel": "phone"}], "errors": {}}, out
        assert fresh.worker_calls("phone.notification.post") == 1
        entry = hub.get("/journal?cap=phone.notification.post&limit=1")["entries"][0]
        assert (entry["person"], entry["ok"], entry["why"]) == ("person:bake", True, WHY)
        assert entry["worker"] == fresh.worker_id

        out = hub.call("notify.send", {"text": "Deploy waits on you", "to": "bake", "channel": "telegram"},
                       worker="rook")
        assert out["ok"] and out["result"]["sent"] == [{"to": "person:bake", "channel": "telegram"}], out
        assert BotAPI.sent[-1]["chat_id"] == "42" and BotAPI.sent[-1]["text"] == "Deploy waits on you"

        out = hub.call("notify.send", {"text": "everywhere", "channel": "all"}, worker="rook")
        assert [s["channel"] for s in out["result"]["sent"]] == ["phone", "telegram"], out
        assert "discord" not in json.dumps(out["result"]["errors"])    # not installed: not a channel at all
        assert fresh.worker_calls("phone.notification.post") == 2
        assert not Path(fresh.hub_dir / "data" / "person-settings.json").read_text().count(TOKEN)
    finally:
        api.shutdown()
        api.server_close()
