"""The Plugins page and the presets step in the browser-facing files:
index.html wiring, the /setup wizard and plugins.js's preset picker."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from rook.hub import plugin_profiles as pp
from rook.hub.settings_store import SettingsStore

WEB = Path(__file__).resolve().parents[1] / "rook" / "web"


# -- the setup wizard and the pages ----------------------------------------------------------

@pytest.mark.asyncio
async def test_setup_wizard_stores_the_preset_choice(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    from rook.remote.bootstrap import CombinedServer
    monkeypatch.setenv("ROOK_SETUP_PATH", str(tmp_path / "setup.json"))
    monkeypatch.setenv("ROOK_ENROLLMENT_DB", str(tmp_path / "enrollment.db"))
    monkeypatch.setenv("ROOK_CHAT_DB", str(tmp_path / "chat.db"))
    monkeypatch.setenv("ROOK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ROOK_SETTINGS_DB", raising=False)
    monkeypatch.delenv("ROOK_BAND_PSK", raising=False)
    server = CombinedServer(bind="127.0.0.1", port=7005)
    async with TestClient(TestServer(server._app)) as client:
        page = await (await client.get("/setup")).text()
        assert "What do you want Rook to do?" in page and 'name="preset" value="hardware"' in page
        assert "Keyboard and mouse control (hid)" in page
        assert 'value="remote" checked' in page and 'value="knowledge">' in page
        form = {"csrf": server._setup_csrf, "band_name": "home", "hub_public": "hub.example.com:443",
                "pyz_domain": "hub.example.com", "band_psk": "piano-haste-nugget-drift-mangle",
                "presets_shown": "1"}
        # The default ticks, with Knowledge ticked and Hardware unticked.
        picks = [p["id"] for p in pp.PRESETS if p["default"] and p["id"] != "hardware"]
        body = list(form.items()) + [("preset", p) for p in picks + ["knowledge"]]
        r = await client.post("/setup", data=body, allow_redirects=False)
        assert r.status == 302
    store = SettingsStore()
    band = server._enrollment.bands()[0]
    defaults = pp.worker_defaults(band["id"], store)
    assert defaults["hid"] is False and defaults["memory"] is True and "shell" not in defaults
    assert pp.hub_state(store)["knowledge"] is True and store.get("knowledge.enabled", "hub", "")["value"]
    assert store.history(key=pp.WORKER_DEFAULTS_KEY)[0]["actor"] == "human:setup"


def test_index_has_the_plugins_view_and_the_preset_step():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'id="tab-plugins"' in html and 'id="view-plugins"' in html
    assert "import('/account/plugins/assets/plugins.js')" in html and "pluginsUI?.deactivate()" in html
    for host in ("first-worker-presets", "install-presets"):
        assert f'id="{host}"' in html
    # The preset step comes before the pairing button on both paths.
    for presets, button in (("first-worker-presets", "first-worker-pairing-btn"),
                            ("install-presets", "install-pairing-btn")):
        assert html.index(presets) < html.index(button)


_HARNESS = r"""
class El{constructor(t){this.tag=t;this.children=[];this.attrs={};this.textContent='';this.className='';this.style='';this.dataset={};this.isConnected=true;}
 setAttribute(k,v){this.attrs[k]=v} append(...k){this.children.push(...k.filter(x=>x!=null))} replaceChildren(...k){this.children=k.filter(x=>x!=null)}
 get classList(){const s=this;return {toggle(c,on){s.className=on?s.className+' '+c:s.className.replace(' '+c,'')}}}
 querySelector(){return null} addEventListener(){} getClientRects(){return [1]}
 get text(){return [this.textContent,...this.children.map(c=>c&&c.text||'')].join('\n')}}
globalThis.document={createElement:t=>new El(t),querySelector:()=>({}),head:{append(){}}};
globalThis.addEventListener=()=>{};
const posted=[];
const resp=(d,s=200)=>({ok:s<400,status:s,json:async()=>d});
globalThis.fetch=async(url,opts={})=>{
  if(url.startsWith('/account/plugins/api?'))return resp({csrf:'k',band:'b1',bands:[{id:'b1',name:'Home',active:1}],presets:[
    {id:'remote',title:'Remote control',summary:'s',tiers:['read','exec'],risk:{tiers:['read','exec']},high_risk:['Runs shell commands (exec)'],selected:true},
    {id:'hardware',title:'Hardware',summary:'s',tiers:['read','exec'],risk:{tiers:['read','exec']},high_risk:['Screen capture'],selected:false}]});
  if(url==='/account/plugins/api'){posted.push(JSON.parse(opts.body));return resp({ok:true,presets:['remote'],band:'b1',hub:[]});}
  throw Error('unexpected '+url);
};
const m=await import('./plugins.mjs');
let applied=null;
const p=m.presetPicker({onApplied:r=>{applied=r;}});
await new Promise(r=>setTimeout(r,10));
const find=(n,f)=>{if(f(n))return n;for(const c of n.children||[]){const x=find(c,f);if(x)return x;}return null;};
const apply=find(p.node,n=>n.tag==='button'&&n.textContent==='Apply');
await apply.onclick();
console.log(JSON.stringify({text:p.node.text,posted,applied}));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_preset_picker_posts_the_choice(tmp_path):
    src = (WEB / "plugins.js").read_text(encoding="utf-8")
    first = src.index("\n", src.index("const {h, useCss"))
    manage = (WEB / "manage.js").read_text(encoding="utf-8")
    (tmp_path / "manage.mjs").write_text(manage, encoding="utf-8")
    (tmp_path / "plugins.mjs").write_text(
        "const {h, useCss: _u, btn, chips, dialog} = await import('./manage.mjs');const useCss=()=>{};"
        + src[first:], encoding="utf-8")
    (tmp_path / "t.mjs").write_text(_HARNESS, encoding="utf-8")
    run = subprocess.run(["node", str(tmp_path / "t.mjs")], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    assert out["posted"] == [{"csrf": "k", "action": "apply_presets", "presets": ["remote"], "band": "b1"}]
    assert "Runs shell commands (exec)" in out["text"] and "Screen capture" in out["text"]
    assert out["applied"]["band"] == "b1"
