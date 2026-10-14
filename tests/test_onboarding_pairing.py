"""One-click pairing on the Install page and the first-worker card: the
browser side (rook/web/manage.js under node) and the endpoints it uses, end
to end on a LAN http:// hub."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from rook.remote import setup_store
from rook.remote.bootstrap import CombinedServer
from rook.remote.enrollment import EnrollmentStore

WEB = Path(__file__).resolve().parents[1] / "rook" / "web"
LAN = "192.0.2.10"


def test_install_page_offers_one_click_pairing():
    src = (WEB / "index.html").read_text(encoding="utf-8")
    assert "Open Tokens, select a band" not in src
    for host in ("install-pairing", "first-worker-pairing"):
        assert f"startPairing('{host}',this)" in src and f'id="{host}"' in src
    assert 'id="first-worker" hidden' in src
    # Versioned by the server's ?v= rewrite like every other module URL.
    assert "import('/account/bands/assets/manage.js')" in src


_HARNESS = r"""
class El{constructor(t){this.tag=t;this.children=[];this.attrs={};this.textContent='';this.className='';this.isConnected=true;}
 setAttribute(k,v){this.attrs[k]=v} append(...k){this.children.push(...k)} replaceChildren(...k){this.children=k}
 getClientRects(){return [1]} addEventListener(){} get text(){return [this.textContent,...this.children.map(c=>c.text||'')].join('\n')}}
globalThis.document={createElement:t=>new El(t),querySelector:()=>null,head:{append(){}}};
globalThis.addEventListener=()=>{};globalThis.setInterval=()=>0;globalThis.clearInterval=()=>{};
const posted=[];
const resp=d=>({ok:true,status:200,redirected:false,headers:{get:()=>'application/json'},json:async()=>d});
globalThis.fetch=async(url,opts={})=>{
  if(url==='/account/session')return resp({user:{csrf:'tok'},bands:[
    {id:'b1',label:'aaaa1111',name:'Home',active:1,role:'owner'},
    {id:'b2',label:'bbbb2222',name:'Lab',active:1,role:'owner'},
    {id:'b3',label:'cccc3333',name:'Guest',active:1,role:'member'}]});
  if(url==='/account/action'){const f=Object.fromEntries(opts.body);posted.push([f,opts.headers]);
    return resp({pairing:{code:'abc123',expires:Date.now()/1000+300,session:'s'},band_id:f.band_id,origin:'http://192.0.2.10:7005',csrf:'tok'});}
  throw Error('unexpected '+url);
};
const m=await import('./manage.mjs');
const lab=await m.mintPairing('bbbb2222');
const fallback=await m.mintPairing('cccc3333');   // not an owner there: first owned band
const panel=m.pairingPanel(lab);
console.log(JSON.stringify({posted,lab:lab.band_name,fallback:fallback.band_name,text:panel.node.text,
  cmds:m.pairCommands('http://192.0.2.10:7005','abc123')}));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_mint_pairing_and_panel(tmp_path):
    (tmp_path / "manage.mjs").write_text((WEB / "manage.js").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "t.mjs").write_text(_HARNESS, encoding="utf-8")
    run = subprocess.run(["node", str(tmp_path / "t.mjs")], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    assert [p[0] for p in out["posted"]] == [{"csrf": "tok", "op": "pair", "band_id": "b2"},
                                             {"csrf": "tok", "op": "pair", "band_id": "b1"}]
    assert out["posted"][0][1] == {"X-Rook-View": "account"}
    assert out["lab"] == "Lab" and out["fallback"] == "Home"
    url = f"http://{LAN}:7005/worker?band=abc123"
    assert [c[2] for c in out["cmds"]] == [f"curl -fsSL '{url}' | bash",
                                           f'iex (irm "{url}&os=windows")',
                                           f"pkg install -y curl && curl -fsSL '{url}' | bash"]
    for label, _cls, cmd, note in out["cmds"]:
        assert label in out["text"] and cmd in out["text"] and note in out["text"]
    # What to expect: UAC and the duration on Windows, the user service on
    # Linux/macOS (what the bootstrap scripts actually do).
    notes = {c[0]: c[3] for c in out["cmds"]}
    assert notes["Windows PowerShell"] == ("Opens an administrator prompt (UAC). Takes 2-5 minutes; "
                                           "the machine appears under Workers when done.")
    assert "systemd user service" in notes["Linux / macOS"] and "1-3 minutes" in notes["Linux / macOS"]
    assert notes["Android (Termux)"] == ""
    assert "abc123" in out["text"] and "seconds remaining" in out["text"]


@pytest.mark.asyncio
async def test_dashboard_login_to_paired_installer_over_http(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOK_SETUP_PATH", str(tmp_path / "setup.json"))
    monkeypatch.setenv("ROOK_ENROLLMENT_DB", str(tmp_path / "enrollment.db"))
    monkeypatch.setenv("ROOK_CHAT_DB", str(tmp_path / "chat.db"))
    monkeypatch.setenv("ROOK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ROOK_PUBLIC_BASE", raising=False)
    setup_store.save({"band_name": "home", "band_psk": "piano-haste-nugget-drift-mangle",
                      "hub_public": f"{LAN}:8765", "pyz_domain": f"http://{LAN}:7005"})
    EnrollmentStore().import_config()
    server = CombinedServer(web_user="owner", web_pass="password", port=7005,
                            domain=f"http://{LAN}:7005", hub_public=f"{LAN}:8765")
    origin = {"Origin": f"http://{LAN}:7005"}
    async with TestClient(TestServer(server._app)) as client:
        # What the button does, starting from a dashboard-only login.
        await client.post("/login", data={"user": "owner", "pass": "password"})
        session = await client.get("/account/session", headers={"Accept": "application/json"})
        assert session.status == 200 and len(session.history) == 1
        data = await session.json()
        band = next(b for b in data["bands"] if b["role"] == "owner" and b["active"])
        pair = await client.post("/account/action", data={"csrf": data["user"]["csrf"], "op": "pair",
                                                          "band_id": band["id"]},
                                 headers={"X-Rook-View": "account", **origin})
        assert pair.status == 200, await pair.text()
        grant = await pair.json()
        assert grant["origin"] == f"http://{LAN}:7005" and len(grant["pairing"]["code"]) == 6
        # The countdown's refresh keeps the same session.
        again = await client.post("/account/pairing", json={"csrf": data["user"]["csrf"],
                                                            "band_id": band["id"],
                                                            "session": grant["pairing"]["session"]},
                                  headers=origin)
        assert again.status == 200 and (await again.json())["code"] == grant["pairing"]["code"]
        # A wrong Origin is still refused (CSRF).
        refused = await client.post("/account/action", data={"csrf": data["user"]["csrf"], "op": "pair",
                                                             "band_id": band["id"]},
                                    headers={"X-Rook-View": "account", "Origin": "http://evil.example"})
        assert refused.status == 403
        script = await client.get(f"/worker?band={grant['pairing']['code']}&os=unix")
        text = await script.text()
        assert script.status == 200 and f"--enroll http://{LAN}:7005" in text
