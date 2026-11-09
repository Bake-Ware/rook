/* rookie.js — avatar for Rook. One state machine, pluggable skins. No dependencies. */
window.Rookie = (function(){
  const COLS=32, ROWS=25, OX=7, OY=6;
  // o outline · s body · S shade · d dark · h highlight · g pupil · w dark panel · y amber · r red · b blue · t/T green · n/N wood · c blush
  const BASE={o:'#1b1a2e',s:'#c9c3d9',S:'#8f88a8',d:'#6c6587',h:'#eeeaf7',g:'#5a5480',y:'#f2b84b',r:'#ef5e6a',b:'#8fd3f4',t:'#6fcf97',T:'#2f7a52',w:'#3a3650',n:'#8a6a3a',N:'#5e4524',c:'#f0a0ae',wire:'#6fe3ff'};
  const THEMES={
    stone:  {},
    rook:   {o:'#0f0e17',s:'#f2b84b',S:'#c8902e',d:'#8a6420',h:'#ffd889',g:'#4a3a16',w:'#2a2238',y:'#8fd3f4',r:'#ef5e6a',b:'#ece8f6',t:'#6fcf97',T:'#2f7a52',n:'#6c6587',N:'#4a4663',c:'#ffb3a0',wire:'#f2b84b'},
    gameboy:{o:'#0f380f',s:'#8bac0f',S:'#306230',d:'#306230',h:'#9bbc0f',g:'#0f380f',w:'#306230',y:'#9bbc0f',r:'#0f380f',b:'#9bbc0f',t:'#8bac0f',T:'#306230',n:'#306230',N:'#0f380f',c:'#8bac0f',wire:'#9bbc0f'},
    amber:  {o:'#1a0f00',s:'#e39a1c',S:'#b06f00',d:'#7a4a00',h:'#ffc54d',g:'#3b2300',w:'#3b2300',y:'#ffd37a',r:'#ffb347',b:'#ffc54d',t:'#e39a1c',T:'#7a4a00',n:'#b06f00',N:'#7a4a00',c:'#ffb347',wire:'#ffb347'},
    cga:    {o:'#000000',s:'#ff55ff',S:'#aa00aa',d:'#550055',h:'#ffffff',g:'#222222',w:'#222222',y:'#ffffff',r:'#ff55ff',b:'#55ffff',t:'#55ffff',T:'#008888',n:'#aa00aa',N:'#550055',c:'#ff55ff',wire:'#55ffff'},
    ghost:  {o:'#dfe7ff',s:'#1c1a29',S:'#2a2740',d:'#3a3650',h:'#2f2c42',g:'#9aa4ff',w:'#14131c',y:'#f2b84b',r:'#ef5e6a',b:'#8fd3f4',t:'#6fcf97',T:'#2f7a52',n:'#5a5480',N:'#3a3650',c:'#8fd3f4',wire:'#dfe7ff'},
    rose:   {o:'#4a2a3a',s:'#ffd6e7',S:'#f3a6c6',d:'#d97aa6',h:'#fff0f6',g:'#7a4a66',w:'#3a2030',y:'#ffd166',r:'#ff6b6b',b:'#9ad0ff',t:'#8be0a8',T:'#3f9a60',n:'#b07a4a',N:'#7a5230',c:'#ff9fbf',wire:'#ff9fbf'},
    matrix: {o:'#00140a',s:'#1fbf5a',S:'#128a3f',d:'#0b5a2a',h:'#7dffb0',g:'#0b3a1c',w:'#062412',y:'#baffc9',r:'#ff4d4d',b:'#7dffb0',t:'#3dff7a',T:'#128a3f',n:'#128a3f',N:'#0b5a2a',c:'#3dff7a',wire:'#3dff7a'},
  };
  const THEME_LIST=Object.keys(THEMES);
  let PAL=Object.assign({},BASE);
  const palFor=name=>Object.assign({},BASE,THEMES[name]||{});
  const BODY=[
    '.ooo..oooo..ooo.','.ohs..ohss..ohs.','.oooooooooooooo.','.ohhhhhhhhhhhSo.','.ohsssssssssSSo.','.ohsdsssssdsSSo.','.oohssssssssSoo.',
    '..ohsssssssSSo..','..ohsssssssSSo..','..ohsdsssssdSo..','.oohssssssssSoo.','.ohsssssssssSSo.','.ohsSSSSSSSSSSo.','.oooooooooooooo.',
    'ohhsssssssssSSSo','oSSSSSSSSSSSSSSo','oooooooooooooooo'];
  const SIDE=['.oooooo.','.ohsssSo','oooooooo','ohhhhhSo','ohsssSSo','ohsdsSSo','ohsssSSo','.ohssSo.','.ohssSo.','.ohsdSo.','ohsssSSo','ohsssSSo','ohSSSSSo','oooooooo','ohssssSo','oSSSSSSo','oooooooo'];
  const MOOD=['idle','listening','thinking','waiting','confused','talking','happy','error','sleeping'];
  const DOING=['loading','searching','websearch','reading','writing','delete','exec','downloading','uploading','testing','deploying','spawning','messaging','locking','music','working'];
  const TRANS=['enter','exit','hidden'];
  const STATES=MOOD.concat(DOING,TRANS);
  const TRANS_LEN=14;
  const ACTS={turn:14, yoyo:22, stretch:8};
  const instances=[]; let tick=0, timer=null;
  const reduce=window.matchMedia&&matchMedia('(prefers-reduced-motion: reduce)').matches;
  const rand=n=>Math.floor(Math.random()*n);
  const hash=n=>{ n=(n*2654435761)>>>0; n^=n>>>13; n=(n*0x5bd1e995)>>>0; return n^(n>>>15); };
  const hex=c=>{ const s=(PAL[c]||c).replace('#',''); return [parseInt(s.slice(0,2),16),parseInt(s.slice(2,4),16),parseInt(s.slice(4,6),16)]; };
  const rgb=a=>'rgb('+a.map(v=>Math.max(0,Math.min(255,Math.round(v)))).join(',')+')';
  const mix=(c,to,t)=>{ const a=hex(c), b=typeof to==='string'?hex(to):to; return rgb([0,1,2].map(i=>a[i]+(b[i]-a[i])*t)); };
  const lighten=(c,t)=>mix(c,[255,255,255],t), darken=(c,t)=>mix(c,[0,0,0],t);

  // ------------------------------------------------------------------ shared pose
  function pose(st,f,inst){
    const act=(st==='idle'&&inst.act)?inst.act:null, ph=act?f-act.t0:0;
    let dx=0,dy=0;
    if(st==='idle'){ if(act&&act.name==='stretch') dy=(ph>=2&&ph<=5)?-1:0; else if(!act) dy=(f%8<4)?0:1; }
    else if(st==='listening') dx=(f%6<3)?0:1;
    else if(st==='working'||st==='writing') dy=(f%3===2)?1:0;
    else if(st==='happy') dy=[0,-2,-3,-2,0,0][f%6];
    else if(st==='error') dx=(f%2)?1:-1;
    else if(st==='sleeping') dy=(f%12<6)?0:1;
    else if(st==='loading') dy=(f%4<2)?0:1;
    else if(st==='waiting') dy=(f%10<5)?0:1;
    else if(st==='confused') dx=(f%8<4)?0:1;
    else if(st==='music') dy=f%2;
    else if(st==='delete'){ const p=f%12; if(p===4||p===5) dx=(f%2)?1:-1; }
    let view='front';
    if(act&&act.name==='turn'){ view = ph<2?'front' : ph<4?'side' : ph<10?'back' : ph<12?'side' : 'front'; }
    const blink=inst.blinkUntil>f, dp=f%12;
    let eyes= st==='happy'?'happy' : st==='error'?'x' : (st==='sleeping'||blink)?'closed' : (st==='working'||st==='spawning'||st==='deploying')?'squint' : st==='listening'?'wide' : 'normal';
    if(act&&act.name==='stretch'&&ph>=1&&ph<=6) eyes='closed';
    if(st==='delete'&&dp>=4&&dp<=6) eyes='closed';
    if(st==='music') eyes=(f%10<7)?'closed':'happy';
    if(st==='messaging'&&f%8>=6) eyes='happy';
    let eyesL=eyes; if(st==='confused') eyesL='wide';
    let lx=0,ly=0;
    if(st==='thinking'){lx=1;ly=-1}
    else if(st==='listening'){lx=1}
    else if(st==='idle'){ if(act&&act.name==='yoyo'){ lx=1; ly=inst.yoyoLow?1:0; } else {lx=inst.look.x;ly=inst.look.y} }
    else if(st==='searching'){ lx=1; ly=[0,0,1,1,1,0][f%6]; }
    else if(st==='writing'||st==='testing'||st==='locking'){ lx=1; ly=1; }
    else if(st==='reading'){ lx=(f%8<4)?0:1; ly=1; }
    else if(st==='websearch'||st==='spawning'||st==='waiting'||st==='delete'){ lx=1; }
    else if(st==='downloading'){ const ay=3+(f%5)*2; lx=1; ly=ay<7?-1:ay<10?0:1; }
    else if(st==='uploading'){ const ay=11-(f%5)*2; lx=1; ly=ay<6?-1:ay<9?0:1; }
    else if(st==='messaging'){ lx=1; ly=-1; }
    let brow=null; if(st==='working'||st==='spawning'||st==='deploying'||st==='delete') brow='angry'; else if(st==='waiting'||st==='confused') brow='raised';
    let mouth='small2';
    if(st==='happy') mouth='smile'; else if(st==='error') mouth='frown'; else if(st==='talking') mouth=['small2','wide','open'][f%3];
    else if(st==='working'||st==='spawning'||st==='deploying'||st==='delete') mouth='flat'; else if(st==='sleeping'||st==='listening'||st==='music') mouth='dot';
    else if(st==='thinking'||st==='searching'||st==='waiting') mouth='small'; else if(st==='confused') mouth='wobble'; else if(st==='messaging') mouth='grin';
    else if(st==='reading') mouth='none'; else if(act&&act.name==='stretch'&&ph>=2&&ph<=5) mouth='open';
    return {act,ph,dx,dy,view,eyes,eyesL,lx,ly,brow,mouth};
  }

  // ------------------------------------------------------------------ pixel rasterizer (also feeds voxel + ascii)
  function raster(st,f,inst,plot){
    const po=pose(st,f,inst), act=po.act, ph=po.ph, dx=po.dx, dy=po.dy, view=po.view;
    const P=(x,y,c)=>{ if(!c||c==='.')return; plot(x,y,c); };
    const S=(x,y,c)=>P(OX+x+dx,OY+y+dy,c);
    for(let x=2;x<14;x++) plot(OX+x,OY+17,'sh');
    if(view==='side'){ for(let y=0;y<SIDE.length;y++) for(let x=0;x<8;x++) S(4+x,y,SIDE[y][x]); }
    else { for(let y=0;y<BODY.length;y++) for(let x=0;x<16;x++) S(x,y,BODY[y][x]); }
    if(view==='back'){ for(let y=6;y<=9;y++) for(let x=5;x<=10;x++) S(x,y,'S'); for(let x=5;x<=10;x++){S(x,6,'o');S(x,9,'o');} for(let y=6;y<=9;y++){S(5,y,'o');S(10,y,'o');} S(6,7,'o');S(9,8,'o'); }
    const showFace = view==='front';
    if(!showFace){}
    else if(st==='exec'){
      for(let y=5;y<=11;y++) for(let x=4;x<=11;x++) S(x,y,'o');
      S(4,5,'w');S(11,5,'w');S(4,11,'w');S(11,11,'w');
      const line=Math.floor(f/2);
      for(let r=0;r<3;r++){ const y=6+r*2, bits=hash(line+r*7); const len=3+(bits&3), bright=r===2;
        for(let i=0;i<len&&i<6;i++){ S(5+i,y,(bits>>(i+2))&1?(bright?'t':'T'):(bright?'T':'w')); } if(r===2&&f%2===0) S(5+len,y,'t'); }
      S(5,6,'t');
    }
    else if(st==='loading'){
      const ring=[[7,5],[8,5],[9,6],[10,7],[10,8],[9,9],[8,10],[7,10],[6,9],[5,8],[5,7],[6,6]]; const k=f%12;
      ring.forEach(([x,y],i)=>{ const d=(k-i+12)%12; if(d===0)S(x,y,'y'); else if(d===1)S(x,y,'S'); else if(d===2)S(x,y,'d'); else if(d===3)S(x,y,'g'); });
      S(7,8,'o');S(8,8,'o');
    }
    else {
      for(const [ex,ey] of [[4,6],[10,6]]){
        const x=ex+po.lx,y=ey+po.ly, k= ex===4?po.eyesL:po.eyes;
        if(k==='normal'){ S(x,y,'o');S(x+1,y,'o');S(x,y+1,'o');S(x+1,y+1,'o');S(x,y,'g'); }
        else if(k==='wide'){ for(let i=0;i<2;i++)for(let j=0;j<3;j++)S(x+i,y-1+j,'o'); S(x,y-1,'g'); }
        else if(k==='squint'||k==='closed'){ S(ex,ey+1,'o');S(ex+1,ey+1,'o'); }
        else if(k==='happy'){ S(ex-1,ey+1,'o');S(ex,ey,'o');S(ex+1,ey,'o');S(ex+2,ey+1,'o'); }
        else if(k==='x'){ const a=ex-1,b=ey-1; S(a,b,'r');S(a+2,b,'r');S(a+1,b+1,'r');S(a,b+2,'r');S(a+2,b+2,'r'); }
      }
      if(st==='thinking'){ S(10,4,'o');S(11,3,'o');S(12,3,'o'); }
      if(po.brow==='angry'){ S(3,4,'o');S(4,5,'o');S(11,5,'o');S(12,4,'o'); }
      if(st==='waiting'){ S(3,4,'o');S(4,4,'o');S(5,5,'o'); }
      if(st==='confused'){ S(3,3,'o');S(4,4,'o');S(5,4,'o'); }
      const m=po.mouth;
      if(m==='smile'){ S(5,9,'o');S(6,10,'o');S(7,10,'o');S(8,10,'o');S(9,10,'o');S(10,9,'o'); }
      else if(m==='frown'){ S(5,11,'o');S(6,10,'o');S(7,10,'o');S(8,10,'o');S(9,10,'o');S(10,11,'o'); }
      else if(m==='wide'||m==='flat'){ for(let x=6;x<=9;x++)S(x,10,'o'); }
      else if(m==='open'){ S(7,10,'o');S(8,10,'o');S(7,11,'o');S(8,11,'o'); }
      else if(m==='dot'){ S(7,10,'o'); }
      else if(m==='small'){ S(6,10,'o');S(7,10,'o'); }
      else if(m==='wobble'){ S(6,10,'o');S(7,11,'o');S(8,10,'o'); }
      else if(m==='grin'){ S(6,10,'o');S(7,10,'o');S(8,10,'o');S(9,10,'o');S(6,9,'o');S(9,9,'o'); }
      else if(m==='none'){ }
      else { S(7,10,'o');S(8,10,'o'); }
    }
    if(act&&act.name==='yoyo'){ S(15,8,'o');S(16,7,'o'); const yy=9+[0,1,3,5,6,7,7,6,5,3,1,0][ph%12]; inst.yoyoLow = yy>12; for(let y=8;y<yy;y++) S(16,y,'h'); S(16,yy,'r');S(17,yy,'r');S(16,yy+1,'r');S(17,yy+1,'r');S(16,yy,'h'); }
    if(act&&act.name==='stretch'&&ph>=1&&ph<=6){ S(-1,6,'o');S(-2,5,'o');S(-2,4,'o');S(16,6,'o');S(17,5,'o');S(17,4,'o'); }
    if(st==='listening'){ S(15,9,'o');S(16,8,'o');S(16,7,'o');S(16,6,'o');S(15,5,'o');S(16,5,'s');S(15,8,'s'); const n=f%4; for(let i=0;i<3;i++) S(5+i*3,-2,i<n?'y':'w'); }
    if(st==='thinking'){ const n=f%4; if(n>=1)P(OX+16,OY-1,'h'); if(n>=2)P(OX+18,OY-3,'h'); if(n>=3){P(OX+20,OY-6,'h');P(OX+21,OY-6,'h');P(OX+20,OY-5,'h');P(OX+21,OY-5,'h');} }
    if(st==='sleeping'){ const off=-(Math.floor(f/2)%3); const Z=(x,y)=>{for(let i=0;i<3;i++){P(x+i,y,'b');P(x+i,y+2,'b');} P(x+1,y+1,'b');}; Z(OX+16,5+off); Z(OX+20,2+off); }
    if(st==='error'){ S(13,-5,'r');S(13,-4,'r');S(13,-3,'r');S(13,-1,'r'); }
    if(st==='confused'&&f%8<7){ [[16,-5],[17,-5],[15,-4],[18,-4],[18,-3],[17,-2],[17,-1],[17,1]].forEach(([x,y])=>S(x,y,'y')); }
    if(st==='talking'&&f%4<2){ S(15,11,'o');S(16,11,'o'); }
    if(st==='working'){
      if(f%3!==2){ S(15,11,'o');S(16,11,'o'); S(17,10,'d');S(17,9,'d');S(17,8,'d'); for(let x=16;x<=18;x++){S(x,6,'S');S(x,7,'S');} S(16,6,'h'); }
      else { S(15,12,'o');S(16,12,'o'); S(17,12,'d');S(18,12,'d');S(19,12,'d'); for(let y=11;y<=13;y++){S(20,y,'S');S(21,y,'S');} S(20,11,'h'); [[22,10],[23,13],[22,14],[19,14]].forEach(([x,y],i)=>{ if((f+i)%2===0)S(x,y,'y'); }); }
    }
    if(st==='happy'){ const [x,y]= f%2? [-3,4] : [18,2]; S(x,y,'h');S(x,y-1,'y');S(x-1,y,'y');S(x+1,y,'y');S(x,y+1,'y'); }
    if(st==='loading'){ const n=Math.floor(f/2)%4; for(let i=0;i<3;i++) S(5+i*3,-2,i<n?'y':'w'); }
    if(st==='searching'){
      for(let i=0;i<3;i++){ const y=15-i; for(let x=17;x<=21;x++) S(x,y,i===2?'h':'s'); S(17,y,'S'); } S(18,13,'g');S(20,13,'g');
      const gy=[6,7,8,9,9,8,7,6][f%8], gx=[17,17,18,18,18,18,17,17][f%8]; S(15,10,'o');S(16,9,'o'); S(gx-1,gy+1,'d');S(gx-2,gy+2,'d');
      [[gx,gy],[gx+1,gy-1],[gx+2,gy-1],[gx+3,gy],[gx+3,gy+1],[gx+2,gy+2],[gx+1,gy+2],[gx,gy+1]].forEach(([x,y])=>S(x,y,'S')); S(gx+1,gy,'b');S(gx+2,gy,'h');S(gx+1,gy+1,'b');S(gx+2,gy+1,'b');
    }
    if(st==='websearch'){
      S(15,10,'o');S(16,9,'o'); const cx=19, cy=6, rot=f%6;
      [[cx-1,cy-2],[cx,cy-2],[cx+1,cy-2],[cx-2,cy-1],[cx+2,cy-1],[cx-2,cy],[cx+2,cy],[cx-2,cy+1],[cx+2,cy+1],[cx-1,cy+2],[cx,cy+2],[cx+1,cy+2]].forEach(([x,y])=>S(x,y,'S'));
      for(let y=cy-1;y<=cy+1;y++) for(let x=cx-1;x<=cx+1;x++) S(x,y,'b');
      [[0,-1],[1,-1],[1,0],[-1,1],[0,1]].forEach(([lx2,ly2])=>{ const x=cx-1+(((lx2+1+rot)%3)+3)%3; if(((lx2+1+rot)%6)<3) S(x,cy+ly2,'t'); }); S(cx-1,cy-1,'h');
    }
    if(st==='reading'){
      for(let y=10;y<=14;y++) for(let x=3;x<=12;x++) S(x,y,'h');
      for(let x=2;x<=13;x++) S(x,15,'n'); S(2,14,'n');S(13,14,'n');S(2,10,'n');S(13,10,'n'); for(let y=10;y<=14;y++){S(2,y,'n');S(13,y,'n');} S(7,15,'N');S(8,15,'N');
      for(let y=10;y<=14;y++){S(7,y,'S');S(8,y,'S');} for(let i=0;i<3;i++){ S(4,11+i,'g');S(5,11+i,'g');S(10,11+i,'g');S(11,11+i,'g'); } S(5,13,'h');S(11,13,'h');
      S(3,9,'o');S(4,9,'o');S(11,9,'o');S(12,9,'o');
      const t=f%16; if(t===13){ S(9,9,'h');S(10,9,'h');S(11,10,'h'); } else if(t===14){ S(7,8,'h');S(8,8,'h');S(6,9,'h');S(9,9,'h'); } else if(t===15){ S(4,9,'h');S(5,9,'h');S(3,10,'h'); }
    }
    if(st==='writing'){
      for(let y=10;y<=15;y++) for(let x=17;x<=21;x++) S(x,y,'w'); for(let x=17;x<=21;x++){S(x,10,'S');S(x,15,'S');} for(let y=10;y<=15;y++){S(17,y,'S');S(21,y,'S');}
      const lines=Math.floor(f/3)%5; for(let i=0;i<lines;i++) for(let x=18;x<=20;x++) if(i<4||x<20) S(x,11+i,'h');
      const px=18+(f%3), py=11+lines; S(15,10,'o');S(16,9,'o'); S(px,py,'h');S(px+1,py-1,'y');S(px+2,py-2,'y');S(px+3,py-3,'r');
    }
    if(st==='delete'){
      const p=f%12, down=p>=3;
      S(15,13,'o');S(16,13,'o');S(17,13,'o');S(15,14,'d');S(16,14,'d');S(17,14,'d');S(15,15,'o');S(16,15,'o');S(17,15,'o');
      if(!down){ S(16,12,'S');S(15,11,'o');S(16,11,'o');S(17,11,'o'); S(15,10,'o');S(16,9,'o'); } else { S(15,12,'o');S(16,12,'o');S(17,12,'o'); S(15,11,'o');S(16,10,'o'); }
      if(p<4){ for(let y=11;y<=15;y++) for(let x=19;x<=22;x++) S(x,y,'w'); for(let x=19;x<=22;x++){S(x,11,'S');S(x,15,'S');} for(let y=11;y<=15;y++){S(19,y,'S');S(22,y,'S');} S(20,12,'h');S(21,12,'h');S(20,13,'h');S(20,14,'h'); S(22,10,p%2?'y':'r'); S(21,10,'S'); }
      else if(p===4){ for(let y=9;y<=15;y++) for(let x=17;x<=23;x++) S(x,y,'h'); for(let y=11;y<=14;y++) for(let x=19;x<=22;x++) S(x,y,'y'); }
      else if(p===5){ const cx=20,cy=12; [[0,-3],[1,-3],[-2,-2],[3,-2],[-3,-1],[4,0],[-3,1],[4,2],[-2,3],[3,3],[0,3],[1,3]].forEach(([a,b])=>S(cx+a,cy+b,'y')); for(let y=11;y<=13;y++) for(let x=19;x<=21;x++) S(x,y,'r'); S(20,12,'y'); }
      else if(p===6){ const cx=20,cy=12; [[0,-4],[-3,-3],[3,-3],[-4,0],[3,0],[-3,3],[3,3],[0,3],[1,-4],[-1,3]].forEach(([a,b])=>S(cx+a,cy+b,'r')); S(19,12,'y');S(21,11,'y');S(20,13,'y'); }
      else { const r=p-7; [[19,15],[22,15],[20,15]].forEach(([x,y])=>S(x,y,'d')); if(r<3){ S(19-r,10-r*2,'w');S(21,8-r*2,'w');S(23,10-r*2,'w');S(20,12-r*2,'w'); } }
    }
    if(st==='downloading'){
      for(let x=17;x<=21;x++){S(x,15,'o');} S(17,14,'o');S(21,14,'o');S(17,13,'o');S(21,13,'o'); const fill=Math.floor(f/5)%5; for(let i=0;i<fill&&i<2;i++) for(let x=18;x<=20;x++) S(x,14-i,'b');
      const ay=3+(f%5)*2; S(19,ay,'b');S(19,ay+1,'b');S(19,ay+2,'b');S(18,ay+2,'b');S(20,ay+2,'b');S(19,ay+3,'b'); S(15,12,'o');S(16,13,'o');
    }
    if(st==='uploading'){
      for(let x=17;x<=21;x++){S(x,15,'o');} S(17,14,'o');S(21,14,'o');S(17,13,'o');S(21,13,'o'); const left=4-Math.floor(f/5)%5; for(let i=0;i<left&&i<2;i++) for(let x=18;x<=20;x++) S(x,14-i,'t');
      const ay=11-(f%5)*2; S(19,ay,'t');S(18,ay+1,'t');S(20,ay+1,'t');S(19,ay+1,'t');S(19,ay+2,'t');S(19,ay+3,'t'); S(15,12,'o');S(16,13,'o');
    }
    if(st==='testing'){
      for(let y=8;y<=15;y++) for(let x=17;x<=21;x++) S(x,y,'h'); for(let x=17;x<=21;x++){S(x,8,'S');S(x,15,'S');} for(let y=8;y<=15;y++){S(17,y,'S');S(21,y,'S');} S(18,8,'o');S(19,8,'o');S(20,8,'o');S(19,7,'o');
      const n=Math.floor(f/4)%6; for(let i=0;i<3;i++){ const y=10+i*2; S(19,y,'g');S(20,y,'g'); if(i<n) S(18,y,(i===1&&n>=5)?'r':'t'); else S(18,y,'w'); } S(15,10,'o');S(16,10,'o');S(16,9,'y');
    }
    if(st==='deploying'){
      const sx=[0,0,1,2,2,1][f%6]; S(15,11,'o');S(16,11,'o');S(15,13,'o');S(16,13,'o'); const bx=17+sx;
      for(let y=11;y<=15;y++) for(let x=bx;x<=bx+4;x++) S(x,y,'n'); for(let x=bx;x<=bx+4;x++){S(x,11,'o');S(x,15,'o');} for(let y=11;y<=15;y++){S(bx,y,'o');S(bx+4,y,'o');}
      S(bx+2,12,'y');S(bx+2,13,'y');S(bx+2,14,'y');S(bx+1,13,'y');S(bx+3,13,'y'); if(sx>0){ S(bx-1,12,'w');S(bx-1,14,'w'); }
    }
    if(st==='spawning'){
      const bx=19, by=9;
      S(bx-1,by,'o');S(bx,by,'o');S(bx+1,by,'o'); S(bx-2,by+1,'o');S(bx-1,by+1,'h');S(bx,by+1,'h');S(bx+1,by+1,'h');S(bx+2,by+1,'o'); S(bx-2,by+2,'o');S(bx-1,by+2,'h');S(bx,by+2,'s');S(bx+1,by+2,'h');S(bx+2,by+2,'o');
      S(bx-1,by+1,'o');S(bx+1,by+1,'o'); S(bx-1,by+3,'o');S(bx,by+3,'o');S(bx+1,by+3,'o'); for(let x=bx-2;x<=bx+2;x++) S(x,by+4,'o'); S(bx-1,by+5,'o');S(bx,by+5,'S');S(bx+1,by+5,'o');
      for(let x=bx-2;x<=bx+2;x++) S(x,by+6,'S'); S(bx-3,by+6,'o');S(bx+3,by+6,'o'); S(15,10,'o');S(16,10,'o');S(17,9,'o');S(18,8,'o');S(19,8,'o');
      const k=f%3, kx=bx+3, ky=by+4; S(kx,ky,'y'); if(k===0){ S(kx+1,ky,'y');S(kx+2,ky,'y');S(kx+3,ky,'h'); } else if(k===1){ S(kx+1,ky-1,'y');S(kx+2,ky-2,'y');S(kx+3,ky-3,'h'); } else { S(kx,ky-1,'y');S(kx,ky-2,'y');S(kx,ky-3,'h'); }
    }
    if(st==='messaging'){
      const p=f%10; if(p<2){ S(15,10,'o');S(16,10,'o'); S(17,9,'h');S(18,9,'h');S(17,10,'h'); }
      else { S(15,8,'o');S(16,7,'o'); const q=p-2, px=17+q, py=7-Math.floor(q/2); if(px<=22){ S(px,py-1,'h');S(px,py,'h');S(px+1,py,'h');S(px+2,py,'S');S(px,py+1,'S'); if(q>0)S(px-2,py+1,'w'); if(q>1)S(px-4,py+2,'w'); } }
    }
    if(st==='locking'){
      const p=f%10, shut=p>=4; for(let y=11;y<=14;y++) for(let x=18;x<=22;x++) S(x,y,'y'); for(let x=18;x<=22;x++){S(x,11,'n');S(x,14,'n');} S(18,12,'n');S(18,13,'n');S(22,12,'n');S(22,13,'n'); S(20,12,'o');S(20,13,'o');
      const sy=shut?0:-2; S(18,9+sy,'S');S(18,10+sy,'S');S(19,8+sy,'S');S(20,8+sy,'S');S(21,8+sy,'S');S(22,9+sy,'S'); if(shut)S(22,10,'S'); if(p===4){ S(23,9,'h');S(24,11,'h');S(23,13,'h'); } S(15,11,'o');S(16,12,'o'); S(16,13,'h');S(17,13,'y');
    }
    if(st==='music'){
      for(let x=3;x<=12;x++) S(x,-1,'o'); S(2,-1,'o');S(13,-1,'o'); for(let y=0;y<=2;y++){ S(-1,y+2,'o');S(0,y+2,'o');S(15,y+2,'o');S(16,y+2,'o'); } S(0,3,'r');S(15,3,'r');
      const n=f%8; const note=(x,y,c)=>{ S(x,y,c);S(x+1,y,c);S(x+1,y-1,c);S(x+1,y-2,c);S(x+2,y-2,c); }; note(18,13-n,'b'); if(n>=4) note(21,13-(n-4),'y'); else note(21,13-(n+4),'y');
    }
    if(st==='waiting'){
      const n=Math.floor(f/3)%7; for(let x=17;x<=21;x++){S(x,7,'o');S(x,15,'o');} S(17,8,'S');S(21,8,'S');S(18,9,'S');S(20,9,'S');S(19,10,'S');S(19,11,'S');S(19,12,'S');S(18,13,'S');S(20,13,'S');S(17,14,'S');S(21,14,'S');
      if(n<6){ S(19,10,'y'); } if(n<4){ S(18,9,'y');S(19,9,'y');S(20,9,'y'); } if(n<2){ S(18,8,'y');S(19,8,'y');S(20,8,'y'); } if(n>0&&n<6&&f%2===0) S(19,12,'y');
      if(n>=1){ S(19,14,'y'); } if(n>=3){ S(18,14,'y');S(20,14,'y'); } if(n>=5){ S(19,13,'y'); } S(15,12,'o');S(16,12,'o');
    }
  }

  // ------------------------------------------------------------------ vector glyph layer (flat / kawaii / comic / wire / n64 props + faces)
  function makeG(ctx, sty){
    const col=c=>PAL[c]||c;
    const fillOf=c=>sty.fill?sty.fill(c):col(c), strokeOf=c=>sty.stroke?sty.stroke(c):null;
    const snap=v=>sty.snap?Math.round(v*sty.snap)/sty.snap+((hash(Math.floor(v*7)+tick)&3)-1.5)*0.03:v;
    function finish(c,lw){ const fl=fillOf(c), sk=strokeOf(c); if(fl){ctx.fillStyle=fl;ctx.fill();} if(sk){ctx.strokeStyle=sk;ctx.lineWidth=lw||sty.lw||0.25;ctx.lineJoin=ctx.lineCap=sty.round?'round':'miter';ctx.stroke();} }
    const g={
      fx:!!sty.fx,
      rect(x,y,w,h,c){ ctx.beginPath(); ctx.rect(snap(x),snap(y),w,h); finish(c); },
      circle(x,y,r,c){ ctx.beginPath(); ctx.arc(snap(x),snap(y),r,0,Math.PI*2); finish(c); },
      poly(pts,c){ ctx.beginPath(); pts.forEach((p,i)=>i?ctx.lineTo(snap(p[0]),snap(p[1])):ctx.moveTo(snap(p[0]),snap(p[1]))); ctx.closePath(); finish(c); },
      line(x1,y1,x2,y2,c,w){ ctx.beginPath(); ctx.moveTo(snap(x1),snap(y1)); ctx.lineTo(snap(x2),snap(y2)); ctx.strokeStyle=sty.lineColor?sty.lineColor(c):col(c); ctx.lineWidth=w||0.5; ctx.lineCap='round'; ctx.stroke(); },
      path(pts,c,w){ ctx.beginPath(); pts.forEach((p,i)=>i?ctx.lineTo(snap(p[0]),snap(p[1])):ctx.moveTo(snap(p[0]),snap(p[1]))); ctx.strokeStyle=sty.lineColor?sty.lineColor(c):col(c); ctx.lineWidth=w||0.5; ctx.lineCap=ctx.lineJoin='round'; ctx.stroke(); },
      arc(x,y,r,a0,a1,c,w){ ctx.beginPath(); ctx.arc(x,y,r,a0,a1); ctx.strokeStyle=sty.lineColor?sty.lineColor(c):col(c); ctx.lineWidth=w||0.5; ctx.lineCap='round'; ctx.stroke(); },
      text(x,y,str,c,size,font){ ctx.font=(size||2)+'px '+(font||sty.font||'"Press Start 2P", monospace'); ctx.textAlign='center'; ctx.textBaseline='middle';
        const sk=strokeOf(c), fl=fillOf(c); if(sk&&sty.textStroke!==false){ ctx.lineWidth=(sty.lw||0.25)*1.6; ctx.strokeStyle=sk; ctx.lineJoin='round'; ctx.strokeText(str,x,y);} if(fl){ ctx.fillStyle=fl; ctx.fillText(str,x,y);} }
    };
    return g;
  }

  // face at given anchors: eyes [[x,y,sx],[x,y,sx]], mouth [x,y,sx]
  function vecFace(g,po,st,f,geo,opt){
    opt=opt||{}; const E=geo.eyes, M=geo.mouth;
    if(st==='exec'){ const [mx,my,sx]=M; g.rect(mx-4*sx,my-5.5,8*sx,7,'o'); const line=Math.floor(f/2);
      for(let r=0;r<3;r++){ const bits=hash(line+r*7), len=(3+(bits&3))*sx; g.rect(mx-3*sx,my-4.5+r*2,len,0.8,r===2?'t':'T'); } if(f%2===0) g.rect(mx-3*sx+(3+(hash(line+14)&3))*sx,my-0.5,0.8,0.8,'t'); return; }
    if(st==='loading'){ const [mx,my]=M, k=f%12; for(let i=0;i<12;i++){ const d=(k-i+12)%12, a=i/12*Math.PI*2-Math.PI/2; if(d<4) g.circle(mx+Math.cos(a)*2.6,my-2+Math.sin(a)*2.6,0.5,['y','S','d','g'][d]); } return; }
    E.forEach(([x,y,sx],i)=>{ const k=i===0?po.eyesL:po.eyes, r=opt.round, X=x+po.lx*0.6*sx, Y=y+po.ly*0.6;
      if(k==='normal'){ if(r){ g.circle(X,Y,1.15,'o'); g.circle(X-0.35,Y-0.4,0.42,'h'); } else { g.rect(X-0.75*sx,Y-0.8,1.5*sx,1.7,'o'); g.rect(X-0.6*sx,Y-0.65,0.5*sx,0.5,'g'); } }
      else if(k==='wide'){ if(r){ g.circle(X,Y,1.5,'o'); g.circle(X-0.45,Y-0.55,0.5,'h'); } else { g.rect(X-0.8*sx,Y-1.3,1.6*sx,2.6,'o'); g.rect(X-0.6*sx,Y-1.1,0.5*sx,0.5,'g'); } }
      else if(k==='closed'||k==='squint'){ g.line(X-1*sx,Y+0.3,X+1*sx,Y+0.3,'o',0.55); }
      else if(k==='happy'){ g.path([[X-1.1*sx,Y+0.5],[X,Y-0.6],[X+1.1*sx,Y+0.5]],'o',0.55); }
      else if(k==='x'){ g.line(X-1*sx,Y-1,X+1*sx,Y+1,'r',0.5); g.line(X+1*sx,Y-1,X-1*sx,Y+1,'r',0.5); }
      if(po.brow==='angry'){ const s=i===0?1:-1; g.line(X-1.2*sx*s,Y-2.2,X+1.2*sx*s,Y-1.6,'o',0.45); }
      if(po.brow==='raised'&&i===0){ g.path([[X-1.2*sx,Y-2.2],[X,Y-2.7],[X+1.2*sx,Y-2.2]],'o',0.4); }
    });
    if(st==='thinking'){ const [X,Y]=E[1]; g.path([[X-1,Y-1.8],[X+0.2,Y-2.6],[X+1.4,Y-2.6]],'o',0.4); }
    const [mx,my,sx]=M, m=po.mouth, w=v=>v*sx;
    if(m==='smile') g.path([[mx-w(2.4),my-0.8],[mx-w(1.4),my+0.3],[mx,my+0.6],[mx+w(1.4),my+0.3],[mx+w(2.4),my-0.8]],'o',0.55);
    else if(m==='frown') g.path([[mx-w(2.4),my+0.8],[mx-w(1.4),my-0.3],[mx,my-0.6],[mx+w(1.4),my-0.3],[mx+w(2.4),my+0.8]],'o',0.55);
    else if(m==='grin'){ g.path([[mx-w(2),my-0.6],[mx-w(1.5),my+0.3],[mx,my+0.5],[mx+w(1.5),my+0.3],[mx+w(2),my-0.6]],'o',0.55); g.line(mx-w(2),my-0.6,mx+w(2),my-0.6,'o',0.5); }
    else if(m==='wide'||m==='flat') g.line(mx-w(1.8),my,mx+w(1.8),my,'o',0.6);
    else if(m==='open') g.rect(mx-w(0.9),my-0.4,w(1.8),1.6,'o');
    else if(m==='dot') g.circle(mx,my,0.4,'o');
    else if(m==='small') g.rect(mx-w(1),my-0.3,w(1.4),0.6,'o');
    else if(m==='wobble') g.path([[mx-w(1.5),my],[mx-w(0.5),my+0.6],[mx+w(0.5),my-0.2],[mx+w(1.5),my+0.4]],'o',0.5);
    else if(m==='none'){}
    else g.rect(mx-w(0.9),my-0.3,w(1.8),0.6,'o');
  }

  // props in absolute cells; hand anchor = right edge of body
  function vecProps(g,po,st,f,inst){
    const dx=po.dx, dy=po.dy, act=po.act, ph=po.ph;
    const HX=OX+15+dx, HY=OY+10+dy;                    // shoulder
    const X=v=>OX+v+dx, Y=v=>OY+v+dy;
    const arm=(x2,y2)=>g.line(HX,HY,x2,y2,'o',0.7);
    const dots=n=>{ for(let i=0;i<3;i++) g.circle(X(5.5+i*3),Y(-1.5),0.5,i<n?'y':'w'); };
    if(act&&act.name==='yoyo'){ const yy=Y(9+[0,1,3,5,6,7,7,6,5,3,1,0][ph%12]); inst.yoyoLow=yy>Y(12); arm(X(16.5),Y(7.5)); g.line(X(16.5),Y(7.5),X(16.5),yy,'h',0.25); g.circle(X(16.5),yy+0.8,1.1,'r'); g.circle(X(16.5),yy+0.8,0.3,'h'); }
    if(act&&act.name==='stretch'&&ph>=1&&ph<=6){ g.line(X(1),Y(8),X(-1.5),Y(3.5),'o',0.7); g.line(X(15),Y(8),X(17.5),Y(3.5),'o',0.7); }
    switch(st){
      case 'listening': g.path([[X(15),Y(10)],[X(16.8),Y(8.5)],[X(16.8),Y(6)],[X(15),Y(4.8)]],'o',0.7); dots(f%4); break;
      case 'thinking': { const n=f%4; if(n>=1)g.circle(X(16.5),Y(-0.5),0.5,'h'); if(n>=2)g.circle(X(18.5),Y(-2.5),0.8,'h'); if(n>=3)g.circle(X(21),Y(-5),1.5,'h'); break; }
      case 'waiting': { const n=Math.floor(f/3)%7, x=X(19.5), t=Y(7), b=Y(15.5);
        g.poly([[x-2.5,t],[x+2.5,t],[x,t+4.2]],'S'); g.poly([[x-2.5,b],[x+2.5,b],[x,b-4.2]],'S');
        const top=Math.max(0,(6-n)/6), bot=n/6; if(top>0) g.poly([[x-2.3*top,t+4.2-4*top],[x+2.3*top,t+4.2-4*top],[x,t+4.1]],'y'); if(bot>0) g.poly([[x-2.3*bot,b-0.2],[x+2.3*bot,b-0.2],[x,b-0.2-3.8*bot]],'y');
        if(n>0&&n<6&&f%2===0) g.rect(x-0.25,t+4,0.5,1.5,'y'); g.line(x-3,t,x+3,t,'o',0.6); g.line(x-3,b,x+3,b,'o',0.6); arm(X(16.5),Y(12.5)); break; }
      case 'confused': if(f%8<7) g.text(X(17.5),Y(-2.5),'?','y',5,'"Press Start 2P", monospace'); break;
      case 'talking': if(f%4<2) arm(X(17),Y(11.5)); break;
      case 'happy': { const [x,y]= f%2? [X(-3),Y(4)] : [X(18),Y(2)]; g.line(x-1.4,y,x+1.4,y,'y',0.5); g.line(x,y-1.4,x,y+1.4,'y',0.5); g.circle(x,y,0.4,'h'); break; }
      case 'error': g.rect(X(12.6),Y(-5.5),0.9,3,'r'); g.circle(X(13.05),Y(-1),0.55,'r'); break;
      case 'sleeping': { const off=-(Math.floor(f/2)%3); g.text(X(17.5),Y(6+off),'z','b',2.4); g.text(X(21.5),Y(3+off),'z','b',3.2); break; }
      case 'loading': dots(Math.floor(f/2)%4); break;
      case 'searching': { for(let i=0;i<3;i++) g.rect(X(17),Y(15-i),5,1,i===2?'h':'s'); const gy=Y([6,7,8,9,9,8,7,6][f%8]), gx=X([17,17,18,18,18,18,17,17][f%8]);
        arm(gx-1.2,gy+2.6); g.line(gx-0.8,gy+2.4,gx+0.6,gy+1,'d',0.6); g.circle(gx+2,gy+0.8,1.8,'b'); g.arc(gx+2,gy+0.8,1.8,0,Math.PI*2,'S',0.45); g.circle(gx+1.4,gy+0.2,0.4,'h'); break; }
      case 'websearch': { const cx=X(19.5), cy=Y(6.5), rot=(f%12)/12; arm(X(17),Y(8.5)); g.circle(cx,cy,2.6,'b'); for(let i=0;i<3;i++){ const u=((rot+i/3)%1)*2-1; g.arc(cx+u*1.8,cy,Math.abs(1.0*(1-u*u))+0.2,-Math.PI/2,Math.PI/2,'t',0.5); } g.arc(cx,cy,2.6,0,Math.PI*2,'S',0.4); g.line(cx-2.6,cy,cx+2.6,cy,'S',0.3); break; }
      case 'reading': { const bx=X(2), by=Y(10); g.rect(bx,by,12,5.6,'n'); g.rect(bx+1,by,4.6,4.8,'h'); g.rect(bx+6.4,by,4.6,4.8,'h'); g.line(bx+6,by,bx+6,by+4.8,'S',0.5);
        for(let i=0;i<3;i++){ g.line(bx+1.8,by+1.2+i*1.1,bx+4.6,by+1.2+i*1.1,'g',0.35); g.line(bx+7.2,by+1.2+i*1.1,bx+10,by+1.2+i*1.1,'g',0.35); }
        g.circle(bx+1.6,by-0.3,0.7,'o'); g.circle(bx+10.4,by-0.3,0.7,'o'); const t=f%16; if(t>=13){ const u=(t-12)/4; g.path([[bx+6,by],[bx+6+(1-2*u)*4.5,by-2.5+Math.abs(u-0.5)*3],[bx+6+(1-2*u)*5,by+0.5]],'h',0.4); } break; }
      case 'writing': { const px0=X(17), py0=Y(10); g.rect(px0,py0,5,6,'w'); g.arc(px0+2.5,py0+3,0.001,0,0.001,'S',0.1); g.poly([[px0,py0],[px0+5,py0],[px0+5,py0+6],[px0,py0+6]],'w');
        const lines=Math.floor(f/3)%5; for(let i=0;i<lines;i++) g.line(px0+1,py0+1.3+i*1.1,px0+(i<4?4:3),py0+1.3+i*1.1,'h',0.4);
        const px=px0+1+(f%3), py=py0+1.3+lines*1.1; arm(px-0.5,py-0.5); g.line(px,py,px+3.2,py-3.2,'y',0.9); g.line(px+3,py-3,px+3.6,py-3.6,'r',0.9); g.circle(px,py,0.3,'h'); break; }
      case 'delete': { const p=f%12, down=p>=3, bx=X(15), by=Y(13); g.rect(bx,by,3,3,'o'); g.rect(bx+0.3,by+1,2.4,0.6,'d');
        if(!down){ g.rect(bx+1.2,by-1.4,0.6,1.4,'S'); g.rect(bx-0.2,by-2.2,3.4,0.8,'o'); arm(bx+1.5,by-2.2); } else { g.rect(bx-0.2,by-0.8,3.4,0.8,'o'); arm(bx+1.5,by-0.8); }
        const fx=X(19), fy=Y(11);
        if(p<4){ g.rect(fx,fy,4,5,'w'); g.line(fx+0.8,fy+1.2,fx+3,fy+1.2,'h',0.4); g.line(fx+0.8,fy+2.3,fx+2.4,fy+2.3,'h',0.4); g.line(fx+0.8,fy+3.4,fx+2.8,fy+3.4,'h',0.4); g.line(fx+3.5,fy,fx+3.5,fy-1.2,'S',0.3); g.circle(fx+3.5,fy-1.4,0.5,p%2?'y':'r'); }
        else if(p===4){ g.circle(fx+2,fy+2.5,4.5,'h'); g.circle(fx+2,fy+2.5,2.5,'y'); if(g.fx) g.text(fx-2,fy-4.5,'BOOM!','y',4,'Bangers, Impact, sans-serif'); }
        else if(p===5){ const pts=[]; for(let i=0;i<12;i++){ const a=i/12*Math.PI*2, r=i%2?4.5:3; pts.push([fx+2+Math.cos(a)*r,fy+2.5+Math.sin(a)*r]); } g.poly(pts,'y'); g.circle(fx+2,fy+2.5,1.8,'r'); if(g.fx) g.text(fx-2,fy-4.5,'BOOM!','y',4.6,'Bangers, Impact, sans-serif'); }
        else if(p===6){ const pts=[]; for(let i=0;i<12;i++){ const a=i/12*Math.PI*2+0.3, r=i%2?5:3.4; pts.push([fx+2+Math.cos(a)*r,fy+2.5+Math.sin(a)*r]); } g.poly(pts,'r'); g.circle(fx+2,fy+2.5,1.4,'y'); if(g.fx) g.text(fx-2,fy-4.5,'BOOM!','r',4,'Bangers, Impact, sans-serif'); }
        else { const r=p-7; [[fx,fy+4.6],[fx+3,fy+4.6],[fx+1.5,fy+4.6]].forEach(([x,y])=>g.circle(x,y,0.4,'d')); if(r<3){ g.circle(fx+1-r*0.5,fy-r*2,1+r*0.3,'w'); g.circle(fx+3,fy-1.5-r*2,0.8+r*0.3,'w'); } } break; }
      case 'downloading': case 'uploading': { const up=st==='uploading', c=up?'t':'b', cx=X(19.5), gy=Y(15.5);
        g.path([[cx-2.5,gy-2.5],[cx-2.5,gy],[cx+2.5,gy],[cx+2.5,gy-2.5]],'o',0.6); const lvl=up?4-Math.floor(f/5)%5:Math.floor(f/5)%5; if(lvl>0) g.rect(cx-2.2,gy-Math.min(lvl,2)*0.9,4.4,Math.min(lvl,2)*0.9,c);
        const ay=up?Y(11-(f%5)*2):Y(3+(f%5)*2); g.line(cx,ay,cx,ay+3,c,0.6); if(up) g.path([[cx-1.2,ay+1.2],[cx,ay],[cx+1.2,ay+1.2]],c,0.6); else g.path([[cx-1.2,ay+1.8],[cx,ay+3],[cx+1.2,ay+1.8]],c,0.6); arm(X(17),Y(13.5)); break; }
      case 'testing': { const bx=X(17), by=Y(8); g.rect(bx,by,5,8,'h'); g.rect(bx+1,by-0.6,3,1.2,'o'); const n=Math.floor(f/4)%6;
        for(let i=0;i<3;i++){ const y=by+2+i*2; g.line(bx+2.2,y,bx+4.2,y,'g',0.4); if(i<n){ const bad=i===1&&n>=5; if(bad){ g.line(bx+0.6,y-0.5,bx+1.6,y+0.5,'r',0.4); g.line(bx+1.6,y-0.5,bx+0.6,y+0.5,'r',0.4);} else g.path([[bx+0.5,y],[bx+1,y+0.5],[bx+1.8,y-0.6]],'t',0.45); } else g.rect(bx+0.6,y-0.5,1,1,'w'); }
        arm(bx-0.5,by+2.5); g.line(bx-0.6,by+2.4,bx+0.4,by+1.2,'y',0.5); break; }
      case 'deploying': { const sx=[0,0,1,2,2,1][f%6], bx=X(17+sx), by=Y(11); arm(bx-0.2,by+1.2); g.line(HX,HY+3,bx-0.2,by+3.2,'o',0.7); g.rect(bx,by,5,5,'n'); g.line(bx+2.5,by,bx+2.5,by+5,'y',0.6); g.line(bx,by+2.5,bx+5,by+2.5,'y',0.6); if(sx>0){ g.line(bx-1.5,by+1,bx-0.5,by+1,'w',0.35); g.line(bx-1.5,by+4,bx-0.5,by+4,'w',0.35); } break; }
      case 'spawning': { const bx=X(19), by=Y(9); g.rect(bx-2.7,by+6,5.4,1.1,'S'); g.rect(bx-0.9,by+4.2,1.8,2,'S'); g.rect(bx-2.2,by+3.6,4.4,0.9,'o'); g.circle(bx,by+1.8,1.9,'h'); g.circle(bx-0.7,by+1.6,0.3,'o'); g.circle(bx+0.7,by+1.6,0.3,'o');
        g.line(HX,HY,bx-0.5,by-0.6,'o',0.7); const k=f%3, kx=bx+2.6, ky=by+4; const ang=[0,-Math.PI/4,-Math.PI/2][k]; g.line(kx,ky,kx+Math.cos(ang)*2.6,ky+Math.sin(ang)*2.6,'y',0.6); g.circle(kx+Math.cos(ang)*2.6,ky+Math.sin(ang)*2.6,0.5,'h'); break; }
      case 'messaging': { const p=f%10; if(p<2){ arm(X(17),Y(10)); g.poly([[X(17),Y(9)],[X(19),Y(9.6)],[X(17.4),Y(10.6)]],'h'); }
        else { arm(X(16.5),Y(7.5)); const q=p-2, px=X(17+q), py=Y(7-q/2); if(q<=6){ g.poly([[px,py-1],[px+3,py],[px,py+1],[px+0.6,py]],'h'); if(q>0) g.circle(px-1.5,py+0.8,0.3,'w'); if(q>1) g.circle(px-3.2,py+1.5,0.3,'w'); } } break; }
      case 'locking': { const p=f%10, shut=p>=4, bx=X(18), by=Y(11); const sy=shut?0:-2; g.arc(bx+2.5,by+sy-0.2,1.8,Math.PI,0,'S',0.6); g.line(bx+0.7,by+sy-0.2,bx+0.7,by+(shut?0.2:-0.5),'S',0.6); if(shut) g.line(bx+4.3,by-0.2,bx+4.3,by+0.2,'S',0.6);
        g.rect(bx,by,5,4,'y'); g.circle(bx+2.5,by+1.6,0.55,'o'); g.rect(bx+2.2,by+1.8,0.6,1.3,'o'); if(p===4){ g.line(bx+5.6,by-1,bx+6.2,by-1.6,'h',0.3); g.line(bx+5.8,by+1,bx+6.6,by+1,'h',0.3); } arm(X(16.5),Y(12.5)); g.line(X(16.3),Y(13),X(17.8),Y(13),'y',0.5); g.circle(X(16.2),Y(13),0.45,'h'); break; }
      case 'music': { g.arc(X(7.5),Y(2.5),8,Math.PI*1.05,Math.PI*1.95,'o',0.7); g.rect(X(-1.2),Y(1.8),2,3.2,'o'); g.rect(X(14.2),Y(1.8),2,3.2,'o'); g.circle(X(-0.2),Y(3.4),0.4,'r'); g.circle(X(15.2),Y(3.4),0.4,'r');
        const n=f%8, note=(x,y,c)=>{ g.circle(x,y,0.7,c); g.line(x+0.6,y,x+0.6,y-2.6,c,0.4); g.line(x+0.6,y-2.6,x+1.8,y-2,c,0.4); }; note(X(18.5),Y(13-n),'b'); note(X(21.5),Y(13-((n+4)%8)),'y'); break; }
      case 'working': { if(f%3!==2){ arm(X(17.5),Y(10.5)); g.line(X(17.5),Y(10.5),X(17.5),Y(7),'d',0.6); g.rect(X(16),Y(6),3,2,'S'); } else { arm(X(17.5),Y(12)); g.line(X(17.5),Y(12),X(20.5),Y(12),'d',0.6); g.rect(X(20),Y(11),2,3,'S'); [[22.5,10],[23.5,13],[22.5,14.5],[19,14.5]].forEach(([x,y],i)=>{ if((f+i)%2===0) g.circle(X(x),Y(y),0.35,'y'); }); } break; }
    }
  }

  // body outline polygon (cells) for 2D vector skins
  const OUTLINE=[[7,23],[7,20],[8,20],[8,16],[9,16],[9,13],[8,13],[8,8],[8,6],[11,6],[11,8],[13,8],[13,6],[17,6],[17,8],[19,8],[19,6],[22,6],[22,8],[22,13],[21,13],[21,16],[22,16],[22,20],[23,20],[23,23]];
  const GEO2D={eyes:[[12,13,1],[18,13,1]],mouth:[15,16.5,1]};
  function shifted(po,sq){ const cx=15; return OUTLINE.map(([x,y])=>[cx+(x-cx)*(sq||1)+po.dx,y+po.dy]); }
  function geoFor(po,sq){ sq=sq||1; const m=p=>[15+(p[0]-15)*sq+po.dx,p[1]+po.dy,sq]; return {eyes:GEO2D.eyes.map(m),mouth:m(GEO2D.mouth)}; }
  function squash(po){ return po.view==='side'?0.5:po.view==='back'?1:1; }

  // ------------------------------------------------------------------ 3D mesh for wire + n64
  const MESH=(function(){
    const rings=[[23,7.5],[21,7.5],[21,6],[17,6],[16,4.9],[13,4.9],[12,6],[8,6],[8,6.6]], N=10, V=[], F=[], E=[];
    const ring=(y,r)=>{ const idx=[]; for(let j=0;j<N;j++){ const a=j/N*Math.PI*2; idx.push(V.push([Math.sin(a)*r,y,Math.cos(a)*r])-1); } return idx; };
    const R=rings.map(([y,r])=>ring(y,r));
    for(let i=0;i<R.length-1;i++) for(let j=0;j<N;j++){ const k=(j+1)%N; F.push([R[i][j],R[i][k],R[i+1][k],R[i+1][j]]); E.push([R[i][j],R[i+1][j]]); }
    R.forEach(r=>{ for(let j=0;j<N;j++) E.push([r[j],r[(j+1)%N]]); });
    F.push(R[R.length-1].slice().reverse());                       // top cap
    for(let m=0;m<4;m++){ const a=m*Math.PI/2+Math.PI/4, cx=Math.sin(a)*5.2, cz=Math.cos(a)*5.2, hw=1.6, hd=1.3, y0=8, y1=5.8; const ca=Math.cos(a), sa=Math.sin(a);
      const c=[]; [[-hw,-hd],[hw,-hd],[hw,hd],[-hw,hd]].forEach(([u,v])=>{ const x=cx+u*ca+v*sa, z=cz-u*sa+v*ca; c.push(V.push([x,y0,z])-1); c.push(V.push([x,y1,z])-1); });
      for(let s=0;s<4;s++){ const t=(s+1)%4; F.push([c[s*2],c[t*2],c[t*2+1],c[s*2+1]]); E.push([c[s*2],c[s*2+1]]); E.push([c[s*2+1],c[t*2+1]]); E.push([c[s*2],c[t*2]]); }
      F.push([c[1],c[3],c[5],c[7]]); }
    return {V,F,E};
  })();
  function project3d(po,f,spin){ const th=spin+(po.act&&po.act.name==='turn'?po.ph/14*Math.PI*2:0), c=Math.cos(th), s=Math.sin(th);
    const pts=MESH.V.map(([x,y,z])=>{ const X=x*c+z*s, Z=-x*s+z*c; return [15+po.dx+X*(1+Z*0.012), y+po.dy+Z*0.18, Z]; }); return {pts,th}; }
  function faceGeo3d(po,th){ const r=5.4, out=[], at=a=>{ const A=a+th, Z=Math.cos(A)*r; return [15+po.dx+Math.sin(A)*r*(1+Z*0.012), 13+po.dy+Z*0.18, Math.cos(A), Z]; };
    const l=at(-0.46), rr=at(0.46), m=at(0); const vis=m[2]>0.15; const mm=[m[0],m[1]+3.2,Math.max(0.2,m[2])];
    return {vis, eyes:[[l[0],l[1],Math.max(0.2,l[2])],[rr[0],rr[1],Math.max(0.2,rr[2])]], mouth:mm}; }

  // ------------------------------------------------------------------ skins
  const SKINS={};
  const cube=(ctx,x,y,c,s)=>{ const d=0.42; ctx.fillStyle=darken(c,0.3); ctx.beginPath(); ctx.moveTo(x+1,y); ctx.lineTo(x+1+d,y-d); ctx.lineTo(x+1+d,y+1-d); ctx.lineTo(x+1,y+1); ctx.fill();
    ctx.fillStyle=lighten(c,c==='o'?0.18:0.28); ctx.beginPath(); ctx.moveTo(x,y); ctx.lineTo(x+d,y-d); ctx.lineTo(x+1+d,y-d); ctx.lineTo(x+1,y); ctx.fill(); ctx.fillStyle=PAL[c]; ctx.fillRect(x,y,1.02,1.02); };

  SKINS.pixel={ label:'pixel', crisp:true, note:'The original. 16×17 cells, hand-placed props.',
    draw(ctx,st,f,inst){ raster(st,f,inst,(x,y,c)=>{ if(c==='sh'){ ctx.fillStyle='rgba(0,0,0,.38)'; } else ctx.fillStyle=PAL[c]; ctx.fillRect(x,y,1,1); }); } };

  SKINS.voxel={ label:'voxel', note:'Same sprite, every cell extruded into a little block. Blocky Rookie.',
    draw(ctx,st,f,inst){ const cells=[]; raster(st,f,inst,(x,y,c)=>{ if(c==='sh')return; cells.push([x,y,c]); });
      ctx.fillStyle='rgba(0,0,0,.35)'; ctx.beginPath(); ctx.ellipse(OX+8.4,OY+17.3,7.5,0.9,0,0,Math.PI*2); ctx.fill();
      cells.sort((a,b)=>b[1]-a[1]||a[0]-b[0]); cells.forEach(([x,y,c])=>cube(ctx,x,y,c)); } };

  SKINS.ascii={ label:'ascii', note:'For the terminal people. Plot the sprite as characters; accents keep their colour.',
    draw(ctx,st,f,inst,scale,dpr){ const CH={o:'#',h:'.',s:':',S:'+',d:'=',g:'@',w:'-',T:'~',N:'%',n:'%'}; const cells=[]; raster(st,f,inst,(x,y,c)=>{ if(c!=='sh')cells.push([x,y,c]); });
      ctx.save(); ctx.setTransform(dpr,0,0,dpr,0,0); ctx.font=(scale*1.25)+'px "IBM Plex Mono", ui-monospace, monospace'; ctx.textAlign='center'; ctx.textBaseline='middle';
      cells.forEach(([x,y,c])=>{ const ch=CH[c]||'*'; ctx.fillStyle = CH[c]&&c!=='n'&&c!=='N' ? (c==='o'?lighten('t',0.25):c==='g'?darken('t',0.35):PAL.t) : PAL[c]; ctx.fillText(ch,(x+0.5)*scale,(y+0.55)*scale); }); ctx.restore(); } };

  SKINS.wire={ label:'wire', note:'Hologram. One rook mesh, cyan edges, a floor grid, slow sway; props are line glyphs.',
    draw(ctx,st,f,inst){ const po=pose(st,f,inst); const cy=PAL.wire; ctx.save(); ctx.shadowColor=cy; ctx.shadowBlur=3; ctx.strokeStyle=cy; ctx.lineWidth=0.14; ctx.globalAlpha=0.35;
      for(let i=0;i<=4;i++){ const y=23.6+i*0.55, hw=8+i*1.6; ctx.beginPath(); ctx.moveTo(15+po.dx-hw,y); ctx.lineTo(15+po.dx+hw,y); ctx.stroke(); } for(let i=-4;i<=4;i++){ ctx.beginPath(); ctx.moveTo(15+po.dx+i*2,23.6); ctx.lineTo(15+po.dx+i*3.6,23.6+2.2); ctx.stroke(); }
      ctx.globalAlpha=1; const {pts,th}=project3d(po,f,Math.sin(f*0.05)*0.3); ctx.beginPath(); MESH.E.forEach(([a,b])=>{ ctx.moveTo(pts[a][0],pts[a][1]); ctx.lineTo(pts[b][0],pts[b][1]); }); ctx.stroke();
      const g=makeG(ctx,{fill:()=>null,stroke:()=>cy,lw:0.2,round:true,lineColor:()=>cy,textStroke:true}); const geo=faceGeo3d(po,th); if(geo.vis&&po.view!=='back') vecFace(g,po,st,f,geo,{}); vecProps(g,po,st,f,inst); ctx.restore(); } };

  SKINS.n64={ label:'n64', note:'Low-poly, flat-shaded, vertex-snapped; the slight wobble is on purpose.',
    draw(ctx,st,f,inst){ const po=pose(st,f,inst); const {pts,th}=project3d(po,f,Math.sin(f*0.04)*0.35); const L=[-0.5,-0.6,0.6], ln=Math.hypot(...L); const jit=i=>((hash(i*31+Math.floor(f/2))&3)-1.5)*0.05, sn=v=>Math.round(v*4)/4;
      ctx.fillStyle='rgba(0,0,0,.35)'; ctx.beginPath(); ctx.ellipse(15+po.dx,23.4,8,1.1,0,0,Math.PI*2); ctx.fill();
      const faces=MESH.F.map((idx,i)=>{ const P=idx.map(k=>pts[k]); const z=P.reduce((s,p)=>s+p[2],0)/P.length; const a=P[0],b=P[1],c=P[2]; const u=[b[0]-a[0],b[1]-a[1],b[2]-a[2]], v=[c[0]-a[0],c[1]-a[1],c[2]-a[2]]; const n=[u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0]]; const nl=Math.hypot(...n)||1; const d=(n[0]*L[0]+n[1]*L[1]+n[2]*L[2])/(nl*ln); return {P,z,sh:0.45+0.55*Math.max(0,-d),i}; });
      faces.sort((a,b)=>a.z-b.z); faces.forEach(({P,sh,i})=>{ ctx.beginPath(); P.forEach((p,k)=>{ const x=sn(p[0])+jit(i*4+k), y=sn(p[1])+jit(i*4+k+1); k?ctx.lineTo(x,y):ctx.moveTo(x,y); }); ctx.closePath(); ctx.fillStyle=mix('s',hex('o'),1-sh); ctx.fill(); ctx.strokeStyle=mix('s',hex('o'),1-sh*0.8); ctx.lineWidth=0.08; ctx.stroke(); });
      const g=makeG(ctx,{fill:c=>PAL[c]||c,stroke:c=>darken(c,0.35),lw:0.1,snap:4}); const geo=faceGeo3d(po,th); if(geo.vis&&po.view!=='back') vecFace(g,po,st,f,geo,{}); vecProps(g,po,st,f,inst); } };

  SKINS.flat={ label:'flat', note:'Material-ish icon: flat fills, a long shadow, no outlines. Sits politely in a normal dashboard.',
    draw(ctx,st,f,inst){ const po=pose(st,f,inst), sq=squash(po); const g=makeG(ctx,{fill:c=>PAL[c]||c,stroke:()=>null});
      const body=shifted(po,sq); ctx.fillStyle='rgba(0,0,0,.28)'; ctx.beginPath(); body.forEach((p,i)=>i?ctx.lineTo(p[0]+1.2,p[1]+1.2):ctx.moveTo(p[0]+1.2,p[1]+1.2)); ctx.closePath(); ctx.fill();
      g.poly(body,'S'); ctx.save(); ctx.beginPath(); body.forEach((p,i)=>i?ctx.lineTo(p[0],p[1]):ctx.moveTo(p[0],p[1])); ctx.closePath(); ctx.clip(); g.rect(7+po.dx,6+po.dy,(15-7)*sq+0.5,18,'s'); g.rect(7+po.dx,20+po.dy,16,0.9,'d'); ctx.restore();
      if(po.view==='back'){ g.rect(12+po.dx,12+po.dy,6,4,'d'); } else if(po.view==='front') vecFace(g,po,st,f,geoFor(po,sq),{}); vecProps(g,po,st,f,inst); } };

  SKINS.kawaii={ label:'kawaii', note:'Round, pastel, enormous eyes, a hint of blush. For the cute crowd.',
    draw(ctx,st,f,inst){ const po=pose(st,f,inst), sq=squash(po); const ink=lighten('o',0.18), pastel=c=>c==='o'?ink:c==='g'?lighten('g',0.2):mix(c,[255,255,255],0.35);
      const g=makeG(ctx,{fill:pastel,stroke:c=>c==='h'||c==='s'||c==='S'?darken('S',0.1):ink,lw:0.3,round:true,lineColor:c=>c==='o'?ink:pastel(c)});
      ctx.fillStyle='rgba(0,0,0,.25)'; ctx.beginPath(); ctx.ellipse(15+po.dx,23.4,8.5,1.1,0,0,Math.PI*2); ctx.fill();
      const body=shifted(po,sq); ctx.save(); ctx.lineJoin='round'; ctx.beginPath(); body.forEach((p,i)=>i?ctx.lineTo(p[0],p[1]):ctx.moveTo(p[0],p[1])); ctx.closePath(); ctx.fillStyle=lighten('s',0.35); ctx.fill(); ctx.strokeStyle=ink; ctx.lineWidth=1.1; ctx.stroke(); ctx.restore();
      if(po.view==='front'){ if(st!=='exec'&&st!=='loading'){ g.circle(10.3+po.dx,15.3+po.dy,0.8,'c'); g.circle(19.7+po.dx,15.3+po.dy,0.8,'c'); } vecFace(g,po,st,f,geoFor(po,sq),{round:true}); }
      else if(po.view==='back'){ g.circle(15+po.dx,14+po.dy,2.2,'S'); } vecProps(g,po,st,f,inst); } };

  SKINS.comic={ label:'comic', note:'Heavy ink, halftone shading, and action words. Delete goes BOOM.',
    draw(ctx,st,f,inst){ const po=pose(st,f,inst), sq=squash(po); const ink=darken('o',0.4); const g=makeG(ctx,{fill:c=>PAL[c]||c,stroke:c=>ink,lw:0.42,round:false,lineColor:c=>c==='o'?ink:PAL[c]||c,fx:true,font:'Bangers, Impact, sans-serif'});
      const body=shifted(po,sq); ctx.save(); ctx.beginPath(); body.forEach((p,i)=>i?ctx.lineTo(p[0],p[1]):ctx.moveTo(p[0],p[1])); ctx.closePath(); ctx.fillStyle=PAL.s; ctx.fill(); ctx.clip();
      ctx.fillStyle=ink; for(let y=6;y<24;y+=1.1) for(let x=18;x<24;x+=1.1){ const r=0.12+Math.max(0,(x-18)/6)*0.33; ctx.beginPath(); ctx.arc(x+po.dx+(Math.floor(y)%2)*0.5,y+po.dy,r,0,Math.PI*2); ctx.fill(); }
      ctx.fillStyle=PAL.h; ctx.fillRect(8.8+po.dx,8.8+po.dy,0.9,10); ctx.restore(); ctx.beginPath(); body.forEach((p,i)=>i?ctx.lineTo(p[0],p[1]):ctx.moveTo(p[0],p[1])); ctx.closePath(); ctx.strokeStyle=ink; ctx.lineWidth=0.7; ctx.lineJoin='miter'; ctx.stroke();
      ctx.beginPath(); ctx.moveTo(8+po.dx,23.4); ctx.lineTo(22+po.dx,23.4); ctx.strokeStyle=ink; ctx.lineWidth=0.5; ctx.stroke();
      if(po.view==='front') vecFace(g,po,st,f,geoFor(po,sq),{}); else if(po.view==='back') g.rect(12+po.dx,12+po.dy,6,4,'S'); vecProps(g,po,st,f,inst);
      const word={error:'!?',happy:'YAY!',deploying:'SHIP IT',spawning:'wind',exec:'clack',listening:'hm?',testing:'tick',searching:'hmm',locking:'click'}[st];
      if(word&&(f%16<11)) g.text(5.5+po.dx,3.2,word,'y',st==='error'?4.5:3); } };

  SKINS.none={ label:'none', note:'No mascot. A status dot and the state name, for people who want the information and nothing else.',
    draw(ctx,st,f,inst,scale,dpr){ const cat= st==='error'?'r' : st==='happy'?'t' : st==='sleeping'?'g' : st==='idle'?'S' : MOOD.indexOf(st)>=0?'b':'y'; const pulse= DOING.indexOf(st)>=0 ? 0.75+0.25*Math.sin(f*0.6) : 1;
      ctx.fillStyle=PAL[cat]; ctx.globalAlpha=0.25*pulse; ctx.beginPath(); ctx.arc(15,11,4.2*pulse,0,Math.PI*2); ctx.fill(); ctx.globalAlpha=1; ctx.beginPath(); ctx.arc(15,11,2.2,0,Math.PI*2); ctx.fill();
      ctx.save(); ctx.setTransform(dpr,0,0,dpr,0,0); ctx.font=(scale*1.6)+'px "IBM Plex Mono", ui-monospace, monospace'; ctx.textAlign='center'; ctx.textBaseline='middle'; ctx.fillStyle=PAL.s; ctx.fillText(st,15*scale,18*scale); ctx.restore(); } };

  const SKIN_LIST=Object.keys(SKINS);

  // ------------------------------------------------------------------ runtime
  // enter/exit: transporter beam. The skin is rendered to a buffer, then re-composited cell by cell with a per-cell threshold,
  // so it works identically for every skin. enter → settles into inst.after (idle); exit → hidden.
  function render(inst,st,f){
    PAL=inst.pal; const ctx=inst.ctx, s=inst.scale, dpr=inst.dpr, skin=SKINS[inst.skin]||SKINS.pixel;
    ctx.setTransform(dpr*s,0,0,dpr*s,0,0); ctx.clearRect(0,0,COLS,ROWS);
    if(st==='hidden') return;
    if(st!=='enter'&&st!=='exit'){ skin.draw(ctx,st,f,inst,s,dpr); return; }
    const p=f-inst.t0, u0=Math.max(0,Math.min(1,p/(TRANS_LEN-2))), u=st==='enter'?u0:1-u0;
    if(!inst.buf){ inst.buf=document.createElement('canvas'); } const buf=inst.buf; buf.width=COLS*s*dpr; buf.height=ROWS*s*dpr;
    const bctx=buf.getContext('2d'); bctx.setTransform(dpr*s,0,0,dpr*s,0,0); bctx.clearRect(0,0,COLS,ROWS);
    const sub={ctx:bctx,scale:s,dpr,skin:inst.skin,pal:inst.pal,state:'idle',look:{x:0,y:0},lookUntil:0,blinkUntil:0,act:null,yoyoLow:false}; const savedT=tick; tick=0; skin.draw(bctx,'idle',0,sub,s,dpr); tick=savedT;
    ctx.save(); ctx.setTransform(dpr,0,0,dpr,0,0);
    const px=s; // one cell in css px
    for(let y=0;y<ROWS;y++) for(let x=0;x<COLS;x++){
      const t=(hash(x*131+y*17+7)&1023)/1023, wave=y/ROWS*0.35;        // top materialises first
      const th=t*0.65+wave;
      if(th<u){ ctx.drawImage(buf,x*px*dpr,y*px*dpr,px*dpr,px*dpr,x*px,y*px,px,px); }
      else if(th<u+0.22&&x>=5&&x<=26){ const a=1-(th-u)/0.22; ctx.globalAlpha=a*0.9; ctx.fillStyle=(hash(x+y*31+p)&1)?PAL.b:PAL.h; ctx.fillRect(x*px+px*0.25,y*px+px*0.25,px*0.5,px*0.5); ctx.globalAlpha=1; }
    }
    // beam shimmer
    ctx.globalAlpha=0.18*(st==='enter'?1-u0:u0); ctx.fillStyle=PAL.b; for(let x=7;x<24;x+=2){ if((hash(x+p*3)&3)!==0) ctx.fillRect(x*px,0,px*0.5,ROWS*px); } ctx.globalAlpha=1;
    ctx.restore();
  }
  function draw(inst){ render(inst,inst.state,tick); }
  function tickAll(){
    tick++;
    for(const i of instances){
      if(tick>=i.nextBlink){ i.blinkUntil=tick+1; i.nextBlink=tick+18+rand(30); }
      if(i.state==='idle'){
        if(i.act&&tick-i.act.t0>=ACTS[i.act.name]){ i.act=null; i.nextAct=tick+25+rand(50); }
        if(!i.act&&tick>=i.nextAct){ const names=Object.keys(ACTS); i.act={name:names[rand(names.length)],t0:tick}; }
        if(!i.act&&tick>=i.nextLook){ const o=[[0,0],[-1,0],[1,0],[0,-1],[0,0]][rand(5)]; i.look={x:o[0],y:o[1]}; i.lookUntil=tick+5; i.nextLook=tick+14+rand(20); }
      } else { i.act=null; i.nextAct=tick+25+rand(50); }
      if(tick>i.lookUntil) i.look={x:0,y:0};
      if((i.state==='enter'||i.state==='exit')&&tick-i.t0>=TRANS_LEN){ i.set(i.state==='enter'?(i.after||'idle'):'hidden'); continue; }
      draw(i);
    }
  }
  function ensureTimer(){ if(!timer) timer=setInterval(tickAll, reduce?700:150); }

  function mount(el,opts){
    opts=opts||{}; const scale=opts.scale||4, dpr=window.devicePixelRatio||1;
    const canvas=document.createElement('canvas');
    canvas.width=COLS*scale*dpr; canvas.height=ROWS*scale*dpr;
    canvas.style.width=COLS*scale+'px'; canvas.style.height=ROWS*scale+'px';
    const ctx=canvas.getContext('2d');
    el.appendChild(canvas);
    const inst={el,canvas,ctx,scale,dpr,skin:SKINS[opts.skin]?opts.skin:'pixel',theme:THEMES[opts.theme]?opts.theme:'stone',pal:palFor(opts.theme),state:opts.state||'idle',look:{x:0,y:0},lookUntil:0,nextLook:10,nextBlink:12,blinkUntil:0,act:null,nextAct:30+rand(30),yoyoLow:false,t0:0,after:'idle',buf:null,sound:false,onChange:null,onSkin:null,onTheme:null,onSay:null,onSayEnd:null,_sayTimer:null};
    inst.set=function(s,after){ if(STATES.indexOf(s)<0)return; if(inst._sayTimer&&s!=='talking'){clearTimeout(inst._sayTimer);inst._sayTimer=null;} if(s==='enter'||s==='exit'){ inst.t0=tick; inst.after=after||(s==='enter'?'idle':'hidden'); } const was=inst.state; inst.state=s; inst.act=null; draw(inst); if(inst.sound&&s!==was) SFX.play(s); if(inst.onChange)inst.onChange(s); };
    inst.enter=function(then){ inst.set('enter',then); }; inst.exit=function(){ inst.set('exit'); };
    inst.setSkin=function(k){ if(!SKINS[k])return; inst.skin=k; draw(inst); if(inst.onSkin)inst.onSkin(k); };
    inst.setTheme=function(k){ if(!THEMES[k])return; inst.theme=k; inst.pal=palFor(k); draw(inst); if(inst.onTheme)inst.onTheme(k); };
    inst.say=function(text,opts2){ opts2=opts2||{}; const back=inst.state==='talking'?(inst._back||'idle'):inst.state; inst._back=back;
      if(inst._sayTimer)clearTimeout(inst._sayTimer);
      inst.state='talking'; inst.act=null; draw(inst); if(inst.sound) SFX.play('talking'); if(inst.onChange)inst.onChange('talking'); if(inst.onSay)inst.onSay(text);
      const ms=opts2.ms||Math.min(9000, 1400+text.length*55);
      inst._sayTimer=setTimeout(()=>{ inst._sayTimer=null; inst.set(opts2.then||back); if(inst.onSayEnd)inst.onSayEnd(); },ms); };
    inst.destroy=function(){ const k=instances.indexOf(inst); if(k>=0)instances.splice(k,1); canvas.remove(); if(!instances.length&&timer){clearInterval(timer);timer=null;} };
    instances.push(inst); draw(inst); ensureTimer();
    return inst;
  }
  // ------------------------------------------------------------------ sprite-sheet baking
  // frames per state = one full animation cycle (capped at 24). idle fidgets get their own rows.
  const FRAMES={enter:14,exit:14,idle:8,listening:12,thinking:4,waiting:21,confused:8,talking:12,happy:6,error:2,sleeping:12,loading:12,searching:8,websearch:12,reading:16,writing:15,delete:12,exec:6,downloading:10,uploading:10,testing:24,deploying:6,spawning:3,messaging:10,locking:10,music:8,working:3,'idle-turn':14,'idle-yoyo':24,'idle-stretch':8};
  function bake(opts){
    opts=opts||{}; const scale=opts.scale||4, skin=SKINS[opts.skin]?opts.skin:'pixel', theme=THEMES[opts.theme]?opts.theme:'stone';
    const rows=opts.rows||Object.keys(FRAMES), cols=Math.max(...rows.map(r=>FRAMES[r]||8));
    const cw=COLS*scale, ch=ROWS*scale, sheet=document.createElement('canvas'); sheet.width=cw*cols; sheet.height=ch*rows.length;
    const sctx=sheet.getContext('2d'); const cell=document.createElement('canvas'); cell.width=cw; cell.height=ch;
    const inst={ctx:cell.getContext('2d'),scale,dpr:1,skin,theme,pal:palFor(theme),state:'idle',look:{x:0,y:0},lookUntil:0,nextLook:1e9,nextBlink:1e9,blinkUntil:0,act:null,yoyoLow:false,t0:0,buf:null};
    const saved=tick, savedPal=PAL; PAL=inst.pal; const manifest={skin,theme,scale,cell:[cw,ch],cols,fps:Math.round(1000/150*10)/10,rows:{}};
    rows.forEach((row,ri)=>{ const n=FRAMES[row]||8; const isAct=row.indexOf('idle-')===0; inst.state=isAct?'idle':row;
      for(let c=0;c<n;c++){ tick=c; inst.act=isAct?{name:row.slice(5),t0:0}:null; if(isAct) inst.look={x:0,y:0}; render(inst,isAct?'idle':row,tick); sctx.drawImage(cell,c*cw,ri*ch); }
      manifest.rows[row]={row:ri,frames:n,y:ri*ch}; });
    tick=saved; PAL=savedPal; return {canvas:sheet,manifest};
  }
  // ------------------------------------------------------------------ sfx: tiny chiptune synth, no assets. Opt in with bot.sound=true (needs a user gesture first).
  const SFX=(function(){
    let ac=null; const ctxOf=()=>{ if(!ac){ try{ ac=new (window.AudioContext||window.webkitAudioContext)(); }catch(e){} } if(ac&&ac.state==='suspended') ac.resume(); return ac; };
    function tone(a,t,freq,dur,type,vol,slide){ const o=a.createOscillator(), g=a.createGain(); o.type=type||'square'; o.frequency.setValueAtTime(freq,t); if(slide) o.frequency.exponentialRampToValueAtTime(Math.max(20,slide),t+dur);
      g.gain.setValueAtTime(0.0001,t); g.gain.exponentialRampToValueAtTime(vol||0.08,t+0.005); g.gain.exponentialRampToValueAtTime(0.0001,t+dur); o.connect(g).connect(a.destination); o.start(t); o.stop(t+dur+0.02); }
    function noise(a,t,dur,vol,lp){ const n=a.sampleRate*dur, b=a.createBuffer(1,n,a.sampleRate), d=b.getChannelData(0); for(let i=0;i<n;i++) d[i]=Math.random()*2-1;
      const src=a.createBufferSource(); src.buffer=b; const f=a.createBiquadFilter(); f.type='lowpass'; f.frequency.setValueAtTime(lp||1200,t); f.frequency.exponentialRampToValueAtTime(120,t+dur);
      const g=a.createGain(); g.gain.setValueAtTime(vol||0.2,t); g.gain.exponentialRampToValueAtTime(0.0001,t+dur); src.connect(f).connect(g).connect(a.destination); src.start(t); }
    const bank={
      enter:(a,t)=>{ for(let i=0;i<10;i++) tone(a,t+i*0.07,600+i*140+(i%2)*90,0.09,'sine',0.05); tone(a,t+0.75,1320,0.25,'triangle',0.06); },
      exit: (a,t)=>{ for(let i=0;i<10;i++) tone(a,t+i*0.07,1800-i*150,0.09,'sine',0.05); tone(a,t+0.75,180,0.3,'triangle',0.05,60); },
      happy:(a,t)=>{ tone(a,t,660,0.09); tone(a,t+0.1,880,0.09); tone(a,t+0.2,1320,0.16); },
      error:(a,t)=>{ tone(a,t,180,0.18,'sawtooth',0.07); tone(a,t+0.2,140,0.28,'sawtooth',0.07); },
      talking:(a,t)=>{ for(let i=0;i<4;i++) tone(a,t+i*0.06,700+((i*37)%5)*60,0.045,'square',0.04); },
      listening:(a,t)=>{ tone(a,t,880,0.08,'sine',0.06); tone(a,t+0.12,1320,0.14,'sine',0.06); },
      thinking:(a,t)=>{ tone(a,t,520,0.07,'triangle',0.04); tone(a,t+0.16,520,0.07,'triangle',0.04); },
      waiting:(a,t)=>{ tone(a,t,440,0.06,'triangle',0.04); },
      confused:(a,t)=>{ tone(a,t,500,0.12,'square',0.05,700); tone(a,t+0.16,700,0.14,'square',0.05,520); },
      sleeping:(a,t)=>{ tone(a,t,440,0.2,'sine',0.04,220); },
      delete:(a,t)=>{ tone(a,t,300,0.05,'square',0.05); tone(a,t+0.42,120,0.3,'square',0.08,30); noise(a,t+0.42,0.6,0.25,900); },
      exec:(a,t)=>{ for(let i=0;i<3;i++) noise(a,t+i*0.05,0.03,0.08,4000); },
      spawning:(a,t)=>{ for(let i=0;i<5;i++) tone(a,t+i*0.09,900+i*60,0.03,'square',0.05); tone(a,t+0.5,1500,0.2,'triangle',0.05); },
      locking:(a,t)=>{ noise(a,t,0.03,0.12,3000); tone(a,t+0.05,1200,0.06,'square',0.05); },
      messaging:(a,t)=>{ noise(a,t,0.25,0.08,2500); tone(a,t,500,0.25,'sine',0.04,1400); },
      deploying:(a,t)=>{ tone(a,t,330,0.1,'square',0.05); tone(a,t+0.12,440,0.1,'square',0.05); tone(a,t+0.24,660,0.2,'square',0.05); },
      testing:(a,t)=>{ tone(a,t,1000,0.04,'square',0.04); tone(a,t+0.1,1000,0.04,'square',0.04); },
      downloading:(a,t)=>{ tone(a,t,900,0.1,'triangle',0.04,450); },
      uploading:(a,t)=>{ tone(a,t,450,0.1,'triangle',0.04,900); },
      searching:(a,t)=>{ tone(a,t,700,0.05,'sine',0.04); tone(a,t+0.08,900,0.05,'sine',0.04); },
      websearch:(a,t)=>{ tone(a,t,600,0.06,'sine',0.04,1200); },
      reading:(a,t)=>{ noise(a,t,0.08,0.06,2000); },
      writing:(a,t)=>{ for(let i=0;i<3;i++) noise(a,t+i*0.07,0.02,0.06,3500); },
      loading:(a,t)=>{ tone(a,t,600,0.05,'triangle',0.04); },
      working:(a,t)=>{ noise(a,t,0.04,0.1,1500); tone(a,t,220,0.06,'square',0.04); },
      music:(a,t)=>{ [523,659,784,1046].forEach((f,i)=>tone(a,t+i*0.1,f,0.12,'triangle',0.05)); },
      idle:()=>{}, hidden:()=>{}
    };
    return { play(name){ const a=ctxOf(); if(!a||!bank[name])return; bank[name](a,a.currentTime); }, names:Object.keys(bank) };
  })();

  // ------------------------------------------------------------------ animated favicon (32×32, the 1x sprite)
  // const fav = Rookie.favicon({skin:'pixel', theme:'rook'}); fav.set('exec'); fav.say('hi'); fav.stop()
  function favicon(opts){
    opts=opts||{}; const host=document.createElement('div'); host.style.display='none'; document.body.appendChild(host);
    const bot=mount(host,{scale:1,skin:opts.skin||'pixel',theme:opts.theme,state:opts.state||'idle'});
    let link=document.querySelector('link[rel~="icon"]'); if(!link){ link=document.createElement('link'); link.rel='icon'; document.head.appendChild(link); }
    const prev=link.href; const out=document.createElement('canvas'); out.width=32; out.height=32; const octx=out.getContext('2d'); octx.imageSmoothingEnabled=false;
    let last=''; const push=()=>{ octx.clearRect(0,0,32,32); octx.drawImage(bot.canvas,0,0,bot.canvas.width,bot.canvas.height,0,3,32,25); const url=out.toDataURL('image/png'); if(url!==last){ last=url; link.href=url; } };
    const iv=setInterval(push, reduce?700:150); push();
    bot.stop=function(){ clearInterval(iv); bot.destroy(); host.remove(); if(prev) link.href=prev; };
    return bot;
  }
  return {mount, bake, favicon, sfx:SFX, FRAMES, STATES, MOOD, DOING, TRANS, SKINS:SKIN_LIST, THEMES:THEME_LIST, skinInfo:k=>SKINS[k]};
})();
