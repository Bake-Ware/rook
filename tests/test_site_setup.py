"""The setup wizard in a real browser (brief HA3): a new hub, open until claimed, from a fresh visit to
done. Headless Chrome over the DevTools protocol (tests/test_site_dock.py's driver), against a real
served hub started as the installer starts it (open until claimed), with nobody signed in.

What the person does and sees, in 0.5's order and words: the look-and-feel steps 1 to 4 on the left
(no agent turn: sound, Rookie's style, theme, name, through agent.look), the kickoff the agent then
gets, the views the agent opens with [[open: ...]] (Account: a name and a password, which claims the
hub; Vault: a secret typed there, never in the chat; Network), and agent.setup {done: true} turning
the site into the normal hub with the dock beside every page. Then Your account on Settings.

The agent's turns are test_site_dock's stand-in (agent.chat.send and agent.chat.read), so the test
says what the agent answers, markup included; the full conversation with Claude Code as the engine
is the brief's live run. Everything else is the real hub: agent.site (its ready forced on, as no
engine worker runs here), agent.look and agent.setup (hub_setup.py on the agents plugin's settings),
the first load that puts a new hub in setup, the open-until-claimed sign-in, secret.set and the
vault, POST /api/password.

With ROOK_SETUP_SHOTS set to a folder, the test leaves a screenshot of each step there."""

import asyncio
import json
import os
import pathlib

import pytest

from rook_hub import Hub
from rook_hub.auth import UNCLAIMED
from rook_hub.plugins import DEFAULTS

# The dock (brief HA1, pull request 453) and the agents plugin's hub part (brief HA2, pull request 452)
# carry these views; until both are in, this test is skipped.
if not ((DEFAULTS / "agents" / "hub.py").exists() and (DEFAULTS / "agents" / "shell" / "dock.js").exists()):
    pytest.skip("needs the dock (brief HA1, #453) and the agents plugin's hub part (brief HA2, #452)",
                allow_module_level=True)

from helpers import eventually  # noqa: E402
from test_site_dock import CHROME, Chrome, StandIn, pytestmark  # noqa: E402, F401  (skipped without Chrome)

SHOTS = os.environ.get("ROOK_SETUP_SHOTS")


async def hub_cap(real, cap, args, user):
    reply = await real("hub", cap, args, "The setup test checks.", user=user)
    assert reply["type"] == "result", reply
    return reply["result"]


def test_setup_from_a_fresh_start_to_done(tmp_path):
    asyncio.run(setup_run(tmp_path))


async def setup_run(tmp_path):
    hub = Hub(tmp_path / "hub", vault_key=os.urandom(32))
    host, port = await hub.serve("127.0.0.1", 0)
    base = f"http://{host}:{port}"
    owner = hub.auth.open_until_claimed("bake")                     # python -m rook_hub --no-password
    agent = StandIn(hub)
    real = hub.call_or_follow
    me = owner["guid"]

    async def call_or_follow(worker, cap, args, why, **actor):
        if worker == "hub" and cap in ("agent.chat.send", "agent.chat.read"):
            return {"type": "result", "id": "-", "result": await agent.answer(cap, args)}
        reply = await real(worker, cap, args, why, **actor)
        if worker == "hub" and cap == "agent.site" and reply["type"] == "result":
            reply["result"]["ready"] = True                          # no engine worker in this test
        return reply
    hub.call_or_follow = call_or_follow
    shots = pathlib.Path(SHOTS) if SHOTS else None

    async def shot(page, name):
        if shots:
            os.environ["ROOK_DOCK_SHOTS"] = str(shots)
            await page.shot(name)

    try:
        async with Chrome() as page:
            assert await hub_cap(real, "agent.setup", {}, me) == {"done": False}    # a new hub starts in setup
            # 1. A fresh browser on a new hub: no login (open until claimed); setup's two panes, the
            #    look-and-feel stage on the left with the first step, the agent's line in the chat.
            await page.cmd("Page.navigate", url=base + "/")
            await page.until("document.body.classList.contains('setup') && document.querySelector('#work').dataset.view === 'look'")
            await page.until("document.querySelector('#work .look-pick').dataset.step === 'sound'")
            await page.until("[...document.querySelectorAll('#dock-log .msg.them .text')].some(t => t.textContent.includes('do you want sound?'))")
            assert agent.sent == []                                  # no agent turn for the look steps
            await shot(page, "01-sound.png")

            # 2. Steps 1 to 4 by clicking, each applied at once through agent.look.
            await page.js("[...document.querySelectorAll('#work .look-pick button')].find(b => b.textContent.includes('No sound')).click(); true")
            await page.until("document.querySelector('#work .look-pick').dataset.step === 'rookie'")
            await shot(page, "02-style.png")
            style = await page.js("document.querySelector('#work .look-pick .looks button span:last-child').textContent")
            await page.js("document.querySelector('#work .look-pick .looks button').click(); true")
            await page.until("document.querySelector('#work .look-pick').dataset.step === 'theme'")
            await shot(page, "03-theme.png")
            await page.js("document.querySelector('#work .look-pick .looks button').click(); true")
            await page.until("document.querySelector('#work .look-pick').dataset.step === 'name'")
            await shot(page, "04-name.png")
            assert agent.sent == []
            agent.reply = "Hi, I'm Jeeves. First, your name and a password. [[open: account]]"
            await page.js("document.querySelector('[data-target=\"look.name.input\"]').value = 'Jeeves'; "
                          "document.querySelector('#work .look-pick form').requestSubmit(); true")
            await eventually(lambda: len(agent.sent) == 1)
            look = await hub_cap(real, "agent.look", {}, me)
            assert look["next"] is None and look["name"] == "Jeeves" and look["sound"] is False
            assert look["rookie"] == style.replace("none (hide me)", "none")
            assert agent.sent[0].startswith("[site] First visit to a new hub; setup isn't finished.")
            await page.until("document.querySelector('#dock-name').textContent === 'Jeeves'")

            # 3. The agent opens the Account view: the sign-in name shown, a name and a password typed
            #    there. Saving claims the hub; the agent hears a note the chat never shows as theirs.
            await page.until("document.querySelector('#work').dataset.view === 'account'")
            assert await page.js("document.querySelector('[data-target=\"account.user\"] b').textContent") == "bake"
            await shot(page, "05-account.png")
            await page.js("document.querySelector('[data-target=\"account.name\"]').value = 'Bake'; "
                          "document.querySelector('[data-target=\"account.password\"]').value = 'correct horse'; "
                          "document.querySelector('[data-target=\"account.again\"]').value = 'correct horse'; "
                          "document.querySelector('#work form').requestSubmit(); true")
            await page.until("!!document.querySelector('#work [data-target=\"account.done\"]')")
            await eventually(lambda: not (hub.state_dir / UNCLAIMED).exists())
            assert hub.auth.has_password(owner["guid"]) and hub.auth.unclaimed_owner() is None
            await eventually(lambda: any("Account saved." in t for t in agent.sent))
            note = next(t for t in agent.sent if "Account saved." in t)
            assert 'asked to be called "Bake"' in note and "They sign in as bake" in note
            assert "correct horse" not in "".join(agent.sent)
            await page.until("[...document.querySelectorAll('#dock-log .msg.me .text')].some(t => t.textContent === 'Saved my account')")
            await shot(page, "06-account-saved.png")

            # 4. Steps 6 and 7 are the conversation: choices, then the Network view with what was checked.
            agent.reply = ("You can open this site from here or other devices.\n"
                           "[[open: network | home=http://192.168.1.20:8740 | status=Opened 8740/tcp in ufw; "
                           "checked http://192.168.1.20:8740/ answers 200]]\n"
                           "[[choices: Just this computer | Other devices at home | From anywhere]]")
            await page.type("A personal assistant")
            await page.until("document.querySelector('#work').dataset.view === 'network'")
            assert await page.js("[...document.querySelectorAll('#work .card h3')].map(h => h.textContent).join('|')") == \
                "Just this computer|Other devices at home|From anywhere"
            assert "http://192.168.1.20:8740" in await page.js("document.querySelector('[data-target=\"network.home\"]').textContent")
            assert "Opened 8740/tcp" in await page.js("document.querySelector('[data-target=\"network.status\"]').textContent")
            await shot(page, "07-network.png")

            # 5. Root: the agent opens the Vault with the key filled in; the value is typed there and goes
            #    to the hub's vault (secret.set), never into the chat.
            agent.reply = "I need sudo to open the firewall. [[open: vault | new=host_password | for=sudo on this machine]]"
            await page.js("[...document.querySelectorAll('#dock-log .widget.choices button')].find(b => b.textContent === 'Other devices at home').click(); true")
            await page.until("document.querySelector('#work').dataset.view === 'vault'")
            assert await page.js("document.querySelector('[data-target=\"vault.key\"]').value") == "host_password"
            assert await page.js("document.activeElement.dataset.target") == "vault.value"
            await shot(page, "08-vault.png")
            await page.js("document.querySelector('[data-target=\"vault.value\"]').value = 'hunter2-sudo'; "
                          "document.querySelector('#work form').requestSubmit(); true")
            await page.until("!!document.querySelector('#work [data-target=\"vault.done\"]')")
            await page.until("[...document.querySelectorAll('[data-target=\"vault.list\"] code')].some(c => c.textContent === 'host_password')")
            [secret] = hub.vault.list({}, {"user": owner["guid"], "token": None})["secrets"]
            assert (secret["name"], secret["scope"]) == ("host_password", "hub")
            await eventually(lambda: any("Secret host_password saved in the vault." in t for t in agent.sent))
            assert "hunter2-sudo" not in "".join(agent.sent)
            await shot(page, "09-vault-saved.png")

            # 6. Done: agent.setup {done: true} (the agent's call), and on the next page the site is the
            #    normal hub: no setup panes, the page with the dock beside it, under the agent's new name.
            assert await hub_cap(real, "agent.setup", {"done": True}, me) == {"done": True}
            await page.cmd("Page.navigate", url=base + "/workers")
            await page.until("!document.body.classList.contains('setup') && !document.querySelector('#dock').hidden")
            await page.until("!!document.querySelector('main.main .topbar, main.main h1')")
            assert await page.js("document.querySelector('#dock-name').textContent") == "Jeeves"
            await shot(page, "10-done.png")

            # 7. Your account on Settings: the look pickers, which change it later (0.5's /account).
            await page.cmd("Page.navigate", url=base + "/settings?section=agents/your-account")
            await page.until("!!document.querySelector('#look .looks button')")
            await page.js("document.querySelector('#look input[type=checkbox]').click(); true")
            for _ in range(100):
                if (await hub_cap(real, "agent.look", {}, me))["sound"] is True:
                    break
                await asyncio.sleep(0.1)
            assert (await hub_cap(real, "agent.look", {}, me))["sound"] is True
            await shot(page, "11-your-account.png")

            assert [e for e in page.errors if "WebGL" not in e] == []
    finally:
        await hub.stop_serving()
        hub.close()
