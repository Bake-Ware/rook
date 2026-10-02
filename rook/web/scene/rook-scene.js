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

// ---- Fleet view: the hub as a rook, its workers as pawns on a polar board ----
//
// Same sketchbook language as the margin rook: toon pigment, inked structural
// edges, drafting guides. Online pawns stand; a pawn that has gone quiet is
// tipped over; each heartbeat sends a spark down the worker's tether to the hub.

const TAU = Math.PI * 2;
const RING0 = 3.1, RING_STEP = 1.65, SLOT = 1.5;
const KIND_SCALE = {computer: 1, tablet: .8, phone: .7, chip: .62};

function inkEdges(geometry) {
  const edges = new THREE.EdgesGeometry(geometry, 22), positions = edges.getAttribute('position'), strokes = [];
  for (let i = 0; i < positions.count; i += 2) {
    const a = new THREE.Vector3().fromBufferAttribute(positions, i), b = new THREE.Vector3().fromBufferAttribute(positions, i + 1);
    const steps = Math.max(2, Math.ceil(a.distanceTo(b) / .09)), axis = b.clone().sub(a).normalize();
    const side = new THREE.Vector3().crossVectors(axis, Math.abs(axis.y) < .9 ? new THREE.Vector3(0, 1, 0) : new THREE.Vector3(1, 0, 0)).normalize();
    let last;
    for (let j = 0; j <= steps; j++) {
      const t = j / steps, point = a.clone().lerp(b, t);
      point.addScaledVector(side, Math.sin(t * 17 + i * 7.13) * .004 * Math.sin(t * Math.PI));
      if (last) strokes.push(last.x, last.y, last.z, point.x, point.y, point.z);
      last = point;
    }
  }
  edges.dispose();
  const traced = new THREE.BufferGeometry();
  traced.setAttribute('position', new THREE.Float32BufferAttribute(strokes, 3));
  return traced;
}

function pawnGeometry() {
  const profile = [[0, 0], [.42, 0], [.45, .05], [.45, .11], [.37, .17], [.34, .24], [.25, .29], [.16, .62], [.27, .67], [.28, .73], [.15, .78]];
  for (let i = 0; i <= 8; i++) {
    const a = -1.15 + i / 8 * (Math.PI / 2 + 1.15);
    profile.push([Math.cos(a) * .21, .96 + Math.sin(a) * .21]);
  }
  return new THREE.LatheGeometry(profile.map(([r, y]) => new THREE.Vector2(r, y)), 14).toNonIndexed();
}

function rookGeometries() {
  const profile = [[0, 0], [.6, 0], [.63, .05], [.63, .14], [.55, .2], [.54, .27], [.45, .31], [.44, .4], [.38, .45],
    [.3, .7], [.26, 1.46], [.29, 1.62], [.39, 1.75], [.41, 1.81], [.5, 1.84], [.5, 1.94], [.48, 1.98], [.48, 2.08], [.33, 2.08], [.33, 1.88], [0, 1.88]];
  const parts = [new THREE.LatheGeometry(profile.map(([r, y]) => new THREE.Vector2(r, y)), 16).toNonIndexed()];
  for (let i = 0; i < 6; i++) {
    const center = i * Math.PI / 3, a = center - .34, b = center + .34, shape = new THREE.Shape();
    shape.moveTo(.49 * Math.cos(a), .49 * Math.sin(a)); shape.absarc(0, 0, .49, a, b, false);
    shape.lineTo(.32 * Math.cos(b), .32 * Math.sin(b)); shape.absarc(0, 0, .32, b, a, true); shape.closePath();
    const crown = new THREE.ExtrudeGeometry(shape, {depth: .3, bevelEnabled: false, curveSegments: 3, steps: 1});
    crown.rotateX(-Math.PI / 2); crown.translate(0, 2.06, 0); parts.push(crown);
  }
  return parts;
}

export function mountFleet(host, options = {}) {
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({alpha: true, antialias: true, powerPreference: 'low-power'});
  } catch {
    host.dataset.mode = 'unavailable';
    return {update() {}, setActive() {}, destroy() {}};
  }
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 1.5));
  renderer.setClearColor(0x000000, 0);
  renderer.shadowMap.enabled = true; renderer.shadowMap.type = THREE.PCFShadowMap;
  const stage = document.createElement('div'); stage.className = 'fleet-stage';
  const labels = document.createElement('div'); labels.className = 'fleet-labels';
  const card = document.createElement('aside'); card.className = 'fleet-card'; card.hidden = true;
  const legend = document.createElement('div'); legend.className = 'fleet-legend';
  legend.textContent = 'standing · online    toppled · quiet    ring · hosts sites    drag to turn · scroll to zoom · click a piece';
  stage.append(renderer.domElement); host.append(stage, labels, legend, card);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(34, 1, .1, 120);
  const view = {theta: -.6, phi: .6, radius: 15, zoomed: false, drag: null, moved: false};
  const resources = new Set(), keep = r => { resources.add(r); return r; };
  scene.add(new THREE.HemisphereLight(0xe3d8ad, 0x2b3323, 1.9));
  const key = new THREE.DirectionalLight(0xffedc4, 2.8);
  key.position.set(-7, 13, 8); key.castShadow = true; key.shadow.mapSize.set(2048, 2048);
  Object.assign(key.shadow.camera, {left: -13, right: 13, top: 13, bottom: -13, near: 1, far: 40});
  key.shadow.bias = -.0004; key.shadow.normalBias = .02; scene.add(key);
  const ramp = keep(new THREE.DataTexture(new Uint8Array([60, 60, 60, 255, 130, 130, 130, 255, 205, 205, 205, 255, 255, 255, 255, 255]), 4, 1, THREE.RGBAFormat));
  ramp.minFilter = ramp.magFilter = THREE.NearestFilter; ramp.needsUpdate = true;
  const toon = color => keep(new THREE.MeshToonMaterial({color, gradientMap: ramp, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1}));
  const pigment = {online: toon(0xa9a67c), quiet: toon(0x5d5f4c), banned: toon(0x9c5a48), hub: toon(0xd8ad6d), picked: toon(0xe9d39a)};
  const ink = keep(new THREE.LineBasicMaterial({color: 0x070907, transparent: true, opacity: .92}));
  const guide = keep(new THREE.LineBasicMaterial({color: 0x9ca880, transparent: true, opacity: .3}));
  const ghost = keep(new THREE.LineBasicMaterial({color: 0xc5af87, transparent: true, opacity: .16}));
  const dashed = keep(new THREE.LineDashedMaterial({color: 0x9da77c, transparent: true, opacity: .34, dashSize: .12, gapSize: .1}));
  const tether = keep(new THREE.LineDashedMaterial({color: 0xd8ad6d, transparent: true, opacity: .22, dashSize: .09, gapSize: .13}));
  const halo = keep(new THREE.LineBasicMaterial({color: 0xd8ad6d, transparent: true, opacity: .85}));
  const spark = keep(new THREE.MeshBasicMaterial({color: 0xf3d99a}));
  const tile = keep(new THREE.MeshBasicMaterial({color: 0xd8ad6d, transparent: true, opacity: .05, depthWrite: false, side: THREE.DoubleSide}));
  const shade = keep(new THREE.ShadowMaterial({opacity: .38}));

  // The board: a polar chessboard under drafting rings.
  const ground = new THREE.Mesh(keep(new THREE.CircleGeometry(13, 64)), shade);
  ground.rotation.x = -Math.PI / 2; ground.receiveShadow = true; scene.add(ground);
  const point = (r, a, y = .01) => new THREE.Vector3(Math.cos(a) * r, y, Math.sin(a) * r);
  const line = (points, material, closed = false) => {
    const geometry = keep(new THREE.BufferGeometry().setFromPoints(points));
    const made = closed ? new THREE.LineLoop(geometry, material) : new THREE.Line(geometry, material);
    if (material.isLineDashedMaterial) made.computeLineDistances();
    scene.add(made); return made;
  };
  const circle = (r, material) => line(Array.from({length: 128}, (_, i) => point(r, i / 128 * TAU)), material, true);
  for (let k = 0; k < 4; k++) {
    const inner = RING0 - RING_STEP / 2 + k * RING_STEP;
    for (let s = 0; s < 24; s++) {
      if ((s + k) % 2) continue;
      const wedge = new THREE.Mesh(keep(new THREE.RingGeometry(inner, inner + RING_STEP, 5, 1, s / 24 * TAU, TAU / 24)), tile);
      wedge.rotation.x = -Math.PI / 2; wedge.position.y = .004; scene.add(wedge);
    }
    circle(inner, k ? ghost : guide);
  }
  circle(1.25, guide); circle(1.6, dashed); circle(RING0 - RING_STEP / 2 + 4 * RING_STEP, guide);
  for (let i = 0; i < 72; i++) {
    const a = i / 72 * TAU, r = RING0 - RING_STEP / 2 + 4 * RING_STEP;
    line([point(r, a), point(r + (i % 6 ? .12 : .3), a)], ghost);
  }
  line([point(11, 0), point(11, Math.PI)], dashed); line([point(11, Math.PI / 2), point(11, -Math.PI / 2)], dashed);

  // Pieces share their geometry; only the pigment differs.
  const pawn = keep(pawnGeometry()); pawn.computeVertexNormals();
  const pawnInk = keep(inkEdges(pawn));
  const rookParts = rookGeometries().map(g => { const n = keep(g.index ? g.toNonIndexed() : g); n.computeVertexNormals(); return n; });
  const rookInk = rookParts.map(g => keep(inkEdges(g)));
  const sparkGeometry = keep(new THREE.SphereGeometry(.075, 8, 6));
  const haloGeometry = keep(new THREE.BufferGeometry().setFromPoints(Array.from({length: 40}, (_, i) => point(.62, i / 40 * TAU, .02))));

  const hub = new THREE.Group(); hub.scale.setScalar(1.35); scene.add(hub);
  const hubMeshes = rookParts.map((g, i) => {
    const mesh = new THREE.Mesh(g, pigment.hub); mesh.castShadow = mesh.receiveShadow = true; mesh.userData.id = '';
    hub.add(mesh, new THREE.LineSegments(rookInk[i], ink)); return mesh;
  });

  const pieces = new Map();   // id -> piece
  const sparks = [];          // {mesh, from, t}
  const groupMarks = [];      // arcs + labels of the current grouping
  let hubInfo = null, picked = null, hovered = null, active = false, disposed = false, signature = '', openSpace = null;
  let timer = 0, frame = 0, previous = 0;

  function label(text, kind) {
    const el = document.createElement('div'); el.className = 'fleet-label ' + kind; el.textContent = text; labels.append(el); return el;
  }
  const hubLabel = label('HUB', 'hub');

  function makePiece(worker) {
    const root = new THREE.Group(), tilt = new THREE.Group(), mesh = new THREE.Mesh(pawn, pigment.online);
    mesh.castShadow = mesh.receiveShadow = true; mesh.userData.id = worker.id;
    tilt.add(mesh, new THREE.LineSegments(pawnInk, ink)); root.add(tilt);
    const ring = new THREE.LineLoop(haloGeometry, halo); root.add(ring);
    const tie = new THREE.Line(keep(new THREE.BufferGeometry()), tether); scene.add(tie); scene.add(root);
    return {root, tilt, mesh, ring, tie, el: label(worker.name, 'worker'), worker, lean: 0, hop: 0, at: new THREE.Vector3()};
  }

  function layout(list) {
    const groups = new Map();
    for (const w of list) { if (!groups.has(w.group)) groups.set(w.group, []); groups.get(w.group).push(w); }
    const total = list.length || 1; let angle = -Math.PI / 2;
    let reach = RING0;
    while (groupMarks.length) { const m = groupMarks.pop(); if (m.isObject3D) scene.remove(m); else m.remove(); }
    for (const [name, members] of groups) {
      const span = TAU * members.length / total, pad = Math.min(.07, span * .12), room = span - pad * 2;
      let ring = 0, placed = 0, outer = RING0;
      while (placed < members.length) {
        const radius = RING0 + ring * RING_STEP, slots = Math.max(1, Math.floor(room * radius / SLOT));
        const row = members.slice(placed, placed + slots);
        row.forEach((w, i) => {
          const a = angle + pad + room * (i + .5) / row.length;
          pieces.get(w.id).at.copy(point(radius, a, 0)); pieces.get(w.id).angle = a;
        });
        placed += row.length; outer = radius; ring++; reach = Math.max(reach, radius);
      }
      if (groups.size > 1) {
        const r = outer + 1.05, steps = Math.max(4, Math.ceil(room * 14));
        const arc = line(Array.from({length: steps + 1}, (_, i) => point(r, angle + pad + room * i / steps)), halo);
        arc.material = guide; groupMarks.push(arc);
        const el = label(`${name} · ${members.length}`, 'group'); el.dataset.angle = String(angle + span / 2); el.dataset.radius = String(r + .55);
        groupMarks.push(el);
      }
      angle += span;
    }
    // Frame what is on the board, until the person zooms for themselves.
    if (!view.zoomed) view.radius = 8.5 + reach * 1.75;
  }

  function update(list) {
    if (disposed) return;
    hubInfo = list.find(w => w.hub) || null;
    hubLabel.textContent = hubInfo ? `HUB · ${hubInfo.version || hubInfo.name}` : 'HUB';
    hubMeshes.forEach(mesh => { mesh.userData.id = hubInfo ? hubInfo.id : ''; });
    const workers = list.filter(w => w !== hubInfo), seen = new Set();
    for (const w of workers) {
      seen.add(w.id);
      let piece = pieces.get(w.id);
      if (!piece) { piece = makePiece(w); pieces.set(w.id, piece); }
      else if (w.age + 1 < piece.worker.age && active) beat(piece);   // its timer reset: a heartbeat landed
      piece.worker = w; piece.el.textContent = w.name;
      piece.root.scale.setScalar(KIND_SCALE[w.kind] || 1);
      piece.ring.visible = !!(w.serves && ((w.serves.sites || []).length + (w.serves.services || []).length));
    }
    for (const [id, piece] of pieces) {
      if (seen.has(id)) continue;
      scene.remove(piece.root, piece.tie); piece.tie.geometry.dispose(); resources.delete(piece.tie.geometry); piece.el.remove(); pieces.delete(id);
      if (picked === id) select(null);
    }
    const next = workers.map(w => w.id + ':' + w.group).join('|');
    if (next !== signature) {
      signature = next; layout(workers);
      for (const piece of pieces.values()) {
        piece.root.position.copy(piece.at); piece.root.rotation.y = -piece.angle;
        piece.tie.geometry.setFromPoints([new THREE.Vector3(piece.at.x * .3, .02, piece.at.z * .3), new THREE.Vector3(piece.at.x, .02, piece.at.z)]);
        piece.tie.computeLineDistances();
      }
    }
    if (picked) showCard(); paint();
  }

  function beat(piece) {
    piece.hop = 1;
    const mesh = new THREE.Mesh(sparkGeometry, spark); scene.add(mesh); sparks.push({mesh, from: piece.at.clone(), t: 0});
  }

  // Quiet once a whole heartbeat window has passed with no announce (the list's 'stale').
  const quietAfter = options.quietAfter || 60;
  function stateOf(w) { return w.banned ? 'banned' : w.age > quietAfter ? 'quiet' : 'online'; }

  function showCard() {
    const w = picked === (hubInfo && hubInfo.id) ? hubInfo : pieces.get(picked)?.worker;
    if (!w) { card.hidden = true; return; }
    card.replaceChildren(); card.hidden = false;
    const closer = () => { const close = document.createElement('button'); close.type = 'button'; close.className = 'fleet-card-close'; close.textContent = '×'; close.setAttribute('aria-label', 'Close'); close.onclick = () => select(null); card.append(close); };
    // The page draws the card, so it matches an expanded row: same links, caps and actions.
    if (options.renderCard) { if (options.renderCard(card, w.id) === false) { card.hidden = true; return; } closer(); return; }
    const add = (tag, text, cls) => { const el = document.createElement(tag); if (cls) el.className = cls; el.textContent = text; card.append(el); return el; };
    add('div', w.hub ? 'HUB · ROOK' : `${(w.kind || 'worker').toUpperCase()} · PAWN`, 'fleet-card-kind');
    add('h3', w.name);
    if (w.description) add('p', w.description, 'fleet-card-note');
    const facts = [w.version ? 'v' + w.version : '', w.bandName || '', w.hub ? 'serves every band' : stateOf(w) === 'online' ? `seen ${Math.round(w.age)}s ago` : stateOf(w) === 'banned' ? 'deauthed' : `quiet for ${Math.round(w.age)}s`,
      w.battery != null ? `battery ${w.battery}%` : ''].filter(Boolean);
    add('p', facts.join(' · '), 'fleet-card-facts');
    for (const [title, items] of [['Sites', w.serves?.sites || []], ['Services', w.serves?.services || []]]) {
      if (!items.length) continue;
      add('h4', title);
      for (const item of items) {
        const web = /^https?:\/\//.test(item.url || '');
        const el = add(web ? 'a' : 'span', item.name || item.url, 'fleet-card-link');
        if (web) { el.href = item.url; el.target = '_blank'; el.rel = 'noopener noreferrer'; }
        else if (item.url) el.title = item.url;
      }
    }
    // Capabilities, folded by namespace: a chip opens that namespace's caps.
    const caps = w.caps || [];
    if (caps.length) {
      add('h4', `Capabilities · ${caps.length}`);
      const spaces = new Map();
      for (const cap of caps) { const ns = cap.split('.')[0]; if (!spaces.has(ns)) spaces.set(ns, []); spaces.get(ns).push(cap); }
      const chips = add('div', '', 'fleet-card-caps'), detail = add('div', '', 'fleet-card-caplist'); detail.hidden = true;
      for (const [ns, list] of [...spaces].sort((a, b) => a[0].localeCompare(b[0]))) {
        const chip = document.createElement('button'); chip.type = 'button'; chip.className = 'fleet-card-chip';
        chip.textContent = `${ns} ${list.length}`; chip.setAttribute('aria-pressed', String(openSpace === ns));
        chip.onclick = () => { openSpace = openSpace === ns ? null : ns; showCard(); };
        chips.append(chip);
        if (openSpace === ns) { detail.hidden = false; detail.textContent = list.slice().sort().join('\n'); }
      }
    }
    if (!w.hub && options.onOpen) { const open = add('button', 'Open in list', 'fleet-card-open'); open.type = 'button'; open.onclick = () => options.onOpen(w.id); }
    const close = add('button', '×', 'fleet-card-close'); close.type = 'button'; close.setAttribute('aria-label', 'Close'); close.onclick = () => select(null);
  }

  function select(id) { if ((id || null) !== picked) openSpace = null; picked = id || null; if (picked) showCard(); else card.hidden = true; paint(); }

  const ray = new THREE.Raycaster(), pointer = new THREE.Vector2();
  function pieceAt(event) {
    const box = renderer.domElement.getBoundingClientRect();
    pointer.set((event.clientX - box.left) / box.width * 2 - 1, -(event.clientY - box.top) / box.height * 2 + 1);
    ray.setFromCamera(pointer, camera);
    const hit = ray.intersectObjects([...hubMeshes, ...[...pieces.values()].map(p => p.mesh)], false)[0];
    return hit ? hit.object.userData.id || null : null;
  }
  const dom = renderer.domElement;
  const down = e => { view.drag = {x: e.clientX, y: e.clientY, theta: view.theta, phi: view.phi}; view.moved = false; dom.setPointerCapture(e.pointerId); };
  const move = e => {
    if (view.drag) {
      const dx = e.clientX - view.drag.x, dy = e.clientY - view.drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 4) view.moved = true;
      view.theta = view.drag.theta + dx * .006; view.phi = Math.min(1.25, Math.max(.22, view.drag.phi + dy * .005)); paint();
    } else {
      const id = pieceAt(e); if (id !== hovered) { hovered = id; dom.style.cursor = id ? 'pointer' : 'grab'; paint(); }
    }
  };
  const up = e => { const wasDrag = view.moved; view.drag = null; if (!wasDrag) select(pieceAt(e)); };
  const wheel = e => { e.preventDefault(); view.zoomed = true; view.radius = Math.min(34, Math.max(8, view.radius * (1 + Math.sign(e.deltaY) * .08))); paint(); };
  const leave = () => { if (hovered) { hovered = null; paint(); } };
  dom.addEventListener('pointerdown', down); dom.addEventListener('pointermove', move); dom.addEventListener('pointerup', up);
  dom.addEventListener('pointerleave', leave); dom.addEventListener('wheel', wheel, {passive: false}); dom.style.cursor = 'grab'; dom.style.touchAction = 'none';

  const projected = new THREE.Vector3();
  function place(el, position, lift) {
    projected.copy(position); projected.y += lift; projected.project(camera);
    const w = stage.clientWidth, h = stage.clientHeight;
    el.style.transform = `translate(-50%,-50%) translate(${((projected.x + 1) / 2 * w).toFixed(1)}px,${((1 - projected.y) / 2 * h).toFixed(1)}px)`;
    el.style.opacity = projected.z > 1 ? '0' : '';
    return projected.z;
  }

  function step(dt) {
    for (const piece of pieces.values()) {
      const state = stateOf(piece.worker), target = state === 'online' ? 0 : 1;
      piece.lean += (target - piece.lean) * Math.min(1, dt * 4);
      piece.tilt.rotation.z = piece.lean * Math.PI / 2 * .97;
      piece.tilt.position.set(0, piece.lean * .44, 0);
      piece.hop = Math.max(0, piece.hop - dt * 2.4);
      const big = piece.worker.id === picked || piece.worker.id === hovered;
      piece.root.position.y = Math.sin(piece.hop * Math.PI) * .22 * (1 - piece.lean);
      piece.mesh.material = piece.worker.id === picked ? pigment.picked : pigment[state];
      const s = (KIND_SCALE[piece.worker.kind] || 1) * (big ? 1.14 : 1);
      piece.root.scale.x += (s - piece.root.scale.x) * Math.min(1, dt * 10); piece.root.scale.y = piece.root.scale.z = piece.root.scale.x;
      piece.el.classList.toggle('quiet', state !== 'online'); piece.el.classList.toggle('on', big);
    }
    hubMeshes.forEach(mesh => { mesh.material = hubInfo && (picked === hubInfo.id || hovered === hubInfo.id) ? pigment.picked : pigment.hub; });
    for (let i = sparks.length - 1; i >= 0; i--) {
      const s = sparks[i]; s.t += dt / .85;
      if (s.t >= 1) { scene.remove(s.mesh); sparks.splice(i, 1); continue; }
      const ease = s.t * s.t * (3 - 2 * s.t);
      s.mesh.position.set(s.from.x * (1 - ease * .82), .16 + Math.sin(s.t * Math.PI) * .5, s.from.z * (1 - ease * .82));
      s.mesh.scale.setScalar(1 - s.t * .5);
    }
  }

  function paint() {
    if (disposed || !active) return;
    camera.position.set(Math.cos(view.theta) * Math.cos(view.phi) * view.radius, Math.sin(view.phi) * view.radius, Math.sin(view.theta) * Math.cos(view.phi) * view.radius);
    camera.lookAt(0, .35, 0); camera.updateMatrixWorld();
    renderer.render(scene, camera);
    place(hubLabel, hub.position, 3.75);
    for (const piece of pieces.values()) {
      const depth = place(piece.el, piece.at, 1.5 * (KIND_SCALE[piece.worker.kind] || 1) * (1 - piece.lean * .55));
      piece.el.style.zIndex = String(Math.round((1 - depth) * 1000));
    }
    for (const mark of groupMarks) if (!mark.isObject3D) place(mark, point(Number(mark.dataset.radius), Number(mark.dataset.angle), 0), .05);
  }

  function tick(now) {
    if (disposed || !active || document.hidden) { previous = 0; return; }
    const dt = previous ? Math.min((now - previous) / 1000, .1) : 0; previous = now;
    if (!view.drag && !hovered && !picked && !reduced.matches) view.theta += dt * .035;
    step(dt); paint();
    timer = setTimeout(() => { frame = requestAnimationFrame(tick); }, 33);   // About 30 fps.
  }
  function stop() { clearTimeout(timer); cancelAnimationFrame(frame); timer = frame = 0; previous = 0; }
  function setActive(on) {
    active = !!on && !disposed; stop();
    if (active) { resize(); frame = requestAnimationFrame(tick); }
  }
  function resize() {
    const width = Math.max(1, stage.clientWidth), height = Math.max(1, stage.clientHeight);
    camera.aspect = width / height; camera.updateProjectionMatrix(); renderer.setSize(width, height, false); paint();
  }
  const observer = new ResizeObserver(resize); observer.observe(stage);
  const visible = () => { if (active) { stop(); frame = requestAnimationFrame(tick); } };
  document.addEventListener('visibilitychange', visible);
  const lost = event => { event.preventDefault(); stop(); host.dataset.mode = 'unavailable'; };
  dom.addEventListener('webglcontextlost', lost);
  function destroy() {
    if (disposed) return; disposed = true; stop(); observer.disconnect(); document.removeEventListener('visibilitychange', visible);
    dom.removeEventListener('webglcontextlost', lost);
    resources.forEach(resource => resource.dispose()); renderer.dispose(); stage.remove(); labels.remove(); legend.remove(); card.remove();
  }
  host.dataset.mode = 'webgl';
  return {update, setActive, destroy, refreshCard: () => { if (picked) showCard(); }};
}
