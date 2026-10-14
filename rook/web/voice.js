// Manage > Voice: the voice hub plugin (rook/hub/plugins/voice). Where speech
// runs (this hub or another machine), whether it is ready, the address phones
// and the desktop app use, and a microphone check that records a few seconds
// in the browser and has the hub transcribe them. Settings are voice.* in the
// settings store; this page writes only the engine choice, through the same
// "set" action the Settings area uses.
// Versioned like this module (?v=...), so a stale edge cache cannot pair it
// with an old manage.js.
const {h, ago, useCss, btn, copy} = await import('/account/bands/assets/manage.js' + new URL(import.meta.url).search);

const API='/account/settings/api';
const RATE=16000, RECORD_S=4;

export async function mountVoice(root){
  useCss();
  let csrf='',d=null,busy=false,rec=null;
  const status=h('p',{class:'mg-status',role:'status'});
  const summary=h('p',{class:'mg-sub',style:'max-width:860px;line-height:1.6',
    text:'Voice lets phones, the desktop app and a browser talk to your agents through this hub. Speech recognition and the spoken replies run either here, on the hub, or on another machine with more power (a computer with a graphics card), which the hub forwards to.'});
  const stateBox=h('section',{class:'mg-box strong','aria-label':'Voice status'});
  const connectBox=h('section',{class:'mg-box','aria-label':'Connect a device'});
  const micBox=h('section',{class:'mg-box','aria-label':'Microphone check'});
  const panel=h('section',{class:'mg-box strong mg-side','aria-label':'Where speech runs'});
  root.replaceChildren(h('div',{class:'mg'},summary,status,
    h('div',{class:'mg-split'},h('div',{class:'mg-main'},stateBox,micBox,connectBox),panel)));

  async function get(){const r=await fetch(API+'?view=voice');const j=await r.json().catch(()=>({error:'Bad response'}));if(j.csrf)csrf=j.csrf;if(!r.ok||j.error)throw Error(j.error||'Sign in with your operator account to set up voice.');return j;}
  async function post(body){const r=await fetch(API,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csrf,...body})});const j=await r.json().catch(()=>({error:'Bad response'}));if(!r.ok||j.error)throw Error(j.error||'Request failed');return j;}
  async function refresh(){d=await get();draw();}
  const run=async(b,work,err)=>{if(busy)return;busy=true;if(b)b.disabled=true;if(err)err.textContent='';try{await work();}catch(e){if(err)err.textContent=e.message;else status.textContent=e.message;}finally{busy=false;if(b)b.disabled=false;}};
  const val=k=>d.settings[k]?.value;

  function state(){
    const s=d.status;
    if(!s.loaded)return ['Not running','mg-warn'];
    if(s.state==='waiting')return ['Waiting for the previous engine to finish loading','mg-warn'];
    if(s.state==='starting')return ['Starting: loading the speech models','mg-warn'];
    if(s.state==='error')return ['Failed to start','mg-bad'];
    const c=s.last_check;
    if(c&&!c.ok)return [s.engine==='remote'?'Voice server not answering':'Not answering','mg-bad'];
    return ['Ready','mg-good'];
  }
  function where(){
    const s=d.status;
    if(s.engine==='remote')return 'On another machine'+(s.remote_host?' ('+s.remote_host+')':val('remote_url')?' ('+hostOf(val('remote_url'))+')':'');
    return 'On this hub';
  }
  const hostOf=u=>{try{return new URL(u).host;}catch{return u||'';}};
  // Label beside the value, or above it on a phone.
  const row=(label,...value)=>h('div',{style:'display:flex;flex-wrap:wrap;align-items:center;gap:6px 12px;padding:9px 0;border-top:1px solid #20261d;font-size:13px'},h('span',{class:'mg-label',style:'flex:0 0 150px',text:label}),h('span',{style:'flex:1 1 220px;min-width:0;white-space:normal'},...value));

  function draw(){drawState();drawMic();drawConnect();drawPanel();}

  function drawState(){
    const s=d.status,req=d.requirements,[label,cls]=state(),err=h('small',{class:'mg-error',role:'alert'});
    const check=btn('Check connection','soft',()=>run(check,async()=>{
      const r=await post({action:'voice_check'});d.status=r.status;d.status.last_check=r.check;drawState();
      status.textContent=r.check.ok?'Voice answered'+(r.check.latency_ms!=null?' in '+r.check.latency_ms+' ms':'')+'.':'No answer: '+(r.check.error||r.check.state);},err));
    check.disabled=!s.loaded;
    const body=[row('State',h('b',{class:cls,text:label})),row('Speech runs',where())];
    if(s.loaded)body.push(row('Served at',h('span',{class:'mg-mono',text:(s.paths||[]).map(p=>p==='/voice'?'/voice/…':p).join('  ')})));
    if(s.last_check)body.push(row('Last check',ago(s.last_check.checked_at)+(s.last_check.version?' · voice server '+s.last_check.version:'')+(s.last_check.error?' · '+s.last_check.error:'')));
    if(s.error)body.push(row('Error',h('span',{class:'mg-bad',text:s.error})));
    const why=!s.loaded?(s.reason||req.reason):'';
    stateBox.replaceChildren(
      h('div',{class:'mg-panel-head'},h('div',{class:'mg-two'},h('h2',{text:'Voice'}),h('small',{text:'The voice plugin on worker rook'})),h('div',{class:'mg-actions'},err,check)),
      h('div',{class:'mg-pad',style:'gap:0'},...body),
      ...(why?[h('div',{class:'mg-pad',style:'border-top:1px solid var(--line)'},needs(why,req))]:[]));
  }
  // What is missing, and the one thing to do about it.
  function needs(why,req){
    const out=[h('p',{class:'mg-sub mg-warn',style:'white-space:normal;line-height:1.6;margin:0',text:why})];
    if(req.extras_missing&&req.extras_missing.length){
      const cmd=d.extras_hint,b=btn('Copy','',()=>copy(cmd,b));
      out.push(h('div',null,h('div',{class:'mg-label',style:'margin-bottom:6px',text:'Install on the hub, then restart it'}),h('div',{class:'mg-actions',style:'flex-wrap:nowrap'},h('code',{class:'mg-code',text:cmd}),b)),
        h('p',{class:'mg-sub',style:'white-space:normal;line-height:1.6',text:'This adds speech recognition and voices to the hub (about a gigabyte). A small hub does better forwarding voice to another machine: choose "On another machine" on the right.'}));
    }
    if(req.models_missing&&req.models_missing.length)
      out.push(h('p',{class:'mg-sub',style:'white-space:normal;line-height:1.6'},'Put these files in ',h('span',{class:'mg-mono',text:req.model_dir}),': ',h('span',{class:'mg-mono',text:req.models_missing.join(', ')}),'.'));
    return h('div',{style:'display:flex;flex-direction:column;gap:10px'},...out);
  }

  function drawConnect(){
    const origin=location.origin,ws=origin.replace(/^http/,'ws')+d.prefix+'/ws';
    const line=(label,text,href)=>{const b=btn('Copy','',()=>copy(text,b));return row(label,h('span',{class:'mg-actions',style:'flex-wrap:nowrap'},href?h('a',{class:'mg-code',href,target:'_blank',rel:'noopener',text}):h('code',{class:'mg-code',text}),b));};
    connectBox.replaceChildren(
      h('div',{class:'mg-panel-head'},h('div',{class:'mg-two'},h('h2',{text:'Connect a device'}),h('small',{text:'Phones, the desktop app and browsers use this hub\'s address'}))),
      h('div',{class:'mg-pad',style:'gap:0'},
        line('Voice address',ws),
        line('In a browser',origin+d.prefix+'/',d.status.loaded?d.prefix+'/':null),
        line('Voice keys',origin+d.prefix+'/admin',d.status.loaded?d.prefix+'/admin':null),
        h('p',{class:'mg-sub',style:'white-space:normal;line-height:1.6;padding-top:10px',text:'In the Rook app, set the voice server to the voice address and paste a voice key. Keys are made on the voice keys page'+(d.status.engine==='remote'?', which belongs to the voice server this hub forwards to.':'.')})));
  }

  // -- microphone check ------------------------------------------------------
  function drawMic(){
    const ready=d.status.loaded&&d.status.state==='ready';
    const meter=h('div',{class:'mg-meter',style:'max-width:none'},h('i',{style:'width:0'}));
    const said=h('pre',{hidden:true}),err=h('small',{class:'mg-error',role:'alert'}),note=h('span',{class:'mg-sub'});
    const go=btn('Record '+RECORD_S+' seconds','primary',()=>rec?rec.stop():start());
    go.disabled=!ready;
    async function start(){
      err.textContent='';said.hidden=true;
      if(!navigator.mediaDevices?.getUserMedia){err.textContent='This browser cannot record here (it needs https, or localhost).';return;}
      try{rec=await record(RECORD_S,level=>{meter.firstChild.style.width=Math.min(100,level*400)+'%';},left=>{note.textContent='Listening… '+left+' s';});}
      catch(e){err.textContent=e.name==='NotAllowedError'?'The browser did not allow the microphone.':'Could not start recording: '+e.message;rec=null;return;}
      go.textContent='Stop';
      const pcm=await rec.done;rec=null;go.textContent='Record '+RECORD_S+' seconds';meter.firstChild.style.width='0';
      note.textContent='Transcribing…';
      await run(go,async()=>{
        const r=await post({action:'voice_transcribe',pcm:b64(pcm)});
        said.hidden=false;said.className='quote';said.textContent=r.text?r.text:'(nothing intelligible: try again closer to the microphone)';
        note.textContent=r.seconds+' s of audio, transcribed in '+(r.latency_ms/1000).toFixed(1)+' s'+(d.status.engine==='remote'?' by the voice server.':' on this hub.');
      },err);
      if(err.textContent)note.textContent='';
    }
    micBox.replaceChildren(
      h('div',{class:'mg-panel-head'},h('div',{class:'mg-two'},h('h2',{text:'Microphone check'}),h('small',{text:'Say something; the hub writes down what it heard'}))),
      h('div',{class:'mg-pad'},
        ready?null:h('p',{class:'mg-sub',text:'Available once voice is ready.'}),
        h('div',{class:'mg-actions',style:'flex-wrap:nowrap'},go,meter),note,said,err,
        h('p',{class:'mg-sub',style:'white-space:normal;line-height:1.6',text:'The recording goes to the hub once, for this check, and is not kept.'})));
  }

  // -- where speech runs ------------------------------------------------------
  function drawPanel(){
    const err=h('small',{class:'mg-error',role:'alert'});
    const engine=val('engine')||'here',locked=k=>!!d.settings[k]?.locked;
    const choice=(v,title,text)=>h('label',{class:'mg-box',style:'display:flex;gap:12px;padding:12px 14px;cursor:pointer;align-items:flex-start'},
      h('input',{type:'radio',name:'voice-engine',value:v,checked:engine===v,disabled:locked('engine'),style:'margin-top:2px;accent-color:var(--accent);width:18px;height:18px;min-height:0'}),
      h('span',{class:'mg-two',style:'gap:4px'},h('b',{text:title,style:'white-space:normal'}),h('small',{text,style:'white-space:normal;line-height:1.5'})));
    const here=choice('here','On this hub','Uses the hub\'s own processor. Needs the voice extras and the speech model files. Best on a home server.');
    const remote=choice('remote','On another machine','Forwards to a voice server on a computer with more power. The hub stays small; devices still use the hub\'s address.');
    const url=h('input',{value:val('remote_url')||'',placeholder:'https://gpu.example.com:8900',autocomplete:'off',spellcheck:'false',style:'font-family:var(--mono)',disabled:locked('remote_url')});
    const tls=h('input',{type:'checkbox',checked:val('remote_verify_tls')!==false,disabled:locked('remote_verify_tls')});
    const urlField=h('label',{class:'mg-field'},'Voice server address',url,h('small',{class:'mg-sub',style:'white-space:normal',text:locked('remote_url')?'Set by '+d.settings.remote_url.env+' in the hub\'s environment.':'Where the standalone voice server listens (python -m services.voice.server).'}));
    const tlsField=h('label',{class:'mg-actions',style:'font-size:13px;color:var(--dim);flex-wrap:nowrap'},tls,'Check its certificate (turn off only for a self-signed one on your network)');
    const pick=()=>root.querySelector('input[name=voice-engine]:checked')?.value||engine;
    const sync=()=>{const r=pick()==='remote';urlField.hidden=tlsField.hidden=!r;};
    for(const c of [here,remote])c.querySelector('input').addEventListener('change',sync);
    const save=btn('Save','primary',()=>run(save,async()=>{
      const want={engine:pick()};
      if(want.engine==='remote'){want.remote_url=url.value.trim();want.remote_verify_tls=tls.checked;
        if(!want.remote_url)throw Error('Enter the voice server\'s address.');}
      const changed=Object.entries(want).filter(([k,v])=>!locked(k)&&v!==val(k));
      for(const [k,v] of changed)await post({action:'set',key:'voice.'+k,value:v,scope:'hub',note:'Voice page'});
      status.textContent=changed.length?'Saved. Voice restarts with it; this takes a few seconds.':'No changes.';
      setTimeout(()=>refresh().catch(()=>{}),1500);setTimeout(()=>refresh().catch(()=>{}),5000);
    },err));
    const lockNote=locked('engine')?h('p',{class:'mg-sub mg-warn',style:'white-space:normal',text:'Set by '+d.settings.engine.env+' in the hub\'s environment; change it there.'}):null;
    panel.replaceChildren(
      h('div',{class:'mg-panel-head'},h('div',{class:'mg-two'},h('h2',{text:'Where speech runs'}),h('small',{text:'Stored as voice.engine in Settings · hub-wide'}))),
      h('div',{class:'mg-pad'},here,remote,lockNote,urlField,tlsField,h('div',{class:'mg-actions end'},err,save),
        h('p',{class:'mg-sub',style:'white-space:normal;line-height:1.6'},'Voices, recognition and the assistant\'s name are in ',h('a',{href:'#settings?view=plugin&target=voice',text:'all voice settings'}),'.')));
    sync();
  }

  return {
    activate(){refresh().catch(e=>{stateBox.replaceChildren(h('p',{class:'mg-empty mg-bad',text:e.message}));micBox.replaceChildren();connectBox.replaceChildren();panel.replaceChildren();});},
    deactivate(){if(rec)rec.stop();}};
}

// Record up to `seconds` of 16 kHz mono audio. Resolves to {stop(), done}:
// done resolves to an Int16Array once recording ends (time up or stop()).
async function record(seconds,onLevel,onTick){
  const stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true}});
  const ctx=new AudioContext({sampleRate:RATE});
  const src=ctx.createMediaStreamSource(stream),node=ctx.createScriptProcessor(4096,1,1);
  const chunks=[];let total=0,finish;
  const done=new Promise(r=>finish=r);
  node.onaudioprocess=e=>{const x=e.inputBuffer.getChannelData(0);chunks.push(new Float32Array(x));total+=x.length;
    let s=0;for(let i=0;i<x.length;i++)s+=x[i]*x[i];onLevel(Math.sqrt(s/x.length));
    if(total>=seconds*ctx.sampleRate)stop();};
  src.connect(node);node.connect(ctx.destination);
  let left=seconds;onTick(left);
  const tick=setInterval(()=>{left=Math.max(0,left-1);onTick(left);},1000);
  let stopped=false;
  function stop(){
    if(stopped)return;stopped=true;clearInterval(tick);
    node.disconnect();src.disconnect();stream.getTracks().forEach(t=>t.stop());
    const rate=ctx.sampleRate;ctx.close();
    const flat=new Float32Array(total);let o=0;for(const c of chunks){flat.set(c,o);o+=c.length;}
    finish(toPcm16(rate===RATE?flat:resample(flat,rate,RATE)));
  }
  return {stop,done};
}
function resample(x,from,to){const n=Math.floor(x.length*to/from),out=new Float32Array(n);for(let i=0;i<n;i++)out[i]=x[Math.min(x.length-1,Math.floor(i*from/to))];return out;}
function toPcm16(x){const out=new Int16Array(x.length);for(let i=0;i<x.length;i++){const v=Math.max(-1,Math.min(1,x[i]));out[i]=v<0?v*0x8000:v*0x7fff;}return out;}
function b64(pcm){const bytes=new Uint8Array(pcm.buffer,pcm.byteOffset,pcm.byteLength);let s='';for(let i=0;i<bytes.length;i+=0x8000)s+=String.fromCharCode.apply(null,bytes.subarray(i,i+0x8000));return btoa(s);}
