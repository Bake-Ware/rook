"""Flat UI pieces drawn in px space (y down): fading emission materials, rounded panels,
text, lines and discs. Used for the HUD, cards, chips, bubbles, captions and the chat box."""
import math, os
import bpy
from mathutils import Vector
from look import lin

HERE = os.path.dirname(os.path.abspath(__file__))
FONTS = os.path.join(HERE, 'fonts')
FACES = {'mono': 'DejaVuSansMono.ttf', 'mono_b': 'DejaVuSansMono-Bold.ttf', 'black': 'InterDisplay-Black.otf',
         'semi': 'InterDisplay-SemiBold.otf', 'reg': 'InterDisplay-Regular.otf', 'sans_b': 'DejaVuSans-Bold.ttf'}
_fonts = {}


def hx(h):
    return ((h >> 16 & 255) / 255, (h >> 8 & 255) / 255, (h & 255) / 255)


FONT_DIRS = [FONTS, '/usr/share/fonts/TTF', '/usr/share/fonts/truetype/dejavu', '/usr/share/fonts/dejavu',
             '/usr/share/fonts/opentype/inter']


def font(face):
    if face not in _fonts:
        for d in FONT_DIRS:
            p = os.path.join(d, FACES[face])
            if os.path.exists(p):
                _fonts[face] = bpy.data.fonts.load(p)
                break
        else:
            raise FileNotFoundError(FACES[face])
    return _fonts[face]


_m = {}


def ui(color, k=1.0):
    """Emission that fades by the object's colour alpha (object.color[3])."""
    key = ('ui', tuple(round(c, 4) for c in color), k)
    if key in _m:
        return _m[key]
    m = bpy.data.materials.new('ui_%d' % len(_m)); m.use_nodes = True
    nt = m.node_tree; nt.nodes.clear(); N, L = nt.nodes.new, nt.links.new
    out = N('ShaderNodeOutputMaterial'); em = N('ShaderNodeEmission'); tr = N('ShaderNodeBsdfTransparent')
    em.inputs['Color'].default_value = (*lin(color), 1); em.inputs['Strength'].default_value = k
    oi = N('ShaderNodeObjectInfo'); mx = N('ShaderNodeMixShader')
    L(oi.outputs['Alpha'], mx.inputs['Fac']); L(tr.outputs[0], mx.inputs[1]); L(em.outputs[0], mx.inputs[2])
    L(mx.outputs[0], out.inputs[0])
    _m[key] = m
    return m


def radial(color, k=1.0, power=1.0):
    """A soft disc on a unit quad (uv 0..1): colour fading to nothing at the edge, times object alpha."""
    key = ('radial', tuple(color), k, power)
    if key in _m:
        return _m[key]
    m = bpy.data.materials.new('radial_%d' % len(_m)); m.use_nodes = True
    nt = m.node_tree; nt.nodes.clear(); N, L = nt.nodes.new, nt.links.new
    out = N('ShaderNodeOutputMaterial'); em = N('ShaderNodeEmission'); tr = N('ShaderNodeBsdfTransparent')
    em.inputs['Color'].default_value = (*lin(color), 1); em.inputs['Strength'].default_value = k
    tc = N('ShaderNodeTexCoord'); sub = N('ShaderNodeVectorMath'); sub.operation = 'SUBTRACT'
    sub.inputs[1].default_value = (.5, .5, 0); L(tc.outputs['UV'], sub.inputs[0])
    ln = N('ShaderNodeVectorMath'); ln.operation = 'LENGTH'; L(sub.outputs[0], ln.inputs[0])
    mr = N('ShaderNodeMapRange'); mr.interpolation_type = 'SMOOTHSTEP'
    mr.inputs['From Min'].default_value = .5; mr.inputs['From Max'].default_value = 0.0
    L(ln.outputs['Value'], mr.inputs['Value'])
    pw = N('ShaderNodeMath'); pw.operation = 'POWER'; pw.inputs[1].default_value = power; L(mr.outputs['Result'], pw.inputs[0])
    oi = N('ShaderNodeObjectInfo'); mm = N('ShaderNodeMath'); mm.operation = 'MULTIPLY'
    L(pw.outputs[0], mm.inputs[0]); L(oi.outputs['Alpha'], mm.inputs[1])
    mx = N('ShaderNodeMixShader'); L(mm.outputs[0], mx.inputs['Fac']); L(tr.outputs[0], mx.inputs[1]); L(em.outputs[0], mx.inputs[2])
    L(mx.outputs[0], out.inputs[0])
    _m[key] = m
    return m


def alpha(ob, a):
    a = max(0.0, min(1.0, a))
    ob.color = (1, 1, 1, a)
    ob.hide_render = a < 0.004
    return a


def _obj(name, coll, mat, parent):
    me = bpy.data.meshes.new(name)
    ob = bpy.data.objects.new(name, me)
    bpy.data.collections[coll].objects.link(ob)
    me.materials.append(mat)
    ob.parent = parent
    ob.color = (1, 1, 1, 1)
    return ob


def rr_pts(x, y, w, h, r, seg=6):
    r = max(0.0, min(r, w / 2, h / 2))
    if r <= 0.01:
        return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    pts = []
    for cx, cy, a0 in ((x + w - r, y + r, -90), (x + w - r, y + h - r, 0), (x + r, y + h - r, 90), (x + r, y + r, 180)):
        for k in range(seg + 1):
            a = math.radians(a0 + 90 * k / seg)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


class Panel:
    """A rounded rectangle with an optional border, in px (y down)."""
    def __init__(self, name, parent, fill, fa=1.0, border=None, bw=2.0, r=12, z=0.0, coll='text'):
        self.fa, self.bw, self.r, self.z = fa, bw, r, z
        self.f = _obj(name + '_fill', coll, ui(fill), parent) if fill is not None else None
        self.b = _obj(name + '_line', coll, ui(border), parent) if border is not None else None
        self.key = None

    def hide(self):
        for o in (self.f, self.b):
            if o: o.hide_render = True

    def set(self, x, y, w, h, a=1.0):
        if a <= 0.004 or w <= 0 or h <= 0:
            self.hide(); return
        key = (round(x, 2), round(y, 2), round(w, 2), round(h, 2))
        if key != self.key:
            self.key = key
            outer = rr_pts(x, y, w, h, self.r)
            if self.f:
                me = self.f.data; me.clear_geometry()
                me.from_pydata([(px, -py, self.z) for px, py in outer], [], [tuple(range(len(outer)))]); me.update()
            if self.b:
                bw = self.bw
                inner = rr_pts(x + bw, y + bw, w - 2 * bw, h - 2 * bw, max(0.0, self.r - bw))
                if len(inner) != len(outer):
                    inner = rr_pts(x + bw, y + bw, w - 2 * bw, h - 2 * bw, max(0.02, self.r - bw)) if self.r > 0.01 else inner
                n = len(outer)
                vs = [(px, -py, self.z + .3) for px, py in outer] + [(px, -py, self.z + .3) for px, py in inner]
                fs = [(i, (i + 1) % n, n + (i + 1) % n, n + i) for i in range(n)]
                me = self.b.data; me.clear_geometry(); me.from_pydata(vs, [], fs); me.update()
        if self.f: alpha(self.f, 1 - (1 - self.fa * a) ** 2.2)   # css alpha (sRGB blend) as linear-light coverage
        if self.b: alpha(self.b, a)


def text(name, parent, size, color, face='mono', x=0, y=0, align='LEFT', valign='TOP_BASELINE', accent=None,
         cursor=None, coll='text', z=1.0, body=''):
    cu = bpy.data.curves.new(name, 'FONT')
    cu.body = body; cu.font = font(face); cu.size = size
    cu.align_x = align; cu.align_y = valign
    ob = bpy.data.objects.new(name, cu)
    bpy.data.collections[coll].objects.link(ob)
    cu.materials.append(ui(color)); cu.materials.append(ui(accent or color)); cu.materials.append(ui(cursor or color))
    ob.parent = parent
    ob.location = (x, -y, z)
    ob.color = (1, 1, 1, 1)
    return ob


def set_text(ob, body, ranges=(), cursor_at=None, color=None, accent=None, cursor=None):
    """ranges: [(a, b)] in accent; cursor_at: index of the cursor glyph."""
    if ob.data.body != body:
        ob.data.body = body
    if color is not None: ob.data.materials[0] = ui(color)
    if accent is not None: ob.data.materials[1] = ui(accent)
    if cursor is not None: ob.data.materials[2] = ui(cursor)
    bf = ob.data.body_format
    for ch in bf:
        ch.material_index = 0
    for a, b in ranges:
        for i in range(max(0, a), min(b, len(bf))):
            bf[i].material_index = 1
    if cursor_at is not None and 0 <= cursor_at < len(bf):
        bf[cursor_at].material_index = 2


def measure(ob):
    """Width of a text object in its own units."""
    bpy.context.view_layer.update()
    return ob.dimensions.x / max(1e-9, abs(ob.scale.x))


def line(name, parent, pts, width, color, k=1.0, coll='text', mat=None):
    cu = bpy.data.curves.new(name, 'CURVE'); cu.dimensions = '3D'
    cu.bevel_depth = width / 2; cu.bevel_resolution = 1
    sp = cu.splines.new('POLY'); sp.points.add(len(pts) - 1)
    for i, p in enumerate(pts):
        sp.points[i].co = (p[0], -p[1], p[2] if len(p) > 2 else 0.5, 1)
    ob = bpy.data.objects.new(name, cu)
    bpy.data.collections[coll].objects.link(ob)
    cu.materials.append(mat or ui(color, k))
    ob.parent = parent; ob.color = (1, 1, 1, 1)
    return ob


def set_line(ob, pts, width=None):
    sp = ob.data.splines[0]
    for i, p in enumerate(pts):
        sp.points[i].co = (p[0], -p[1], p[2] if len(p) > 2 else 0.5, 1)
    if width is not None:
        ob.data.bevel_depth = width / 2


def disc(name, parent, r, color, k=1.0, coll='text', segs=20, mat=None):
    ob = _obj(name, coll, mat or ui(color, k), parent)
    ob.data.from_pydata([(r * math.cos(2 * math.pi * i / segs), r * math.sin(2 * math.pi * i / segs), 0) for i in range(segs)],
                        [], [tuple(range(segs))])
    return ob


def quad(name, parent, w, h, mat, coll='text'):
    """A unit-uv quad, centred, w x h px."""
    ob = _obj(name, coll, mat, parent)
    me = ob.data
    me.from_pydata([(-w / 2, -h / 2, 0), (w / 2, -h / 2, 0), (w / 2, h / 2, 0), (-w / 2, h / 2, 0)], [], [(0, 1, 2, 3)])
    uv = me.uv_layers.new()
    for li, (u, v) in enumerate(((0, 0), (1, 0), (1, 1), (0, 1))):
        uv.data[li].uv = (u, v)
    return ob
