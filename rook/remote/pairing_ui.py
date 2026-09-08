"""Pairing and PSK lifecycle controls embedded in the existing Tokens page."""

import html


def pairing_section(bands: list[dict]) -> str:
    options = ''.join(
        f'<option value="{html.escape(b["id"])}">{html.escape(b["name"])}'
        f' ({html.escape(b["label"])}){" — revoked" if not b["active"] else ""}</option>'
        for b in bands)
    return """
<section id="pairing">
<style>#pair-new-key{word-break:normal;overflow-wrap:anywhere;user-select:all}</style>
<h2>Join a band</h2>
<p><small>A pairing code installs a worker into one band. It expires after five
minutes and rolls while this page is open. No worker login is required.</small></p>
<label>Band <select id="pair-band">""" + options + """</select></label>
<button type="button" id="pair-start">Start pairing</button>
<button type="button" class="warn" id="pair-stop">Revoke code</button>
<div id="pair-output" hidden>
  <div class="tok-box" id="pair-code" style="font-size:2rem;letter-spacing:.15em"></div>
  <small id="pair-expiry"></small>
  <p><code id="pair-command" style="overflow-wrap:anywhere"></code></p>
  <button type="button" id="pair-copy">Copy installer command</button>
  <p><small>Windows PowerShell:</small><br><code id="pair-windows" style="overflow-wrap:anywhere"></code></p>
</div>
<p id="pair-status" role="status"></p>
<h2>Band key</h2>
<p><small>The five-word PSK is permanent until replaced. Replacing it requires
devices to re-enroll. Revocation stops new joins and site control. Devices still
using an old key can remain accessible through it until reconfigured or taken offline.</small></p>
<input id="pair-new-psk" type="password" autocomplete="new-password"
  autocapitalize="off" spellcheck="false" placeholder="New PSK (blank generates five words)">
<button type="button" id="pair-rotate">Replace PSK</button>
<button type="button" class="warn" id="pair-revoke-band">Revoke band</button>
<div id="pair-new-key" class="tok-box" hidden></div>
</section>
<script>
(() => {
  const $ = id => document.getElementById(id);
  let running=false, grant=null, generation=0, pending=false;
  const status = text => { $('pair-status').textContent=text; };
  function clear(){ running=false; grant=null; generation++; $('pair-output').hidden=true; $('pair-new-key').hidden=true; }
  async function post(path, body){
    const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Rook-Request':'tokens'},body:JSON.stringify(body)});
    const data=await r.json(); if(!r.ok)throw new Error(data.error||('HTTP '+r.status)); return data;
  }
  async function refresh(){
    if(!running||pending)return;
    const version=generation, band=$('pair-band').value; pending=true;
    try {
      const d=await post('/tokens/pairing',{band_id:band,...(grant?{session:grant.session}:{})});
      if(version!==generation)return;
      grant=d; $('pair-output').hidden=false; $('pair-code').textContent=d.code;
      $('pair-command').textContent=d.command; $('pair-windows').textContent=d.windows;
      status('Anyone with this current code can join the selected band.'); tick();
    } catch(e){ if(version===generation){clear();status(e.message);} }
    finally {pending=false;}
  }
  function tick(){
    if(!grant)return;
    const left=Math.max(0,Math.ceil(grant.expires-Date.now()/1000));
    $('pair-expiry').textContent=left ? `Expires in ${Math.floor(left/60)}:${String(left%60).padStart(2,'0')}` : 'Refreshing code…';
    if(!left&&running&&!document.hidden)refresh();
  }
  $('pair-band').onchange=()=>{clear();status('Select Start pairing to generate a code.');};
  $('pair-start').onclick=()=>{clear();running=true;refresh();};
  $('pair-copy').onclick=async()=>{try {await navigator.clipboard.writeText($('pair-command').textContent);status('Copied.');} catch(e){status('Select and copy the command above.');}};
  $('pair-stop').onclick=async()=>{clear();try{await post('/tokens/pairing/revoke',{band_id:$('pair-band').value});status('Pairing code revoked.');}catch(e){status(e.message);}};
  async function change(revoke){
    if(!confirm(revoke?'Revoke this band? Existing workers must be re-enrolled with a replacement key.':'Replace this band’s PSK? Existing workers must be re-enrolled.'))return;
    clear();
    try{
      const d=await post('/tokens/bands/'+(revoke?'revoke':'rotate'),{band_id:$('pair-band').value,psk:$('pair-new-psk').value});
      $('pair-new-psk').value='';
      if(d.psk){$('pair-new-key').textContent=d.psk;$('pair-new-key').hidden=false;}
      const option=$('pair-band').selectedOptions[0];
      if(option)option.textContent=revoke?option.textContent.replace(/ — revoked$/,'')+' — revoked':d.name+' ('+d.label+')';
      status(revoke?'Band revoked. Replace its PSK to enable it again.':'PSK replaced. Save the new key, or start pairing to re-enroll devices.');
    }catch(e){status(e.message);}
  }
  $('pair-rotate').onclick=()=>change(false); $('pair-revoke-band').onclick=()=>change(true);
  setInterval(tick,1000);
})();
</script>
"""
