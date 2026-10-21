"""Scene-kit models for the Rook Moves series, built in Blender and exported as glTF.

    blender -b --factory-startup -P models.py -- assets/      (or python3 with the bpy module)

Every asset is low poly with flat faces; the three.js kit replaces materials by
name (body, dark, metal, wood, stone, screen, glass, light, accent, skin, cloth...)
with its toon palette, and draws ink on the edges. Named empties are pivots the
clips animate (door, lid, cover, pen, bed, head...). One unit is one Board square.
"""
import math, os, sys
import bpy, bmesh
from mathutils import Vector, Matrix

OUT = sys.argv[sys.argv.index('--') + 1] if '--' in sys.argv else os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets')
os.makedirs(OUT, exist_ok=True)
_mats = {}


def mat(name):
    if name not in _mats:
        m = bpy.data.materials.new(name)
        _mats[name] = m
    return _mats[name]


def reset():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    _mats.clear()


def link(ob, parent=None):
    bpy.context.scene.collection.objects.link(ob)
    if parent is not None:
        ob.parent = parent
    return ob


def empty(name, loc=(0, 0, 0), parent=None):
    e = bpy.data.objects.new(name, None)
    e.location = loc
    return link(e, parent)


def mesh(name, verts, faces, m, parent=None, loc=(0, 0, 0)):
    me = bpy.data.meshes.new(name)
    me.from_pydata([tuple(v) for v in verts], [], faces)
    bm = bmesh.new(); bm.from_mesh(me)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(me); bm.free()
    me.materials.append(mat(m))
    for p in me.polygons:
        p.use_smooth = False
    ob = bpy.data.objects.new(name, me)
    ob.location = loc
    return link(ob, parent)


def box(name, size, loc, m, parent=None, rot=(0, 0, 0), bevel=0.0):
    sx, sy, sz = (s / 2 for s in size)
    v = [(-sx, -sy, -sz), (sx, -sy, -sz), (sx, sy, -sz), (-sx, sy, -sz),
         (-sx, -sy, sz), (sx, -sy, sz), (sx, sy, sz), (-sx, sy, sz)]
    f = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    ob = mesh(name, v, f, m, parent, loc)
    ob.rotation_euler = rot
    if bevel > 0:
        md = ob.modifiers.new('bev', 'BEVEL'); md.width = bevel; md.segments = 1; md.limit_method = 'ANGLE'
    return ob


def cyl(name, r, h, loc, m, parent=None, segs=12, axis='Z', r2=None, cap=True):
    r2 = r if r2 is None else r2
    v, f = [], []
    for i in range(segs):
        a = 2 * math.pi * i / segs
        v.append((r * math.cos(a), r * math.sin(a), -h / 2)); v.append((r2 * math.cos(a), r2 * math.sin(a), h / 2))
    for i in range(segs):
        j = (i + 1) % segs
        f.append((2 * i, 2 * j, 2 * j + 1, 2 * i + 1))
    if cap:
        f.append(tuple(2 * i for i in reversed(range(segs)))); f.append(tuple(2 * i + 1 for i in range(segs)))
    ob = mesh(name, v, f, m, parent, loc)
    if axis == 'X': ob.rotation_euler = (0, math.pi / 2, 0)
    if axis == 'Y': ob.rotation_euler = (math.pi / 2, 0, 0)
    return ob


def lathe(name, profile, segs, m, parent=None, loc=(0, 0, 0), rot0=0.0):
    v, f = [], []
    n = len(profile)
    for s in range(segs):
        a = rot0 + 2 * math.pi * s / segs
        for r, z in profile:
            v.append((r * math.sin(a), -r * math.cos(a), z))
    for s in range(segs):
        s2 = (s + 1) % segs
        for i in range(n - 1):
            f.append((s * n + i, s2 * n + i, s2 * n + i + 1, s * n + i + 1))
    return mesh(name, v, f, m, parent, loc)


def prism(name, outline, z0, z1, m, parent=None, loc=(0, 0, 0)):
    """Extrude an XY outline from z0 to z1."""
    n = len(outline)
    v = [(x, y, z0) for x, y in outline] + [(x, y, z1) for x, y in outline]
    f = [tuple(reversed(range(n))), tuple(range(n, 2 * n))] + [(i, (i + 1) % n, n + (i + 1) % n, n + i) for i in range(n)]
    return mesh(name, v, f, m, parent, loc)


def sphere(name, r, loc, m, parent=None, segs=10, rings=6, scale=(1, 1, 1)):
    v, f = [], []
    for j in range(1, rings):
        th = math.pi * j / rings
        for i in range(segs):
            ph = 2 * math.pi * i / segs
            v.append((r * math.sin(th) * math.cos(ph) * scale[0], r * math.sin(th) * math.sin(ph) * scale[1], r * math.cos(th) * scale[2]))
    v += [(0, 0, r * scale[2]), (0, 0, -r * scale[2])]
    top, bot = len(v) - 2, len(v) - 1
    for j in range(rings - 2):
        for i in range(segs):
            a, b = j * segs + i, j * segs + (i + 1) % segs
            f.append((a, b, b + segs, a + segs))
    for i in range(segs):
        f.append((top, (i + 1) % segs, i)); f.append((bot, (rings - 2) * segs + i, (rings - 2) * segs + (i + 1) % segs))
    return mesh(name, v, f, m, parent, loc)


def smooth(ob, angle=40):
    """Smooth shading with crisp edges past angle (degrees)."""
    for poly in ob.data.polygons:
        poly.use_smooth = True
    ob.data.set_sharp_from_angle(angle=math.radians(angle))
    return ob


def torus(name, R, r, loc, m, parent=None, segs=24, tsegs=10, rot=(0, 0, 0), arc=2 * math.pi, a0=0.0):
    """A ring in the XZ plane (or part of one: arc), tube radius r."""
    closed = arc >= 2 * math.pi - 1e-6
    n = segs if closed else segs + 1
    v, f = [], []
    for i in range(n):
        a = a0 + arc * i / segs
        cx, cz = R * math.cos(a), R * math.sin(a)
        for j in range(tsegs):
            b = 2 * math.pi * j / tsegs
            d = R + r * math.cos(b)
            v.append((d * math.cos(a), r * math.sin(b), d * math.sin(a)))
    for i in range(segs):
        i2 = (i + 1) % n if closed else i + 1
        for j in range(tsegs):
            j2 = (j + 1) % tsegs
            f.append((i * tsegs + j, i2 * tsegs + j, i2 * tsegs + j2, i * tsegs + j2))
    ob = mesh(name, v, f, m, parent, loc)
    ob.rotation_euler = rot
    return ob


def tube(name, a, b, r, m, parent=None, segs=10):
    """A cylinder from point a to point b."""
    import mathutils
    a, b = mathutils.Vector(a), mathutils.Vector(b)
    d = b - a
    ob = cyl(name, r, d.length, tuple((a + b) / 2), m, parent, segs=segs)
    ob.rotation_euler = d.to_track_quat('Z', 'Y').to_euler()
    return ob


def export(name, scale=1.0):
    """scale brings the model to real size in metres (the root node carries it)."""
    roots = [o for o in bpy.context.scene.objects if o.parent is None]
    for r in roots:
        r.scale = (scale, scale, scale)
    path = os.path.join(OUT, name + '.glb')
    bpy.ops.export_scene.gltf(filepath=path, export_format='GLB', export_apply=True, export_yup=True,
                              export_materials='EXPORT', export_extras=True)
    print('wrote', path, os.path.getsize(path))


# ===================================================================== the rook (exact, docs/promo-v3)
PROFILE = [(0, -2.15), (1.08, -2.15), (1.12, -2.06), (1.12, -1.91), (.98, -1.79), (.96, -1.67), (.81, -1.61),
           (.78, -1.44), (.67, -1.35), (.53, -.93), (.46, .46), (.52, .76), (.70, .98), (.73, 1.09), (.90, 1.14),
           (.90, 1.31), (.86, 1.39), (.86, 1.58), (.59, 1.58), (.59, 1.22), (0, 1.22)]


def rook_body(root, m='gold', segs=16, merlon_m=None, door=True):
    prof = [(r, z + 2.15) for r, z in PROFILE]          # base on z = 0
    lathe('body', prof, segs, m, root)
    for i in range(6):
        c = i * math.pi / 3 + math.pi / 2; a, b = c - .34, c + .34
        outline = [(.88 * math.cos(a + (b - a) * k / 3), .88 * math.sin(a + (b - a) * k / 3)) for k in range(4)] + \
                  [(.58 * math.cos(b - (b - a) * k / 3), .58 * math.sin(b - (b - a) * k / 3)) for k in range(4)]
        prism('merlon%d' % i, outline, 1.54 + 2.15, 2.07 + 2.15, merlon_m or m, root)
    if not door:
        return
    # the raised door with a stone surround, a wooden leaf on a hinge, stairs up to it
    dw, dh, z0, sw = .34, .62, -1.30 + 2.15, .065
    k = (1 - math.cos(math.pi / 16)) / math.sin(math.pi / 16)

    def r_at(z):
        for (r0, y0), (r1, y1) in zip(prof, prof[1:]):
            if y0 <= z <= y1 and y1 > y0:
                return r0 + (r1 - r0) * (z - y0) / (y1 - y0)
        return prof[-2][0]

    def sy(x, z, d=0.0):
        return -(r_at(z) - abs(x) * k) - d

    def arch(hw, za, zb, n=10):
        pts = [(-hw, za), (-hw, zb - hw)]
        for i in range(1, n):
            t = math.pi - math.pi * i / n
            pts.append((hw * math.cos(t), zb - hw + hw * math.sin(t)))
        return pts + [(hw, zb - hw), (hw, za)]

    inner, outer = arch(dw / 2, z0, z0 + dh), arch(dw / 2 + sw, z0, z0 + dh + sw)
    n = len(outer)
    # the front of the surround is flat and stands proud of the door leaf all the way up (the body
    # narrows as it rises, so a surround that followed the surface fell behind the top of the leaf)
    yf = lambda x: sy(x, z0, .065)
    v = [(x, yf(x), z) for x, z in outer] + [(x, yf(x), z) for x, z in inner]
    v += [(x, sy(x, z, -.02), z) for x, z in outer] + [(x, sy(x, z, -.02), z) for x, z in inner]
    f = [(i, i + 1, n + i + 1, n + i) for i in range(n - 1)] + [(2 * n + i + 1, 2 * n + i, i, i + 1) for i in range(n - 1)] + \
        [(n + i, n + i + 1, 3 * n + i + 1, 3 * n + i) for i in range(n - 1)]
    mesh('surround', v, f, 'stone', root)
    hole = mesh('doorway', [(x * .985, sy(x, z, .022), z) for x, z in inner], [tuple(range(len(inner)))], 'light', root)
    yh = sy(-dw / 2, z0, .012)
    hinge = empty('door', (-dw / 2, yh, z0), root)
    lf = [(x + dw / 2, z - z0) for x, z in arch(dw / 2 - .006, z0, z0 + dh - .004)]
    m_ = len(lf)
    lv = [(x, 0, z) for x, z in lf] + [(x, -.035, z) for x, z in lf]
    lfs = [tuple(range(m_)), tuple(range(2 * m_ - 1, m_ - 1, -1))] + [(i, (i + 1) % m_, m_ + (i + 1) % m_, m_ + i) for i in range(m_)]
    mesh('leaf', lv, lfs, 'wood', hinge)
    for zz in (.11, .40):
        box('strap%d' % int(zz * 100), (dw - .03, .01, .026), (dw / 2, -.041, zz), 'iron', hinge)
    nst, run, stw = 6, .145, .42
    wall = sy(0, z0)
    rise = z0 / nst
    for i in range(nst):
        top = rise * (i + 1)
        ya, yb = wall - run * (nst - i), wall + .25
        box('step%d' % i, (stw, yb - ya, top), (0, (ya + yb) / 2, top / 2), 'stone', root)


def build_rook():
    reset()
    root = empty('rook')
    rook_body(root)
    export('rook')


def build_agent():
    """A smaller, simpler rook for agents: 10 sides, no door; tinted per agent in three.js."""
    reset()
    root = empty('agent')
    prof = [(0, 0), (1.0, 0), (1.0, .14), (.82, .3), (.62, .5), (.5, 1.4), (.6, 1.62), (.78, 1.72), (.78, 1.98), (.52, 1.98), (.52, 1.86), (0, 1.86)]
    lathe('body', prof, 10, 'accent', root)
    for i in range(5):
        c = i * 2 * math.pi / 5; a, b = c - .32, c + .32
        outline = [(.78 * math.cos(a + (b - a) * k / 3), .78 * math.sin(a + (b - a) * k / 3)) for k in range(4)] + \
                  [(.52 * math.cos(b - (b - a) * k / 3), .52 * math.sin(b - (b - a) * k / 3)) for k in range(4)]
        prism('merlon%d' % i, outline, 1.98, 2.3, 'accent', root)
    export('agent')


def build_pawn():
    """A pawn for worker agents: same base as the agent rook, about 1.9 units tall; tinted in three.js."""
    reset()
    root = empty('pawn')
    prof = [(0, 0), (.9, 0), (.92, .12), (.75, .26), (.56, .42), (.42, .9), (.36, 1.2), (.52, 1.28), (.54, 1.34), (.3, 1.4),
            (.38, 1.5), (.44, 1.62), (.44, 1.72), (.38, 1.82), (.24, 1.88), (0, 1.9)]
    smooth(lathe('body', prof, 24, 'accent', root), 50)
    export('pawn')


# ===================================================================== devices
def build_phone():
    reset()
    root = empty('phone')
    box('body', (.36, .74, .045), (0, 0, .0225), 'dark', root, bevel=.02)
    box('screen', (.32, .68, .004), (0, 0, .047), 'screen', root)
    box('cam', (.08, .08, .01), (-.09, .26, -.002), 'metal', root)
    export('phone', 0.21)


def build_laptop():
    reset()
    root = empty('laptop')
    box('base', (.86, .6, .04), (0, 0, .02), 'body', root)
    box('keys', (.72, .3, .006), (0, .06, .043), 'dark', root)
    box('pad', (.24, .14, .004), (0, -.19, .042), 'dark', root)
    lid = empty('lid', (0, .3, .04), root)
    lid.rotation_euler.x = math.radians(-12)        # tilt back (open ~102 degrees)
    box('lidb', (.86, .03, .56), (0, .015, .28), 'body', lid)
    box('screen', (.8, .004, .5), (0, -.002, .29), 'screen', lid)
    export('laptop', 0.4)


def build_people():
    """The person, turned like the chess pieces (lathe limbs with beads at the joints), facing -Y, 1.75 m.
    person_m wears the king's cross; person_f the queen's crown and a bust. Same rig as 'figure':
    hips > legL/legR, torso > head, armL/armR > fore."""
    for name, female in (('person_m', False), ('person_f', True)):
        reset()
        root = empty(name)
        hips = empty('hips', (0, 0, .92), root)
        for s, leg_name in ((-1, 'legL'), (1, 'legR')):
            leg = empty(leg_name, (s * .095, 0, 0), hips)
            lathe('thigh', [(0, 0), (.085, 0), (.088, -.04), (.072, -.2), (.062, -.38), (.074, -.43), (.072, -.47), (0, -.47)], 20, 'cloth2', leg)
            shin = empty('shin', (0, 0, -.45), leg)          # the knee
            lathe('calf', [(0, 0), (.064, 0), (.058, -.06), (.05, -.31), (.056, -.37), (0, -.37)], 20, 'cloth2', shin)
            box('shoe', (.1, .22, .07), (0, -.05, -.435), 'dark', shin, bevel=.02)
        torso = empty('torso', (0, 0, 0), hips)
        if female:   # finer rings through the chest so the bust can be sculpted into the shirt
            prof = [(0, -.04), (.2, -.04), (.21, .04), (.15, .24)] + \
                   [(.15 + (.17 - .15) * (z - .24) / .18 if z <= .42 else .17 + (.19 - .17) * (z - .42) / .1, z) for z in [.27 + .0125 * k for k in range(20)]] + \
                   [(.19, .52), (.17, .56), (.11, .6), (0, .6)]
        else:
            prof = [(0, -.02), (.16, -.02), (.18, .06), (.16, .25), (.19, .42), (.22, .52), (.2, .56), (.12, .6), (0, .6)]
        shirt = 'cloth4' if female else 'cloth'
        chest = lathe('chest', prof, 64 if female else 28, shirt, torso)
        if female:   # a soft bust pushed out of the front of the shirt: two lobes, no seams
            for v in chest.data.vertices:
                x, y, z = v.co
                if y >= 0:
                    continue
                r = math.hypot(x, y) or 1
                # each breast: rounder across, a long gentle slope above and a fuller, quicker curve below
                lobe = min(1, sum(math.exp(-((x - s * .078) ** 2) / (2 * .042 ** 2)) for s in (-1, 1)))   # summed: no crease between
                zc, su, sd = .395, .075, .032
                vert = math.exp(-((z - zc) ** 2) / (2 * (su if z > zc else sd) ** 2))
                d = .085 * lobe * vert * max(0, -y / r) ** 1.5
                v.co.y -= d
            smooth(chest, 89)
        else:
            smooth(chest, 40)
        cyl('collar', .13, .04, (0, 0, .64), 'paper', torso, segs=24)
        head = empty('head', (0, 0, .7), torso)
        cyl('neck', .05, .08, (0, 0, -.01), 'skin', head, segs=16)
        lathe('skull', [(0, .03), (.09, .03), (.13, .1), (.135, .2), (.145, .26), (0, .26)], 24, 'skin', head)
        if female:   # the queen's crown, kept simple: five balls on the rim and one on top
            for i in range(5):
                a = i * 2 * math.pi / 5 + math.pi / 2
                sphere('pt%d' % i, .03, (.12 * math.cos(a), .12 * math.sin(a), .29), 'paper', head, segs=14, rings=9)
            sphere('top', .038, (0, 0, .3), 'paper', head, segs=14, rings=9)
        else:        # the king's cross
            box('crossv', (.035, .035, .14), (0, 0, .33), 'paper', head)
            box('crossh', (.11, .035, .035), (0, 0, .35), 'paper', head)
        for s, arm_name in ((-1, 'armL'), (1, 'armR')):
            arm = empty(arm_name, (s * .26, 0, .52), torso)
            lathe('upper', [(0, .04), (.05, .04), (.065, 0), (.05, -.05), (.044, -.15), (.05, -.25), (.058, -.29), (0, -.3)], 16, shirt, arm)
            fore = empty('fore', (0, 0, -.3), arm)
            lathe('lower', [(0, 0), (.046, 0), (.04, -.08), (.044, -.2), (.034, -.26), (0, -.26)], 16, 'skin', fore)
            sphere('hand', .05, (0, 0, -.31), 'skin', fore, segs=14, rings=9)
        for ob in bpy.data.objects:          # turned wood reads smooth; the ink still draws the profile
            if ob.type == 'MESH' and ob.name.split('.')[0] not in ('shoe', 'crossv', 'crossh', 'collar', 'chest'):
                for poly in ob.data.polygons:
                    poly.use_smooth = True
                ob.data.set_sharp_from_angle(angle=math.radians(40))   # keep rims and caps crisp
        export(name)


def build_workstation():
    """A real-size desk (1.5 x 0.7 m, 0.74 m high) with a 27" monitor, keyboard, tower and lamp."""
    reset()
    root = empty('workstation')
    box('desk', (1.5, .7, .04), (0, 0, .72), 'wood', root)
    for x in (-.7, .7):
        for y in (-.3, .3):
            box('leg', (.04, .04, .7), (x, y, .35), 'dark', root)
    box('foot', (.24, .16, .015), (0, .16, .7475), 'dark', root)
    box('neck', (.04, .03, .2), (0, .2, .85), 'dark', root)
    mon = empty('monitor', (0, .18, 1.06), root)
    box('frame', (.62, .03, .38), (0, 0, 0), 'dark', mon)
    box('screen', (.58, .004, .34), (0, -.017, 0), 'screen', mon)
    box('kb', (.44, .14, .02), (0, -.14, .75), 'body', root)
    box('tower', (.2, .42, .44), (.6, .08, .96), 'body', root)
    box('tower_led', (.02, .004, .02), (.55, -.131, 1.1), 'light', root)
    cyl('lampbase', .07, .02, (-.6, .2, .75), 'dark', root)
    lamp = empty('lamp', (-.6, .2, .76), root)
    cyl('arm1', .01, .38, (0, 0, .19), 'metal', lamp)
    cyl('shade', .07, .1, (0, -.08, .38), 'metal', lamp, r2=.025)
    sphere('bulb', .035, (0, -.08, .34), 'light', lamp)
    export('workstation')


def build_gpurig():
    reset()
    root = empty('gpurig')
    box('case', (.6, .9, 1.0), (0, 0, .5), 'body', root)
    box('window', (.004, .7, .7), (-.302, 0, .55), 'glass', root)
    for i in range(3):
        cyl('fan%d' % i, .13, .02, (0, -.452, .25 + i * .26), 'dark', root, segs=12, axis='Y')
        cyl('hub%d' % i, .04, .03, (0, -.462, .25 + i * .26), 'metal', root, segs=8, axis='Y')
    for i in range(2):
        box('gpu%d' % i, (.44, .6, .08), (-.06, .02, .45 + i * .2), 'dark', root)
        box('gpuglow%d' % i, (.006, .5, .02), (-.285, .02, .45 + i * .2), 'light', root)
    box('led', (.03, .005, .03), (.2, -.452, .9), 'light', root)
    export('gpurig', 0.5)


def build_printer():
    reset()
    root = empty('printer')
    t, W, D, H = .05, 1.0, 1.0, 1.1
    box('top', (W, D, t), (0, 0, H - t / 2), 'body', root)
    box('bottom', (W, D, .2), (0, 0, .1), 'body', root)
    for x in (-1, 1):
        box('side%d' % x, (t, D, H), (x * (W - t) / 2, 0, H / 2), 'body', root)
    box('back', (W, t, H), (0, (D - t) / 2, H / 2), 'body', root)
    box('door_glass', (W - 2 * t, .01, H - .25), (0, -D / 2, H / 2 + .02), 'glass', root)
    box('screen', (.22, .01, .12), (.3, -D / 2 - .01, .12), 'screen', root)
    bed = empty('bed', (0, 0, .4), root)          # moves down as layers print
    box('plate', (.7, .7, .02), (0, 0, 0), 'metal', bed)
    head = empty('head', (0, 0, .92), root)
    box('carriage', (.14, .14, .12), (0, 0, 0), 'dark', head)
    cyl('nozzle', .02, .06, (0, 0, -.09), 'metal', head, r2=.035, segs=8)
    box('gantry', (W - 2 * t, .05, .05), (0, 0, .98), 'metal', root)
    box('ams', (.9, .4, .2), (0, 0, H + .1), 'body', root)
    for i in range(4):
        cyl('spool%d' % i, .07, .12, (-.32 + i * .21, -.05, H + .12), 'accent', root, axis='X', segs=12)
    export('printer', 0.45)


def build_car():
    reset()
    root = empty('car')
    body = [(-1.1, .15), (1.15, .15), (1.2, .45), (.6, .52), (.25, .85), (-.55, .85), (-.95, .52), (-1.15, .5)]
    v = [(x, -.45, z) for x, z in body] + [(x, .45, z) for x, z in body]
    n = len(body)
    f = [tuple(reversed(range(n))), tuple(range(n, 2 * n))] + [(i, (i + 1) % n, n + (i + 1) % n, n + i) for i in range(n)]
    mesh('body', v, f, 'body', root)
    win = [(-.5, .55), (.2, .55), (.45, .55), (.22, .8), (-.48, .8), (-.85, .55)]
    for s in (-1, 1):
        mesh('win%d' % s, [(x, s * .455, z) for x, z in [(.2, .56), (.5, .54), (.24, .8), (-.45, .8)]] , [(0, 1, 2, 3)], 'glass', root)
    for x in (-.75, .75):
        for y in (-.42, .42):
            cyl('wheel', .17, .12, (x, y, .17), 'dark', root, axis='Y', segs=10)
    for y in (-.3, .3):
        box('lamp', (.02, .14, .06), (1.19, y, .38), 'light', root)
    export('car', 1.85)


def build_tablet():
    reset()
    root = empty('tablet')
    stand = empty('tilt', (0, 0, .05), root)
    stand.rotation_euler.x = math.radians(65)
    box('body', (.8, .03, .56), (0, 0, .28), 'dark', stand, bevel=.015)
    box('screen', (.74, .004, .5), (0, -.017, .28), 'screen', stand)
    box('stand', (.6, .3, .04), (0, .15, .02), 'body', root)
    export('tablet', 0.31)


def build_nas():
    reset()
    root = empty('nas')
    box('case', (.55, .6, .65), (0, 0, .325), 'body', root)
    for i in range(4):
        box('bay%d' % i, (.42, .02, .1), (-.03, -.305, .12 + i * .13), 'metal', root)
        box('led%d' % i, (.03, .01, .03), (.22, -.31, .12 + i * .13), 'light', root)
    export('nas', 0.38)


def build_icons():
    """Calendar and mail pieces: connectors as chess-sized blocks."""
    reset()
    root = empty('calendar')
    box('block', (.6, .2, .6), (0, 0, .3), 'body', root)
    box('page', (.5, .01, .38), (0, -.105, .27), 'paper', root)
    box('header', (.5, .012, .1), (0, -.106, .52), 'accent', root)
    for r in range(3):
        for c in range(4):
            box('cell', (.08, .012, .06), (-.18 + c * .12, -.112, .38 - r * .1), 'dark', root)
    for x in (-.15, .15):
        cyl('ring', .02, .1, (x, 0, .62), 'metal', root, segs=8)
    export('calendar')
    reset()
    root = empty('mail')
    box('block', (.66, .2, .44), (0, 0, .22), 'body', root)
    mesh('flap', [(-.33, -.102, .44), (.33, -.102, .44), (0, -.102, .2)], [(0, 1, 2)], 'paper', root)
    export('mail')


# ===================================================================== the folio (EDC notebook folio)
def build_folio():
    """A3-ish? No: a pocket folio 0.9 x 1.3 units per cover. Spine along Y at x = 0.
    Covers hinge on interleaved loops; the pen is the hinge pin; notepad sits in the back cover."""
    reset()
    root = empty('folio')
    W, L, T = .9, 1.3, .05
    back = empty('back', (0, 0, 0), root)
    box('cover', (W, L, T), (W / 2 + .05, 0, T / 2), 'body', back)
    pad = empty('notepad', (W / 2 + .05, 0, T), root)
    box('pad', (W - .16, L - .2, .08), (0, 0, .04), 'paper', pad)
    for i in range(6):
        box('line%d' % i, (W - .3, .006, .002), (0, -.4 + i * .16, .081), 'dark', pad)
    lips = empty('lips', (0, 0, 0), root)
    for sx in (0, 1):
        for sy in (-1, 1):
            x = .05 + (W - .03 if sx else .03) ; y = sy * (L / 2 - .03)
            box('lipA', (.18, .03, .09), (x + (-.09 if sx else .09), y, T + .045), 'accent', lips)   # on the backing,
            box('lipB', (.03, .18, .09), (x, y - sy * .09, T + .045), 'accent', lips)                # as tall as the pad
    front = empty('front', (0, 0, T / 2), root)       # rotates about the spine (Y axis)
    box('cover', (W, L, T), (-W / 2 - .05, 0, 0), 'body', front)
    n = 7
    for i in range(n):
        y = -L / 2 + (i + .5) * L / n
        par = back if i % 2 == 0 else front
        cyl('loop%d' % i, .07, L / n * .92, (0, y, T / 2 if par is back else 0), 'dark', par, segs=10, axis='Y')
    pen = empty('pen', (0, 0, T / 2), root)
    cyl('barrel', .035, L * 1.08, (0, 0, 0), 'accent', pen, segs=10, axis='Y')
    cyl('tip', .035, .1, (0, -L * .54 - .05, 0), 'metal', pen, segs=10, axis='Y', r2=.005)
    cyl('cap', .04, .06, (0, L * .54 + .03, 0), 'dark', pen, segs=10, axis='Y')
    box('clip', (.012, .3, .02), (0, L * .4, .045), 'metal', pen)
    export('folio', 0.12)


# ===================================================================== figures and props
def build_figure():
    """A stylized faceless figure, about 1.75 units tall, standing on z = 0, facing -Y."""
    reset()
    root = empty('figure')
    hips = empty('hips', (0, 0, .95), root)
    for s, name in ((-1, 'legL'), (1, 'legR')):
        leg = empty(name, (s * .1, 0, 0), hips)
        cyl('thigh', .075, .48, (0, 0, -.24), 'cloth2', leg, segs=8, r2=.085)
        cyl('shin', .06, .46, (0, 0, -.68), 'cloth2', leg, segs=8, r2=.072)
        box('shoe', (.11, .24, .07), (0, -.05, -.92), 'dark', leg)
    torso = empty('torso', (0, 0, 0), hips)
    cyl('chest', .19, .58, (0, 0, .29), 'cloth', torso, segs=8, r2=.22)
    head = empty('head', (0, 0, .7), torso)
    cyl('neck', .05, .1, (0, 0, -.02), 'skin', head, segs=8)
    sphere('skull', .12, (0, 0, .13), 'skin', head, segs=10, rings=7, scale=(1, 1.05, 1.15))
    for s, name in ((-1, 'armL'), (1, 'armR')):
        arm = empty(name, (s * .26, 0, .52), torso)
        cyl('upper', .055, .3, (0, 0, -.15), 'cloth', arm, segs=8)
        fore = empty('fore', (0, 0, -.3), arm)
        cyl('lower', .045, .28, (0, 0, -.14), 'skin', fore, segs=8)
        sphere('hand', .05, (0, 0, -.31), 'skin', fore, segs=8, rings=5)
    export('figure')


def build_props():
    reset()
    root = empty('nightstand')
    box('top', (.9, .7, .05), (0, 0, .62), 'wood', root)
    box('body', (.84, .64, .55), (0, 0, .33), 'wood', root)
    box('drawer', (.7, .02, .18), (0, -.33, .45), 'wood2', root)
    box('knob', (.06, .03, .03), (0, -.35, .45), 'metal', root)
    cyl('lampbase', .1, .04, (.28, .15, .66), 'dark', root)
    cyl('lampstem', .015, .3, (.28, .15, .83), 'metal', root)
    cyl('lampshade', .16, .2, (.28, .15, 1.02), 'paper', root, r2=.1, cap=False)
    export('nightstand')
    reset()
    root = empty('counter')
    # a bathroom vanity, 1.2 m: cabinet with two doors, stone top, a porcelain basin set in, a chrome
    # gooseneck tap, tiled splashback, a mirror on the wall, soap pump and a cup with toothbrushes
    box('cab', (1.2, .55, .82), (0, 0, .41), 'wood', root, bevel=.01)
    for sx in (-1, 1):
        box('door', (.56, .01, .66), (sx * .29, -.28, .44), 'wood2', root, bevel=.005)
        box('pull', (.015, .02, .12), (sx * .04, -.295, .62), 'chrome', root)
    box('top', (1.26, .6, .04), (0, 0, .84), 'stone', root, bevel=.008)
    bx, by = -.25, -.02                                   # basin centre
    smooth(lathe('basin', [(0, .66), (.12, .67), (.17, .72), (.2, .8), (.215, .862), (.24, .866), (.24, .874), (.2, .874),
                           (.19, .82), (.16, .76), (.11, .72), (0, .71)], 32, 'porcelain', root, loc=(bx, by, 0)), 50)
    cyl('drain', .022, .004, (bx, by, .712), 'chrome', root, segs=16)
    tx, ty = bx, by + .25                                 # the tap behind the basin
    smooth(cyl('tapbase', .028, .02, (tx, ty, .87), 'chrome', root, segs=20))
    smooth(tube('tapcol', (tx, ty, .87), (tx, ty, 1.08), .016, 'chrome', root, segs=16))
    smooth(torus('tapneck', .08, .016, (tx, ty - .08, 1.08), 'chrome', root, segs=14, tsegs=10, rot=(0, 0, math.pi / 2), arc=math.pi))
    smooth(tube('spout', (tx, ty - .16, 1.08), (tx, ty - .16, 1.04), .016, 'chrome', root, segs=16))
    smooth(tube('lever', (tx, ty, 1.03), (tx + .07, ty + .02, 1.07), .009, 'chrome', root, segs=10))
    # splashback tiles and the mirror
    box('splash', (1.26, .02, .3), (0, .29, 1.01), 'tile', root)
    for k in range(1, 6):
        box('grout', (.004, .021, .3), (-.63 + k * .21, .29, 1.01), 'dark', root)
    box('grout', (1.26, .021, .004), (0, .29, 1.01), 'dark', root)
    box('mirror_frame', (.9, .03, .8), (0, .3, 1.6), 'wood2', root, bevel=.005)
    box('mirror', (.84, .02, .74), (0, .285, 1.6), 'mirror', root)
    box('light', (.6, .06, .06), (0, .25, 2.05), 'light', root)
    # soap pump and a cup with two toothbrushes, to the right of the basin
    smooth(lathe('cup', [(0, .86), (.032, .86), (.04, .97), (.036, .97), (.028, .87), (0, .87)], 20, 'tile', root, loc=(.2, .14, 0)))
    for k, (dx, col) in enumerate(((-.012, 'accent'), (.012, 'agentblue'))):
        base, tip = (.2 + dx, .14, .88), (.2 + dx * 2.5, .14 + .008, 1.05)          # leaning out of the cup
        smooth(tube('brush%d' % k, base, (tip[0], tip[1], tip[2] - .03), .0055, col, root, segs=10))        # handle
        smooth(tube('neck%d' % k, (tip[0], tip[1], tip[2] - .03), tip, .0035, col, root, segs=8))            # slim neck
        box('head%d' % k, (.012, .006, .028), (tip[0], tip[1] - .002, tip[2] + .012), col, root, bevel=.002)  # head
        box('bristle%d' % k, (.011, .012, .022), (tip[0], tip[1] - .01, tip[2] + .012), 'paper', root)       # bristles, facing out
    export('counter')
    reset()
    root = empty('dash')
    # a car interior seen from the driver's seat (left-hand drive, forward is +Y): dash with a
    # cluster, gauges, a centre screen and vents, a steering wheel on its column, A-pillars, the
    # roof header, the rear-view mirror, the bonnet ahead and a phone mat on the dash top
    box('dash', (1.7, .55, .3), (0, .1, .85), 'dark', root, bevel=.02)
    box('dashtop', (1.7, .4, .05), (0, .22, 1.02), 'dark2', root, bevel=.01)
    box('cluster', (.46, .16, .1), (-.42, -.1, 1.05), 'dark2', root, bevel=.02)
    for gx in (-.53, -.31):
        smooth(cyl('gauge', .07, .01, (gx, -.18, .99), 'gauge', root, segs=24, axis='Y'))
        box('needle', (.006, .004, .06), (gx + .01, -.19, 1.0), 'gaugered', root, rot=(0, -.6, 0))
    box('screen', (.26, .02, .16), (.02, -.17, .93), 'screen', root, rot=(-.35, 0, 0))
    for vx in (-.75, -.12, .16, .75):
        box('vent', (.14, .02, .05), (vx, -.18, .97 if abs(vx) < .5 else .95), 'iron', root)
    box('console', (.3, .5, .35), (.02, -.35, .62), 'dark', root, bevel=.02)
    box('phonemat', (.2, .14, .006), (.02, .14, 1.048), 'iron', root)
    wheel = empty('wheel', (-.42, -.38, .98), root)
    wheel.rotation_euler.x = math.radians(68)
    smooth(torus('rim', .18, .022, (0, 0, 0), 'dark2', wheel, segs=36, tsegs=10, rot=(math.pi / 2, 0, 0)))
    smooth(cyl('hub', .06, .05, (0, 0, 0), 'dark', wheel, segs=20))
    for k in range(3):
        a = math.radians(-90 + k * 120)
        smooth(tube('spoke%d' % k, (.05 * math.cos(a), .05 * math.sin(a), 0), (.17 * math.cos(a), .17 * math.sin(a), 0), .016, 'dark', wheel, segs=10))
    smooth(tube('column', (-.42, -.38, .98), (-.42, -.05, .9), .035, 'dark2', root, segs=14))
    for sx in (-1, 1):
        tube('pillar', (sx * .85, .38, 1.02), (sx * .74, -.4, 1.55), .045, 'dark2', root, segs=8)
    box('header', (1.6, .14, .06), (0, -.42, 1.56), 'dark2', root)
    box('mirror', (.24, .04, .07), (.02, -.3, 1.44), 'iron', root, bevel=.01)
    tube('mirrorstem', (.02, -.32, 1.48), (.02, -.36, 1.55), .01, 'iron', root)
    box('glass', (1.6, .02, .9), (0, 0, 1.29), 'glass', root, rot=(math.radians(-52), 0, 0))
    box('hood', (1.75, 1.7, .05), (0, 1.25, .86), 'cloth2', root, rot=(math.radians(-4), 0, 0))
    box('doorL', (.06, 1.2, .55), (-.88, -.3, .8), 'dark', root)
    export('dash')
    reset()
    root = empty('table')
    cyl('top', 1.3, .08, (0, 0, .75), 'wood', root, segs=24)
    cyl('stem', .12, .7, (0, 0, .36), 'dark', root, segs=10)
    cyl('foot', .5, .04, (0, 0, .02), 'dark', root, segs=16)
    export('table')
    reset()
    root = empty('chair')
    box('seat', (.45, .45, .05), (0, 0, .45), 'wood', root)
    box('back', (.45, .05, .5), (0, .2, .72), 'wood', root)
    for x in (-.19, .19):
        for y in (-.19, .19):
            box('leg', (.04, .04, .45), (x, y, .225), 'dark', root)
    export('chair')
    reset()
    root = empty('bed')
    box('frame', (1.6, 2.1, .3), (0, 0, .2), 'wood', root)
    box('mattress', (1.5, 2.0, .2), (0, 0, .45), 'cloth3', root)
    box('blanket', (1.52, 1.2, .06), (0, -.35, .58), 'cloth', root)
    box('pillow', (.6, .35, .12), (0, .75, .6), 'paper', root)
    box('head', (1.6, .08, .9), (0, 1.05, .55), 'wood', root)
    export('bed')
    reset()
    root = empty('coffee')
    # a mug, real size (9 cm tall), with a handle, on a coaster
    smooth(lathe('mug', [(0, .006), (.038, .006), (.041, .012), (.042, .09), (.044, .096), (.039, .096), (.037, .02), (0, .02)],
                 36, 'accent', root), 50)
    cyl('coffee', .037, .002, (0, 0, .082), 'dark', root, segs=36)
    smooth(torus('handle', .025, .007, (.04, 0, .052), 'accent', root, segs=16, tsegs=8, arc=math.pi, a0=-math.pi / 2), 50)
    smooth(cyl('coaster', .052, .006, (0, 0, .003), 'wood2', root, segs=32))
    export('coffee')


if __name__ == '__main__':
    for fn in (build_rook, build_agent, build_pawn, build_phone, build_laptop, build_workstation, build_gpurig, build_printer, build_car,
               build_tablet, build_nas, build_icons, build_folio, build_figure, build_people, build_props):
        fn()
