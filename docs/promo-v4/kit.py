"""Rook remaster asset kit: gel pen on black grid paper.

Visual grammar
  white ink  : the world (devices, people, scenery, frames)
  gold       : Rook acting (the rook, agents, pulses, beams)
  red        : threat or failure
  blue       : you (your asks, your hands, your phone's voice)

Everything is drawn by Freestyle over emission-only Cycles shading, so a frame
renders fast on CPU and looks like ink on paper.
"""
import math, os
import bpy, bmesh
from mathutils import Vector, Matrix

HERE = os.path.dirname(os.path.abspath(__file__))
FONTS = os.path.join(HERE, 'fonts')

PAPER = (0.055, 0.067, 0.055)      # near-black, the site's #0e110e
GRID = (0.16, 0.20, 0.16)          # dim grey-green grid lines
INK = (0.93, 0.94, 0.89)           # white gel pen
DIM = (0.52, 0.56, 0.50)           # faded ink
GOLD = (0.95, 0.74, 0.38)          # gold leaf
GOLD_HI = (1.0, 0.86, 0.55)
RED = (0.93, 0.33, 0.27)
BLUE = (0.28, 0.66, 1.0)

INKS = {'ink': INK, 'dim': DIM, 'gold': GOLD, 'red': RED, 'blue': BLUE}


def lin(c):
    """sRGB -> linear for shader colours."""
    return tuple(x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c)


# ------------------------------------------------------------------ scene
def reset():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    for name in INKS:
        col = bpy.data.collections.new(name)
        sc.collection.children.link(col)
    for name in ('paper', 'text'):
        col = bpy.data.collections.new(name)
        sc.collection.children.link(col)
    return sc


def setup_render(sc, w=1920, h=1080, line=2.6, samples=6):
    sc.render.engine = 'CYCLES'
    sc.cycles.device = 'CPU'
    sc.cycles.samples = samples
    sc.cycles.use_denoising = False
    sc.cycles.max_bounces = 0
    sc.cycles.use_adaptive_sampling = False
    sc.render.resolution_x, sc.render.resolution_y = w, h
    sc.render.resolution_percentage = 100
    sc.render.fps = 30
    sc.render.film_transparent = False
    sc.render.image_settings.file_format = 'PNG'
    sc.render.image_settings.color_mode = 'RGB'
    sc.view_settings.view_transform = 'Standard'
    sc.view_settings.look = 'None'
    # world = paper
    world = bpy.data.worlds.new('paper')
    sc.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes['Background']
    bg.inputs[0].default_value = (*lin(PAPER), 1)
    bg.inputs[1].default_value = 1.0
    # freestyle
    sc.render.use_freestyle = True
    sc.render.line_thickness_mode = 'ABSOLUTE'
    sc.render.line_thickness = 1.0
    vl = sc.view_layers[0]
    vl.use_freestyle = True
    fs = vl.freestyle_settings
    fs.crease_angle = math.radians(150)
    fs.use_smoothness = True
    for ls in list(fs.linesets):
        fs.linesets.remove(ls)
    for name, col in INKS.items():
        add_lineset(fs, name, col, line * (1.15 if name == 'gold' else 1.0))
    return fs


def add_lineset(fs, name, color, thickness):
    ls = fs.linesets.new(name)
    ls.select_by_collection = True
    ls.collection = bpy.data.collections[name]
    ls.select_by_visibility = True
    ls.visibility = 'VISIBLE'
    ls.select_by_edge_types = True
    ls.select_silhouette = True
    ls.select_border = True
    ls.select_crease = True
    ls.select_contour = True
    ls.select_external_contour = True
    ls.select_material_boundary = False
    st = ls.linestyle
    st.name = 'pen_' + name
    st.color = lin(color)
    st.thickness = thickness
    st.thickness_position = 'CENTER'
    st.caps = 'ROUND'
    st.chaining = 'PLAIN'
    st.use_chain_count = False
    # a fine nib: a hair of hand tremor, thicker on downstrokes, tapered ends
    pn = st.geometry_modifiers.new('wobble', 'PERLIN_NOISE_1D')
    pn.frequency = 14.0
    pn.amplitude = 0.35
    pn.octaves = 1
    pn.seed = 1
    cal = st.thickness_modifiers.new('nib', 'CALLIGRAPHY')
    cal.orientation = 45.0
    cal.thickness_min = thickness * 0.45
    cal.thickness_max = thickness * 1.1
    cal.blend = 'MINIMUM'
    tm = st.thickness_modifiers.new('taper', 'ALONG_STROKE')
    tm.mapping = 'CURVE'
    tm.blend = 'MULTIPLY'
    tm.value_min, tm.value_max = 0.35, 1.0
    cm = tm.curve.curves[0]
    cm.points[0].location = (0, 0.3)
    cm.points[1].location = (1, 0.4)
    cm.points.new(0.12, 1.0)
    cm.points.new(0.9, 1.0)
    tm.curve.update()
    ls.select_edge_mark = True
    return ls


def boil(sc, frame, step=2):
    """Redraw the lines on twos: a new wobble seed every `step` frames."""
    seed = 1 + (frame // step) % 97
    for ls in sc.view_layers[0].freestyle_settings.linesets:
        m = ls.linestyle.geometry_modifiers.get('wobble')
        if m:
            m.seed = seed


def compositor_glow(sc, threshold=1.05, size=0.6, strength=1.0):
    """A bloom on anything brighter than paper-white (beams, pulses, door light).
    Freestyle ink stays <= 1.0 so it doesn't bloom."""
    ng = bpy.data.node_groups.new('comp', 'CompositorNodeTree')
    ng.interface.new_socket('Image', in_out='OUTPUT', socket_type='NodeSocketColor')
    rl = ng.nodes.new('CompositorNodeRLayers')
    gl = ng.nodes.new('CompositorNodeGlare')
    gl.inputs['Type'].default_value = 'Bloom'
    gl.inputs['Quality'].default_value = 'Medium'
    gl.inputs['Threshold'].default_value = threshold
    gl.inputs['Size'].default_value = size
    gl.inputs['Strength'].default_value = strength
    out = ng.nodes.new('NodeGroupOutput')
    ng.links.new(rl.outputs['Image'], gl.inputs['Image'])
    ng.links.new(gl.outputs[0], out.inputs[0])
    sc.compositing_node_group = ng
    return gl


# ------------------------------------------------------------------ materials
_mats = {}


def mat(name, color, strength=1.0):
    key = (name, color, strength)
    if key in _mats:
        return _mats[key]
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    out = nt.nodes.new('ShaderNodeOutputMaterial')
    em = nt.nodes.new('ShaderNodeEmission')
    em.inputs[0].default_value = (*lin(color), 1)
    em.inputs[1].default_value = strength
    nt.links.new(em.outputs[0], out.inputs[0])
    _mats[key] = m
    return m


def paper_fill():
    """Faces filled with paper colour: ink drawn over things hides what's behind."""
    return mat('paper_fill', PAPER)


def tint_fill(color, k=0.10):
    c = tuple(PAPER[i] * (1 - k) + color[i] * k for i in range(3))
    return mat('tint_%s_%.2f' % ('_'.join('%.2f' % x for x in color), k), c)


def set_fill(obj, m):
    obj.data.materials.clear()
    obj.data.materials.append(m)


def link(obj, coll):
    for c in list(obj.users_collection):
        c.objects.unlink(obj)
    bpy.data.collections[coll].objects.link(obj)
    return obj


def facet_shade(color, name='facets', light=(-0.55, -0.75, 0.62), lo=0.015, hi=0.10):
    """Low-poly look: each flat facet gets one tone of `color` from a fixed key light."""
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    N, L = nt.nodes.new, nt.links.new
    out = N('ShaderNodeOutputMaterial'); em = N('ShaderNodeEmission')
    geo = N('ShaderNodeNewGeometry')
    dot = N('ShaderNodeVectorMath'); dot.operation = 'DOT_PRODUCT'
    dot.inputs[1].default_value = tuple(Vector(light).normalized())
    L(geo.outputs['Normal'], dot.inputs[0])
    mr = N('ShaderNodeMapRange')
    mr.inputs['From Min'].default_value = -0.4; mr.inputs['From Max'].default_value = 1.0
    mr.inputs['To Min'].default_value = lo; mr.inputs['To Max'].default_value = hi
    L(dot.outputs['Value'], mr.inputs['Value'])
    oi = N('ShaderNodeObjectInfo')
    fa = N('ShaderNodeMath'); fa.operation = 'MULTIPLY'
    L(mr.outputs['Result'], fa.inputs[0]); L(oi.outputs['Alpha'], fa.inputs[1])
    mx = N('ShaderNodeMix'); mx.data_type = 'RGBA'
    mx.inputs['A'].default_value = (*lin(PAPER), 1)
    mx.inputs['B'].default_value = (*lin(color), 1)
    L(fa.outputs[0], mx.inputs['Factor'])
    L(mx.outputs['Result'], em.inputs['Color']); L(em.outputs[0], out.inputs[0])
    return m


def hatch_shade(color, name='hatch', light=(-0.55, -0.75, 0.62), spacing=11.0, width=0.30, base=0.02,
                cross=0.86):
    """Da Vinci-style tonal hatching in `color` on paper. Tone comes from a fixed
    key light; strokes run '\\' (upper left to lower right) in screen space and thicken
    with the light; the brightest areas get a second, crossing layer."""
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    N = nt.nodes.new
    L = nt.links.new
    out = N('ShaderNodeOutputMaterial')
    em = N('ShaderNodeEmission')
    geo = N('ShaderNodeNewGeometry')
    tc = N('ShaderNodeTexCoord')
    dot = N('ShaderNodeVectorMath'); dot.operation = 'DOT_PRODUCT'
    Lv = Vector(light).normalized()
    dot.inputs[1].default_value = tuple(Lv)
    L(geo.outputs['Normal'], dot.inputs[0])
    tone = N('ShaderNodeMath'); tone.operation = 'MULTIPLY_ADD'
    tone.inputs[1].default_value = 0.75; tone.inputs[2].default_value = 0.25    # wrap lighting
    L(dot.outputs['Value'], tone.inputs[0])
    tcl = N('ShaderNodeClamp'); L(tone.outputs[0], tcl.inputs['Value'])
    # screen-space stripes
    sep = N('ShaderNodeSeparateXYZ'); L(tc.outputs['Window'], sep.inputs[0])
    def stripes(ax, ay, spc):
        mx = N('ShaderNodeMath'); mx.operation = 'MULTIPLY'; mx.inputs[1].default_value = ax / spc
        my = N('ShaderNodeMath'); my.operation = 'MULTIPLY'; my.inputs[1].default_value = ay / spc
        L(sep.outputs[0], mx.inputs[0]); L(sep.outputs[1], my.inputs[0])
        ad = N('ShaderNodeMath'); ad.operation = 'ADD'
        L(mx.outputs[0], ad.inputs[0]); L(my.outputs[0], ad.inputs[1])
        pp = N('ShaderNodeMath'); pp.operation = 'PINGPONG'; pp.inputs[1].default_value = 0.5
        L(ad.outputs[0], pp.inputs[0])
        return pp.outputs[0]
    # stroke width follows tone: width*tone^1.5
    pw = N('ShaderNodeMath'); pw.operation = 'POWER'; pw.inputs[1].default_value = 2.2
    L(tcl.outputs[0], pw.inputs[0])
    wv = N('ShaderNodeMath'); wv.operation = 'MULTIPLY'; wv.inputs[1].default_value = width * 0.62
    L(pw.outputs[0], wv.inputs[0])
    lt = N('ShaderNodeMath'); lt.operation = 'LESS_THAN'
    L(stripes(1920, 1080, spacing), lt.inputs[0]); L(wv.outputs[0], lt.inputs[1])
    # cross layer above `cross`
    cr = N('ShaderNodeMath'); cr.operation = 'SUBTRACT'; cr.inputs[1].default_value = cross
    L(tcl.outputs[0], cr.inputs[0])
    crw = N('ShaderNodeMath'); crw.operation = 'MULTIPLY'; crw.inputs[1].default_value = 0.5 * width / (1 - cross)
    L(cr.outputs[0], crw.inputs[0])
    lt2 = N('ShaderNodeMath'); lt2.operation = 'LESS_THAN'
    L(stripes(1920, -1080, spacing * 1.15), lt2.inputs[0]); L(crw.outputs[0], lt2.inputs[1])
    hm = N('ShaderNodeMath'); hm.operation = 'MAXIMUM'
    L(lt.outputs[0], hm.inputs[0]); L(lt2.outputs[0], hm.inputs[1])
    # strokes fade a little in the darkest areas
    sk = N('ShaderNodeMath'); sk.operation = 'MULTIPLY_ADD'
    sk.inputs[1].default_value = 0.65; sk.inputs[2].default_value = 0.35
    L(tcl.outputs[0], sk.inputs[0])
    st = N('ShaderNodeMath'); st.operation = 'MULTIPLY'
    L(hm.outputs[0], st.inputs[0]); L(sk.outputs[0], st.inputs[1])
    # object alpha lets a whole object fade
    oi = N('ShaderNodeObjectInfo')
    fa = N('ShaderNodeMath'); fa.operation = 'MULTIPLY'
    L(st.outputs[0], fa.inputs[0]); L(oi.outputs['Alpha'], fa.inputs[1])
    bg = tuple(PAPER[i] * (1 - base) + color[i] * base for i in range(3))
    mixc = N('ShaderNodeMix'); mixc.data_type = 'RGBA'
    mixc.inputs['A'].default_value = (*lin(bg), 1)
    mixc.inputs['B'].default_value = (*lin(color), 1)
    L(fa.outputs[0], mixc.inputs['Factor'])
    L(mixc.outputs['Result'], em.inputs['Color'])
    L(em.outputs[0], out.inputs[0])
    return m


# ------------------------------------------------------------------ paper
def grid_paper(size=400, cell=1.0, z=0.0, name='grid', major=4, fade_near=6.0, fade_far=22.0):
    """A huge plane with a procedural grid in the shader (not drawn by Freestyle)."""
    bpy.ops.mesh.primitive_plane_add(size=size, location=(0, 0, z))
    p = bpy.context.object
    p.name = name
    m = bpy.data.materials.new('gridpaper')
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    out = nt.nodes.new('ShaderNodeOutputMaterial')
    em = nt.nodes.new('ShaderNodeEmission')
    tc = nt.nodes.new('ShaderNodeTexCoord')
    sep = nt.nodes.new('ShaderNodeSeparateXYZ')
    nt.links.new(tc.outputs['Object'], sep.inputs[0])

    def line_mask(axis_out, period, width):
        # distance to nearest grid line -> 1 on the line
        div = nt.nodes.new('ShaderNodeMath'); div.operation = 'DIVIDE'; div.inputs[1].default_value = period
        nt.links.new(axis_out, div.inputs[0])
        fr = nt.nodes.new('ShaderNodeMath'); fr.operation = 'PINGPONG'; fr.inputs[1].default_value = 0.5
        nt.links.new(div.outputs[0], fr.inputs[0])
        lt = nt.nodes.new('ShaderNodeMath'); lt.operation = 'LESS_THAN'; lt.inputs[1].default_value = width / period
        nt.links.new(fr.outputs[0], lt.inputs[0])
        return lt.outputs[0]
    w = 0.018 * cell
    mx = nt.nodes.new('ShaderNodeMath'); mx.operation = 'MAXIMUM'
    nt.links.new(line_mask(sep.outputs[0], cell, w), mx.inputs[0])
    nt.links.new(line_mask(sep.outputs[1], cell, w), mx.inputs[1])
    mj = nt.nodes.new('ShaderNodeMath'); mj.operation = 'MAXIMUM'
    nt.links.new(line_mask(sep.outputs[0], cell * major, w * 1.6), mj.inputs[0])
    nt.links.new(line_mask(sep.outputs[1], cell * major, w * 1.6), mj.inputs[1])
    tot = nt.nodes.new('ShaderNodeMath'); tot.operation = 'ADD'
    sc_ = nt.nodes.new('ShaderNodeMath'); sc_.operation = 'MULTIPLY'; sc_.inputs[1].default_value = 0.6
    nt.links.new(mx.outputs[0], tot.inputs[0]); nt.links.new(mj.outputs[0], sc_.inputs[0]); nt.links.new(sc_.outputs[0], tot.inputs[1])
    cl = nt.nodes.new('ShaderNodeMath'); cl.operation = 'MINIMUM'; cl.inputs[1].default_value = 1.0
    nt.links.new(tot.outputs[0], cl.inputs[0])
    cd = nt.nodes.new('ShaderNodeCameraData')
    fade = nt.nodes.new('ShaderNodeMapRange')
    fade.inputs['From Min'].default_value = fade_near
    fade.inputs['From Max'].default_value = fade_far
    fade.inputs['To Min'].default_value = 1.0
    fade.inputs['To Max'].default_value = 0.0
    nt.links.new(cd.outputs['View Distance'], fade.inputs['Value'])
    fm = nt.nodes.new('ShaderNodeMath'); fm.operation = 'MULTIPLY'
    nt.links.new(cl.outputs[0], fm.inputs[0]); nt.links.new(fade.outputs[0], fm.inputs[1])
    cl = fm
    mixc = nt.nodes.new('ShaderNodeMix'); mixc.data_type = 'RGBA'
    mixc.inputs['A'].default_value = (*lin(PAPER), 1)
    mixc.inputs['B'].default_value = (*lin(GRID), 1)
    nt.links.new(cl.outputs[0], mixc.inputs['Factor'])
    nt.links.new(mixc.outputs['Result'], em.inputs[0])
    nt.links.new(em.outputs[0], out.inputs[0])
    set_fill(p, m)
    link(p, 'paper')
    return p


# ------------------------------------------------------------------ mesh helpers
def mesh_obj(name, verts, faces, coll='ink', fill=None):
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    me.update()
    ob = bpy.data.objects.new(name, me)
    bpy.data.collections[coll].objects.link(ob)
    set_fill(ob, fill or paper_fill())
    return ob


def lathe(name, profile, sides=8, coll='gold', fill=None, rot=0.0):
    """Revolve [(r, z), ...] around Z with `sides` facets (flat-shaded, faceted)."""
    verts, faces = [], []
    n = len(profile)
    for s in range(sides):
        a = rot + 2 * math.pi * s / sides
        for r, z in profile:
            verts.append((r * math.cos(a), r * math.sin(a), z))
    for s in range(sides):
        s2 = (s + 1) % sides
        for i in range(n - 1):
            faces.append((s * n + i, s2 * n + i, s2 * n + i + 1, s * n + i + 1))
    # caps
    verts.append((0, 0, profile[0][1])); b = len(verts) - 1
    verts.append((0, 0, profile[-1][1])); t = len(verts) - 1
    for s in range(sides):
        s2 = (s + 1) % sides
        faces.append((s2 * n, s * n, b))
        faces.append((s * n + n - 1, s2 * n + n - 1, t))
    ob = mesh_obj(name, verts, faces, coll, fill)
    fix_normals(ob)
    return ob


def fix_normals(ob):
    bm = bmesh.new(); bm.from_mesh(ob.data)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    bm.to_mesh(ob.data); bm.free()


def box(name, size, loc=(0, 0, 0), coll='ink', fill=None):
    sx, sy, sz = (v / 2 for v in size)
    v = [(-sx, -sy, -sz), (sx, -sy, -sz), (sx, sy, -sz), (-sx, sy, -sz),
         (-sx, -sy, sz), (sx, -sy, sz), (sx, sy, sz), (-sx, sy, sz)]
    f = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    ob = mesh_obj(name, v, f, coll, fill)
    ob.location = loc
    return ob


def join(objs, name):
    bpy.ops.object.select_all(action='DESELECT')
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]
    bpy.ops.object.join()
    o = bpy.context.object
    o.name = name
    return o


def empty(name, loc=(0, 0, 0)):
    e = bpy.data.objects.new(name, None)
    e.location = loc
    bpy.context.scene.collection.objects.link(e)
    return e


def mark_edges(me, pred):
    """Freestyle edge marks (Blender 4+ stores them as the 'freestyle_edge' attribute)."""
    at = me.attributes.get('freestyle_edge') or me.attributes.new('freestyle_edge', 'BOOLEAN', 'EDGE')
    for i, e in enumerate(me.edges):
        v0, v1 = me.vertices[e.vertices[0]].co, me.vertices[e.vertices[1]].co
        if pred(v0, v1):
            at.data[i].value = True


# ------------------------------------------------------------------ the rook
ROOK_PROFILE = [(1.04, 0.00), (1.04, 0.12), (0.90, 0.22), (0.84, 0.30), (0.68, 0.42), (0.61, 0.62),
                (0.565, 1.30), (0.58, 1.46), (0.80, 1.58), (0.80, 1.68), (0.70, 1.74), (0.745, 2.04)]
CROWN_IN, CROWN_OUT, CROWN_TOP = 0.53, 0.745, 2.33


def _r_at(z):
    p = ROOK_PROFILE
    for (r0, z0), (r1, z1) in zip(p, p[1:]):
        if z0 <= z <= z1 and z1 > z0:
            return r0 + (r1 - r0) * (z - z0) / (z1 - z0)
    return p[-1][0]


def _sector(name, r0, r1, a0, a1, z0, z1, segs, coll, fill):
    vs, fs = [], []
    for k in range(segs + 1):
        a = a0 + (a1 - a0) * k / segs
        for r in (r0, r1):
            for z in (z0, z1):
                vs.append((r * math.cos(a), r * math.sin(a), z))
    # index: k*4 + (ri*2 + zi)
    I = lambda k, ri, zi: k * 4 + ri * 2 + zi
    for k in range(segs):
        fs.append((I(k, 1, 0), I(k + 1, 1, 0), I(k + 1, 1, 1), I(k, 1, 1)))   # outer
        fs.append((I(k + 1, 0, 0), I(k, 0, 0), I(k, 0, 1), I(k + 1, 0, 1)))   # inner
        fs.append((I(k, 0, 1), I(k, 1, 1), I(k + 1, 1, 1), I(k + 1, 0, 1)))   # top
        fs.append((I(k + 1, 0, 0), I(k + 1, 1, 0), I(k, 1, 0), I(k, 0, 0)))   # bottom
    fs.append((I(0, 0, 0), I(0, 1, 0), I(0, 1, 1), I(0, 0, 1)))
    fs.append((I(segs, 1, 0), I(segs, 0, 0), I(segs, 0, 1), I(segs, 1, 1)))
    ob = mesh_obj(name, vs, fs, coll, fill)
    fix_normals(ob)
    return ob


def rook(name='rook', sides=8, merlons=4, coll='gold', fill=None):
    """A Staunton rook, 2.33 units tall, door on -Y with stairs up to it.

    Returns (root, parts): body, crown, floor, surround, steps, door_frame,
    door_light, door_panel, door_hinge (empty at the door's left jamb), and
    'door_center' (a Vector in the root's space)."""
    fill = fill or facet_shade(GOLD, 'rook_facets')
    g = empty(name)
    prof = list(ROOK_PROFILE) + [(CROWN_IN, 2.04), (CROWN_IN, 1.94)]
    body = lathe(name + '_body', prof, sides=sides, coll=coll, fill=fill, rot=math.pi / sides)
    body.parent = g
    # merlons: annular sectors on the rim, gaps (crenels) between them
    span = 2 * math.pi / merlons
    ms = []
    for i in range(merlons):
        a0 = -math.pi / 2 + span * i + span * 0.17
        a1 = a0 + span * 0.66
        ms.append(_sector('%s_merlon%d' % (name, i), CROWN_IN, CROWN_OUT, a0, a1, 2.04, CROWN_TOP, 2, coll, fill))
    crown = join(ms, name + '_crown')
    crown.parent = g
    # door in the shaft, centred on -Y
    dw, z0, z1 = 0.27, 0.60, 1.10
    rz = _r_at((z0 + z1) / 2)
    cf = math.cos(math.pi / sides)
    yf = -_r_at(z0) * cf - 0.004
    fy = lambda z, d: -_r_at(z) * cf - d
    def arch(hw, za, zb, segs=14):
        pts = [(-hw, za), (-hw, zb - hw)]
        for k in range(segs + 1):
            t = math.pi - math.pi * k / segs
            pts.append((hw * math.cos(t), zb - hw + hw * math.sin(t)))
        pts.append((hw, za))
        return pts
    inner = arch(dw / 2, z0, z1)
    outer = arch(dw / 2 + 0.045, z0 - 0.0, z1 + 0.045)
    # stone surround: the band between outer and inner arch
    sv = [(x, fy(z, 0.016), z) for x, z in outer] + [(x, fy(z, 0.016), z) for x, z in inner]
    n = len(outer)
    sf = [(i, i + 1, n + i + 1, n + i) for i in range(n - 1)]
    surround = mesh_obj(name + '_surround', sv, sf, coll, fill)
    surround.parent = g
    hole = mesh_obj(name + '_door_frame', [(x, fy(z, 0.008), z) for x, z in inner], [tuple(range(len(inner)))],
                    coll, mat('doorhole', (0.006, 0.007, 0.006)))
    hole.parent = g
    light = mesh_obj(name + '_door_light', [(x * 0.97, fy(z, 0.011), z) for x, z in inner], [tuple(range(len(inner)))],
                     'paper', mat('doorlight', GOLD_HI, 3.0))
    light.parent = g
    hinge = empty(name + '_door_hinge', (-dw / 2, yf - 0.014, 0))
    hinge.parent = g
    pv = [(x + dw / 2, 0, z) for x, z in inner]
    panel = mesh_obj(name + '_door_panel', pv, [tuple(range(len(pv)))], 'ink', paper_fill())
    panel.parent = hinge
    # planks drawn as marked edges
    me = panel.data
    bm = bmesh.new(); bm.from_mesh(me)
    for k in (1, 2):
        x = dw * k / 3
        res = bmesh.ops.bisect_plane(bm, geom=bm.verts[:] + bm.edges[:] + bm.faces[:], plane_co=(x, 0, 0), plane_no=(1, 0, 0))
    bm.to_mesh(me); bm.free()
    mark_edges(me, lambda v0, v1: abs(v0.x - v1.x) < 1e-5 and 0.01 < v0.x < dw - 0.01)
    # stairs: from the ground up to the sill
    steps = []
    nsteps, run, sw = 6, 0.11, 0.36
    rise = z0 / nsteps
    wall = yf - 0.02
    for k in range(nsteps):
        top = rise * (k + 1)
        y0, y1 = wall - run * (nsteps - k), wall + 0.30
        steps.append(box('%s_step%d' % (name, k), (sw, y1 - y0, top), (0, (y0 + y1) / 2, top / 2), coll=coll, fill=fill))
    stairs = join(steps, name + '_stairs')
    stairs.parent = g
    # threshold slab in front of the door
    sill = box(name + '_sill', (dw + 0.12, 0.10, 0.03), (0, yf - 0.05, z0 - 0.015), coll=coll, fill=fill)
    sill.parent = g
    door_center = Vector((0, yf - 0.01, (z0 + z1) / 2))
    return g, dict(body=body, crown=crown, surround=surround, stairs=stairs, sill=sill, door_frame=hole,
                   door_light=light, door_panel=panel, door_hinge=hinge, door_center=door_center)


# ------------------------------------------------------------------ text
_fonts = {}


def font(name):
    if name not in _fonts:
        paths = {
            'hand': os.path.join(FONTS, 'Kalam-Regular.ttf'),
            'hand_bold': os.path.join(FONTS, 'Kalam-Bold.ttf'),
            'hand_light': os.path.join(FONTS, 'Kalam-Light.ttf'),
            'scrawl': os.path.join(FONTS, 'Caveat[wght].ttf'),
            'mono': '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf',
            'mono_bold': '/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf',
            'display': '/usr/share/fonts/opentype/inter/InterDisplay-Black.otf',
        }
        _fonts[name] = bpy.data.fonts.load(paths[name])
    return _fonts[name]


def text(name, body, size=0.5, color=INK, face='hand', align='CENTER', valign='CENTER',
         loc=(0, 0, 0), strength=1.0, coll='text'):
    cu = bpy.data.curves.new(name, 'FONT')
    cu.body = body
    cu.font = font(face)
    cu.size = size
    cu.align_x = align
    cu.align_y = valign
    ob = bpy.data.objects.new(name, cu)
    ob.location = loc
    bpy.data.collections[coll].objects.link(ob)
    ob.data.materials.append(mat('txt_%s_%.2f' % ('_'.join('%.2f' % x for x in color), strength), color, strength))
    return ob


def set_color(ob, color, strength=1.0):
    ob.data.materials.clear()
    ob.data.materials.append(mat('txt_%s_%.2f' % ('_'.join('%.2f' % x for x in color), strength), color, strength))


def camera(name='cam', loc=(0, -10, 3), target=(0, 0, 1), lens=50):
    cd = bpy.data.cameras.new(name)
    cd.lens = lens
    cam = bpy.data.objects.new(name, cd)
    bpy.context.scene.collection.objects.link(cam)
    cam.location = loc
    look_at(cam, target)
    bpy.context.scene.camera = cam
    return cam


def look_at(ob, target, roll=0.0):
    d = Vector(target) - ob.location
    q = d.to_track_quat('-Z', 'Y')
    ob.rotation_euler = q.to_euler()
    if roll:
        ob.rotation_euler.rotate_axis('Z', math.radians(roll))
