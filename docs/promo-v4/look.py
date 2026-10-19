"""The original film's look, rebuilt in Blender: toon-shaded parchment pigment,
dark ink lines, drafting guides, warm door light, on a dark aged page.

Geometry is ported 1:1 from docs/promo-v3/scene3d.html (three.js y-up units),
mapped to Blender as (x, y, z)_three -> (x, -z, y).
"""
import math, os
import bpy, bmesh
from mathutils import Vector, Matrix, Euler
import kit
from kit import lin, mesh_obj, empty, link

# palette from scene3d.html
PIGMENT = (0xaa / 255, 0xa1 / 255, 0x7d / 255)
STONE = (0x9d / 255, 0x94 / 255, 0x73 / 255)
WOOD = (0x7a / 255, 0x64 / 255, 0x40 / 255)
IRON = (0x2d / 255, 0x2a / 255, 0x22 / 255)
INKC = (0x08 / 255, 0x0a / 255, 0x08 / 255)
GUIDE = (0x9c / 255, 0xa8 / 255, 0x80 / 255)
GHOST = (0xc5 / 255, 0xaf / 255, 0x87 / 255)
PAGE = (0x0e / 255, 0x11 / 255, 0x0e / 255)
GLOW = (1.0, 0.95, 0.84)
KEY = Vector((-3, -5, 6)).normalized()      # three.js key light at (-3, 6, 5)
DOOR_POS = Vector((0, -0.70, -1.0))          # warm light source (the raised door)


def T(x, y, z):
    """three.js coords -> Blender."""
    return Vector((x, -z, y))


# ------------------------------------------------------------------ scene setup
SOLIDS = ('solid', 'inkgeo', 'ground', 'text')
BOXC = ('boxui', 'boxsolid')


def _lineset(fs, name, coll, color, thickness, crease=True, alpha=1.0, wobble=(12.0, 0.45, 1), offset=None):
    ls = fs.linesets.new(name)
    ls.select_by_collection = True; ls.collection = bpy.data.collections[coll]
    ls.select_by_visibility = True; ls.visibility = 'VISIBLE'
    ls.select_by_edge_types = True
    for k in ('select_silhouette', 'select_border', 'select_external_contour', 'select_edge_mark'):
        setattr(ls, k, True)
    ls.select_crease = crease
    st = ls.linestyle
    st.color = lin(color); st.alpha = alpha
    st.thickness = thickness; st.caps = 'ROUND'; st.chaining = 'PLAIN'
    pn = st.geometry_modifiers.new('wobble', 'PERLIN_NOISE_1D')
    pn.frequency, pn.amplitude, pn.octaves = wobble; pn.seed = 1 if not offset else 31
    if offset:
        off = st.geometry_modifiers.new('offset', '2D_OFFSET'); off.x, off.y = offset
    tm = st.thickness_modifiers.new('taper', 'ALONG_STROKE')
    tm.mapping = 'CURVE'; tm.blend = 'MULTIPLY'
    cm = tm.curve.curves[0]
    cm.points[0].location = (0, 0.45); cm.points[1].location = (1, 0.5)
    cm.points.new(0.15, 1.0); cm.points.new(0.85, 1.0)
    tm.curve.update()
    return ls


def setup(sc, w=1920, h=1080, samples=8, line=2.2):
    sc.render.engine = 'CYCLES'
    sc.cycles.device = 'CPU'
    sc.cycles.samples = samples
    sc.cycles.use_denoising = False
    sc.cycles.max_bounces = 0
    sc.cycles.transparent_max_bounces = 8
    sc.cycles.use_adaptive_sampling = False
    sc.render.resolution_x, sc.render.resolution_y = w, h
    sc.render.resolution_percentage = 100
    sc.render.fps = 30
    sc.render.image_settings.file_format = 'PNG'
    sc.render.image_settings.color_mode = 'RGB'
    sc.render.film_transparent = True
    sc.view_settings.view_transform = 'Standard'
    world = bpy.data.worlds.new('page'); sc.world = world
    world.use_nodes = True
    world.node_tree.nodes['Background'].inputs[0].default_value = (*lin(PAGE), 1)
    ALL = SOLIDS + ('fx', 'guides', 'portal', 'portalfx', 'mask') + BOXC
    for name in ALL:
        if name not in bpy.data.collections:
            c = bpy.data.collections.new(name); sc.collection.children.link(c)
    sc.render.use_freestyle = True
    sc.render.line_thickness_mode = 'ABSOLUTE'
    # main: solids, cards and text, drawn in ink
    main = sc.view_layers[0]; main.name = 'main'
    main.use_freestyle = True
    for c in main.layer_collection.children:
        c.exclude = c.name not in SOLIDS
    fs = main.freestyle_settings
    fs.crease_angle = math.radians(179)
    for ls in list(fs.linesets):
        fs.linesets.remove(ls)
    _lineset(fs, 'ink', 'solid', INKC, line)
    _lineset(fs, 'retrace', 'solid', (0x11 / 255, 0x15 / 255, 0x0f / 255), line * 0.8, alpha=0.38,
             wobble=(9.0, 1.1, 1), offset=(1.2, 0.8))
    # fx: additive light; everything solid holds it out
    fx = sc.view_layers.new('fx'); fx.use_freestyle = False
    for c in fx.layer_collection.children:
        if c.name in SOLIDS:
            c.holdout = True
        else:
            c.exclude = c.name not in ('fx', 'guides')
        if c.name in BOXC:
            c.exclude = True
    # portal: the world inside the rook
    po = sc.view_layers.new('portal'); po.use_freestyle = True
    for c in po.layer_collection.children:
        c.exclude = c.name not in ('portal', 'portalfx')
    pfs = po.freestyle_settings
    pfs.crease_angle = math.radians(150)
    for l in list(pfs.linesets):
        pfs.linesets.remove(l)
    _lineset(pfs, 'ink', 'portal', INKC, line * 0.8)
    # box: the chat box and its shards, laid over everything (camera space)
    bx = sc.view_layers.new('box'); bx.use_freestyle = True
    for c in bx.layer_collection.children:
        c.exclude = c.name not in BOXC
    bfs = bx.freestyle_settings
    bfs.crease_angle = math.radians(150)
    for l in list(bfs.linesets):
        bfs.linesets.remove(l)
    _lineset(bfs, 'ink', 'boxsolid', INKC, line * 0.9)
    # mask: the doorway opening, cut by what stands in front of it
    mk = sc.view_layers.new('mask'); mk.use_freestyle = False
    for c in mk.layer_collection.children:
        if c.name in ('solid', 'inkgeo'):
            c.holdout = True
        else:
            c.exclude = c.name != 'mask'
    return main, fx


def compositor(sc, grain_img, shade_img=None):
    """main over the page, fx added, the portal in the doorway, then shade, bloom, flash/white/black
    and a parchment grain over everything. Returns the nodes to drive per frame."""
    ng = bpy.data.node_groups.new('comp', 'CompositorNodeTree')
    ng.interface.new_socket('Image', in_out='OUTPUT', socket_type='NodeSocketColor')
    N, L = ng.nodes.new, ng.links.new
    rm = N('CompositorNodeRLayers'); rm.layer = 'main'
    rf = N('CompositorNodeRLayers'); rf.layer = 'fx'
    rp = N('CompositorNodeRLayers'); rp.layer = 'portal'
    rk = N('CompositorNodeRLayers'); rk.layer = 'mask'
    def mix(blend, a, b, fac=1.0, fac_sock=None):
        m = N('ShaderNodeMix'); m.data_type = 'RGBA'; m.blend_type = blend
        m.inputs['Factor'].default_value = fac
        if fac_sock is not None: L(fac_sock, m.inputs['Factor'])
        if isinstance(a, tuple): m.inputs['A'].default_value = a
        else: L(a, m.inputs['A'])
        if isinstance(b, tuple): m.inputs['B'].default_value = b
        else: L(b, m.inputs['B'])
        return m
    # premultiplied 'over': page * (1 - alpha) + image
    pg = mix('MIX', (*lin(PAGE), 1), (0, 0, 0, 1), fac_sock=rm.outputs['Alpha'])
    over = mix('ADD', pg.outputs['Result'], rm.outputs['Image'])
    add = mix('ADD', over.outputs['Result'], rf.outputs['Image'])
    psky = mix('MIX', (*lin((0.035, 0.03, 0.02)), 1), rp.outputs['Image'], fac_sock=rp.outputs['Alpha'])
    force = N('ShaderNodeMath'); force.operation = 'MAXIMUM'; force.inputs[1].default_value = 0.0
    L(rk.outputs['Alpha'], force.inputs[0])
    door = mix('MIX', add.outputs['Result'], psky.outputs['Result'], fac_sock=force.outputs[0])
    pagefade = mix('MIX', door.outputs['Result'], (*lin(PAGE), 1), 0.0)
    rb = N('CompositorNodeRLayers'); rb.layer = 'box'
    bk = mix('MIX', pagefade.outputs['Result'], (0, 0, 0, 1), fac_sock=rb.outputs['Alpha'])
    boxo = mix('ADD', bk.outputs['Result'], rb.outputs['Image'])
    last = boxo.outputs['Result']
    shade = None
    if shade_img is not None:
        im2 = N('CompositorNodeImage'); im2.image = shade_img
        shade = mix('MULTIPLY', last, im2.outputs['Image'], 0.0)
        last = shade.outputs['Result']
    gl = N('CompositorNodeGlare')
    gl.inputs['Type'].default_value = 'Bloom'
    gl.inputs['Quality'].default_value = 'Medium'
    gl.inputs['Threshold'].default_value = 0.85
    gl.inputs['Size'].default_value = 0.65
    gl.inputs['Strength'].default_value = 0.8
    L(last, gl.inputs['Image'])
    flash = mix('SCREEN', gl.outputs[0], (1.0, 0.93, 0.80, 1), 0.0)
    white = mix('MIX', flash.outputs['Result'], (1.0, 0.96, 0.88, 1), 0.0)
    black = mix('MIX', white.outputs['Result'], (0, 0, 0, 1), 0.0)
    im = N('CompositorNodeImage'); im.image = grain_img
    ov = mix('OVERLAY', black.outputs['Result'], im.outputs['Image'], 0.45)
    out = N('NodeGroupOutput')
    L(ov.outputs['Result'], out.inputs[0])
    sc.compositing_node_group = ng
    return dict(ng=ng, force=force.inputs[1], pagefade=pagefade.inputs['Factor'],
                shade=shade.inputs['Factor'] if shade else None, flash=flash.inputs['Factor'],
                white=white.inputs['Factor'], black=black.inputs['Factor'], glare=gl)


def parchment_image(w=1920, h=1080, seed=7):
    """A parchment grain/mottle plate, centred on mid-grey for an overlay blend."""
    import numpy as np
    rng = np.random.default_rng(seed)
    def blur_noise(scale):
        sw, sh = max(2, w // scale), max(2, h // scale)
        n = rng.standard_normal((sh, sw)).astype(np.float32)
        ys = np.linspace(0, sh - 1, h); xs = np.linspace(0, sw - 1, w)
        y0 = np.floor(ys).astype(int); x0 = np.floor(xs).astype(int)
        y1 = np.minimum(y0 + 1, sh - 1); x1 = np.minimum(x0 + 1, sw - 1)
        fy = (ys - y0)[:, None]; fx = (xs - x0)[None, :]
        fy = fy * fy * (3 - 2 * fy); fx = fx * fx * (3 - 2 * fx)
        a = n[y0][:, x0]; b = n[y0][:, x1]; c = n[y1][:, x0]; d = n[y1][:, x1]
        return (a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy
    mott = 0.55 * blur_noise(240) + 0.3 * blur_noise(90) + 0.18 * blur_noise(30) + 0.08 * blur_noise(8)
    fib = rng.standard_normal((h, w)).astype(np.float32)
    # stretched fibres: smear the fine noise horizontally
    k = 9
    fib = np.cumsum(np.pad(fib, ((0, 0), (k, 0))), axis=1)
    fib = (fib[:, k:] - fib[:, :-k]) / k
    v = 0.5 + 0.055 * mott + 0.03 * fib
    # vignette (darker edges, like the original)
    yy, xx = np.mgrid[0:h, 0:w]
    r = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2)
    v -= 0.10 * np.clip(r - 0.6, 0, 1) ** 1.4
    v = np.clip(v, 0, 1).astype(np.float32)
    img = bpy.data.images.new('parchment', w, h, alpha=False, float_buffer=True)
    px = np.ones((h, w, 4), dtype=np.float32)
    px[..., 0] = px[..., 1] = px[..., 2] = v[::-1]
    img.colorspace_settings.name = 'Non-Color'      # before the pixels: changing it regenerates the buffer
    img.pixels.foreach_set(px.ravel())
    img.pack()
    return img


# ------------------------------------------------------------------ materials
_m = {}


def toon(color, name, tex=0.10, rough=1.0):
    """Toon shading like MeshToonMaterial with the film's 4-step ramp, plus a parchment
    texture in object space. Emission only, so it renders fast and flat."""
    key = (name, tuple(color))
    if key in _m:
        return _m[key]
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree; nt.nodes.clear()
    N, L = nt.nodes.new, nt.links.new
    out = N('ShaderNodeOutputMaterial'); em = N('ShaderNodeEmission')
    geo = N('ShaderNodeNewGeometry')
    dot = N('ShaderNodeVectorMath'); dot.operation = 'DOT_PRODUCT'
    dot.inputs[1].default_value = tuple(KEY)
    L(geo.outputs['Normal'], dot.inputs[0])
    ramp = N('ShaderNodeValToRGB')
    cr = ramp.color_ramp; cr.interpolation = 'CONSTANT'
    cr.elements[0].position = 0.0; cr.elements[0].color = (65 / 255,) * 3 + (1,)
    cr.elements[1].position = 0.25; cr.elements[1].color = (135 / 255,) * 3 + (1,)
    e = cr.elements.new(0.5); e.color = (210 / 255,) * 3 + (1,)
    e = cr.elements.new(0.75); e.color = (1, 1, 1, 1)
    # map dot -1..1 -> 0..1 (half-lambert like the toon material's diffuse term)
    mr = N('ShaderNodeMapRange')
    mr.inputs['From Min'].default_value = 0.0; mr.inputs['From Max'].default_value = 1.0
    mr.inputs['To Min'].default_value = 0.0; mr.inputs['To Max'].default_value = 1.0
    L(dot.outputs['Value'], mr.inputs['Value'])
    L(mr.outputs['Result'], ramp.inputs['Fac'])
    # hemisphere fill: sky e3d8ad from above, ground 35402b from below
    sep = N('ShaderNodeSeparateXYZ'); L(geo.outputs['Normal'], sep.inputs[0])
    hz = N('ShaderNodeMapRange')
    hz.inputs['From Min'].default_value = -1; hz.inputs['From Max'].default_value = 1
    L(sep.outputs['Z'], hz.inputs['Value'])
    hemi = N('ShaderNodeMix'); hemi.data_type = 'RGBA'
    hemi.inputs['A'].default_value = (*lin((0x35 / 255, 0x40 / 255, 0x2b / 255)), 1)
    hemi.inputs['B'].default_value = (*lin((0xe3 / 255, 0xd8 / 255, 0xad / 255)), 1)
    L(hz.outputs['Result'], hemi.inputs['Factor'])
    # light = 0.42*hemi + 0.78*key_ramp (tuned to the film's frames)
    kmul = N('ShaderNodeMix'); kmul.data_type = 'RGBA'; kmul.blend_type = 'MULTIPLY'
    kmul.inputs['Factor'].default_value = 1.0
    kmul.inputs['A'].default_value = (*lin((1.0, 0.93, 0.77)), 1)
    L(ramp.outputs['Color'], kmul.inputs['B'])
    hk = N('ShaderNodeMix'); hk.data_type = 'RGBA'; hk.blend_type = 'ADD'
    hk.inputs['Factor'].default_value = 0.95
    hs = N('ShaderNodeMix'); hs.data_type = 'RGBA'; hs.blend_type = 'MULTIPLY'
    hs.inputs['Factor'].default_value = 1.0; hs.inputs['B'].default_value = (0.55, 0.55, 0.55, 1)
    L(hemi.outputs['Result'], hs.inputs['A'])
    L(hs.outputs['Result'], hk.inputs['A']); L(kmul.outputs['Result'], hk.inputs['B'])
    # parchment: mottle + fibres in object space
    tc = N('ShaderNodeTexCoord')
    n1 = N('ShaderNodeTexNoise'); n1.inputs['Scale'].default_value = 5.0; n1.inputs['Detail'].default_value = 6.0
    n2 = N('ShaderNodeTexNoise'); n2.inputs['Scale'].default_value = 60.0; n2.inputs['Detail'].default_value = 2.0
    n2.inputs['Distortion'].default_value = 0.4
    L(tc.outputs['Object'], n1.inputs['Vector']); L(tc.outputs['Object'], n2.inputs['Vector'])
    pm = N('ShaderNodeMath'); pm.operation = 'MULTIPLY_ADD'
    pm.inputs[1].default_value = 0.6; L(n1.outputs['Fac'], pm.inputs[0]); L(n2.outputs['Fac'], pm.inputs[2])
    pt = N('ShaderNodeMapRange')
    pt.inputs['From Min'].default_value = 0.3; pt.inputs['From Max'].default_value = 1.3
    pt.inputs['To Min'].default_value = 1 - tex; pt.inputs['To Max'].default_value = 1 + tex * 0.5
    L(pm.outputs[0], pt.inputs['Value'])
    base = N('ShaderNodeMix'); base.data_type = 'RGBA'; base.blend_type = 'MULTIPLY'
    base.inputs['Factor'].default_value = 1.0; base.inputs['A'].default_value = (*lin(color), 1)
    L(hk.outputs['Result'], base.inputs['B'])
    tx = N('ShaderNodeMix'); tx.data_type = 'RGBA'; tx.blend_type = 'MULTIPLY'
    tx.inputs['Factor'].default_value = 1.0
    L(base.outputs['Result'], tx.inputs['A'])
    cmb = N('ShaderNodeCombineColor')
    for i in range(3): L(pt.outputs['Result'], cmb.inputs[i])
    L(cmb.outputs['Color'], tx.inputs['B'])
    # door light warms nearby surfaces: driven per frame through the object colour
    oi = N('ShaderNodeObjectInfo')
    dv = N('ShaderNodeVectorMath'); dv.operation = 'DISTANCE'; dv.inputs[1].default_value = tuple(DOOR_POS)
    L(geo.outputs['Position'], dv.inputs[0])
    fall = N('ShaderNodeMapRange'); fall.interpolation_type = 'SMOOTHSTEP'
    fall.inputs['From Min'].default_value = 0.9; fall.inputs['From Max'].default_value = 0.0
    L(dv.outputs['Value'], fall.inputs['Value'])
    wf = N('ShaderNodeMath'); wf.operation = 'MULTIPLY'
    L(fall.outputs['Result'], wf.inputs[0]); L(oi.outputs['Alpha'], wf.inputs[1])
    warm = N('ShaderNodeMix'); warm.data_type = 'RGBA'; warm.blend_type = 'ADD'
    L(tx.outputs['Result'], warm.inputs['A'])
    warm.inputs['B'].default_value = (*lin((0.42, 0.30, 0.14)), 1)
    L(wf.outputs[0], warm.inputs['Factor'])
    L(warm.outputs['Result'], em.inputs['Color']); L(em.outputs[0], out.inputs[0])
    _m[key] = m
    return m


def flat(color, strength=1.0, name='flat'):
    key = (name, tuple(color), strength)
    if key in _m:
        return _m[key]
    m = kit.mat(name + '_%d' % len(_m), color, strength)
    _m[key] = m
    return m


def additive(color, name='add', strength=1.0, uv_mode=None):
    """An emission that adds light; object colour alpha scales it (for fades)."""
    key = (name, tuple(color), strength, uv_mode)
    if key in _m:
        return _m[key]
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree; nt.nodes.clear()
    N, L = nt.nodes.new, nt.links.new
    out = N('ShaderNodeOutputMaterial')
    tr = N('ShaderNodeBsdfTransparent'); em = N('ShaderNodeEmission')
    em.inputs['Color'].default_value = (*lin(color), 1)
    addsh = N('ShaderNodeAddShader')
    oi = N('ShaderNodeObjectInfo')
    a = oi.outputs['Alpha']
    if uv_mode:
        uv = N('ShaderNodeTexCoord'); sep = N('ShaderNodeSeparateXYZ'); L(uv.outputs['UV'], sep.inputs[0])
        if uv_mode == 'shaft':      # (1-v)^2 * smooth edges in u
            one = N('ShaderNodeMath'); one.operation = 'SUBTRACT'; one.inputs[0].default_value = 1.0
            L(sep.outputs['Y'], one.inputs[1])
            sq = N('ShaderNodeMath'); sq.operation = 'POWER'; sq.inputs[1].default_value = 2.0
            L(one.outputs[0], sq.inputs[0])
            e1 = N('ShaderNodeMapRange'); e1.interpolation_type = 'SMOOTHSTEP'
            e1.inputs['From Min'].default_value = 0; e1.inputs['From Max'].default_value = .35
            L(sep.outputs['X'], e1.inputs['Value'])
            e2 = N('ShaderNodeMapRange'); e2.interpolation_type = 'SMOOTHSTEP'
            e2.inputs['From Min'].default_value = 1; e2.inputs['From Max'].default_value = .65
            L(sep.outputs['X'], e2.inputs['Value'])
            m1 = N('ShaderNodeMath'); m1.operation = 'MULTIPLY'; L(sq.outputs[0], m1.inputs[0]); L(e1.outputs['Result'], m1.inputs[1])
            m2 = N('ShaderNodeMath'); m2.operation = 'MULTIPLY'; L(m1.outputs[0], m2.inputs[0]); L(e2.outputs['Result'], m2.inputs[1])
            mask = m2.outputs[0]
        else:                        # 'radial': soft disc
            sub = N('ShaderNodeVectorMath'); sub.operation = 'SUBTRACT'; sub.inputs[1].default_value = (.5, .5, 0)
            L(uv.outputs['UV'], sub.inputs[0])
            ln = N('ShaderNodeVectorMath'); ln.operation = 'LENGTH'; L(sub.outputs[0], ln.inputs[0])
            mr = N('ShaderNodeMapRange'); mr.interpolation_type = 'SMOOTHSTEP'
            mr.inputs['From Min'].default_value = 0.5; mr.inputs['From Max'].default_value = 0.0
            L(ln.outputs['Value'], mr.inputs['Value'])
            pw = N('ShaderNodeMath'); pw.operation = 'POWER'; pw.inputs[1].default_value = 1.6
            L(mr.outputs['Result'], pw.inputs[0])
            mask = pw.outputs[0]
        mm = N('ShaderNodeMath'); mm.operation = 'MULTIPLY'; L(mask, mm.inputs[0]); L(a, mm.inputs[1])
        a = mm.outputs[0]
    sm = N('ShaderNodeMath'); sm.operation = 'MULTIPLY'; sm.inputs[1].default_value = strength
    L(a, sm.inputs[0]); L(sm.outputs[0], em.inputs['Strength'])
    L(tr.outputs[0], addsh.inputs[0]); L(em.outputs[0], addsh.inputs[1])
    L(addsh.outputs[0], out.inputs[0])
    _m[key] = m
    return m


# ------------------------------------------------------------------ geometry helpers
def lathe3(name, profile, segs, mat, coll='solid'):
    """three.js LatheGeometry(profile, segs): vertex phi=0 faces the viewer (-Y)."""
    pts = [(r, y) for r, y in profile]
    vs, fs = [], []
    n = len(pts)
    for s in range(segs):
        phi = 2 * math.pi * s / segs
        for r, y in pts:
            vs.append(tuple(T(r * math.sin(phi), y, r * math.cos(phi))))
    for s in range(segs):
        s2 = (s + 1) % segs
        for i in range(n - 1):
            a, b, c, d = s * n + i, s2 * n + i, s2 * n + i + 1, s * n + i + 1
            fs.append((a, b, c, d))
    ob = mesh_obj(name, vs, fs, coll, mat)
    bm = bmesh.new(); bm.from_mesh(ob.data)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
    bmesh.ops.dissolve_degenerate(bm, edges=bm.edges, dist=1e-6)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(ob.data); bm.free()
    return ob


def extrude_shape(name, outline, depth, mat, coll='solid', holes=()):
    """Extrude a 2D outline (three.js shape space, x right, y up) along +z by depth.
    Returns a mesh in three space converted to Blender coords (object at origin)."""
    bm = bmesh.new()
    def ring(pts, z):
        return [bm.verts.new(tuple(T(x, y, z))) for x, y in pts]
    front, back = ring(outline, depth), ring(outline, 0)
    try:
        bm.faces.new(front)
        bm.faces.new(list(reversed(back)))
    except ValueError:
        pass
    n = len(outline)
    for i in range(n):
        j = (i + 1) % n
        bm.faces.new((back[i], back[j], front[j], front[i]))
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    me = bpy.data.meshes.new(name); bm.to_mesh(me); bm.free()
    ob = bpy.data.objects.new(name, me)
    bpy.data.collections[coll].objects.link(ob)
    me.materials.append(mat)
    return ob


def arch_pts(w, h, x0=0.0, y0=0.0, seg=10):
    """three.js arch(): bottom-left, bottom-right, up, half circle over, back down."""
    r = w / 2
    pts = [(x0 - r, y0), (x0 + r, y0), (x0 + r, y0 + h - r)]
    for k in range(1, seg):
        a = math.pi * k / seg
        pts.append((x0 + r * math.cos(a), y0 + h - r + r * math.sin(a)))
    pts.append((x0 - r, y0 + h - r))
    return pts


def ink_line(name, pts, width=0.006, coll='solid', color=INKC):
    """A drawn line as thin geometry (for voussoirs, planks): dark ink."""
    cu = bpy.data.curves.new(name, 'CURVE'); cu.dimensions = '3D'
    cu.bevel_depth = width; cu.bevel_resolution = 0
    for a, b in pts:
        sp = cu.splines.new('POLY'); sp.points.add(1)
        sp.points[0].co = (*a, 1); sp.points[1].co = (*b, 1)
    ob = bpy.data.objects.new(name, cu)
    bpy.data.collections['inkgeo'].objects.link(ob)
    ob.data.materials.append(flat(color, 1.0, 'ink'))
    ob['no_lines'] = True
    return ob


def guide_line(name, pts, color, alpha, width=0.0045, closed=False, dash=None):
    """Faint drafting guide: emission on the fx layer (additive on a dark page)."""
    cu = bpy.data.curves.new(name, 'CURVE'); cu.dimensions = '3D'
    cu.bevel_depth = width; cu.bevel_resolution = 0
    segs = []
    if dash:
        d, g = dash
        P = pts + ([pts[0]] if closed else [])
        for a, b in zip(P, P[1:]):
            a, b = Vector(a), Vector(b)
            L = (b - a).length
            t = 0
            while t < L:
                t1 = min(L, t + d)
                segs.append((a.lerp(b, t / L), a.lerp(b, t1 / L)))
                t += d + g
    else:
        P = pts + ([pts[0]] if closed else [])
        segs = [(Vector(P[0]), None)]
    if dash:
        for a, b in segs:
            sp = cu.splines.new('POLY'); sp.points.add(1)
            sp.points[0].co = (*a, 1); sp.points[1].co = (*b, 1)
    else:
        sp = cu.splines.new('POLY'); sp.points.add(len(pts) - 1)
        for i, p in enumerate(pts):
            sp.points[i].co = (*p, 1)
        sp.use_cyclic_u = closed
    ob = bpy.data.objects.new(name, cu)
    bpy.data.collections['guides'].objects.link(ob)
    ob.data.materials.append(additive(color, 'guide_%s' % name, 1.0))
    ob.color = (1, 1, 1, alpha)
    return ob


# ------------------------------------------------------------------ the rook (exact)
PROFILE = [(0, -2.15), (1.08, -2.15), (1.12, -2.06), (1.12, -1.91), (.98, -1.79), (.96, -1.67), (.81, -1.61),
           (.78, -1.44), (.67, -1.35), (.53, -.93), (.46, .46), (.52, .76), (.70, .98), (.73, 1.09), (.90, 1.14),
           (.90, 1.31), (.86, 1.39), (.86, 1.58), (.59, 1.58), (.59, 1.22), (0, 1.22)]
DW, DH, FW, FH, Y0, ZF, DEPTH = .5, .82, .82, 1.06, -2.15, 1.18, 1.0


def rook():
    """The rook from scene3d.html. Returns (root, parts)."""
    root = empty('rook')
    pig, stone, wood, iron = toon(PIGMENT, 'pigment'), toon(STONE, 'stone'), toon(WOOD, 'wood', .14), toon(IRON, 'iron', .05)
    body = lathe3('rook_body', PROFILE, 16, pig); body.parent = root
    merl = []
    for i in range(6):
        c = i * math.pi / 3; a, b = c - .34, c + .34
        outline = [(.88 * math.cos(a + (b - a) * k / 3), .88 * math.sin(a + (b - a) * k / 3)) for k in range(4)] + \
                  [(.58 * math.cos(b - (b - a) * k / 3), .58 * math.sin(b - (b - a) * k / 3)) for k in range(4)]
        # shape xy -> after rotateX(-pi/2): (x, z_ext, -y) + y 1.54 -> Blender (x, y, z_ext + 1.54)
        vs, fs = [], []
        for zz in (0.0, .53):
            for x, y in outline:
                vs.append((x, y, 1.54 + zz))
        n = len(outline)
        fs.append(tuple(range(n))); fs.append(tuple(range(2 * n - 1, n - 1, -1)))
        for k in range(n):
            j = (k + 1) % n
            fs.append((k, j, n + j, n + k))
        ob = mesh_obj('rook_merlon%d' % i, vs, fs, 'solid', pig)
        kit.fix_normals(ob)
        ob.parent = root; merl.append(ob)
    # ---- the door: raised in the lower shaft, a stone surround, stairs up to it (Blender coords)
    gate = empty('gate'); gate.parent = root
    dw, dh, z0 = 0.34, 0.62, -1.30
    sw = 0.065
    def r_at(z):                     # profile radius at Blender height z
        for (r0, y0), (r1, y1) in zip(PROFILE, PROFILE[1:]):
            if y0 <= z <= y1 and y1 > y0:
                return r0 + (r1 - r0) * (z - y0) / (y1 - y0)
        return PROFILE[-2][0]
    k = (1 - math.cos(math.pi / 16)) / math.sin(math.pi / 16)   # 16-gon, a vertex faces the viewer
    def surf_y(x, z, d=0.0):         # front of the 16-gon (a vertex faces the viewer)
        r = r_at(z)
        return -(r - abs(x) * k) - d
    def arch2(hw, za, zb, segs=12):
        pts = [(-hw, za), (-hw, zb - hw)]
        for i in range(1, segs):
            t = math.pi - math.pi * i / segs
            pts.append((hw * math.cos(t), zb - hw + hw * math.sin(t)))
        pts += [(hw, zb - hw), (hw, za)]
        return pts
    inner = arch2(dw / 2, z0, z0 + dh)
    outer = arch2(dw / 2 + sw, z0, z0 + dh + sw)
    # surround: a raised stone band, 0.03 proud of the wall
    n = len(outer)
    vs = [(x, surf_y(x, z, 0.03), z) for x, z in outer] + [(x, surf_y(x, z, 0.03), z) for x, z in inner]
    vs += [(x, surf_y(x, z, -0.02), z) for x, z in outer] + [(x, surf_y(x, z, -0.02), z) for x, z in inner]
    fs = [(i, i + 1, n + i + 1, n + i) for i in range(n - 1)]                       # face
    fs += [(2 * n + i + 1, 2 * n + i, i, i + 1) for i in range(n - 1)]              # outer edge
    fs += [(n + i, n + i + 1, 3 * n + i + 1, 3 * n + i) for i in range(n - 1)]      # inner reveal
    surround = mesh_obj('door_surround', vs, fs, 'solid', stone); kit.fix_normals(surround)
    surround.parent = gate
    # voussoirs on the arch, two courses on each jamb (ink)
    cz = z0 + dh - dw / 2; ri, ro = dw / 2, dw / 2 + sw
    segs = []
    for i in range(7):
        a = i / 6 * math.pi
        xa, za = math.cos(a) * ri, cz + math.sin(a) * ri
        xb, zb = math.cos(a) * ro, cz + math.sin(a) * ro
        segs.append(((xa, surf_y(xa, za, 0.034), za), (xb, surf_y(xb, zb, 0.034), zb)))
    for z in (z0 + .16, z0 + .32):
        for xa, xb in ((-ro, -ri), (ri, ro)):
            segs.append(((xa, surf_y(xa, z, 0.034), z), (xb, surf_y(xb, z, 0.034), z)))
    vous = ink_line('door_voussoirs', segs, 0.0045); vous.parent = gate
    # the doorway: a glowing opening just proud of the wall
    back = mesh_obj('door_mask', [(x * .985, surf_y(x, z, 0.022), z) for x, z in inner], [tuple(range(len(inner)))],
                    'mask', flat((1, 1, 1), 1.0, 'maskwhite'))
    back.parent = gate
    # the leaf: the original's wooden door, hinged on its left edge, swings out
    yh = surf_y(-dw / 2, z0, 0.012)
    hinge = empty('gate_hinge', (-dw / 2, yh, z0)); hinge.parent = gate
    lf = [(x + dw / 2, z - z0) for x, z in arch2(dw / 2 - .006, z0, z0 + dh - .004)]
    lv = [(x, 0, z) for x, z in lf] + [(x, -0.035, z) for x, z in lf]
    m = len(lf)
    lfs = [tuple(range(m)), tuple(range(2 * m - 1, m - 1, -1))] + [(i, (i + 1) % m, m + (i + 1) % m, m + i) for i in range(m)]
    leaf = mesh_obj('gate_leaf', lv, lfs, 'solid', wood); kit.fix_normals(leaf); leaf.parent = hinge
    psegs = []
    for i in (1, 2, 3):
        x = dw * i / 4
        top = dh - dw / 2 + math.sqrt(max(0, (dw / 2) ** 2 - (x - dw / 2) ** 2)) - .02
        psegs.append(((x, -0.037, .02), (x, -0.037, top)))
    planks = ink_line('gate_planks', psegs, 0.0035); planks.parent = hinge
    for zz in (.11, .40):
        st_ = kit.box('gate_strap%d' % int(zz * 100), (dw - .03, .01, .026), (dw / 2, -0.041, zz), coll='solid', fill=iron)
        st_.parent = hinge
    bpy.ops.mesh.primitive_torus_add(major_radius=.022, minor_radius=.005, major_segments=16, minor_segments=6)
    ringo = bpy.context.object; ringo.name = 'gate_ring'
    link(ringo, 'solid'); ringo.data.materials.append(iron)
    ringo.location = (dw - .065, -0.045, .27); ringo.rotation_euler = (math.radians(90), 0, 0)
    ringo.parent = hinge
    # stairs from the floor up to the sill, over the base
    steps = []
    nst, run, stw = 6, 0.145, 0.42
    wall = surf_y(0, z0, 0.0)
    rise = (z0 - Y0) / nst
    for i in range(nst):
        top = Y0 + rise * (i + 1)
        ya, yb = wall - run * (nst - i), wall + 0.25
        steps.append(kit.box('step%d' % i, (stw, yb - ya, top - Y0), (0, (ya + yb) / 2, (top + Y0) / 2), coll='solid', fill=stone))
    stairs = kit.join(steps, 'stairs'); stairs.parent = gate
    sill = kit.box('sill', (dw + .1, .1, .025), (0, wall - .04, z0 - .0125), coll='solid', fill=stone); sill.parent = gate
    door_c = Vector((0, surf_y(0, z0 + dh * .45, 0.004), z0 + dh * .45))
    D0 = Vector((0, wall, z0))       # door sill centre: where the light comes from
    # light: floor spill, glow sprite, shafts (fx layer, additive)
    spill = mesh_obj('spill', [(-1.7, -2.1, 0), (1.7, -2.1, 0), (1.7, 2.1, 0), (-1.7, 2.1, 0)], [(0, 1, 2, 3)], 'fx',
                     additive((1.0, 0.84, 0.59), 'spill', 1.0, 'radial'))
    spill.data.uv_layers.new(); _uv_quad(spill)
    spill.location = (0, D0.y - 1.9, Y0 + .002); spill.parent = gate
    sprite = mesh_obj('glowsprite', [(-.8, 0, -.8), (.8, 0, -.8), (.8, 0, .8), (-.8, 0, .8)], [(0, 1, 2, 3)], 'fx',
                      additive((1.0, 0.94, 0.80), 'sprite', 1.2, 'radial'))
    sprite.data.uv_layers.new(); _uv_quad(sprite)
    sprite.location = (0, D0.y - .08, z0 + .3); sprite.parent = gate
    shafts = []
    return root, dict(body=body, merlons=merl, frame=surround, stairs=stairs, back=back, hinge=hinge, leaf=leaf, spill=spill,
                      sprite=sprite, shafts=shafts, door_c=door_c, gate=gate,
                      solids=[body, *merl, surround, stairs, sill, leaf, ringo])


def _uv_quad(ob):
    uv = ob.data.uv_layers[0].data
    for li, (u, v) in zip(range(4), ((0, 0), (1, 0), (1, 1), (0, 1))):
        uv[li].uv = (u, v)


def _rnd(seed):
    a = (int(seed) + 0x6D2B79F5) & 0xffffffff
    t = a
    t = ((t ^ (t >> 15)) * (t | 1)) & 0xffffffff
    t ^= (t + (((t ^ (t >> 7)) * (t | 61)) & 0xffffffff)) & 0xffffffff
    return ((t ^ (t >> 14)) & 0xffffffff) / 4294967296


def drafting_guides():
    """Rings, ticks and dashed axes from the original artwork, on the ground plane."""
    gy = -2.148
    obs = []
    def ring(r, color, alpha, dash=None):
        pts = [tuple(T(math.cos(i / 96 * 2 * math.pi) * r, gy, math.sin(i / 96 * 2 * math.pi) * r)) for i in range(96)]
        return guide_line('ring%.2f' % r, pts, color, alpha, closed=True, dash=dash)
    obs.append(ring(1.42, GUIDE, .27))
    obs.append(ring(1.78, GUIDE, .29, dash=(.065, .055)))
    obs.append(ring(2.15, GHOST, .14))
    tick = []
    for i in range(36):
        a = i / 36 * 2 * math.pi; r = 2.29 if i % 3 == 0 else 2.21
        tick.append((tuple(T(math.cos(a) * 2.15, gy, math.sin(a) * 2.15)), tuple(T(math.cos(a) * r, gy, math.sin(a) * r))))
    cu = bpy.data.curves.new('ticks', 'CURVE'); cu.dimensions = '3D'; cu.bevel_depth = .0045
    for a, b in tick:
        sp = cu.splines.new('POLY'); sp.points.add(1); sp.points[0].co = (*a, 1); sp.points[1].co = (*b, 1)
    t = bpy.data.objects.new('ticks', cu); bpy.data.collections['guides'].objects.link(t)
    cu.materials.append(additive(GHOST, 'guide_ticks', 1.0)); t.color = (1, 1, 1, .14); obs.append(t)
    obs.append(guide_line('axis_x', [tuple(T(-2.65, gy, 0)), tuple(T(2.65, gy, 0))], GUIDE, .29, dash=(.065, .055)))
    obs.append(guide_line('axis_z', [tuple(T(0, gy, -2.65)), tuple(T(0, gy, 2.65))], GUIDE, .29, dash=(.065, .055)))
    obs.append(guide_line('axis_y', [tuple(T(0, -2.5, 0)), tuple(T(0, 2.75, 0))], GUIDE, .29, dash=(.065, .055)))
    return obs


def grid_mat(name='portalgrid', cell=0.16, color=(0xd8 / 255, 0xad / 255, 0x6d / 255), k=1.3, near=0.5, far=14.0, origin=(0, 0, 0)):
    """Gold grid lines on a dark floor, glowing near the viewer and fading to the horizon."""
    m = bpy.data.materials.new(name); m.use_nodes = True
    nt = m.node_tree; nt.nodes.clear(); N, L = nt.nodes.new, nt.links.new
    out = N('ShaderNodeOutputMaterial'); em = N('ShaderNodeEmission')
    geo = N('ShaderNodeNewGeometry'); sep = N('ShaderNodeSeparateXYZ')
    sub = N('ShaderNodeVectorMath'); sub.operation = 'SUBTRACT'; sub.inputs[1].default_value = origin
    L(geo.outputs['Position'], sub.inputs[0]); L(sub.outputs[0], sep.inputs[0])
    def lm(sock, period, w):
        d = N('ShaderNodeMath'); d.operation = 'DIVIDE'; d.inputs[1].default_value = period; L(sock, d.inputs[0])
        p = N('ShaderNodeMath'); p.operation = 'PINGPONG'; p.inputs[1].default_value = .5; L(d.outputs[0], p.inputs[0])
        t = N('ShaderNodeMath'); t.operation = 'LESS_THAN'; t.inputs[1].default_value = w / period; L(p.outputs[0], t.inputs[0])
        return t.outputs[0]
    mx = N('ShaderNodeMath'); mx.operation = 'MAXIMUM'
    L(lm(sep.outputs[0], cell, cell * .012), mx.inputs[0]); L(lm(sep.outputs[1], cell, cell * .012), mx.inputs[1])
    mj = N('ShaderNodeMath'); mj.operation = 'MAXIMUM'
    L(lm(sep.outputs[0], cell * 5, cell * .022), mj.inputs[0]); L(lm(sep.outputs[1], cell * 5, cell * .022), mj.inputs[1])
    tot = N('ShaderNodeMath'); tot.operation = 'MULTIPLY_ADD'; tot.inputs[1].default_value = .55
    L(mx.outputs[0], tot.inputs[0]); L(mj.outputs[0], tot.inputs[2])
    ln = N('ShaderNodeVectorMath'); ln.operation = 'LENGTH'; L(sub.outputs[0], ln.inputs[0])
    fd = N('ShaderNodeMapRange'); fd.inputs['From Min'].default_value = near; fd.inputs['From Max'].default_value = far
    fd.inputs['To Min'].default_value = 1.0; fd.inputs['To Max'].default_value = 0.0
    L(ln.outputs['Value'], fd.inputs['Value'])
    a = N('ShaderNodeMath'); a.operation = 'MULTIPLY'; L(tot.outputs[0], a.inputs[0]); L(fd.outputs['Result'], a.inputs[1])
    mc = N('ShaderNodeMix'); mc.data_type = 'RGBA'
    mc.inputs['A'].default_value = (*lin((0.05, 0.045, 0.03)), 1)
    mc.inputs['B'].default_value = (*[c * k for c in lin(color)], 1)
    L(a.outputs[0], mc.inputs['Factor']); L(mc.outputs['Result'], em.inputs['Color']); L(em.outputs[0], out.inputs[0])
    return m


def portal_world(door_c, sill_z):
    """What you see through the door: the band's world, a gold grid floor running to a warm horizon,
    devices and agents scattered across it."""
    root = empty('portal_world')
    fl = mesh_obj('portal_floor', [(-40, -0.3, 0), (40, -0.3, 0), (40, 60, 0), (-40, 60, 0)], [(0, 1, 2, 3)], 'portalfx',
                  grid_mat(cell=0.45, k=0.6, near=0.0, far=22.0, origin=(1.125 + .225, door_c.y, sill_z)))
    fl.location = (0, door_c.y, sill_z - 0.001); fl.parent = root
    # horizon glow: a big warm disc far back
    sky = mesh_obj('portal_sky', [(-30, 0, -10), (30, 0, -10), (30, 0, 20), (-30, 0, 20)], [(0, 1, 2, 3)], 'portalfx',
                   additive((1.0, 0.80, 0.50), 'portal_sky', 0.7, 'radial'))
    sky.data.uv_layers.new(); _uv_quad(sky)
    sky.location = (0, door_c.y + 30, sill_z - 2); sky.parent = root
    # devices: toon boxes at a distance, agents as small gold hex cores
    pig = toon(PIGMENT, 'pigment')
    spots = [(-0.9, 2.2, 'box'), (0.8, 3.0, 'rack'), (-1.8, 4.6, 'lap'), (1.9, 5.4, 'box'), (0.1, 7.5, 'rack'),
             (-3.0, 8.0, 'lap'), (3.2, 9.0, 'box'), (-0.6, 11.0, 'box')]
    for i, (x, y, kind) in enumerate(spots):
        w, d, h = {'box': (.24, .3, .4), 'rack': (.4, .3, .26), 'lap': (.42, .3, .05)}[kind]
        b = kit.box('pdev%d' % i, (w, d, h), (x, door_c.y + y, sill_z + h / 2), coll='portal', fill=pig)
        b.rotation_euler.z = (i * 0.37) % 0.6 - 0.3
        b.parent = root
        if kind == 'lap':
            lid = kit.box('plid%d' % i, (w, .02, .28), (x, door_c.y + y + d / 2, sill_z + .16), coll='portal', fill=pig)
            lid.rotation_euler = (math.radians(-12), 0, b.rotation_euler.z); lid.parent = root
    for i, (x, y) in enumerate([(1.15, 2.3), (-1.3, 3.6), (1.4, 6.6), (-2.2, 10.0)]):
        h = mesh_obj('phex%d' % i, [(math.cos(a) * .09, math.sin(a) * .09, 0) for a in [k * math.pi / 3 for k in range(6)]],
                     [tuple(range(6))], 'portalfx', flat((1.0, 0.82, 0.48), 3.0, 'pcore'))
        h.location = (x, door_c.y + y, sill_z + .3); h.rotation_euler.x = math.radians(90); h.parent = root
        beam = mesh_obj('pbeam%d' % i, [(-.004, 0, 0), (.004, 0, 0), (.004, 0, .3), (-.004, 0, .3)], [(0, 1, 2, 3)],
                        'portalfx', flat((1.0, 0.82, 0.48), 1.6, 'pbeam'))
        beam.location = (x, door_c.y + y, sill_z); beam.parent = root
    return root


def ground(z=-2.151, size=60):
    g = mesh_obj('ground', [(-size, -size, z), (size, -size, z), (size, size, z), (-size, size, z)], [(0, 1, 2, 3)],
                 'ground', flat(PAGE, 1.0, 'page'))
    return g


def rook_camera(cam, drift=0.0, target=(0, .05, 0), fov=18, dist=17.2, elev=.2):
    base = Vector((math.sin(drift) * math.cos(elev), math.sin(elev), math.cos(drift) * math.cos(elev))) * dist
    cam.location = T(*base)
    cam.data.sensor_fit = 'VERTICAL'
    cam.data.angle_y = math.radians(fov)
    kit.look_at(cam, T(*target))
    return base
