"""'Give your agents agency', remastered in Blender.

Same script, beat map and score as docs/promo-v3 (128 BPM, 142 beats, 1998 frames
at 30 fps). The look: the original toon pigment and ink, on an aged dark page with
a parchment grain; the rook's door opens onto the band's grid world and the
camera flies through it. Every frame is set from the beat time by `apply(tb)`.

  blender -b -P film2.py -- stills OUTDIR t1 t2 ...     (seconds)
  blender -b -P film2.py -- frames OUTDIR F0 F1 [step]
  env W, H (default 1920x1080), SAMPLES (default 8)
"""
import sys, os, math, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bpy, bmesh
import numpy as np
from mathutils import Vector, Matrix, Euler
import kit, look, ui
from look import lin, T
from ui import hx, Panel, text, set_text, alpha, line, set_line, disc, quad, measure

BEAT = 60 / 128
FPS = 30
NFRAMES = 1998
S = 1 / 400
TAN16 = math.tan(math.radians(16))
EL = math.radians(48)
RW, RH = int(os.environ.get('W', 1920)), int(os.environ.get('H', 1080))

FG, SOFT, DIM, ACC, ACCHI = hx(0xe2e3d9), hx(0xcfd3bd), hx(0x92998c), hx(0xd8ad6d), hx(0xf0c98c)
OK, AGC, RED, TGB, LINEC = hx(0x8fc79a), hx(0x79d2c8), hx(0xe0675c), hx(0x3aa6dd), hx(0x2b322a)
DARK = hx(0x090c09)


# ------------------------------------------------------------------ easing (film.html)
def clamp(x, a=0.0, b=1.0): return min(b, max(a, x))
def lerp(a, b, t): return a + (b - a) * t
def inv(a, b, x): return clamp((x - a) / (b - a))
def eOutC(t): return 1 - (1 - t) ** 3
def eOutE(t): return 1.0 if t >= 1 else 1 - 2 ** (-10 * t)
def eInOut(t): return 4 * t ** 3 if t < .5 else 1 - (-2 * t + 2) ** 3 / 2
def eInC(t): return t ** 3
def eInE(t): return 0.0 if t <= 0 else 2 ** (10 * t - 10)
def since(tb, step): return tb - math.floor(tb / step) * step
def typed(s, p): return s[:int(math.floor(clamp(p) * len(s) + 1e-6))]


def rnd(seed):
    a = (int(seed) + 0x6D2B79F5) & 0xffffffff
    t = a
    t = ((t ^ (t >> 15)) * (t | 1)) & 0xffffffff
    t ^= (t + (((t ^ (t >> 7)) * (t | 61)) & 0xffffffff)) & 0xffffffff
    return ((t ^ (t >> 14)) & 0xffffffff) / 4294967296


def descendants(o):
    out = [o]
    for c in o.children:
        out += descendants(c)
    return out


def show(ob, on=True):
    for o in descendants(ob):
        o.hide_render = not on


# ================================================================== scene
bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene
look.setup(sc, w=RW, h=RH, samples=int(os.environ.get('SAMPLES', 8)), line=2.2 * RW / 1920)
if os.environ.get('THREADS'):
    sc.render.threads_mode = 'FIXED'; sc.render.threads = int(os.environ['THREADS'])
sc.render.use_single_layer = False


def shade_image():
    """The caption shade: dark at the bottom, clear by 42% up (multiply)."""
    h, w = RH, RW
    t = (np.arange(h, dtype=np.float32) / (h - 1))[:, None]          # 0 at the bottom row
    a = np.where(t < .22, .9 - .3 * (t / .22), np.where(t < .42, .6 * (1 - (t - .22) / .2), 0.0))
    v = np.repeat((1 - a) ** 2.2, w, axis=1)            # the CSS gradient is in sRGB; the compositor is linear
    img = bpy.data.images.new('shade', w, h, alpha=False, float_buffer=True)
    px = np.ones((h, w, 4), dtype=np.float32)
    px[..., 0] = px[..., 1] = px[..., 2] = v
    img.colorspace_settings.name = 'Non-Color'      # before the pixels: changing it regenerates the buffer
    img.pixels.foreach_set(px.ravel())
    img.pack()
    return img


COMP = look.compositor(sc, look.parchment_image(RW, RH), shade_image())

cam = kit.camera()
cam.data.sensor_fit = 'VERTICAL'
cam.data.clip_start = 0.005
cam.data.clip_end = 400
cam.rotation_mode = 'XYZ'

# px overlays in camera space, re-scaled every frame for the current field of view
HUD = kit.empty('hud'); HUD.parent = cam
BOX = kit.empty('box'); BOX.parent = cam
BOXD = 6.0


def kpx():
    return 2 * math.tan(cam.data.angle_y / 2) / 1080


def place_overlays(shake=(0, 0)):
    k = kpx()
    HUD.location = (-960 * k, 540 * k, -1.0); HUD.scale = (k, k, k)
    BOX.location = ((-960 + shake[0]) * k * BOXD, (540 - shake[1]) * k * BOXD, -BOXD); BOX.scale = (k * BOXD,) * 3


def screen(p):
    """World point -> (px x, px y, px per unit, in front)."""
    q = cam.matrix_world.inverted() @ p
    d = -q.z
    k = kpx()
    if d <= 1e-6:
        return 960, 540, 1, False
    return 960 + q.x / (d * k), 540 - q.y / (d * k), 1 / (d * k), True


# ================================================================== the box (0-16) and the glass
def box_px(name, kind='ui'):
    return 'boxui' if kind == 'ui' else 'boxsolid'


BOXGLOW = quad('box_glow', BOX, 1630, 1010, ui.radial(ACC, 1.0, 1.4), coll='boxui')
BOXGLOW.location = (960, -594, -2)
walls = {}
for k_ in ('l', 'r', 't', 'b'):
    walls[k_] = (Panel('wall_' + k_, BOX, hx(0x070907), 1.0, None, r=0, z=3, coll='boxui'),
                 Panel('wedge_' + k_, BOX, hx(0x514b35), 1.0, None, r=0, z=3.2, coll='boxui'))
chatbox = Panel('chatbox', BOX, hx(0x141813), 1.0, hx(0x3b4232), 2, 14, z=0, coll='boxui')
chat_hd = Panel('chat_hd', BOX, LINEC, 1.0, None, r=0, z=.6, coll='boxui')
chat_dots = [disc('chat_dot%d' % i, BOX, 6, hx(0x3b4232), coll='boxui') for i in range(3)]
for i, d_ in enumerate(chat_dots):
    d_.location = (510 + 28 + i * 24, -358, .8)
chat_title = text('chat_title', BOX, 22, DIM, 'mono', 510 + 22 + 3 * 24 + 32, 366, coll='boxui', body='agent · session')
MSGS = [(1.5, 4.3, "I can't reach that machine."), (5, 7.8, "I don't remember yesterday."), (8.5, 11.3, "I can't tell you when it's done.")]
msg_who = [text('who%d' % i, BOX, 22, AGC, 'mono', 548, 435 + i * 93, coll='boxui', body='agent') for i in range(3)]
msg_txt = [text('msg%d' % i, BOX, 34, FG, 'mono', 548, 477 + i * 93, cursor=ACC, coll='boxui') for i in range(3)]
CHATBOX_OBS = [chatbox.f, chatbox.b, chat_hd.f, chat_title, *chat_dots, *msg_who, *msg_txt]
chatbox.set(510, 330, 900, 420); chat_hd.set(510 + 2, 330 + 56, 896, 1)

# the glass pane, the cracks and the shards (scene3d.html 'shatter'), in pane coords (y up)
BW_, BH_ = 900, 420
PANE = kit.empty('pane'); PANE.parent = BOX; PANE.location = (510, -750, 0)   # pane (0,0) = bottom-left
chat_img = bpy.data.images.load(os.path.join(HERE, 'chat.png'))


def tex_mat(name, img, k=1.0):
    m = bpy.data.materials.new(name); m.use_nodes = True
    nt = m.node_tree; nt.nodes.clear(); N, L = nt.nodes.new, nt.links.new
    out = N('ShaderNodeOutputMaterial'); em = N('ShaderNodeEmission'); ti = N('ShaderNodeTexImage'); ti.image = img
    em.inputs['Strength'].default_value = k
    L(ti.outputs['Color'], em.inputs['Color']); L(em.outputs[0], out.inputs[0])
    return m


PANE_MAT = tex_mat('pane', chat_img)
pane = quad('pane_q', PANE, BW_, BH_, PANE_MAT, coll='boxui'); pane.location = (BW_ / 2, BH_ / 2, 0.2)
IMP = Vector((610, 150))
NR = 15
rays = []
for i in range(NR):
    a = i / NR * math.pi * 2 + (rnd(i * 3) - .5) * .35
    pts = [IMP.copy()]; r = 0
    while r < 1300:
        r += 26 + rnd(i * 97 + r) * 40
        ang = a + (rnd(i * 31 + r) - .5) * .16
        pts.append(Vector((IMP.x + math.cos(ang) * r, IMP.y + math.sin(ang) * r)))
    rays.append(pts)
RINGS_R = [34, 78, 140, 230, 360, 540, 800, 1200]


def point_at(i, R):
    pts = rays[(i + NR) % NR]
    for k in range(1, len(pts)):
        if (pts[k] - IMP).length >= R:
            a, b = pts[k - 1], pts[k]
            da, db = (a - IMP).length, (b - IMP).length
            return a.lerp(b, (R - da) / (db - da))
    return pts[-1].copy()


def clip_seg(a, b, x0=2, y0=2, x1=BW_ - 2, y1=BH_ - 2):
    t0, t1 = 0.0, 1.0
    d = b - a
    for p, q in ((-d.x, a.x - x0), (d.x, x1 - a.x), (-d.y, a.y - y0), (d.y, y1 - a.y)):
        if abs(p) < 1e-9:
            if q < 0: return None
            continue
        t = q / p
        if p < 0: t0 = max(t0, t)
        else: t1 = min(t1, t)
        if t0 > t1: return None
    return a + d * t0, a + d * t1


T0 = 13.6
crack_v, crack_f, crack_t = [], [], []


def strip(a, b, t, w):
    c = clip_seg(a, b)
    if not c: return
    a, b = c
    d = b - a
    if d.length < 1e-6: return
    n = Vector((-d.y, d.x)).normalized() * (w / 2)
    k = len(crack_v)
    for p in (a + n, a - n, b - n, b + n):
        crack_v.append((p.x, p.y, 1.5)); crack_t.append(t)
    crack_f.append((k, k + 1, k + 2, k + 3))


for i, pts in enumerate(rays):
    for k in range(1, len(pts)):
        r = (pts[k] - IMP).length
        stage = 0 if r < 120 else 1 if r < 300 else 2 if r < 520 else 3
        strip(pts[k - 1], pts[k], T0 + stage * .55 + r / 1800 + rnd(i) * .15, 3.2 if stage == 0 else 2.2)
for ri, R in enumerate(RINGS_R[:6]):
    for i in range(NR):
        if rnd(ri * 50 + i) < .3: continue
        strip(point_at(i, R), point_at(i + 1, R * (1 + (rnd(ri + i * 7) - .5) * .25)), T0 + .3 + ri * .38 + rnd(i * ri) * .2, 1.6)


def crack_mat():
    m = bpy.data.materials.new('cracks'); m.use_nodes = True
    nt = m.node_tree; nt.nodes.clear(); N, L = nt.nodes.new, nt.links.new
    out = N('ShaderNodeOutputMaterial'); em = N('ShaderNodeEmission'); tr = N('ShaderNodeBsdfTransparent')
    em.inputs['Color'].default_value = (1.0, .94, .8, 1); em.inputs['Strength'].default_value = 1.4
    at = N('ShaderNodeAttribute'); at.attribute_name = 'aT'; at.attribute_type = 'GEOMETRY'
    ut = N('ShaderNodeValue'); ut.name = 'uT'; ut.outputs[0].default_value = 0
    sb = N('ShaderNodeMath'); sb.operation = 'SUBTRACT'; L(ut.outputs[0], sb.inputs[0]); L(at.outputs['Fac'], sb.inputs[1])
    mu = N('ShaderNodeMath'); mu.operation = 'MULTIPLY'; mu.use_clamp = True; mu.inputs[1].default_value = 8; L(sb.outputs[0], mu.inputs[0])
    m2 = N('ShaderNodeMath'); m2.operation = 'MULTIPLY'; m2.inputs[1].default_value = .92; L(mu.outputs[0], m2.inputs[0])
    mx = N('ShaderNodeMixShader'); L(m2.outputs[0], mx.inputs['Fac']); L(tr.outputs[0], mx.inputs[1]); L(em.outputs[0], mx.inputs[2])
    L(mx.outputs[0], out.inputs[0])
    return m, ut


CRACK_MAT, CRACK_T = crack_mat()
me = bpy.data.meshes.new('cracks'); me.from_pydata(crack_v, [], crack_f); me.update()
att = me.attributes.new('aT', 'FLOAT', 'POINT'); att.data.foreach_set('value', crack_t)
cracks = bpy.data.objects.new('cracks', me); bpy.data.collections['boxui'].objects.link(cracks)
me.materials.append(CRACK_MAT); cracks.parent = PANE


def clip_poly(poly):
    out = poly
    for inside, cut in ((lambda p: p.x >= 0, lambda a, b: a.lerp(b, (0 - a.x) / (b.x - a.x))),
                        (lambda p: p.x <= BW_, lambda a, b: a.lerp(b, (BW_ - a.x) / (b.x - a.x))),
                        (lambda p: p.y >= 0, lambda a, b: a.lerp(b, (0 - a.y) / (b.y - a.y))),
                        (lambda p: p.y <= BH_, lambda a, b: a.lerp(b, (BH_ - a.y) / (b.y - a.y)))):
        inp = out; out = []
        for i in range(len(inp)):
            cur, prev = inp[i], inp[i - 1]
            if inside(cur):
                if not inside(prev): out.append(cut(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(cut(prev, cur))
        if not out: break
    return out


SIDE_MAT = look.toon((.96, .89, .72), 'shard_side', tex=.05)
SHARDS = []
rings_all = [0] + RINGS_R
for ri in range(len(rings_all) - 1):
    for i in range(NR):
        r0, r1 = rings_all[ri], rings_all[ri + 1]
        poly = [IMP.copy(), point_at(i, r1), point_at(i + 1, r1)] if r0 == 0 else \
            [point_at(i, r0), point_at(i, r1), point_at(i + 1, r1), point_at(i + 1, r0)]
        polys = [[poly[0], poly[1], poly[1].lerp(poly[2], .5), poly[0].lerp(poly[3], .5)],
                 [poly[0].lerp(poly[3], .5), poly[1].lerp(poly[2], .5), poly[2], poly[3]]] if r1 > 300 else [poly]
        for p0 in polys:
            c = clip_poly([p.copy() for p in p0])
            if len(c) < 3: continue
            cx = sum(p.x for p in c) / len(c); cy = sum(p.y for p in c) / len(c)
            n = len(SHARDS)
            bm = bmesh.new()
            front = [bm.verts.new((p.x - cx, p.y - cy, 0)) for p in c]
            back = [bm.verts.new((p.x - cx, p.y - cy, -6)) for p in c]
            try:
                ff = bm.faces.new(front)
            except ValueError:
                bm.free(); continue
            bf = bm.faces.new(list(reversed(back)))
            for k in range(len(c)):
                j = (k + 1) % len(c)
                f = bm.faces.new((back[k], back[j], front[j], front[k])); f.material_index = 1
            bf.material_index = 1
            uvl = bm.loops.layers.uv.new()
            for f in bm.faces:
                for lp in f.loops:
                    lp[uvl].uv = ((lp.vert.co.x + cx) / BW_, (lp.vert.co.y + cy) / BH_)
            bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
            sm = bpy.data.meshes.new('shard%d' % n); bm.to_mesh(sm); bm.free()
            sm.materials.append(PANE_MAT); sm.materials.append(SIDE_MAT)
            ob = bpy.data.objects.new('shard%d' % n, sm); bpy.data.collections['boxsolid'].objects.link(ob)
            ob.parent = PANE; ob.rotation_mode = 'QUATERNION'
            d = Vector((cx, cy)) - IMP; dist = d.length; d = d.normalized()
            ax = Vector((rnd(n + 1) - .5, rnd(n + 2) - .5, rnd(n + 3) - .5)).normalized()
            SHARDS.append(dict(ob=ob, cx=cx, cy=cy, dir=d, sp=350 + rnd(n) * 700 - dist * .2,
                               vz=300 + rnd(n + 9) * 1500 * math.exp(-dist / 500), ax=ax, w=(rnd(n + 4) - .5) * 14, delay=dist / 2600))
print('shards', len(SHARDS), 'crack strips', len(crack_f))

# ================================================================== the rook (16-32, 130.5-142)
rook_root, RP = look.rook()
GUIDES = look.drafting_guides()
GROUND = look.ground()
PORTAL = look.portal_world(RP['door_c'], -1.30)
ROOK_OBS = descendants(rook_root) + GUIDES + [GROUND] + descendants(PORTAL)
DOOR_C = RP['door_c']
DIST, ELEV = 17.2, .2


def rook_cam(tb):
    """scene3d.html's rig. Returns how far the camera is past the door plane (>0 inside)."""
    end = tb >= 120
    drift = lerp(.05, -.04, inv(130.5, 142, tb)) if end else lerp(-.07, .03, inv(16.5, 27.3, tb))
    base = T(math.sin(drift) * math.cos(ELEV), math.sin(ELEV), math.cos(drift) * math.cos(ELEV)) * DIST
    tgt0 = T(0, .05, 0)
    dirn = (DOOR_C - base).normalized()
    inside = DOOR_C + dirn * .5
    if not end:
        z = eInE(inv(27.3, 31.5, tb))
        pos = base.lerp(inside, z)
        target = tgt0.lerp(DOOR_C + dirn, eOutC(inv(27.3, 30, tb)))
        fov = lerp(18, 34, inv(.55, 1, z))
        pos.z += math.sin(tb * 1.7) * .02 * (1 - z)
    else:
        out = eOutE(inv(130.5, 133.2, tb))
        settle = eOutC(inv(131.5, 135, tb))
        wide = base * lerp(1, 1.55, settle)
        pos = inside.lerp(wide, out)
        end_t = T(0, lerp(.05, -1.52, settle), 0)
        target = (DOOR_C + dirn).lerp(end_t, eOutC(inv(130.5, 132.6, tb)))
        fov = lerp(34, 18, out)
    cam.location = pos
    cam.data.angle_y = math.radians(fov)
    kit.look_at(cam, target)
    return pos.y - (DOOR_C.y - 0.01)


# ================================================================== the world (32-130.5)
WO = Vector((80.0, 0.0, 0.0))


def W(x, y, h=0.0):
    return WO + Vector((x * S, -y * S, h * S))


WORLD = kit.empty('world', tuple(WO))
MAT = dict(body=look.toon(hx(0xaaa17d), 'pigment'), dark=look.toon(hx(0x33352c), 'dark', .06),
           metal=look.toon(hx(0x8c8a7a), 'metal'), board=look.toon(hx(0x5f6b45), 'board'), chip=look.toon(hx(0x24261f), 'chip', .05))


def grp(name, parent, x=0, y=0, z=0, rx=0.0, ry=0.0):
    e = kit.empty(name, tuple(T(x, y, z) * S)); e.parent = parent
    e.rotation_euler = (rx, 0, ry)
    return e


def part(name, w, h, d, mat, x, y, z, parent, rx=0.0, ry=0.0, rz=0.0, coll='solid'):
    ob = kit.box(name, (w * S, d * S, h * S), tuple(T(x, y, z) * S), coll=coll, fill=mat)
    ob.rotation_euler = (rx, -rz, ry)
    ob.parent = parent
    return ob


def sphere(name, r, mat, parent, loc, coll='text', segs=8, rings=5):
    vs, fs = [], []
    for j in range(1, rings):
        th = math.pi * j / rings
        for i in range(segs):
            ph = 2 * math.pi * i / segs
            vs.append((r * math.sin(th) * math.cos(ph), r * math.sin(th) * math.sin(ph), r * math.cos(th)))
    vs += [(0, 0, r), (0, 0, -r)]
    top, bot = len(vs) - 2, len(vs) - 1
    for j in range(rings - 2):
        for i in range(segs):
            a, b = j * segs + i, j * segs + (i + 1) % segs
            fs.append((a, b, b + segs, a + segs))
    for i in range(segs):
        fs.append((top, (i + 1) % segs, i))
        fs.append((bot, (rings - 2) * segs + i, (rings - 2) * segs + (i + 1) % segs))
    ob = kit.mesh_obj(name, vs, fs, coll, mat)
    ob.location = loc; ob.parent = parent
    return ob


def screen_mat():
    m = bpy.data.materials.new('screen'); m.use_nodes = True
    nt = m.node_tree; nt.nodes.clear(); N, L = nt.nodes.new, nt.links.new
    out = N('ShaderNodeOutputMaterial'); em = N('ShaderNodeEmission')
    oi = N('ShaderNodeObjectInfo'); mx = N('ShaderNodeMix'); mx.data_type = 'RGBA'
    mx.inputs['A'].default_value = (*lin(hx(0x1a2520)), 1); mx.inputs['B'].default_value = (*lin(hx(0x557f6e)), 1)
    L(oi.outputs['Alpha'], mx.inputs['Factor']); L(mx.outputs['Result'], em.inputs['Color']); L(em.outputs[0], out.inputs[0])
    return m


SCREEN = screen_mat()


def scr(name, w, h, parent, x, y, z, screens):
    vs = [tuple(T(px, py, 0) * S) for px, py in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2))]
    ob = kit.mesh_obj(name, vs, [(0, 1, 2, 3)], 'solid', SCREEN)
    ob.location = tuple(T(x, y, z) * S); ob.parent = parent; ob.color = (1, 1, 1, 0)
    screens.append(ob)
    for i in range(4):
        bw = w * (.35 + .4 * ((i * 37) % 10) / 10)
        bx, by, bh = -w * .42 + bw / 2, h * (.32 - i * .17), h * .07
        bv = [tuple(T(bx + px, by + py, .5) * S) for px, py in ((-bw / 2, -bh / 2), (bw / 2, -bh / 2), (bw / 2, bh / 2), (-bw / 2, bh / 2))]
        b = kit.mesh_obj(name + '_bar%d' % i, bv, [(0, 1, 2, 3)], 'text', ui.ui(hx(0x3d4a40) if i else hx(0x8a6f45)))
        b.location = ob.location; b.parent = parent


def led(name, parent, x, y, z, col, r=4):
    return sphere(name, r * S, ui.ui(col, 1.4), parent, tuple(T(x, y, z) * S))


def annulus_prism(name, ro, ri, depth, segs, mat, coll='solid'):
    vs, fs = [], []
    for zz in (0, depth):
        for i in range(segs):
            a = 2 * math.pi * i / segs
            vs.append((ro * math.cos(a), ro * math.sin(a), zz)); vs.append((ri * math.cos(a), ri * math.sin(a), zz))
    n = 2 * segs
    for i in range(segs):
        j = (i + 1) % segs
        o0, i0, o1, i1 = 2 * i, 2 * i + 1, 2 * j, 2 * j + 1
        fs += [(o0, o1, i1, i0), (n + o0, n + i0, n + i1, n + o1), (o0, n + o0, n + o1, o1), (i0, i1, n + i1, n + i0)]
    ob = kit.mesh_obj(name, vs, fs, coll, mat)
    kit.fix_normals(ob)
    return ob


def build(kind, g, nm):
    sc_ = []
    if kind == 'laptop':
        part(nm + 'base', 170, 9, 118, MAT['body'], 0, 4.5, 0, g)
        lid = grp(nm + 'lid', g, 0, 9, -59, rx=-.3)
        part(nm + 'lidb', 170, 112, 7, MAT['body'], 0, 56, 0, lid)
        scr(nm + 'scr', 156, 98, lid, 0, 56, 3.7, sc_)
    elif kind == 'phone':
        p = grp(nm + 'tilt', g, 0, 4, 0, rx=-.32)
        part(nm + 'body', 66, 132, 10, MAT['dark'], 0, 66, 0, p)
        part(nm + 'stand', 80, 8, 46, MAT['body'], 0, 4, 18, g)
        scr(nm + 'scr', 58, 118, p, 0, 66, 5.2, sc_)
    elif kind == 'desktop':
        part(nm + 'foot', 90, 6, 56, MAT['body'], 0, 3, 0, g)
        part(nm + 'neck', 18, 60, 12, MAT['body'], 0, 36, -6, g)
        part(nm + 'mon', 220, 136, 12, MAT['body'], 0, 128, 0, g)
        scr(nm + 'scr', 204, 120, g, 0, 128, 6.2, sc_)
    elif kind == 'tablet':
        p = grp(nm + 'tilt', g, 0, 6, 0, rx=-.5)
        part(nm + 'body', 180, 124, 9, MAT['dark'], 0, 62, 0, p)
        part(nm + 'stand', 150, 8, 40, MAT['body'], 0, 4, 22, g)
        scr(nm + 'scr', 166, 110, p, 0, 62, 4.7, sc_)
    elif kind == 'box':
        part(nm + 'case', 96, 160, 150, MAT['body'], 0, 80, 0, g, ry=.35)
        f = grp(nm + 'f', g, ry=.35)
        fan = annulus_prism(nm + 'fan', 34 * S, 26 * S, 8 * S, 16, MAT['dark'])
        fan.rotation_euler = (math.radians(90), 0, 0); fan.location = tuple(T(0, 100, 72) * S); fan.parent = f
        for i in range(3):
            part(nm + 'blade%d' % i, 6, 50, 3, MAT['dark'], 0, 100, 76, f, rz=i * math.pi / 3)
        led(nm + 'led', f, 30, 30, 76, OK)
    elif kind == 'rack':
        for i in range(3):
            part(nm + 'u%d' % i, 160, 30, 116, MAT['body'], 0, 17 + i * 34, 0, g)
            led(nm + 'led%d' % i, g, 62, 17 + i * 34, 59, ACCHI if i == 1 else OK)
            part(nm + 'slot%d' % i, 90, 4, 2, MAT['dark'], -20, 17 + i * 34, 59, g)
    elif kind == 'nas':
        part(nm + 'case', 116, 136, 116, MAT['body'], 0, 68, 0, g)
        for i in range(4):
            part(nm + 'bay%d' % i, 90, 22, 6, MAT['metal'], -6, 30 + i * 28, 59, g)
            led(nm + 'led%d' % i, g, 46, 30 + i * 28, 62, OK, 3.5)
    elif kind == 'pi':
        part(nm + 'board', 130, 8, 88, MAT['board'], 0, 4, 0, g)
        part(nm + 'chip', 40, 8, 34, MAT['chip'], -10, 12, 0, g)
        part(nm + 'usb1', 26, 18, 22, MAT['metal'], 52, 13, 20, g)
        part(nm + 'usb2', 26, 18, 22, MAT['metal'], 52, 13, -14, g)
        part(nm + 'hdr', 80, 8, 8, MAT['dark'], -16, 12, -36, g)
    return sc_


def flat_ring(name, ro, ri, segs, mat, coll='fx', parent=None, z=0.0):
    vs, fs = [], []
    for i in range(segs):
        a = 2 * math.pi * i / segs
        vs.append((ro * math.cos(a), ro * math.sin(a), z)); vs.append((ri * math.cos(a), ri * math.sin(a), z))
    for i in range(segs):
        j = (i + 1) % segs
        fs.append((2 * i, 2 * j, 2 * j + 1, 2 * i + 1))
    ob = kit.mesh_obj(name, vs, fs, coll, mat)
    ob.parent = parent; ob.color = (1, 1, 1, 0)
    return ob


DEV = [('my laptop', 'laptop', -950, -350), ('work laptop', 'laptop', 1850, -650), ('my phone', 'phone', 700, 420),
       ('my desktop', 'desktop', -650, 640), ('tablet', 'tablet', -900, 980), ('build box', 'box', 950, -560),
       ('home server', 'rack', 120, -760), ('gpu box', 'box', 1600, 120), ('media box', 'box', 1350, 900),
       ('nas', 'nas', -1600, 180), ('pi', 'pi', -1350, -900), ('game server', 'rack', -2000, -380)]
AG = [('claude code', 0, 0, 0x79d2c8), ('codex', -520, -1000, 0xa9b8f0), ('home agent', -1750, 760, 0xe8a0c0),
      ('voice agent', 1150, 470, 0xf0c98c), ('ops agent', 1000, -170, 0x9fd37c), ('claude code ', -880, -1220, 0x79d2c8)]
TG = dict(name='telegram', x=100, y=760)
MESH = [(0, 3), (0, 10), (0, 6), (6, 5), (5, 1), (5, 7), (7, 2), (2, 8), (2, 3), (3, 4), (4, 9), (9, 11), (11, 10),
        (10, 6), (9, 0), (7, 8), (1, 7), (6, 0)]

LABEL_COLL = 'text'
D = {}
for name, kind, x, y in DEV:
    root = kit.empty('dev_' + name.replace(' ', '_'), tuple(W(x, y))); root.parent = WORLD
    root.location = tuple(Vector((x * S, -y * S, 0)))
    inner = kit.empty('devin_' + name); inner.parent = root
    inner.rotation_euler.z = math.atan2(-x * .15, 900)
    screens = build(kind, inner, name.replace(' ', '_') + '_')
    ring = flat_ring('glow_' + name, 130 * S, 118 * S, 64, look.additive(ACCHI, 'devring', 1.0), parent=root, z=1.5 * S)
    pool = kit.mesh_obj('pool_' + name, [(150 * S * math.cos(2 * math.pi * i / 40), 150 * S * math.sin(2 * math.pi * i / 40), S)
                                          for i in range(40)], [tuple(range(40))], 'fx', look.additive(hx(0xffcf8a), 'devpool', 1.0))
    pool.parent = root; pool.color = (1, 1, 1, 0)
    lab = text('lab_' + name, None, 1.0, SOFT, 'mono', align='CENTER', valign='TOP', z=0, body=name)
    D[name] = dict(name=name, x=x, y=y, root=root, screens=screens, ring=ring, pool=pool, label=lab, glow=0.0, scale=1.0, op=0.0)
DL = [D[n] for n, _, _, _ in DEV]

# telegram badge: a floating disc, billboarded
TGB_ROOT = kit.empty('tg_badge'); TGB_ROOT.parent = None
tg_disc = disc('tg_disc', TGB_ROOT, 120, hx(0x2f9fd8), segs=40)
tg_ring = flat_ring('tg_ring', 124, 116, 48, ui.ui(ACC), coll='text', parent=TGB_ROOT, z=.3)
_pl = [(62, 126), (190, 74), (166, 186), (132, 154), (112, 180), (110, 146), (170, 96), (98, 140)]
tg_plane = kit.mesh_obj('tg_plane', [(px - 128, 128 - py, .6) for px, py in _pl], [tuple(range(len(_pl)))], 'text', ui.ui((1, 1, 1)))
tg_plane.parent = TGB_ROOT
TG_LAB = text('lab_tg', None, 1.0, SOFT, 'mono', align='CENTER', valign='TOP', z=0, body='telegram')
TG_OBS = [TGB_ROOT, tg_disc, tg_ring, tg_plane, TG_LAB]

A = {}
for key, x, y, col in AG:
    c = hx(col)
    root = kit.empty('ag_' + key.strip().replace(' ', '_') + ('2' if key.endswith(' ') else ''))
    root.parent = WORLD; root.location = (x * S, -y * S, 0)
    hexo = kit.lathe('hex_' + key, [(46 * S, -13 * S), (46 * S, 13 * S)], sides=6, coll='solid', fill=look.toon(tuple(min(1, v * 1.08 + .06) for v in c), 'agent_%x' % col, .04))
    hexo.location = (0, 0, 125 * S); hexo.parent = root
    halo = flat_ring('halo_' + key, 67 * S, 61 * S, 48, ui.ui(c), coll='text', parent=root, z=125 * S)
    pcu = bpy.data.curves.new('pillar_' + key, 'CURVE'); pcu.dimensions = '3D'; pcu.bevel_depth = 5 * S; pcu.bevel_resolution = 2
    sp = pcu.splines.new('POLY'); sp.points.add(1); sp.points[0].co = (0, 0, 0, 1); sp.points[1].co = (0, 0, 112 * S, 1)
    pillar = bpy.data.objects.new('pillar_' + key, pcu); bpy.data.collections['fx'].objects.link(pillar)
    pcu.materials.append(look.additive(c, 'pillar_%x' % col, 1.0)); pillar.parent = root
    base = flat_ring('agbase_' + key, 70 * S, 40 * S, 6, look.additive(c, 'agbase_%x' % col, 1.0), parent=root, z=1.5 * S)
    base.rotation_euler.z = math.radians(30)
    lab = text('alab_' + key, None, 1.0, c, 'mono_b', align='CENTER', valign='TOP', z=0, body=key.strip())
    sub = text('asub_' + key, None, 1.0, DIM, 'mono', align='CENTER', valign='TOP', z=0, body='agent')
    A[key] = dict(name=key.strip(), key=key, x=x, y=y, col=c, root=root, hex=hexo, halo=halo, pillar=pillar, base=base,
                  label=lab, sub=sub, live=hexo.data.materials[0], deadm=look.toon(hx(0x5a5f55), 'agent_dead', .04),
                  op=0.0, scale=1.0, dead=False)

# the floor: a faint gold grid that fades into the dark, compass rings, dashed links


def world_grid_mat():
    m = bpy.data.materials.new('worldgrid'); m.use_nodes = True
    nt = m.node_tree; nt.nodes.clear(); N, L = nt.nodes.new, nt.links.new
    out = N('ShaderNodeOutputMaterial'); em = N('ShaderNodeEmission'); tr = N('ShaderNodeBsdfTransparent'); add = N('ShaderNodeAddShader')
    geo = N('ShaderNodeNewGeometry'); sep = N('ShaderNodeSeparateXYZ')
    sub = N('ShaderNodeVectorMath'); sub.operation = 'SUBTRACT'; sub.inputs[1].default_value = tuple(WO)
    L(geo.outputs['Position'], sub.inputs[0]); L(sub.outputs[0], sep.inputs[0])
    cell, wd = 128 * S, 3.2 * S

    def lm(sock):
        d = N('ShaderNodeMath'); d.operation = 'DIVIDE'; d.inputs[1].default_value = cell; L(sock, d.inputs[0])
        p = N('ShaderNodeMath'); p.operation = 'PINGPONG'; p.inputs[1].default_value = .5; L(d.outputs[0], p.inputs[0])
        t = N('ShaderNodeMath'); t.operation = 'LESS_THAN'; t.inputs[1].default_value = wd / cell / 2; L(p.outputs[0], t.inputs[0])
        return t.outputs[0]
    mx = N('ShaderNodeMath'); mx.operation = 'MAXIMUM'; L(lm(sep.outputs[0]), mx.inputs[0]); L(lm(sep.outputs[1]), mx.inputs[1])
    cd = N('ShaderNodeCameraData')
    fd = N('ShaderNodeMapRange'); fd.inputs['From Min'].default_value = 3500 * S; fd.inputs['From Max'].default_value = 12000 * S
    fd.inputs['To Min'].default_value = 1.0; fd.inputs['To Max'].default_value = 0.0
    L(cd.outputs['View Distance'], fd.inputs['Value'])
    a = N('ShaderNodeMath'); a.operation = 'MULTIPLY'; L(mx.outputs[0], a.inputs[0]); L(fd.outputs['Result'], a.inputs[1])
    oi = N('ShaderNodeObjectInfo'); a2 = N('ShaderNodeMath'); a2.operation = 'MULTIPLY'; L(a.outputs[0], a2.inputs[0]); L(oi.outputs['Alpha'], a2.inputs[1])
    em.inputs['Color'].default_value = (*lin(ACC), 1)
    L(a2.outputs[0], em.inputs['Strength'])
    L(tr.outputs[0], add.inputs[0]); L(em.outputs[0], add.inputs[1]); L(add.outputs[0], out.inputs[0])
    return m


GRIDP = kit.mesh_obj('worldgrid', [(-4608 * S, -4608 * S, .5 * S), (4608 * S, -4608 * S, .5 * S), (4608 * S, 4608 * S, .5 * S),
                                   (-4608 * S, 4608 * S, .5 * S)], [(0, 1, 2, 3)], 'guides', world_grid_mat())
GRIDP.parent = WORLD; GRIDP.color = (1, 1, 1, .07)
COMPASS = []
for r, o in ((700, .16), (1500, .1), (2600, .07)):
    pts = [(math.cos(i / 160 * 2 * math.pi) * r * S, math.sin(i / 160 * 2 * math.pi) * r * S, S) for i in range(160)]
    g = look.guide_line('compass%d' % r, pts, look.GHOST, o, width=2.2 * S, closed=True); g.parent = WORLD
    COMPASS.append(g)
MESH_OBS = []
for i, (a_, b_) in enumerate(MESH):
    pa, pb = DL[a_], DL[b_]
    g = look.guide_line('mesh%d' % i, [(pa['x'] * S, -pa['y'] * S, 2 * S), (pb['x'] * S, -pb['y'] * S, 2 * S)], ACC, .25,
                        width=2.4 * S, dash=(14 * S, 26 * S))
    g.parent = WORLD; MESH_OBS.append(g)

# pools: beams (core + glow), packets, rings
BEAMS, RINGS, PKS = {}, {}, []
NB, NR_, NP = {}, {}, [0]


def _tube(name, col, k):
    cu = bpy.data.curves.new(name, 'CURVE'); cu.dimensions = '3D'; cu.bevel_depth = .01; cu.bevel_resolution = 2
    sp = cu.splines.new('POLY'); sp.points.add(1)
    ob = bpy.data.objects.new(name, cu); bpy.data.collections['fx'].objects.link(ob)
    cu.materials.append(look.additive(col, 'beam_%.3f_%.3f_%.3f_%.1f' % (*col, k), k))
    return ob


def beam(pa, pb, grow, col, packet=None, op=1.0, w=1.0):
    if op <= .01 or grow <= .001:
        return
    key = tuple(round(c, 3) for c in col)
    pool = BEAMS.setdefault(key, [])
    n = NB.get(key, 0)
    if n >= len(pool):
        pool.append((_tube('beamc%d' % len(pool), key, 1.35), _tube('beamg%d' % len(pool), key, 1.0)))
    core, glow = pool[n]; NB[key] = n + 1
    e = pa.lerp(pb, grow)
    for ob, r, a in ((core, 5 * w * S, op), (glow, 20 * w * S, .2 * op)):
        sp = ob.data.splines[0]
        sp.points[0].co = (*pa, 1); sp.points[1].co = (*e, 1)
        ob.data.bevel_depth = r
        alpha(ob, a)
    if packet is not None and 0 <= packet <= 1:
        pk_at(pa.lerp(pb, packet), 13 * S, op)


def pk_at(p, r, op):
    if NP[0] >= len(PKS):
        o = sphere('pk%d' % len(PKS), 1.0, look.additive((1, 1, 1), 'packet', 1.6), None, (0, 0, 0), coll='fx', segs=10, rings=6)
        PKS.append(o)
    o = PKS[NP[0]]; NP[0] += 1
    o.location = p; o.scale = (r, r, r); alpha(o, op)


def ring_at(p, r, col, op):
    if op <= .01:
        return
    key = tuple(round(c, 3) for c in col)
    pool = RINGS.setdefault(key, [])
    n = NR_.get(key, 0)
    if n >= len(pool):
        pool.append(flat_ring('ring%d' % len(pool), 1.0, .96, 64, look.additive(key, 'ring_%.3f_%.3f_%.3f' % key, 1.3)))
    o = pool[n]; NR_[key] = n + 1
    o.location = p; o.scale = (max(r, S), max(r, S), 1); alpha(o, op)


def frame_reset():
    NB.clear(); NR_.clear(); NP[0] = 0


def frame_finish():
    for key, pool in BEAMS.items():
        for c_, g_ in pool[NB.get(key, 0):]:
            c_.hide_render = g_.hide_render = True
    for key, pool in RINGS.items():
        for o in pool[NR_.get(key, 0):]:
            o.hide_render = True
    for o in PKS[NP[0]:]:
        o.hide_render = True


# ================================================================== cards, chips, bubbles
CARD_BORDER, CHIP_BORDER = hx(0x514b35), hx(0x4c6951)
KIND = {'ch': (ACC, 21), 'cmd': (FG, 24), 'arg': (ACCHI, 24), 'out': (DIM, 24), 'ok': (OK, 24), 'err': ((1, 1, 1), 24),
        'hand': (SOFT, 24)}


def bl(lh, size):
    """Baseline offset inside a line box (DejaVu Sans Mono metrics)."""
    return lh / 2 + .346 * size


class Card:
    def __init__(self, name, parent, w=640, n=7, coll='text'):
        self.root = kit.empty('card_' + name); self.root.parent = parent
        self.px = kit.empty('cardpx_' + name); self.px.parent = self.root
        self.w = w
        self.bg = Panel(name + '_bg', self.px, DARK, .95, CARD_BORDER, 2, 12, z=0, coll=coll)
        self.err = Panel(name + '_err', self.px, hx(0x38201b), 1.0, RED, 1.5, 8, z=.6, coll=coll)
        self.bars = [Panel('%s_bar%d' % (name, i), self.px, ACC, 1.0, None, r=0, z=.6, coll=coll) for i in range(n)]
        self.lines = [text('%s_l%d' % (name, i), self.px, 24, FG, 'mono', accent=AGC, cursor=ACC, coll=coll) for i in range(n)]
        self.h = 0

    def hide(self):
        self.bg.hide(); self.err.hide()
        for b in self.bars: b.hide()
        for t in self.lines: t.hide_render = True

    def set(self, rows, a=1.0, extra=0):
        """rows: [(kind, text, accent_ranges, cursor)]. Returns the card height."""
        if a <= .02 or not rows:
            self.hide(); return 0
        y = 20.0
        self.err.hide()
        nb = 0
        for i, t in enumerate(self.lines):
            if i >= len(rows):
                t.hide_render = True; continue
            kind, txt, rng, cur = rows[i]
            col, size = KIND[kind]
            lh = size * 1.45
            body = txt + ('█' if cur else '')
            t.data.size = size
            t.data.space_character = 1.05 if kind == 'ch' else 1.0
            set_text(t, body, rng, len(body) - 1 if cur else None, color=col)
            x = 24
            if kind == 'arg': x += 1.2 * size
            if kind == 'err':
                y += 8
                bh = lh + 16 + 3
                self.err.set(24, y, self.w - 48, bh, a)
                t.location = (24 + 1.5 + 12, -(y + 1.5 + 8 + bl(lh, size)), 1)
                y += bh
            elif kind == 'hand':
                y += 6
                self.bars[nb].set(24, y, 3, lh, a); nb += 1
                t.location = (24 + 3 + 14, -(y + bl(lh, size)), 1)
                y += lh + 6
            else:
                t.location = (x, -(y + bl(lh, size)), 1)
                y += lh + (10 if kind == 'ch' else 0)
            alpha(t, a)
        for b in self.bars[nb:]:
            b.hide()
        h = y + extra + 22
        self.bg.set(0, 0, self.w, h, a)
        self.h = h
        return h


def card_rows(head, nb, lines, p):
    """film.html cardHTML: head with its first nb chars bold (agent colour), lines revealed in sequence."""
    rows = [('ch', head, ((0, nb),), False)]
    t = .15
    for k, txt in lines:
        dur = 1.0 if k == 'cmd' else .7 if k == 'arg' else .5 if k == 'hand' else .25
        if p < t:
            break
        shown = typed(txt, (p - t) / dur) if k in ('cmd', 'arg') else txt
        cur = k in ('cmd', 'arg') and p < t + dur
        if k == 'cmd':
            rows.append((k, '› ' + shown, ((0, 1),), cur))
        else:
            rows.append((k, shown, (), cur))
        t += dur + (.55 if k == 'arg' else .35)
    return rows


class Chip:
    def __init__(self, name, parent, coll='text'):
        self.root = kit.empty('chip_' + name); self.root.parent = parent
        self.px = kit.empty('chippx_' + name); self.px.parent = self.root
        self.bg = Panel(name + '_bg', self.px, DARK, .92, CHIP_BORDER, 1.5, 8, coll=coll)
        self.t = text(name + '_t', self.px, 30, OK, 'mono', coll=coll)

    def hide(self):
        self.bg.hide(); self.t.hide_render = True

    def set(self, txt, a):
        if a <= .02:
            self.hide(); return 0, 0
        set_text(self.t, txt)
        w = len(txt) * 30 * .602 + 24 + 3
        h = 30 * 1.164 + 8 + 3
        self.bg.set(0, 0, w, h, a)
        self.t.location = (13.5, -(1.5 + 4 + .928 * 30), 1)
        alpha(self.t, a)
        return w, h


class Bubble:
    def __init__(self, name, parent, fill, border, coll='text'):
        self.root = kit.empty('bub_' + name); self.root.parent = parent
        self.px = kit.empty('bubpx_' + name); self.px.parent = self.root
        self.bg = Panel(name + '_bg', self.px, fill, .96, border, 2, 18, coll=coll)
        self.lab = text(name + '_lab', self.px, 20, DIM, 'mono', coll=coll)
        self.t = text(name + '_t', self.px, 34, FG, 'semi', coll=coll)
        self.wcache = {}

    def hide(self):
        self.bg.hide(); self.lab.hide_render = True; self.t.hide_render = True

    def set(self, label, txt, full, a):
        """full: the finished text, so the bubble does not grow while it types."""
        if a <= .02:
            self.hide(); return
        if full not in self.wcache:
            set_text(self.t, full); set_text(self.lab, label)
            self.wcache[full] = max(measure(self.t), measure(self.lab))
        set_text(self.t, txt); set_text(self.lab, label)
        w = self.wcache[full] + 48 + 4
        self.bg.set(0, 0, w, 112, a)
        self.lab.location = (26, -38.6, 1); self.t.location = (26, -83, 1)
        alpha(self.lab, a); alpha(self.t, a)


def billboard(root, p, k, off=(0, 0)):
    root.location = p
    root.rotation_euler = cam.rotation_euler.copy()
    root.scale = (k, k, k)
    root.children[0].location = (off[0], off[1], 0)


TOUR = [(40, 'build box', [('cmd', 'rook_call build-box shell.exec'), ('arg', 'docker build -t app:0.3 .'), ('out', 'step 12/12 · exporting layers'), ('ok', '✓ built in 41s')]),
        (46, 'home server', [('cmd', 'rook_call home-server hub.deploy'), ('arg', 'release v0.3 · signature ✓'), ('out', 'health: dashboard ✓  mcp ✓'), ('ok', '✓ live · rollback armed')]),
        (52, 'my laptop', [('cmd', 'rook_call my-laptop shell.exec'), ('arg', 'pytest -q'), ('out', '........................ 100%'), ('ok', '✓ 418 passed in 9.2s')]),
        (58, 'nas', [('cmd', 'rook_call nas shell.exec'), ('arg', 'restic backup /data'), ('out', 'processed 18,204 files'), ('ok', '✓ snapshot 4f2a · 2.1 GB')])]
CHIPS = {'claude code': ['build ✓', 'PR #42 ✓', 'tests ✓'], 'codex': ['lint ✓', 'deploy ✓', 'tests ✓'],
         'home agent': ['lights off ✓', 'grocery list ✓', 'thermostat 20° ✓'],
         'voice agent': ['timer 10m ✓', 'weather ✓', 'playing jazz ✓'], 'ops agent': ['backup ✓', 'disk 61% ✓', 'cert renewed ✓']}
TRAFFIC = []
for i, ak in enumerate(['claude code', 'codex', 'home agent', 'voice agent', 'ops agent']):
    for k in range(12):
        TRAFFIC.append(dict(ag=ak, b=72.4 + i * .37 + k * 1.33, dev=DL[(i * 5 + k * 7 + (i if k > 3 else 0)) % 12], chip=CHIPS[ak][k % 3]))

tour_cards = [Card('tour%d' % i, None, 640, 5) for i in range(len(TOUR))]
codex_card = Card('codex', None, 640, 5)
hand_card = Card('handoff', None, 640, 7)
ops_card = Card('ops', None, 640, 5)
fleet_chips = [Chip('fl%d' % i, None) for i in range(12)]
traffic_chips = [Chip('tr%d' % i, None) for i in range(len(TRAFFIC))]
vchip, tchip = Chip('vchip', None), Chip('tchip', None)
vbub = Bubble('vbub', None, hx(0x141813), ACC)
tbub = Bubble('tbub', None, hx(0x0e1e28), TGB)
WCARDS = [*tour_cards, codex_card, hand_card, ops_card]
WCHIPS = [*fleet_chips, *traffic_chips, vchip, tchip]

# ================================================================== camera-space HUD (px)
hud_label = text('hud_label', HUD, 28, AGC, 'mono', 56, 74, accent=DIM, body='◉ agent · claude code · running in the cloud')
BRACKETS = [line('bracket%d' % i, HUD, [(x, y + 60 * sy), (x, y), (x + 60 * sx, y)], 4, AGC)
            for i, (x, y, sx, sy) in enumerate(((30, 30, 1, 1), (1890, 30, -1, 1), (30, 1050, 1, -1), (1890, 1050, -1, -1)))]
tour_glow = line('tour_glow', HUD, [(0, 0), (0, 1)], 18, AGC)
tour_core = line('tour_core', HUD, [(0, 0), (0, 1)], 5, AGC, 1.5)
tour_pk = [disc('tour_pk%d' % i, HUD, 9, (1, 1, 1), 1.4) for i in range(3)]
RETICLE = [line('ret%d' % i, HUD, [(0, 0), (0, 1), (1, 1)], 4, AGC) for i in range(4)]
fleet_lines = [line('fleet_l%d' % i, HUD, [(0, 0), (0, 1)], 3, AGC) for i in range(12)]
fleet_card = Card('fleet', HUD, 900, 5)
fleet_bar_bg = Panel('fleet_barbg', fleet_card.px, hx(0x20261d), 1.0, None, r=7, z=.6)
fleet_bar = Panel('fleet_bar', fleet_card.px, OK, 1.0, None, r=7, z=.8)
def shade_mat():
    m = bpy.data.materials.new('capshade'); m.use_nodes = True
    nt = m.node_tree; nt.nodes.clear(); N, L = nt.nodes.new, nt.links.new
    out = N('ShaderNodeOutputMaterial'); em = N('ShaderNodeEmission'); tr = N('ShaderNodeBsdfTransparent')
    em.inputs['Color'].default_value = (*lin((5 / 255, 7 / 255, 5 / 255)), 1)
    tc = N('ShaderNodeTexCoord'); sep = N('ShaderNodeSeparateXYZ'); L(tc.outputs['UV'], sep.inputs[0])
    rp = N('ShaderNodeValToRGB'); cr = rp.color_ramp
    # film.html: rgba(5,7,5,.9) 0%, .6 at 22%, 0 at 42%; as linear-light coverage
    cr.elements[0].position = 0.0; cr.elements[0].color = (1 - .1 ** 2.2,) * 3 + (1,)
    cr.elements[1].position = 0.42; cr.elements[1].color = (0, 0, 0, 1)
    e = cr.elements.new(0.22); e.color = (1 - .4 ** 2.2,) * 3 + (1,)
    L(sep.outputs['Y'], rp.inputs['Fac'])
    oi = N('ShaderNodeObjectInfo'); mm = N('ShaderNodeMath'); mm.operation = 'MULTIPLY'
    L(rp.outputs['Color'], mm.inputs[0]); L(oi.outputs['Alpha'], mm.inputs[1])
    mx = N('ShaderNodeMixShader'); L(mm.outputs[0], mx.inputs['Fac']); L(tr.outputs[0], mx.inputs[1]); L(em.outputs[0], mx.inputs[2])
    L(mx.outputs[0], out.inputs[0])
    return m


cap_shade = quad('cap_shade', HUD, 1924, 1084, shade_mat()); cap_shade.location = (960, -540, -3)
title_scrim = quad('title_scrim', HUD, 1900, 760, ui.radial((0.01, 0.012, 0.01), 1.0, .8)); title_scrim.location = (960, -560, -1)
title1 = text('title1', HUD, 150, FG, 'black', 960, 476, 'CENTER', body='GIVE YOUR AGENTS')
title2 = text('title2', HUD, 250, ACC, 'black', 960, 702, 'CENTER', body='AGENCY.')
for t_ in (title1, title2):
    t_.data.space_character = .97
cap = text('cap', HUD, 104, FG, 'black', 96, 925, accent=ACC)
cap.data.space_character = .97
sub = text('sub', HUD, 32, ACCHI, 'mono', 100, 984)

# chat panel (113.6-128.5)
CHAT = kit.empty('chatpanel'); CHAT.parent = HUD
chat_bg = Panel('cp_bg', CHAT, hx(0x0e110e), .96, hx(0x3b4232), 2, 16, z=0)
chat_hl = Panel('cp_hl', CHAT, LINEC, 1.0, None, r=0, z=.5)
chat_h1 = text('cp_h1', CHAT, 26, FG, 'mono', 26, 44, body='# release')
chat_h2 = text('cp_h2', CHAT, 20, DIM, 'mono', 26 + 9 * 26 * .602 + 14, 44, body='3 members · 2 agents')


def chat_msg(i):
    g = kit.empty('cm%d' % i); g.parent = CHAT
    av = Panel('cm%d_av' % i, g, AGC, 1.0, None, r=12, z=.5)
    ini = text('cm%d_ini' % i, g, 22, hx(0x0e110e), 'mono_b', 26 + 27, 34.6, 'CENTER')
    nm = text('cm%d_nm' % i, g, 21, AGC, 'mono', 96, 19.5, accent=DIM)
    tx = text('cm%d_tx' % i, g, 31, FG, 'reg', 96, 61.8, accent=ACCHI)
    tx.data.text_boxes[0].width = 638
    tx.data.space_line = 1.07
    dots = [disc('cm%d_dot%d' % (i, k), g, 6, DIM) for k in range(3)]
    for k, d_ in enumerate(dots):
        d_.location = (96 + 6 + k * 20, -(30.4 + 12), 1)
    return dict(g=g, av=av, ini=ini, nm=nm, tx=tx, dots=dots)


CM = [chat_msg(0), chat_msg(1)]
CHAT_OBS = descendants(CHAT)

# end card (130.5-142)
end_title = text('end_title', HUD, 104, FG, 'black', 960, 742, 'CENTER', accent=ACC, body='GIVE YOUR AGENTS AGENCY.')
end_title.data.space_character = .97
end_brand = text('end_brand', HUD, 64, FG, 'sans_b', 960 + 11, 842, 'CENTER', body='♜ ROOK')
end_brand.data.space_character = 1.5
end_cta1 = text('end_cta1', HUD, 38, ACCHI, 'mono', 960, 912, 'CENTER')
end_cta2 = text('end_cta2', HUD, 24, DIM, 'mono', 960, 958, 'CENTER', body='open source · self-hosted · end-to-end encrypted')
end_cta2.data.space_character = 1.08
END_OBS = [end_title, end_brand, end_cta1, end_cta2]
HUD_OBS = [o for o in descendants(HUD) if o is not HUD]
# the HUD renders in the overlay layer, so its flat pieces never hide the ink lines of the world behind
CARD_OBS = [o for c in WCARDS for o in descendants(c.root)] + [o for c in WCHIPS for o in descendants(c.root)] + \
    descendants(vbub.root) + descendants(tbub.root)
for o in HUD_OBS + CARD_OBS:   # cards and chips float over the world, like the original's html cards
    if o.type != 'EMPTY':
        for c in list(o.users_collection):
            c.objects.unlink(o)
        bpy.data.collections['boxui'].objects.link(o)
BOX_OBS = [o for o in descendants(BOX) if o is not BOX]
WORLD_OBS = descendants(WORLD) + [d['label'] for d in DL] + TG_OBS + [a['label'] for a in A.values()] + \
    [a['sub'] for a in A.values()] + [o for c in WCARDS for o in descendants(c.root)] + \
    [o for c in WCHIPS for o in descendants(c.root)] + descendants(vbub.root) + descendants(tbub.root)


def fit_cap(base, maxw):
    cap.data.size = base
    w = measure(cap)
    if w > maxw:
        cap.data.size = math.floor(base * maxw / w)


# ================================================================== camera rigs
CAMK = [(32, 0, 0, 2.8, -10), (34, 0, 0, .44, 0, 'out'), (39.6, 90, -40, .5, 3),
        (40.7, 1230, -600, 1.2, 0, 'out'), (45.9, 1250, -610, 1.27, 1),
        (46.7, 400, -800, 1.2, -1, 'out'), (51.9, 420, -815, 1.27, 0),
        (52.7, -670, -390, 1.2, 1, 'out'), (57.9, -650, -400, 1.27, 0),
        (58.7, -1320, 140, 1.2, -1, 'out'), (63.9, -1300, 130, 1.27, 0),
        (65.2, 0, -60, .43, 0, 'out'), (71.8, 0, -60, .47, 2),
        (72.6, 0, 0, .34, -3, 'out'), (87.9, 0, 0, .37, 4),
        (89.8, -250, -1040, 1.3, 0, 'out'), (91.9, -240, -1040, 1.33, 0),
        (100.5, -760, -1040, .74, -2), (103.9, -740, -1020, .78, -1),
        (104.8, 330, 470, .82, 0, 'out'), (113.2, 340, 480, .86, 1),
        (114.6, 1230, -100, .9, 0, 'out'), (127.9, 1240, -110, .95, 0),
        (130.5, 0, 0, .12, 0, 'in')]


def cam_at(tb):
    if tb <= CAMK[0][0]:
        return CAMK[0][1:5]
    for i in range(1, len(CAMK)):
        if tb <= CAMK[i][0]:
            a, b = CAMK[i - 1], CAMK[i]
            p = inv(a[0], b[0], tb)
            mode = b[5] if len(b) > 5 else None
            p = eOutC(p) if mode == 'out' else eInC(p) if mode == 'in' else eInOut(p)
            return (lerp(a[1], b[1], p), lerp(a[2], b[2], p), math.exp(lerp(math.log(a[3]), math.log(b[3]), p)), lerp(a[4], b[4], p))
    return CAMK[-1][1:5]


def set_world_cam(cx, cy, zoom, rot, shake=(0, 0)):
    cam.data.angle_y = math.radians(32)
    Dd = 540 / (zoom * TAN16)
    az = -math.radians(rot)
    cam.location = W(cx + Dd * math.sin(az) * math.cos(EL), cy + Dd * math.cos(az) * math.cos(EL), Dd * math.sin(EL))
    kit.look_at(cam, W(cx, cy, 0))
    if shake != (0, 0):
        m = cam.matrix_world.to_3x3()
        k = kpx() * Dd * S
        cam.location += m @ Vector((shake[0] * k, -shake[1] * k, 0))
    return Dd


def place_label(ob, p, px_world, cap_px, op, col=None):
    if op <= .02:
        ob.hide_render = True; return 0
    sx, sy, k, front = screen(p)
    fs = min(cap_px / k, px_world * S)
    ob.location = p; ob.rotation_euler = cam.rotation_euler.copy(); ob.scale = (fs, fs, fs)
    if col is not None:
        ob.data.materials[0] = ui.ui(col)
    alpha(ob, op)
    return fs


def hud_line(ob, pts, a, width=None):
    set_line(ob, pts, width); alpha(ob, a)


# ================================================================== per-frame state
def apply(tb):
    frame_reset()
    flash = white = black = shade = 0.0
    force, pagefade = 0.0, 0.0
    wshake = 0.0
    scene = 'box' if tb < 16.5 else 'rook' if (tb < 32 or tb >= 130.5) else 'world'
    for o in ROOK_OBS: o.hide_render = scene != 'rook'
    for o in WORLD_OBS: o.hide_render = scene != 'world'
    # transient pieces start hidden every frame and are shown only when set below
    for o in HUD_OBS: o.hide_render = True
    for o in BOX_OBS: o.hide_render = True
    for c in WCARDS: c.hide()
    for c in WCHIPS: c.hide()
    vbub.hide(); tbub.hide()
    for d in DL: d['label'].hide_render = True
    for a in A.values(): a['label'].hide_render = True; a['sub'].hide_render = True
    for o in TG_OBS: o.hide_render = True
    CRACK_T.outputs[0].default_value = tb
    box_shake = (0, 0)

    # ---------------------------------------------------------- the box (0-16) and the glass (to 19.6)
    if tb < 16:
        alpha(BOXGLOW, .14 * (.25 + .4 * math.exp(-since(tb, 2) / .3)) * 1.6)
        c = eInC(inv(10.5, 16, tb))
        gapX, gapY = lerp(960, 480, c), lerp(560, 245, c)
        pulse = 8 * math.exp(-since(tb, .5 if tb > 14 else 1) / .12) if tb > 10.5 else 0
        Lw, Rw = 960 - gapX + pulse, 960 + gapX - pulse
        Tw, Bw = 540 - gapY + pulse * .6, 540 + gapY - pulse * .6
        for k_, (rect, edge) in (('l', ((-200, -200, Lw + 200, 1480), (Lw - 4, -200, 4, 1480))),
                                 ('r', ((Rw, -200, 2120 - Rw, 1480), (Rw, -200, 4, 1480))),
                                 ('t', ((-200, -200, 2320, Tw + 200), (-200, Tw - 4, 2320, 4))),
                                 ('b', ((-200, Bw, 2320, 1280 - Bw), (-200, Bw, 2320, 4)))):
            fillp, edgep = walls[k_]
            on = (Lw > 0) if k_ == 'l' else (Rw < 1920) if k_ == 'r' else (Tw > 0) if k_ == 't' else (Bw < 1080)
            if on:
                fillp.set(*rect); edgep.set(*edge)
            else:
                fillp.hide(); edgep.hide()
        if tb < 11.95:
            for o in CHATBOX_OBS: o.hide_render = False
            alpha(chatbox.f, 1); alpha(chatbox.b, 1); alpha(chat_hd.f, 1)
            for i, (a, b, s) in enumerate(MSGS):
                p = inv(a, b, tb)
                on = tb >= a
                msg_who[i].hide_render = not on; msg_txt[i].hide_render = not on
                cur = on and (p < 1 or (i == 2 and int(tb * 2) % 2 == 0))
                body = typed(s, p) + ('█' if cur else '')
                set_text(msg_txt[i], body, (), len(body) - 1 if cur else None)
        else:
            pane.hide_render = False
            cracks.hide_render = tb < T0
        sh = 2 + 8 * inv(11, 16, tb) if tb > 11 else 0
        if sh:
            box_shake = ((rnd(int(tb * 8)) - .5) * sh * math.exp(-since(tb, 1) / .2) * 2, (rnd(int(tb * 8) + 5) - .5) * sh)
        if tb < 1.2:
            black = 1 - inv(0, 1.2, tb)
    elif tb < 19.7:
        s = (tb - 16) * BEAT
        for x in SHARDS:
            t = max(0.0, s - x['delay'])
            fade = 1 - inv(1.2, 1.7, s)
            ob = x['ob']
            ob.hide_render = fade <= 0
            sp = x['sp'] * (1 - math.exp(-t * 3))
            ob.location = (x['cx'] + x['dir'].x * sp * t * .9, x['cy'] + x['dir'].y * sp * t * .9 - 520 * t * t, x['vz'] * t * (1 - .25 * t))
            from mathutils import Quaternion
            ob.rotation_quaternion = Quaternion(x['ax'], x['w'] * t * .35)
    if 16 <= tb < 19:
        flash = max(flash, .9 * math.exp(-(tb - 16) / .35))

    # ---------------------------------------------------------- the rook (16.5-32, 130.5-142)
    if scene == 'rook' or scene == 'box':
        end = tb >= 120
        open_ = 1.0 if end else eInOut(inv(22.2, 25.5, tb))
        crack = 0.0 if end else inv(21.3, 22.2, tb) * (1 - inv(22.2, 22.6, tb))
        RP['hinge'].rotation_euler.z = -(open_ * 1.85 + crack * .05)
        Lt = 1.0 if end else inv(21.4, 23.4, tb)
        flick = 1 + .05 * math.sin(tb * 9.1) + .03 * math.sin(tb * 23.7)
        for o in RP['solids']:
            o.color = (1, 1, 1, .45 * max(open_, Lt * .35) * flick)
        alpha(RP['spill'], .6 * open_ * flick)
        alpha(RP['sprite'], .55 * max(open_, crack * .6) * flick)
        if scene == 'box':
            rook_cam(16.5)
        else:
            past = rook_cam(tb)
            force = 1.0 if past > 0 else 0.0
            if not end:
                pagefade = 1 - eInOut(inv(17, 20.5, tb)) ** 2.2
                white = eInC(inv(31.2, 32, tb))
            else:
                white = 1.0 if tb < 131 else 1 - eOutC(inv(131, 132, tb))
                s_ = eOutE(inv(133, 133.4, tb))
                set_text(end_title, 'GIVE YOUR AGENTS AGENCY.', ((17, 24),))
                alpha(end_title, s_); end_title.scale = (lerp(1.3, 1, s_),) * 3
                alpha(end_brand, inv(134, 134.6, tb))
                set_text(end_cta1, typed('github.com/Bake-Ware/rook', inv(135, 136.4, tb)))
                alpha(end_cta1, 1 if tb >= 135 else 0)
                alpha(end_cta2, inv(136.6, 137.2, tb))
                if s_ > .02:
                    title_scrim.location = (960, -850, -1); title_scrim.scale = (1, .55, 1)
                    alpha(title_scrim, .7 * s_)
                black = eInC(inv(139, 142, tb))

    # ---------------------------------------------------------- the world (32-130.5)
    if scene == 'world':
        cx, cy, zm, rot = cam_at(tb)
        KZ = clamp(.5 / zm, 1, 1.6)
        CS = clamp(.95 / zm, 1, 3)
        set_world_cam(cx, cy, zm, rot)
        for i, d in enumerate(DL):
            a = 32.15 + rnd(i * 3) * 1.6
            s = eOutE(inv(a, a + .5, tb))
            d['scale'] = (.4 + .6 * s) * KZ; d['op'] = s
            d['root'].scale = (d['scale'],) * 3
            show(d['root'], s > .02)
            d['glow'] = 0.0
        tgs = eOutC(inv(104.3, 105, tb)) * (1 if tb < 129 else 0)
        for i, m in enumerate(MESH_OBS):
            alpha(m, .22 * inv(33, 35, tb) * (.5 if 40 <= tb < 72 else 1) * 1.6)
        alpha(GRIDP, .07)
        for g in COMPASS:
            g.hide_render = False
        for i, (key, _, _, _) in enumerate(AG):
            a = A[key]
            s = eOutE(inv(72 + i * .25, 72.5 + i * .25, tb)) if tb >= 72 else 0
            if key == 'claude code ':
                s = eOutE(inv(97, 97.8, tb))
            dead = key == 'codex' and tb >= 91.5
            op = s * (lerp(1, .28, inv(91.5, 93, tb)) if dead else 1)
            a['op'] = op; a['scale'] = (.5 + .5 * s) * KZ; a['dead'] = dead
            a['root'].scale = (a['scale'],) * 3
            show(a['root'], op > .02)
            if op > .02:
                pulse = 1 + .06 * math.exp(-since(tb, 1) / .15)
                a['halo'].scale = (pulse, pulse, 1)
                a['halo'].data.materials[0] = ui.ui(hx(0x5a5f55) if dead else a['col'])
                alpha(a['halo'], .9 * op)
                alpha(a['pillar'], (.05 if dead else .22) * op * 2)
                alpha(a['base'], (.1 if dead else .35) * op * 1.5)
                a['hex'].rotation_euler.z = tb * .35 + a['x']
                a['hex'].data.materials[0] = a['deadm'] if dead else a['live']
        if 97 <= tb < 99:
            ring_at(W(A['claude code ']['x'], A['claude code ']['y'], 3), (60 + 260 * eOutC(inv(97, 98.5, tb))) * S, AGC, 1 - inv(97, 98.5, tb))

        # ----- title (32-40)
        if tb < 40:
            s1, s2 = eOutE(inv(32.1, 32.5, tb)), eOutE(inv(33, 33.4, tb))
            fade = 1 - inv(39, 39.8, tb)
            alpha(title1, s1 * fade); alpha(title2, s2 * fade)
            title1.scale = (lerp(1.5, 1, s1),) * 3; title2.scale = (lerp(1.6, 1, s2),) * 3
            title_scrim.location = (960, -560, -1); title_scrim.scale = (1, 1, 1)
            alpha(title_scrim, .75 * max(s1, s2) * fade)
            flash = max(flash, .95 * math.exp(-(tb - 32) / .35))
            if tb > 34:
                for i, (a_, b_) in enumerate(MESH):
                    p = since(tb + rnd(i) * 3, 3) / 3
                    da, db = DL[a_], DL[b_]
                    pk_at(W(lerp(da['x'], db['x'], p), lerp(da['y'], db['y'], p), 3), 8 * S, .9)
            wshake = 6 * math.exp(-(tb - 32) / .8)

        # ----- first person tour (40-72)
        if 40 <= tb < 72:
            set_text(hud_label, '◉ agent · claude code · running in the cloud', ((20, 46),))
            alpha(hud_label, 1)
            for b in BRACKETS: alpha(b, .7)
            tour = [x for x in TOUR if x[0] <= tb]
            if tb < 64.5 and tour:
                ti = TOUR.index(tour[-1])
                b0, dn, lines = tour[-1]
                d = D[dn]; u = tb - b0
                px, py, k, _ = screen(W(d['x'], d['y'], 70))
                grow = eOutC(clamp((u - .5) / .5))
                sx, sy = 960 - 120, 1080 + 40
                ex, ey = lerp(sx, px, grow), lerp(sy, py + 40, grow)
                if grow > .001:
                    hud_line(tour_glow, [(sx, sy, 0), (ex, ey, 0)], .3)
                    hud_line(tour_core, [(sx, sy, .2), (ex, ey, .2)], 1)
                if grow >= 1:
                    for kk in range(3):
                        pp = since(u * 1.3 + kk / 3, 1)
                        tour_pk[kk].location = (lerp(sx, px, pp), -lerp(sy, py + 40, pp), .4); alpha(tour_pk[kk], 1)
                pk = k * S
                rs = 120 * pk * (1 + .25 * math.exp(-u / .2))
                for qi, (qx, qy) in enumerate(((-1, -1), (1, -1), (-1, 1), (1, 1))):
                    x0, y0 = px + qx * rs, py + qy * rs
                    hud_line(RETICLE[qi], [(x0, y0 - qy * 34), (x0, y0), (x0 - qx * 34, y0)], grow)
                d['glow'] = grow
                rows = card_rows('claude code → ' + dn, 11, lines, u - .9)
                al = eOutC(inv(.8, 1.1, u)) * (1 - inv(5.6, 6, u))
                tour_cards[ti].set(rows, al)
                billboard(tour_cards[ti].root, W(d['x'] + 150, d['y'] - 230, 150), S)
            if tb >= 64.5:
                done = 0
                sx, sy = 960, 1080 + 40
                for i, d in enumerate(DL):
                    a = 65.2 + i * .5
                    g = eOutC(inv(a, a + .35, tb))
                    if g <= 0: continue
                    px, py, k, _ = screen(W(d['x'], d['y'], 70))
                    hud_line(fleet_lines[i], [(sx, sy), (lerp(sx, px, g), lerp(sy, py, g))], .75)
                    if g >= 1:
                        done += 1
                        d['glow'] = .8
                        w_, h_ = fleet_chips[i].set('✓ 0.3', 1)
                        billboard(fleet_chips[i].root, W(d['x'], d['y'] - 140 * KZ, 150), S * CS, (-w_ / 2, h_))
                frac = done / 12
                fa = eOutC(inv(64.5, 65, tb)) * (1 - inv(71.6, 72, tb))
                rows = [('ch', 'claude code → all 12 machines', ((0, 11),), False), ('cmd', '› rook_call --all worker.update', ((0, 1),), False),
                        ('arg', 'build 0.3 · signed', (), False),
                        ('ok' if done == 12 else 'out', '✓ 12/12 updated · 0 ports opened' if done == 12 else 'updating %d/12 …' % done, (), False)]
                h = fleet_card.set(rows, fa, extra=26)
                fleet_card.root.location = (510, -(1010 - h), 0)
                fleet_bar_bg.set(24, h - 22 - 14, 852, 14, fa)
                if frac > 0:
                    fleet_bar.set(24, h - 22 - 14, max(14, 852 * frac), 14, fa)
            if tb < 40.6:
                flash = max(flash, .2 * math.exp(-(tb - 40) / .15))
            wshake = 2.5

        # ----- one layer: traffic (72-131)
        if tb >= 72:
            traff = 1 if tb < 88 else .25 if tb < 104 else .18 if tb < 128 else .4
            for i, tr in enumerate(TRAFFIC):
                a = A[tr['ag']]; d = tr['dev']
                b0 = tr['b']
                while b0 + 16 < tb: b0 += 16
                u = tb - b0
                if u < 0 or u > 2.2: continue
                if tr['ag'] == 'codex' and tb >= 91.5: continue
                g = eOutC(clamp(u / .45))
                fade = 1 - inv(1.6, 2.2, u)
                beam(W(a['x'], a['y'], 125), W(d['x'], d['y'], 80), g, a['col'], since(u * 1.5, 1) if g >= 1 else None, traff * fade * .9, 1.6)
                if g >= 1:
                    d['glow'] = max(d['glow'], .7 * fade * traff)
                if g >= 1 and tb < 88 and fade > .02:
                    w_, h_ = traffic_chips[i].set(tr['chip'], fade)
                    billboard(traffic_chips[i].root, W(d['x'], d['y'] - 140 * KZ, 150), S * CS, (-w_ / 2, h_))
            if tb < 88:
                parts = [(72, 'ONE LAYER.'), (76, 'EVERY AGENT.'), (80, 'EVERY DEVICE.')]
                cur = [p for p in parts if p[0] <= tb]
                txt = ' '.join(p[1] for p in cur)
                last = cur[-1][0]
                n0 = len(txt) - len(cur[-1][1])
                set_text(cap, txt, ((n0, len(txt)),)); fit_cap(104, 1728)
                sl = eOutE(inv(last, last + .3, tb))
                cap.scale = (lerp(1.08, 1, sl),) * 3; alpha(cap, 1)
                s_txt = 'claude code · codex · your home agent · your voice agent · ops' if tb < 80 else 'laptops · phones · servers · a pi in the closet'
                set_text(sub, typed(s_txt, inv(last + .3, last + 2, tb))); alpha(sub, 1)
                if tb < 73: flash = max(flash, .7 * math.exp(-(tb - 72) / .3))
                wshake = 5
                shade = 1

        # ----- limit reached + handoff (88-104)
        cA, hs = A['codex'], D['home server']
        u = tb - 88.6
        if 88.6 <= tb < 104:
            lines = [('out', 'release v0.3 · step 4 of 6'), ('cmd', 'rook_call home-server hub.deploy'), ('out', 'uploading release …')]
            rows = card_rows('codex → home server', 5, lines, u)
            if tb >= 91.5: rows.append(('err', '⊘ usage limit reached · session ended', (), False))
            al = eOutC(inv(88.6, 89, tb)) * (1 if tb < 93 else lerp(1, .35, inv(93, 96, tb))) * (1 - inv(103, 104, tb))
            codex_card.set(rows, al)
            billboard(codex_card.root, W(cA['x'] + 140, cA['y'] - 330, 150), S)
        if 89.6 <= tb < 91.5:
            beam(W(cA['x'], cA['y'], 125), W(hs['x'], hs['y'], 80), eOutC(inv(89.6, 90, tb)), cA['col'], since(tb * 1.5, 1), 1, 1.5)
        if 91.5 <= tb < 92.3:
            beam(W(cA['x'], cA['y'], 125), W(hs['x'], hs['y'], 80), 1, RED, None, 1 - inv(91.5, 92.3, tb), 1.5)
        if 91.5 <= tb < 92:
            flash = max(flash, .25 * (1 - inv(91.5, 92, tb)))
        nA = A['claude code ']
        u2 = tb - 98.2
        if 98.2 <= tb < 104:
            hl = [('cmd', 'rook_handoff_get release-v0.3'), ('hand', 'step 4 of 6: deploy the hub'), ('hand', 'next: roll out the workers'),
                  ('hand', 'note: rollback is armed'), ('cmd', 'resume')]
            rows = card_rows('claude code · new session', 11, hl, u2)
            if tb >= 102.6: rows.append(('ok', '✓ hub live · on to step 5 of 6', (), False))
            hand_card.set(rows, eOutC(inv(98.2, 98.6, tb)) * (1 - inv(103.4, 104, tb)))
            billboard(hand_card.root, W(nA['x'] - 1000, nA['y'] - 160, 150), S * 1.35)
        if 101.8 <= tb < 104:
            beam(W(nA['x'], nA['y'], 125), W(hs['x'], hs['y'], 80), eOutC(inv(101.8, 102.3, tb)), nA['col'], since(tb * 1.5, 1), 1 - inv(103.4, 104, tb), 1.5)
            hs['glow'] = 1
        if 99.8 <= tb < 104:
            txt = 'THE SESSION ENDS.' if tb < 101.8 else "THE SESSION ENDS. THE WORK DOESN'T."
            set_text(cap, txt, ((18, len(txt)),) if tb >= 101.8 else ()); fit_cap(104, 1728)
            st = 99.8 if tb < 101.8 else 101.8
            cap.scale = (lerp(1.08, 1, eOutE(inv(st, st + .3, tb))),) * 3; alpha(cap, 1)
            set_text(sub, typed('handoffs, tasks and knowledge live on the band, not in the chat', inv(102, 103.6, tb))); alpha(sub, 1)
            shade = 1

        # ----- tools to talk to you (104-114)
        cc, ph, ops = A['claude code'], D['my phone'], A['ops agent']
        if 104 <= tb < 114:
            fo = 1 - inv(113, 114, tb)
            if tb >= 104.6:
                beam(W(cc['x'], cc['y'], 125), W(ph['x'], ph['y'], 80), eOutC(inv(104.6, 105.1, tb)), cc['col'], since(tb * 1.5, 1), fo, 1.5)
                ph['glow'] = 1
                for k in range(3):
                    p = since(tb + k * .5, 1.5) / 1.5
                    if tb > 105.2:
                        ring_at(W(ph['x'], ph['y'], 3), (70 + 160 * p) * S, ACCHI, (1 - p) * fo)
                w_, h_ = vchip.set('voice.speak', eOutC(inv(104.8, 105.2, tb)) * fo)
                billboard(vchip.root, W((cc['x'] + ph['x']) / 2 - 40, (cc['y'] + ph['y']) / 2 - 30, 150), S * 1.5, (-w_ / 2, h_))
                full = '“PR #42 is merged. v0.3 is live.”'
                q = '“' + typed('PR #42 is merged. v0.3 is live.', inv(105.4, 107.4, tb)) + '”'
                vbub.set('♪ my phone, out loud', q, full, eOutC(inv(105.2, 105.6, tb)) * fo)
                billboard(vbub.root, W(ph['x'] - 200, ph['y'] - 440, 150), S * 1.4)
            if tb >= 108.6:
                beam(W(cc['x'], cc['y'], 125), W(TG['x'], TG['y'], 90), eOutC(inv(108.6, 109.1, tb)), cc['col'], since(tb * 1.5, 1), fo, 1.5)
                w_, h_ = tchip.set('telegram.send', eOutC(inv(108.8, 109.2, tb)) * fo)
                billboard(tchip.root, W((cc['x'] + TG['x']) / 2 - 320, (cc['y'] + TG['y']) / 2, 150), S * 1.5, (-w_ / 2, h_))
                full = '✓ PR #42 merged · v0.3 deployed'
                tbub.set('✈ telegram', full, full, eOutC(inv(109.3, 109.7, tb)) * fo)
                billboard(tbub.root, W(TG['x'] + 130, TG['y'] - 120, 150), S * 1.3)
            sl = eOutE(inv(104, 104.3, tb))
            set_text(cap, 'TOOLS TO TALK TO YOU', ((9, 20),)); fit_cap(104, 1728)
            cap.scale = (lerp(1.3, 1, sl),) * 3; alpha(cap, sl)
            set_text(sub, typed('voice on your phone · telegram · notifications', inv(104.4, 106, tb))); alpha(sub, 1)
            if tb < 104.6: flash = max(flash, .7 * math.exp(-(tb - 104) / .3))
            wshake = 4
            shade = 1

        # ----- agents that work together (113.6-128.5)
        if 113.6 <= tb < 128.5:
            ci = eOutC(inv(113.6, 114.2, tb))
            fade = 1 - inv(127.6, 128.4, tb)
            CHAT.location = (1100 + (1 - ci) * 900, -110, 0)
            chat_bg.set(0, 0, 760, 860, fade); chat_hl.set(2, 70, 756, 1, fade)
            alpha(chat_h1, fade); alpha(chat_h2, fade)
            msgs = [(0, 'CC', AGC, 'claude code', '@ops-agent notified the user as requested: voice and telegram. PR #42 is merged.', 115, 114.2,
                     ((0, 10), (63, 69)), 2),
                    (1, 'OA', hx(0x9fd37c), 'ops agent', 'ok. updating the knowledge docs and assigning the blocked task.', 118.6, 117.2, (), 2)]
            y = 96
            for i, ini, col, nm, txt, a_, typing_at, acc, nl in msgs:
                m = CM[i]
                if tb < typing_at:
                    continue
                m['g'].location = (0, -y, 0)
                m['av'].f.data.materials[0] = ui.ui(col)
                set_text(m['ini'], ini); set_text(m['nm'], nm + ' · agent' if tb >= a_ else nm, ((len(nm), len(nm) + 8),), color=col)
                if tb < a_:
                    m['av'].set(26, 0, 54, 54, fade); alpha(m['ini'], fade); alpha(m['nm'], fade)
                    for k, d_ in enumerate(m['dots']):
                        alpha(d_, fade * (.4 + .6 * (int(tb * 4) % 2) if k == 2 else fade))
                    y += 30.4 + 12 + 26 + 12
                else:
                    sl = eOutC(inv(a_, a_ + .3, tb))
                    m['g'].location = (0, -(y + (1 - sl) * 30), 0)
                    set_text(m['tx'], txt, acc)
                    m['av'].set(26, 0, 54, 54, sl * fade)
                    for o in (m['ini'], m['nm'], m['tx']): alpha(o, sl * fade)
                    y += 30.4 + nl * 40.3 + 26
            u = tb - 119.6
            ol = [('cmd', 'rook_knowledge update release-v0.3'), ('ok', '✓ knowledge page updated'),
                  ('cmd', 'rook_task assign "rotate backup key"'), ('ok', '✓ blocked task → home agent')]
            if tb >= 119.6:
                ops_card.lines[0].data.materials[1] = ui.ui(hx(0x9fd37c))
                ops_card.set(card_rows('ops agent', 9, ol, u), eOutC(inv(119.6, 120, tb)) * fade)
                billboard(ops_card.root, W(ops['x'] - 360, ops['y'] - 520, 150), S)
            if tb >= 125.6:
                beam(W(ops['x'], ops['y'], 125), W(A['home agent']['x'], A['home agent']['y'], 125), eOutC(inv(125.6, 126.6, tb)), ops['col'],
                     since(tb * 1.5, 1), fade, 1.5)
            sl = eOutE(inv(114, 114.3, tb))
            set_text(cap, 'AGENTS THAT WORK TOGETHER', ((12, 25),)); fit_cap(96, 924)
            cap.scale = (lerp(1.2, 1, sl),) * 3; alpha(cap, sl * (1 - inv(127.6, 128.2, tb)))
            set_text(sub, typed('shared chat · tasks · knowledge · handoffs', inv(114.4, 116, tb))); alpha(sub, 1 - inv(127.6, 128.2, tb))
            wshake = 3
            shade = 1
        if tb >= 128:
            white = max(white, eInC(inv(129.4, 131, tb))); wshake = 3

        # device glow, telegram, labels
        for d in DL:
            alpha(d['ring'], .6 * d['glow']); alpha(d['pool'], .07 * d['glow'] * 2)
            for s_ in d['screens']:
                s_.color = (1, 1, 1, d['glow'])
        if wshake > 0:
            e = math.exp(-since(tb, 1) / .16) * wshake
            n = int(tb)
            set_world_cam(cx, cy, zm, rot, ((rnd(n * 2 + 1) - .5) * 2 * e, (rnd(n * 2 + 7) - .5) * 2 * e))
        for d in DL:
            place_label(d['label'], W(d['x'], d['y'] + 120 * d['scale']), 30 * d['scale'], 40, d['op'])
        for a in A.values():
            fs = place_label(a['label'], W(a['x'], a['y'] + 105 * a['scale']), 32 * a['scale'], 42, a['op'],
                             hx(0x6b7064) if a['dead'] else a['col'])
            if fs:
                up = cam.matrix_world.col[1].xyz
                place_label(a['sub'], W(a['x'], a['y'] + 105 * a['scale']) - up * fs * 1.2, 32 * .72 * a['scale'], 42 * .72, a['op'])
            else:
                a['sub'].hide_render = True
        if tgs > .02:
            p = W(TG['x'], TG['y'], 90)
            TGB_ROOT.location = p; TGB_ROOT.rotation_euler = cam.rotation_euler.copy()
            TGB_ROOT.scale = (120 / 256 * S * KZ,) * 3
            for o in (tg_disc, tg_ring, tg_plane): alpha(o, tgs)
            place_label(TG_LAB, W(TG['x'], TG['y'] + 90 * KZ), 30 * KZ, 40, tgs)
        else:
            for o in TG_OBS: o.hide_render = True
        # chips, cards and bubbles that were not set this frame stay hidden (hide() at the top)

    frame_finish()
    place_overlays(box_shake)
    vl = sc.view_layers
    vl['portal'].use = vl['mask'].use = scene == 'rook'
    COMP['force'].default_value = force
    COMP['pagefade'].default_value = clamp(pagefade)
    # film.html blended in sRGB; the compositor works in linear light
    COMP['flash'].default_value = clamp(flash, 0, .95) ** 2.2
    COMP['white'].default_value = clamp(white) ** 2.2
    COMP['black'].default_value = 1 - (1 - clamp(black)) ** 2.2
    if COMP['shade'] is not None:
        COMP['shade'].default_value = 0.0
    if shade > 0:
        alpha(cap_shade, shade)


def render_frame(f, path):
    apply(f / FPS / BEAT)
    sc.render.filepath = path
    bpy.ops.render.render(write_still=True)


if __name__ == '__main__':
    args = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else []
    if args:
        mode, outdir = args[0], args[1]
        os.makedirs(outdir, exist_ok=True)
        if mode == 'stills':
            for s in args[2:]:
                f = int(round(float(s) * FPS))
                t0 = time.time()
                render_frame(f, os.path.join(outdir, 't%06.2f.png' % float(s)))
                print('STILL %s f%d %.1fs' % (s, f, time.time() - t0), flush=True)
        elif mode == 'frames':
            f0, f1 = int(args[2]), int(args[3])
            step = int(args[4]) if len(args) > 4 else 1
            for f in range(f0, f1, step):
                p = os.path.join(outdir, 'f%04d.png' % f)
                if os.path.exists(p):
                    continue
                t0 = time.time()
                render_frame(f, p)
                print('FRAME %d %.1fs' % (f, time.time() - t0), flush=True)
