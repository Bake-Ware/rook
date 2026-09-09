import * as THREE from 'three';

// Decorative geometry only: no models, textures, network requests, or controls.
export function mountRook(host, toggle) {
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const compact = matchMedia('(max-width: 700px)');
  let choice = null;
  try {choice = localStorage.getItem('rook.art.motion');} catch {}
  let paused = choice === 'off' || (choice !== 'on' && (reduced.matches || compact.matches));
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({alpha:true, antialias:true, powerPreference:'low-power'});
  } catch {
    host.dataset.mode = 'sketch'; toggle.hidden = true;
    return {destroy(){}};
  }
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 1.25));
  renderer.setClearColor(0x000000, 0);
  renderer.domElement.setAttribute('aria-hidden','true');
  renderer.domElement.tabIndex = -1;
  host.querySelector('.rook-canvas').append(renderer.domElement);
  const scene = new THREE.Scene();
  const camera = new THREE.OrthographicCamera(-3.2,3.2,3.8,-3.8,.1,50);
  camera.position.set(5,3.2,8); camera.lookAt(0,.05,0);
  const study = new THREE.Group(); study.rotation.y = -.42; scene.add(study);
  const resources = new Set();
  const keep = resource => {resources.add(resource);return resource;};
  const pencil = keep(new THREE.LineBasicMaterial({color:0xc0a779,transparent:true,opacity:.7}));
  const guide = keep(new THREE.LineBasicMaterial({color:0x9ca880,transparent:true,opacity:.27}));
  const ghost = keep(new THREE.LineBasicMaterial({color:0xc5af87,transparent:true,opacity:.14}));
  const dashed = keep(new THREE.LineDashedMaterial({color:0x9da77c,transparent:true,opacity:.29,dashSize:.065,gapSize:.055}));
  const pigment = keep(new THREE.ShaderMaterial({
    uniforms:{ink:{value:new THREE.Color(0x181e15)},shade:{value:new THREE.Color(0x566049)},paper:{value:new THREE.Color(0xb4ab85)},light:{value:new THREE.Color(0xd5bd88)}},
    vertexShader:`varying vec3 vNormal; varying vec3 vPosition;
      void main(){vNormal=normalize(normalMatrix*normal);vPosition=position;gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.);}`,
    fragmentShader:`uniform vec3 ink;uniform vec3 shade;uniform vec3 paper;uniform vec3 light;
      varying vec3 vNormal;varying vec3 vPosition;
      float hash(vec2 p){return fract(sin(dot(p,vec2(127.1,311.7)))*43758.5453);}
      float noise(vec2 p){vec2 i=floor(p),f=fract(p);f=f*f*(3.-2.*f);return mix(mix(hash(i),hash(i+vec2(1,0)),f.x),mix(hash(i+vec2(0,1)),hash(i+vec2(1,1)),f.x),f.y);}
      void main(){
        float lit=dot(normalize(vNormal),normalize(vec3(-.6,.9,1.)));
        // Shadows are accumulated ink strokes on paper, never solid black fills.
        vec3 tone=mix(paper,light,step(.72,lit));
        float grain=(noise(vPosition.xy*35.+vPosition.z*4.)-.5)*.22;
        float a=(vPosition.y*.8+vPosition.x*2.4+vPosition.z*1.6)*23.+grain;
        float b=(vPosition.y*.9-vPosition.x*2.1-vPosition.z*1.5)*26.+grain;
        float c=(vPosition.x+vPosition.z)*38.+grain;
        float wa=max(fwidth(a),.025), wb=max(fwidth(b),.025), wc=max(fwidth(c),.025);
        float hatch=1.-smoothstep(.09-wa,.09+wa,abs(fract(a)-.5));
        float cross=1.-smoothstep(.075-wb,.075+wb,abs(fract(b)-.5));
        float dense=1.-smoothstep(.065-wc,.065+wc,abs(fract(c)-.5));
        float strokes=max(hatch*(1.-smoothstep(.45,.75,lit)),cross*(1.-smoothstep(.12,.58,lit)));
        strokes=max(strokes,dense*(1.-smoothstep(-.3,.05,lit)));
        tone=mix(tone,ink,strokes*mix(.65,.98,noise(vPosition.xy*60.)));
        gl_FragColor=vec4(tone,1.);
        #include <tonemapping_fragment>
        #include <colorspace_fragment>
      }`
  }));
  function solid(source) {
    const geometry = keep(source.toNonIndexed()); source.dispose(); geometry.computeVertexNormals();
    study.add(new THREE.Mesh(geometry,pigment));
    const edges = keep(new THREE.EdgesGeometry(geometry,16));
    study.add(new THREE.LineSegments(edges,pencil));
    // A second, slightly imperfect stroke softens the CAD-like precision.
    const loose = keep(edges.clone()), positions = loose.getAttribute('position');
    for(let i=0;i<positions.count;i++){
      positions.setXYZ(i,positions.getX(i)+Math.sin(i*12.73)*.009,positions.getY(i)+Math.cos(i*4.19)*.009,positions.getZ(i)+Math.sin(i*6.37)*.009);
    }
    study.add(new THREE.LineSegments(loose,ghost));
  }
  const profile = [
    [0,-2.15],[1.08,-2.15],[1.12,-2.06],[1.12,-1.91],[.98,-1.79],
    [.96,-1.67],[.81,-1.61],[.78,-1.44],[.67,-1.35],
    [.53,-.93],[.46,.46],[.52,.76],[.70,.98],[.73,1.09],
    [.90,1.14],[.90,1.31],[.86,1.39],[.86,1.58],
    [.59,1.58],[.59,1.22],[0,1.22]
  ].map(([r,y])=>new THREE.Vector2(r,y));
  solid(new THREE.LatheGeometry(profile,16));
  // Six hollow crown sectors, separated by true notches.
  for(let i=0;i<6;i++){
    const center=i*Math.PI/3, a=center-.34, b=center+.34;
    const shape=new THREE.Shape();
    shape.moveTo(.88*Math.cos(a),.88*Math.sin(a));
    shape.absarc(0,0,.88,a,b,false);
    shape.lineTo(.58*Math.cos(b),.58*Math.sin(b));
    shape.absarc(0,0,.58,b,a,true);shape.closePath();
    const geometry=new THREE.ExtrudeGeometry(shape,{depth:.53,bevelEnabled:false,curveSegments:3,steps:1});
    // ExtrudeGeometry is already non-indexed; make it indexed before solid().
    geometry.setIndex(Array.from({length:geometry.getAttribute('position').count},(_,n)=>n));
    geometry.rotateX(-Math.PI/2);geometry.translate(0,1.54,0);solid(geometry);
  }
  function line(points,material=guide,parent=scene,closed=false){
    const geometry=keep(new THREE.BufferGeometry().setFromPoints(points));
    const result=closed?new THREE.LineLoop(geometry,material):new THREE.Line(geometry,material);
    if(material.isLineDashedMaterial)result.computeLineDistances();parent.add(result);return result;
  }
  const point=(x,y,z)=>new THREE.Vector3(x,y,z);
  function ring(radius,y,material=guide){return line(Array.from({length:96},(_,i)=>point(Math.cos(i/96*Math.PI*2)*radius,y,Math.sin(i/96*Math.PI*2)*radius)),material,scene,true);}
  ring(1.42,-2.23);ring(1.78,-2.23,dashed);ring(2.15,-2.23,ghost);
  for(let i=0;i<36;i++){
    const angle=i/36*Math.PI*2, r=i%3===0?2.29:2.21;
    line([point(Math.cos(angle)*2.15,-2.23,Math.sin(angle)*2.15),point(Math.cos(angle)*r,-2.23,Math.sin(angle)*r)],ghost);
  }
  line([point(-2.65,-2.23,0),point(2.65,-2.23,0)],dashed);
  line([point(0,-2.23,-2.65),point(0,-2.23,2.65)],dashed);
  line([point(0,-2.5,0),point(0,2.75,0)],dashed);
  // Elevation rule and witness marks, like a sketchbook measurement.
  line([point(-1.65,-2.15,0),point(-1.65,2.07,0)],guide);
  for(const y of [-2.15,-1.61,1.14,2.07]){
    line([point(-1.82,y,0),point(-1.48,y,0)],pencil);
    line([point(-1.65,y,0),point(-.98,y,0)],dashed);
  }
  let timer=0, frame=0, previous=0, disposed=false, contextLost=false;
  function paint(){if(!disposed&&!contextLost&&!host.hidden)renderer.render(scene,camera);}
  function stop(){clearTimeout(timer);cancelAnimationFrame(frame);timer=frame=0;previous=0;}
  function tick(now){
    if(disposed||contextLost||paused||document.hidden||host.hidden)return;
    if(previous)study.rotation.y+=Math.min((now-previous)/1000,.2)*.035;
    previous=now;paint();
    timer=setTimeout(()=>{frame=requestAnimationFrame(tick);},50); // At most 20 fps.
  }
  function sync(){
    stop();toggle.textContent=paused?'Animate artwork':'Pause artwork';toggle.setAttribute('aria-pressed',String(!paused));
    host.dataset.motion=paused?'paused':'on';
    if(!disposed&&!contextLost&&!document.hidden&&!host.hidden){paint();if(!paused)frame=requestAnimationFrame(tick);}
  }
  function resize(){
    const box=host.querySelector('.rook-canvas');
    const width=Math.max(1,box.clientWidth),height=Math.max(1,box.clientHeight),aspect=width/height;
    camera.left=-3.1*aspect;camera.right=3.1*aspect;camera.top=3.1;camera.bottom=-3.1;
    camera.updateProjectionMatrix();renderer.setSize(width,height,false);paint();
  }
  const observer=new ResizeObserver(resize);observer.observe(host.querySelector('.rook-canvas'));
  const preference=()=>{if(choice===null){paused=reduced.matches||compact.matches;sync();}};
  reduced.addEventListener('change',preference);compact.addEventListener('change',preference);
  document.addEventListener('visibilitychange',sync);host.addEventListener('rook:visibility',sync);
  toggle.onclick=()=>{paused=!paused;choice=paused?'off':'on';try{localStorage.setItem('rook.art.motion',choice);}catch{}sync();};
  const lost=event=>{event.preventDefault();contextLost=true;stop();host.dataset.mode='sketch';toggle.hidden=true;};
  const restored=()=>{contextLost=false;resize();host.dataset.mode='webgl';toggle.hidden=false;sync();};
  renderer.domElement.addEventListener('webglcontextlost',lost);
  renderer.domElement.addEventListener('webglcontextrestored',restored);
  function destroy(){
    if(disposed)return;disposed=true;stop();observer.disconnect();
    reduced.removeEventListener('change',preference);compact.removeEventListener('change',preference);
    document.removeEventListener('visibilitychange',sync);host.removeEventListener('rook:visibility',sync);window.removeEventListener('pagehide',pagehide);
    window.removeEventListener('pageshow',pageshow);toggle.onclick=null;
    renderer.domElement.removeEventListener('webglcontextlost',lost);renderer.domElement.removeEventListener('webglcontextrestored',restored);
    resources.forEach(resource=>resource.dispose());renderer.dispose();renderer.domElement.remove();
  }
  const pagehide=event=>{if(event.persisted)stop();else destroy();};
  const pageshow=event=>{if(event.persisted)sync();};
  window.addEventListener('pagehide',pagehide);window.addEventListener('pageshow',pageshow);
  resize();host.dataset.mode='webgl';toggle.hidden=false;sync();
  return {destroy};
}
