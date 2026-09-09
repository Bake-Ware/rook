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
  renderer.shadowMap.enabled=true;renderer.shadowMap.type=THREE.PCFSoftShadowMap;
  renderer.domElement.setAttribute('aria-hidden','true');
  renderer.domElement.tabIndex = -1;
  host.querySelector('.rook-canvas').append(renderer.domElement);
  const scene = new THREE.Scene();
  const camera = new THREE.OrthographicCamera(-3.2,3.2,3.8,-3.8,.1,50);
  camera.position.set(5,3.2,8); camera.lookAt(0,.05,0);
  const study = new THREE.Group(); study.rotation.y = -.42; scene.add(study);
  const resources = new Set();
  const keep = resource => {resources.add(resource);return resource;};
  const pencil = keep(new THREE.LineBasicMaterial({color:0x080a08,transparent:true,opacity:.94}));
  const retraced = keep(new THREE.LineBasicMaterial({color:0x11150f,transparent:true,opacity:.38}));
  const guide = keep(new THREE.LineBasicMaterial({color:0x9ca880,transparent:true,opacity:.27}));
  const ghost = keep(new THREE.LineBasicMaterial({color:0xc5af87,transparent:true,opacity:.14}));
  const dashed = keep(new THREE.LineDashedMaterial({color:0x9da77c,transparent:true,opacity:.29,dashSize:.065,gapSize:.055}));
  // A stationary light casts real shadows as the rook turns beneath it.
  scene.add(new THREE.HemisphereLight(0xe3d8ad,0x35402b,2.));
  const key=new THREE.DirectionalLight(0xffedc4,3.);
  key.position.set(-3,6,5);key.castShadow=true;
  key.shadow.mapSize.set(1024,1024);key.shadow.camera.left=-3;key.shadow.camera.right=3;
  key.shadow.camera.top=3;key.shadow.camera.bottom=-3;key.shadow.camera.near=.5;key.shadow.camera.far=18;
  key.shadow.bias=-.0003;key.shadow.normalBias=.015;scene.add(key);
  const ramp=keep(new THREE.DataTexture(new Uint8Array([65,65,65,255,135,135,135,255,210,210,210,255,255,255,255,255]),4,1,THREE.RGBAFormat));
  ramp.minFilter=ramp.magFilter=THREE.NearestFilter;ramp.needsUpdate=true;
  const pigment=keep(new THREE.MeshToonMaterial({color:0xaaa17d,gradientMap:ramp,flatShading:true,polygonOffset:true,polygonOffsetFactor:1,polygonOffsetUnits:1}));
  function solid(source) {
    const geometry = keep(source.toNonIndexed()); source.dispose(); geometry.computeVertexNormals();
    const mesh=new THREE.Mesh(geometry,pigment);mesh.castShadow=mesh.receiveShadow=true;study.add(mesh);
    // Trace the structural mesh edges, not the triangulation diagonals.
    const edges = new THREE.EdgesGeometry(geometry,1), positions=edges.getAttribute('position');
    for(let pass=0;pass<2;pass++){
      const strokes=[];
      for(let i=0;i<positions.count;i+=2){
        const a=new THREE.Vector3().fromBufferAttribute(positions,i),b=new THREE.Vector3().fromBufferAttribute(positions,i+1);
        const length=a.distanceTo(b),steps=Math.max(3,Math.ceil(length/.075));
        const axis=b.clone().sub(a).normalize();
        const side=new THREE.Vector3().crossVectors(axis,Math.abs(axis.y)<.9?new THREE.Vector3(0,1,0):new THREE.Vector3(1,0,0)).normalize();
        const other=new THREE.Vector3().crossVectors(axis,side);let last;
        for(let j=0;j<=steps;j++){
          const t=j/steps,seed=i*7.13+pass*31.;
          const envelope=Math.sin(t*Math.PI),amplitude=pass?.008:.004;
          const point=a.clone().lerp(b,t);
          point.addScaledVector(side,(Math.sin(t*19.+seed)+.45*Math.sin(t*47.+seed))*amplitude*envelope);
          point.addScaledVector(other,Math.sin(t*27.+seed)*amplitude*.5*envelope);
          // The second stroke drifts like a lightly retraced pen line.
          if(pass)point.addScaledVector(side,.006);
          if(last)strokes.push(last.x,last.y,last.z,point.x,point.y,point.z);
          last=point;
        }
      }
      const traced=keep(new THREE.BufferGeometry());traced.setAttribute('position',new THREE.Float32BufferAttribute(strokes,3));
      study.add(new THREE.LineSegments(traced,pass?retraced:pencil));
    }
    edges.dispose();
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
