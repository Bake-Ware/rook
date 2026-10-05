const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
const element = () => ({classList:{add(){}},addEventListener(){},appendChild(){},scrollIntoView(){},value:'af_heart'});
let stopped=0, scheduled=0;
class AudioContext {
 constructor(){this.currentTime=0;this.audioWorklet={addModule:async()=>{}};this.destination={};}
 createMediaStreamSource(){return {connect(){}};}
 createGain(){return {gain:{},connect(){}};}
 createBuffer(ch,n,sr){return {duration:n/sr,copyToChannel(){}};}
 createBufferSource(){return {connect(){},start(){scheduled++},stop(){stopped++}};}
}
class Worklet {constructor(){this.port={};}connect(){}}
class Socket {constructor(){this.readyState=1;this.sent=[];}send(data){this.sent.push(data);}close(){}}
const ctx = vm.createContext({console,Float32Array,Int16Array,ArrayBuffer,DataView,Math,JSON,Blob,URLSearchParams,
 URL:{createObjectURL(){return 'blob:test'}},crypto:{randomUUID(){return 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'}},
 localStorage:{getItem(){return null},setItem(){}},location:{search:'',protocol:'https:',host:'voice.test'},
 navigator:{mediaDevices:{getUserMedia:async()=>({getAudioTracks:()=>[{getSettings:()=>({echoCancellation:true})}]})}},
 document:{getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id)},createElement:element,addEventListener(){}},
 fetch:async()=>({json:async()=>({voices:[],default:'af_heart'})}),AudioContext,AudioWorkletNode:Worklet,WebSocket:Socket});
const source=fs.readFileSync(__dirname+'/../services/voice/static/index.html','utf8').split('<script>')[1].split('</script>')[0];
vm.runInContext(source,ctx);
(async()=>{
 await vm.runInContext('connect(); ws.onopen()',ctx);
 const messages=()=>vm.runInContext('ws.sent.filter(x=>typeof x==="string").map(x=>JSON.parse(x))',ctx);
 assert.equal(messages()[0].type,'hello');assert.equal(messages()[0].protocol,2);
 assert.equal(messages()[1].type,'audio_config');assert.equal(messages()[1].aec,true);
 vm.runInContext('activeTurn=3; setState("speaking");',ctx);
 function frame(){vm.runInContext('workletNode.port.onmessage({data:{rms:0.1,pcm:new ArrayBuffer(640)}})',ctx)}
 for(let i=0;i<5;i++)frame();assert.equal(messages().filter(x=>x.type==='speech_start').length,0);
 frame();assert.equal(messages().filter(x=>x.type==='speech_start').length,1);
 function packet(turn){ctx.testTurn=turn;vm.runInContext('let b=new ArrayBuffer(12);let d=new DataView(b);d.setUint32(0,0x524b3241);d.setUint32(4,testTurn);schedulePCM(b);',ctx);vm.runInContext('b=undefined;d=undefined;',ctx)}
 packet(3);assert.equal(scheduled,0,'audio from interrupted turn must be dropped');
 ctx.testTurn=4;vm.runInContext('activeTurn=4; let b2=new ArrayBuffer(12);let d2=new DataView(b2);d2.setUint32(0,0x524b3241);d2.setUint32(4,4);schedulePCM(b2);',ctx);
 assert.equal(scheduled,1,'next turn should play');
 vm.runInContext('manualStop();schedulePCM(b2)',ctx);assert.equal(scheduled,1);assert.equal(stopped,1);
 assert.equal(messages().at(-1).type,'stop');
 console.log('Browser protocol, echo-cancellation negotiation, speech interruption, stale-audio rejection, and immediate stop: passed');
})().catch(e=>{console.error(e);process.exitCode=1});
