"""Colours, fonts and the icons, drawn as shapes at any size (the bridge's
8x8 bitmaps are for a 128x64 OLED; the Deck gets real ones)."""

from __future__ import annotations

import math

import pygame

BG = (14, 17, 23)
PANEL = (24, 29, 38)
PANEL2 = (32, 39, 51)
LINE = (58, 68, 86)
TEXT = (226, 232, 240)
DIM = (140, 150, 168)
ACCENT = (64, 196, 180)
ACCENT2 = (240, 170, 70)
BAD = (230, 92, 92)
GOOD = (110, 200, 120)

ROUTE_COLORS = {"ir": (235, 110, 90), "rook": (64, 196, 180), "wifi": (110, 160, 240), "bt": (150, 130, 230)}

_fonts: dict = {}


def font(size: int, bold: bool = False) -> pygame.font.Font:
    k = (size, bold)
    if k not in _fonts:
        f = pygame.font.Font(None, size)
        f.set_bold(bold)
        _fonts[k] = f
    return _fonts[k]


def text(surf, s, pos, size=28, color=TEXT, bold=False, anchor="topleft", maxw=None):
    f = font(size, bold)
    if maxw is not None:
        while s and f.size(s)[0] > maxw:
            s = s[:-2] + "…" if len(s) > 2 else s[:-1]
    img = f.render(s, True, color)
    r = img.get_rect(**{anchor: pos})
    surf.blit(img, r)
    return r


def rounded(surf, rect, color, radius=12, width=0):
    pygame.draw.rect(surf, color, rect, width, border_radius=radius)


# -- icons: each draws in a square box (x, y, s) ----------------------------------------

def _tv(s, x, y, z, c):
    w = max(2, z // 14)
    pygame.draw.rect(s, c, (x + z * .08, y + z * .15, z * .84, z * .56), w, border_radius=int(z * .06))
    pygame.draw.line(s, c, (x + z * .5, y + z * .71), (x + z * .5, y + z * .82), w)
    pygame.draw.line(s, c, (x + z * .3, y + z * .85), (x + z * .7, y + z * .85), w)


def _projector(s, x, y, z, c):
    w = max(2, z // 14)
    pygame.draw.rect(s, c, (x + z * .06, y + z * .3, z * .88, z * .42), w, border_radius=int(z * .08))
    pygame.draw.circle(s, c, (x + z * .68, y + z * .51), z * .13, w)
    pygame.draw.line(s, c, (x + z * .2, y + z * .76), (x + z * .2, y + z * .86), w)
    pygame.draw.line(s, c, (x + z * .8, y + z * .76), (x + z * .8, y + z * .86), w)


def _speaker(s, x, y, z, c):
    w = max(2, z // 14)
    pygame.draw.polygon(s, c, [(x + z * .12, y + z * .38), (x + z * .3, y + z * .38), (x + z * .52, y + z * .18),
                               (x + z * .52, y + z * .82), (x + z * .3, y + z * .62), (x + z * .12, y + z * .62)])
    for r in (.2, .32):
        pygame.draw.arc(s, c, (x + z * (.5 - r), y + z * (.5 - r), z * 2 * r, z * 2 * r), -0.8, 0.8, w)


def _streaming(s, x, y, z, c):
    w = max(2, z // 14)
    pygame.draw.rect(s, c, (x + z * .06, y + z * .3, z * .88, z * .4), w, border_radius=int(z * .08))
    pygame.draw.polygon(s, c, [(x + z * .42, y + z * .4), (x + z * .42, y + z * .6), (x + z * .6, y + z * .5)])


def _light(s, x, y, z, c):
    w = max(2, z // 14)
    pygame.draw.circle(s, c, (x + z * .5, y + z * .4), z * .24, w)
    pygame.draw.rect(s, c, (x + z * .38, y + z * .64, z * .24, z * .16), w)


def _phone(s, x, y, z, c):
    w = max(2, z // 14)
    pygame.draw.rect(s, c, (x + z * .3, y + z * .1, z * .4, z * .8), w, border_radius=int(z * .07))
    pygame.draw.circle(s, c, (x + z * .5, y + z * .78), max(2, z * .04))


def _car(s, x, y, z, c):
    w = max(2, z // 14)
    pygame.draw.rect(s, c, (x + z * .08, y + z * .45, z * .84, z * .25), w, border_radius=int(z * .06))
    pygame.draw.polygon(s, c, [(x + z * .24, y + z * .45), (x + z * .34, y + z * .26), (x + z * .66, y + z * .26),
                               (x + z * .76, y + z * .45)], w)
    for cx in (.28, .72):
        pygame.draw.circle(s, c, (x + z * cx, y + z * .74), z * .08)


def _game(s, x, y, z, c):
    w = max(2, z // 14)
    pygame.draw.rect(s, c, (x + z * .08, y + z * .3, z * .84, z * .42), w, border_radius=int(z * .2))
    pygame.draw.line(s, c, (x + z * .22, y + z * .51), (x + z * .38, y + z * .51), w)
    pygame.draw.line(s, c, (x + z * .3, y + z * .43), (x + z * .3, y + z * .59), w)
    pygame.draw.circle(s, c, (x + z * .66, y + z * .46), max(2, z * .04))
    pygame.draw.circle(s, c, (x + z * .74, y + z * .56), max(2, z * .04))


def _generic(s, x, y, z, c):
    w = max(2, z // 14)
    pygame.draw.rect(s, c, (x + z * .15, y + z * .15, z * .7, z * .7), w, border_radius=int(z * .1))
    pygame.draw.circle(s, c, (x + z * .5, y + z * .5), z * .12)


def _plus(s, x, y, z, c):
    w = max(3, z // 9)
    pygame.draw.line(s, c, (x + z * .5, y + z * .2), (x + z * .5, y + z * .8), w)
    pygame.draw.line(s, c, (x + z * .2, y + z * .5), (x + z * .8, y + z * .5), w)


def _ir(s, x, y, z, c):
    """An LED: a dome with two legs, and its rays."""
    w = max(2, z // 14)
    pygame.draw.circle(s, c, (x + z * .5, y + z * .52), z * .18, 0, draw_top_left=True, draw_top_right=True)
    pygame.draw.rect(s, c, (x + z * .32, y + z * .52, z * .36, z * .16))
    pygame.draw.line(s, c, (x + z * .42, y + z * .68), (x + z * .42, y + z * .9), w)
    pygame.draw.line(s, c, (x + z * .58, y + z * .68), (x + z * .58, y + z * .84), w)
    for a in (-60, -90, -120):
        r = math.radians(a)
        pygame.draw.line(s, c, (x + z * .5 + math.cos(r) * z * .26, y + z * .4 + math.sin(r) * z * .26),
                         (x + z * .5 + math.cos(r) * z * .38, y + z * .4 + math.sin(r) * z * .38), w)


def _rook(s, x, y, z, c):
    """The chess rook (IC_ROOK's shape)."""
    pts = [(.2, .12), (.32, .12), (.32, .22), (.44, .22), (.44, .12), (.56, .12), (.56, .22), (.68, .22), (.68, .12),
           (.8, .12), (.8, .34), (.68, .42), (.68, .72), (.82, .8), (.82, .9), (.18, .9), (.18, .8), (.32, .72),
           (.32, .42), (.2, .34)]
    pygame.draw.polygon(s, c, [(x + px * z, y + py * z) for px, py in pts])


def _wifi(s, x, y, z, c):
    w = max(2, z // 11)
    for r in (.42, .3, .18):
        pygame.draw.arc(s, c, (x + z * (.5 - r), y + z * (.72 - r), z * 2 * r, z * 2 * r), math.radians(45),
                        math.radians(135), w)
    pygame.draw.circle(s, c, (x + z * .5, y + z * .74), max(2, z * .06))


def _bt(s, x, y, z, c):
    w = max(2, z // 12)
    pts = [(.3, .3), (.7, .68), (.5, .86), (.5, .14), (.7, .32), (.3, .7)]
    pygame.draw.lines(s, c, False, [(x + px * z, y + py * z) for px, py in pts], w)


ICONS = {"tv": _tv, "projector": _projector, "speaker": _speaker, "streaming": _streaming, "light": _light,
         "phone": _phone, "car": _car, "game": _game, "generic": _generic, "plus": _plus,
         "ir": _ir, "rook": _rook, "wifi": _wifi, "bt": _bt}


def icon(surf, name, x, y, size, color=TEXT, struck=False):
    """An icon in the square (x, y, size); struck: a slash across it (the hub not there, IR unavailable)."""
    ICONS.get(name or "generic", _generic)(surf, x, y, size, color)
    if struck:
        w = max(3, size // 10)
        pygame.draw.line(surf, BG, (x + size * .12, y + size * .88), (x + size * .88, y + size * .12), w + 4)
        pygame.draw.line(surf, BAD, (x + size * .12, y + size * .88), (x + size * .88, y + size * .12), w)


# Glyphs the default font has no shape for (arrows, the media keys), drawn.
def glyph(surf, name, center, size, color):
    """Draws name ("up", "down", "left", "right", "play", "pause", "stop", "rewind", "forward", "prev", "next",
    "more") centred at center, about size tall. False: no such glyph (the caller writes it out)."""
    cx, cy = center
    h = size / 2

    def tri(x, y, d):         # a triangle pointing d (1: right, -1: left), at x
        pygame.draw.polygon(surf, color, [(x - d * h * .7, y - h * .8), (x - d * h * .7, y + h * .8), (x + d * h * .7, y)])
    if name in ("up", "down"):
        d = -1 if name == "up" else 1
        pygame.draw.polygon(surf, color, [(cx - h * .8, cy - d * h * .5), (cx + h * .8, cy - d * h * .5), (cx, cy + d * h * .7)])
    elif name in ("left", "right"):
        tri(cx, cy, 1 if name == "right" else -1)
    elif name == "play":
        tri(cx, cy, 1)
    elif name == "pause":
        for dx in (-.45, .45):
            pygame.draw.rect(surf, color, (cx + dx * h - h * .2, cy - h * .8, h * .4, h * 1.6))
    elif name == "stop":
        pygame.draw.rect(surf, color, (cx - h * .7, cy - h * .7, h * 1.4, h * 1.4))
    elif name in ("rewind", "forward"):
        d = 1 if name == "forward" else -1
        tri(cx - d * h * .55, cy, d)
        tri(cx + d * h * .55, cy, d)
    elif name in ("prev", "next"):
        d = 1 if name == "next" else -1
        tri(cx - d * h * .25, cy, d)
        pygame.draw.rect(surf, color, (cx + d * h * .55 - h * .12, cy - h * .8, h * .24, h * 1.6))
    elif name == "more":
        for dx in (-1, 0, 1):
            pygame.draw.circle(surf, color, (cx + dx * h * .6, cy), max(2, h * .18))
    else:
        return False
    return True


ROUTE_ORDER = [(1, "ir"), (2, "rook"), (4, "wifi"), (8, "bt")]


def route_row(surf, x, y, size, prof_icon, routes, color=TEXT, gap=None, ir_unavailable=True):
    """The profile's icon, then one per route its keys take (IR, Rook, Wi-Fi, BT). IR is struck through:
    the Deck has no IR LED. Answers the width drawn."""
    gap = gap if gap is not None else size // 4
    cx = x
    if prof_icon is not None:
        icon(surf, prof_icon, cx, y, size, color)
        cx += size + gap * 2
    for bit, name in ROUTE_ORDER:
        if routes & bit:
            icon(surf, name, cx, y, size, ROUTE_COLORS[name], struck=(name == "ir" and ir_unavailable))
            cx += size + gap
    return cx - x
