import math
import struct
import json
import os
from typing import List, Optional, Tuple, Dict

from debug_trace import TRACE


D3D11_FILTER_MIN_MAG_LINEAR_MIP_POINT = 0x10

D3D11_TEXTURE_ADDRESS_WRAP = 1
D3D11_TEXTURE_ADDRESS_MIRROR = 2
D3D11_TEXTURE_ADDRESS_CLAMP = 3
D3D11_TEXTURE_ADDRESS_BORDER = 4
D3D11_TEXTURE_ADDRESS_MIRROR_ONCE = 5

D3D11_COMPARISON_NEVER = 0
D3D11_COMPARISON_LESS = 1
D3D11_COMPARISON_EQUAL = 2
D3D11_COMPARISON_LESS_EQUAL = 3
D3D11_COMPARISON_GREATER = 4
D3D11_COMPARISON_NOT_EQUAL = 5
D3D11_COMPARISON_GREATER_EQUAL = 6
D3D11_COMPARISON_ALWAYS = 7

FILTER_MAP = {
    "D3D11_FILTER_MIN_MAG_LINEAR_MIP_POINT": 0x10,
    "D3D11_FILTER_MIN_MAG_MIP_LINEAR": 0x15,
    "D3D11_FILTER_MIN_MAG_POINT_MIP_LINEAR": 0x14,
    "D3D11_FILTER_MIN_POINT_MAG_LINEAR_MIP_POINT": 0x11,
}

ADDRESS_MAP = {
    "D3D11_TEXTURE_ADDRESS_WRAP": 1,
    "D3D11_TEXTURE_ADDRESS_MIRROR": 2,
    "D3D11_TEXTURE_ADDRESS_CLAMP": 3,
    "D3D11_TEXTURE_ADDRESS_BORDER": 4,
    "D3D11_TEXTURE_ADDRESS_MIRROR_ONCE": 5,
}


# ---------------------------------------------------------------------------
# BC7 block decoder (D3D11 / BPTC spec). Needed because captures dump
# BC7-compressed volumes/textures as raw .img data whose BMP fallback is
# 24-bit (alpha lost) and 2D-only (sekiro4's 40x8x76 irradiance volumes).
# Tables are the canonical BPTC partition/anchor tables; decode validated
# texel-exact against RenderDoc's BMP conversions of the same resources.
# ---------------------------------------------------------------------------

_BC7_PART2 = [
    [0,0,1,1,0,0,1,1,0,0,1,1,0,0,1,1], [0,0,0,1,0,0,0,1,0,0,0,1,0,0,0,1],
    [0,1,1,1,0,1,1,1,0,1,1,1,0,1,1,1], [0,0,0,1,0,0,1,1,0,0,1,1,0,1,1,1],
    [0,0,0,0,0,0,0,1,0,0,0,1,0,0,1,1], [0,0,1,1,0,1,1,1,0,1,1,1,1,1,1,1],
    [0,0,0,1,0,0,1,1,0,1,1,1,1,1,1,1], [0,0,0,0,0,0,0,1,0,0,1,1,0,1,1,1],
    [0,0,0,0,0,0,0,0,0,0,0,1,0,0,1,1], [0,0,1,1,0,1,1,1,1,1,1,1,1,1,1,1],
    [0,0,0,0,0,0,0,1,0,1,1,1,1,1,1,1], [0,0,0,0,0,0,0,0,0,0,0,1,0,1,1,1],
    [0,0,0,1,0,1,1,1,1,1,1,1,1,1,1,1], [0,0,0,0,0,0,0,0,1,1,1,1,1,1,1,1],
    [0,0,0,0,1,1,1,1,1,1,1,1,1,1,1,1], [0,0,0,0,0,0,0,0,0,0,0,0,1,1,1,1],
    [0,0,0,0,1,0,0,0,1,1,1,0,1,1,1,1], [0,1,1,1,0,0,0,1,0,0,0,0,0,0,0,0],
    [0,0,0,0,0,0,0,0,1,0,0,0,1,1,1,0], [0,1,1,1,0,0,1,1,0,0,0,1,0,0,0,0],
    [0,0,1,1,0,0,0,1,0,0,0,0,0,0,0,0], [0,0,0,0,1,0,0,0,1,1,0,0,1,1,1,0],
    [0,0,0,0,0,0,0,0,1,0,0,0,1,1,0,0], [0,1,1,1,0,0,1,1,0,0,1,1,0,0,0,1],
    [0,0,1,1,0,0,0,1,0,0,0,1,0,0,0,0], [0,0,0,0,1,0,0,0,1,0,0,0,1,1,0,0],
    [0,1,1,0,0,1,1,0,0,1,1,0,0,1,1,0], [0,0,1,1,0,1,1,0,0,1,1,0,1,1,0,0],
    [0,0,0,1,0,1,1,1,1,1,1,0,1,0,0,0], [0,0,0,0,1,1,1,1,1,1,1,1,0,0,0,0],
    [0,1,1,1,0,0,0,1,1,0,0,0,1,1,1,0], [0,0,1,1,1,0,1,1,1,1,0,1,1,1,0,0],
    [0,1,0,1,0,1,0,1,0,1,0,1,0,1,0,1], [0,0,0,0,1,1,1,1,0,0,0,0,1,1,1,1],
    [0,1,0,1,1,0,1,0,0,1,0,1,1,0,1,0], [0,0,1,1,0,0,1,1,1,1,0,0,1,1,0,0],
    [0,0,1,1,1,1,0,0,0,0,1,1,1,1,0,0], [0,1,0,1,0,1,0,1,1,0,1,0,1,0,1,0],
    [0,1,1,0,1,0,0,1,0,1,1,0,1,0,0,1], [0,1,0,1,1,0,1,0,1,0,1,0,0,1,0,1],
    [0,1,1,1,0,0,1,1,1,1,0,0,1,1,1,0], [0,0,0,1,0,0,1,1,1,1,0,0,1,0,0,0],
    [0,0,1,1,0,0,1,0,0,1,0,0,1,1,0,0], [0,0,1,1,1,0,1,1,1,1,0,1,1,1,0,0],
    [0,1,1,0,1,0,0,1,1,0,0,1,0,1,1,0], [0,0,1,1,1,1,0,0,1,1,0,0,0,0,1,1],
    [0,1,1,0,0,1,1,0,1,0,0,1,1,0,0,1], [0,0,0,0,0,1,1,0,0,1,1,0,0,0,0,0],
    [0,1,0,0,1,1,1,0,0,1,0,0,0,0,0,0], [0,0,1,0,0,1,1,1,0,0,1,0,0,0,0,0],
    [0,0,0,0,0,0,1,0,0,1,1,1,0,0,1,0], [0,0,0,0,0,1,0,0,1,1,1,0,0,1,0,0],
    [0,1,1,0,1,1,0,0,1,0,0,1,0,0,1,1], [0,0,1,1,0,1,1,0,1,1,0,0,1,0,0,1],
    [0,1,1,0,0,0,1,1,1,0,0,1,1,1,0,0], [0,0,1,1,1,0,0,1,1,1,0,0,0,1,1,0],
    [0,1,1,0,1,1,0,0,1,1,0,0,1,0,0,1], [0,1,1,0,0,0,1,1,0,0,1,1,1,0,0,1],
    [0,1,1,1,1,1,1,0,1,0,0,0,0,0,0,1], [0,0,0,1,1,0,0,0,1,1,1,0,0,1,1,1],
    [0,0,0,0,1,1,1,1,0,0,1,1,0,0,1,1], [0,0,1,1,0,0,1,1,1,1,1,1,0,0,0,0],
    [0,0,1,0,0,0,1,0,1,1,1,0,1,1,1,0], [0,1,0,0,0,1,0,0,0,1,1,1,0,1,1,1],
]

_BC7_PART3 = [
    [0,0,1,1,0,0,1,1,0,2,2,1,2,2,2,2], [0,0,0,1,0,0,1,1,2,2,1,1,2,2,2,1],
    [0,0,0,0,2,0,0,1,2,2,1,1,2,2,1,1], [0,2,2,2,0,0,2,2,0,0,1,1,0,1,1,1],
    [0,0,0,0,0,0,0,0,1,1,2,2,1,1,2,2], [0,0,1,1,0,0,1,1,0,0,2,2,0,0,2,2],
    [0,0,2,2,0,0,2,2,1,1,1,1,1,1,1,1], [0,0,1,1,0,0,1,1,2,2,1,1,2,2,1,1],
    [0,0,0,0,0,0,0,0,1,1,1,1,2,2,2,2], [0,0,0,0,1,1,1,1,1,1,1,1,2,2,2,2],
    [0,0,0,0,1,1,1,1,2,2,2,2,2,2,2,2], [0,0,1,2,0,0,1,2,0,0,1,2,0,0,1,2],
    [0,1,1,2,0,1,1,2,0,1,1,2,0,1,1,2], [0,1,2,2,0,1,2,2,0,1,2,2,0,1,2,2],
    [0,0,1,1,0,1,1,2,1,1,2,2,1,2,2,2], [0,0,1,1,2,0,0,1,2,2,0,0,2,2,2,0],
    [0,0,0,1,0,0,1,1,0,1,1,2,1,1,2,2], [0,1,1,1,0,0,1,1,2,0,0,1,2,2,0,0],
    [0,0,0,0,1,1,2,2,1,1,2,2,1,1,2,2], [0,0,2,2,0,0,2,2,0,0,2,2,1,1,1,1],
    [0,1,1,1,0,1,1,1,0,2,2,2,0,2,2,2], [0,0,0,1,0,0,0,1,2,2,2,1,2,2,2,1],
    [0,0,0,0,0,0,1,1,0,1,2,2,0,1,2,2], [0,0,0,0,1,1,0,0,2,2,1,0,2,2,1,0],
    [0,1,2,2,0,1,2,2,0,0,1,1,0,0,0,0], [0,0,1,2,0,0,1,2,1,1,2,2,2,2,2,2],
    [0,1,1,0,1,2,2,1,1,2,2,1,0,1,1,0], [0,0,0,0,0,1,1,0,1,2,2,1,1,2,2,1],
    [0,0,2,2,1,1,0,2,1,1,0,2,0,0,2,2], [0,1,1,0,0,1,1,0,2,0,0,2,2,2,2,2],
    [0,0,1,1,0,1,2,2,0,1,2,2,0,0,1,1], [0,0,0,0,2,0,0,0,2,2,1,1,2,2,2,1],
    [0,0,0,0,0,0,0,2,1,1,2,2,1,2,2,2], [0,2,2,2,0,0,2,2,0,0,1,2,0,0,1,1],
    [0,0,1,1,0,0,1,2,0,0,2,2,0,2,2,2], [0,1,2,0,0,1,2,0,0,1,2,0,0,1,2,0],
    [0,0,0,0,1,1,1,1,2,2,2,2,0,0,0,0], [0,1,2,0,1,2,0,1,2,0,1,2,0,1,2,0],
    [0,1,2,0,2,0,1,2,1,2,0,1,0,1,2,0], [0,0,1,1,2,2,0,0,1,1,2,2,0,0,1,1],
    [0,0,1,1,1,1,2,2,2,2,0,0,0,0,1,1], [0,1,0,1,0,1,0,1,2,2,2,2,2,2,2,2],
    [0,0,0,0,0,0,0,0,2,1,2,1,2,1,2,1], [0,0,2,2,1,1,2,2,0,0,2,2,1,1,2,2],
    [0,0,2,2,0,0,1,1,0,0,2,2,0,0,1,1], [0,2,2,0,1,2,2,1,0,2,2,0,1,2,2,1],
    [0,1,0,1,2,2,2,2,2,2,2,2,0,1,0,1], [0,0,0,0,2,1,2,1,2,1,2,1,2,1,2,1],
    [0,1,0,1,0,1,0,1,0,1,0,1,2,2,2,2], [0,2,2,2,0,1,1,1,0,2,2,2,0,1,1,1],
    [0,0,0,2,1,1,1,2,0,0,0,2,1,1,1,2], [0,0,0,0,2,1,1,2,2,1,1,2,2,1,1,2],
    [0,2,2,2,0,1,1,1,0,1,1,1,0,2,2,2], [0,0,0,2,1,1,1,2,1,1,1,2,0,0,0,2],
    [0,1,1,0,0,1,1,0,0,1,1,0,2,2,2,2], [0,0,0,0,0,0,0,0,2,1,1,2,2,1,1,2],
    [0,1,1,0,0,1,1,0,2,2,2,2,2,2,2,2], [0,0,2,2,0,0,1,1,0,0,1,1,0,0,2,2],
    [0,0,2,2,1,1,2,2,1,1,2,2,0,0,2,2], [0,0,0,0,0,0,0,0,0,0,0,0,2,1,1,2],
    [0,0,0,2,0,0,0,1,0,0,0,2,0,0,0,1], [0,2,2,2,1,2,2,2,0,2,2,2,1,2,2,2],
    [0,1,0,1,2,2,2,2,2,2,2,2,2,2,2,2], [0,1,1,1,2,0,1,1,2,2,0,1,2,2,2,0],
]

_BC7_ANCHOR2 = [
    15,15,15,15,15,15,15,15,15,15,15,15,15,15,15,15,
    15, 2, 8, 2, 2, 8, 8,15, 2, 8, 2, 2, 8, 8, 2, 2,
    15,15, 6, 8, 2, 8,15,15, 2, 8, 2, 2, 2,15,15, 6,
     6, 2, 6, 8,15,15, 2, 2,15,15,15,15,15, 2, 2,15,
]
_BC7_ANCHOR3_1 = [
     3, 3,15,15, 8, 3,15,15, 8, 8, 6, 6, 6, 5, 3, 3,
     3, 3, 8,15, 3, 3, 6,10, 5, 8, 8, 6, 8, 5,15,15,
     8,15, 3, 5, 6,10, 8,15,15, 3,15, 5,15,15,15,15,
     3,15, 5, 5, 5, 8, 5,10, 5,10, 8,13,15,12, 3, 3,
]
_BC7_ANCHOR3_2 = [
    15, 8, 8, 3,15,15, 3, 8,15,15,15,15,15,15,15, 8,
    15, 8,15, 3,15, 8,15, 8, 3,15, 6,10,15,15,10, 8,
    15, 3,15,10,10, 8, 9,10, 6,15, 8,15, 3, 6, 6, 8,
    15, 3,15,15,15,15,15,15,15,15,15,15, 3,15,15, 8,
]

_BC7_WEIGHTS = {
    2: (0, 21, 43, 64),
    3: (0, 9, 18, 27, 37, 46, 55, 64),
    4: (0, 4, 9, 13, 17, 21, 26, 30, 34, 38, 43, 47, 51, 55, 60, 64),
}

# mode -> (subsets, partition_bits, rotation_bits, index_sel_bit,
#          color_bits, alpha_bits, pbit_mode, index_bits, index2_bits)
# pbit_mode: 0 = none, 1 = per-endpoint, 2 = shared per subset
_BC7_MODES = {
    0: (3, 4, 0, 0, 4, 0, 1, 3, 0),
    1: (2, 6, 0, 0, 6, 0, 2, 3, 0),
    2: (3, 6, 0, 0, 5, 0, 0, 2, 0),
    3: (2, 6, 0, 0, 7, 0, 1, 2, 0),
    4: (1, 0, 2, 1, 5, 6, 0, 2, 3),
    5: (1, 0, 2, 0, 7, 8, 0, 2, 2),
    6: (1, 0, 0, 0, 7, 7, 1, 4, 0),
    7: (2, 6, 0, 0, 5, 5, 1, 2, 0),
}


def _bc7_decode_block(block: bytes):
    """Decode one 16-byte BC7 block into a list of 16 (r, g, b, a) tuples
    (0-255 ints), texels in row-major order. All-zero mode byte (reserved)
    decodes to transparent black per spec."""
    bits = int.from_bytes(block, 'little')
    pos = 0

    def read(n):
        nonlocal pos
        v = (bits >> pos) & ((1 << n) - 1)
        pos += n
        return v

    mode = 0
    while mode < 8 and not (bits >> mode) & 1:
        mode += 1
    if mode == 8:
        return [(0, 0, 0, 0)] * 16
    pos = mode + 1

    (subsets, part_bits, rot_bits, sel_bits,
     cbits, abits, pmode, ibits, i2bits) = _BC7_MODES[mode]

    partition = read(part_bits) if part_bits else 0
    rotation = read(rot_bits) if rot_bits else 0
    index_sel = read(sel_bits) if sel_bits else 0

    nep = subsets * 2
    # Endpoints are stored component-major: all R, then G, then B[, then A].
    ep = [[0, 0, 0, 255] for _ in range(nep)]
    for c in range(3):
        for e in range(nep):
            ep[e][c] = read(cbits)
    if abits:
        for e in range(nep):
            ep[e][3] = read(abits)

    pbits = []
    if pmode == 1:
        pbits = [read(1) for _ in range(nep)]
    elif pmode == 2:
        shared = [read(1) for _ in range(subsets)]
        pbits = [shared[e // 2] for e in range(nep)]

    # Expand endpoints to 8 bits (append pbit, then replicate top bits).
    for e in range(nep):
        for c in range(4):
            n = cbits if c < 3 else abits
            if c == 3 and not abits:
                continue
            v = ep[e][c]
            if pbits:
                v = (v << 1) | pbits[e]
                n += 1
            v = (v << (8 - n))
            v |= v >> n
            ep[e][c] = v & 0xFF

    # Subset assignment + anchor texels.
    if subsets == 1:
        assign = [0] * 16
        anchors = {0: 0}
    elif subsets == 2:
        assign = _BC7_PART2[partition]
        anchors = {0: 0, 1: _BC7_ANCHOR2[partition]}
    else:
        assign = _BC7_PART3[partition]
        anchors = {0: 0, 1: _BC7_ANCHOR3_1[partition],
                   2: _BC7_ANCHOR3_2[partition]}
    anchor_set = set(anchors.values())

    def read_indices(nbits):
        out = []
        for t in range(16):
            out.append(read(nbits - 1) if t in anchor_set else read(nbits))
        return out

    idx0 = read_indices(ibits)
    idx1 = read_indices(i2bits) if i2bits else None

    w0 = _BC7_WEIGHTS[ibits]
    w1 = _BC7_WEIGHTS[i2bits] if i2bits else None

    texels = []
    for t in range(16):
        s = assign[t]
        e0, e1 = ep[s * 2], ep[s * 2 + 1]
        if idx1 is None:
            wc = wa = w0[idx0[t]]
        elif index_sel:
            # index_sel swaps the streams: idx1 (3-bit) drives color.
            wc, wa = w1[idx1[t]], w0[idx0[t]]
        else:
            wc, wa = w0[idx0[t]], w1[idx1[t]]
        r = (e0[0] * (64 - wc) + e1[0] * wc + 32) >> 6
        g = (e0[1] * (64 - wc) + e1[1] * wc + 32) >> 6
        b = (e0[2] * (64 - wc) + e1[2] * wc + 32) >> 6
        a = (e0[3] * (64 - wa) + e1[3] * wa + 32) >> 6
        if rotation == 1:
            a, r = r, a
        elif rotation == 2:
            a, g = g, a
        elif rotation == 3:
            a, b = b, a
        texels.append((r, g, b, a))
    return texels


def _bc1_block_colors(data: bytes, off: int, always4: bool):
    """The two RGB565 endpoints and derived palette of a BC1 color block.
    `always4` (BC2/BC3 color blocks) forces 4-color mode regardless of
    endpoint order."""
    c0 = data[off] | (data[off + 1] << 8)
    c1 = data[off + 2] | (data[off + 3] << 8)

    def expand(c):
        r = (c >> 11) & 31
        g = (c >> 5) & 63
        b = c & 31
        return ((r << 3 | r >> 2) / 255.0,
                (g << 2 | g >> 4) / 255.0,
                (b << 3 | b >> 2) / 255.0)
    r0, g0, b0 = expand(c0)
    r1, g1, b1 = expand(c1)
    pal = [(r0, g0, b0, 1.0), (r1, g1, b1, 1.0)]
    if always4 or c0 > c1:
        pal.append(((2*r0 + r1)/3, (2*g0 + g1)/3, (2*b0 + b1)/3, 1.0))
        pal.append(((r0 + 2*r1)/3, (g0 + 2*g1)/3, (b0 + 2*b1)/3, 1.0))
    else:
        pal.append(((r0 + r1)/2, (g0 + g1)/2, (b0 + b1)/2, 1.0))
        pal.append((0.0, 0.0, 0.0, 0.0))
    return pal


def _bc1_decode_block(data: bytes, off: int, always4: bool = False):
    pal = _bc1_block_colors(data, off, always4)
    bits = int.from_bytes(data[off + 4:off + 8], 'little')
    return [list(pal[(bits >> (2*i)) & 3]) for i in range(16)]


def _bc4_decode_channel(data: bytes, off: int):
    """One BC4 (alpha) block → 16 floats."""
    a0, a1 = data[off], data[off + 1]
    f0, f1 = a0 / 255.0, a1 / 255.0
    if a0 > a1:
        pal = [f0, f1] + [((7 - i)*f0 + i*f1) / 7 for i in range(1, 7)]
    else:
        pal = [f0, f1] + [((5 - i)*f0 + i*f1) / 5 for i in range(1, 5)] + [0.0, 1.0]
    bits = int.from_bytes(data[off + 2:off + 8], 'little')
    return [pal[(bits >> (3*i)) & 7] for i in range(16)]


def _decode_bc_image(data: bytes, width: int, height: int, kind: str):
    """Decode a BC1/BC3/BC4/BC5 surface into a top-left RGBA float grid."""
    bsize = 8 if kind in ('BC1', 'BC4') else 16
    bw, bh = (width + 3) // 4, (height + 3) // 4
    if bw * bh * bsize > len(data):
        return None
    pixels = [[[0.0, 0.0, 0.0, 1.0] for _ in range(width)] for _ in range(height)]
    for by in range(bh):
        for bx in range(bw):
            off = (by * bw + bx) * bsize
            if kind == 'BC1':
                texels = _bc1_decode_block(data, off)
            elif kind == 'BC3':
                alpha = _bc4_decode_channel(data, off)
                texels = _bc1_decode_block(data, off + 8, always4=True)
                for i in range(16):
                    texels[i][3] = alpha[i]
            elif kind == 'BC4':
                ch = _bc4_decode_channel(data, off)
                texels = [[v, 0.0, 0.0, 1.0] for v in ch]
            else:  # BC5: two BC4 channels (r, g)
                rc = _bc4_decode_channel(data, off)
                gc = _bc4_decode_channel(data, off + 8)
                texels = [[rc[i], gc[i], 0.0, 1.0] for i in range(16)]
            for ty in range(4):
                y = by * 4 + ty
                if y >= height:
                    break
                row = pixels[y]
                for tx in range(4):
                    x = bx * 4 + tx
                    if x >= width:
                        break
                    row[x] = texels[ty * 4 + tx]
    return pixels


def decode_bc7_image(data: bytes, width: int, height: int, offset: int = 0):
    """Decode a BC7 image (one 2D surface) into a top-left-origin RGBA float
    grid. Returns None if `data` is too short."""
    bw, bh = (width + 3) // 4, (height + 3) // 4
    if offset + bw * bh * 16 > len(data):
        return None
    pixels = [[[0.0, 0.0, 0.0, 1.0] for _ in range(width)] for _ in range(height)]
    for by in range(bh):
        for bx in range(bw):
            block = data[offset + (by * bw + bx) * 16:
                         offset + (by * bw + bx) * 16 + 16]
            texels = _bc7_decode_block(block)
            for ty in range(4):
                y = by * 4 + ty
                if y >= height:
                    break
                row = pixels[y]
                for tx in range(4):
                    x = bx * 4 + tx
                    if x >= width:
                        break
                    r, g, b, a = texels[ty * 4 + tx]
                    row[x] = [r / 255.0, g / 255.0, b / 255.0, a / 255.0]
    return pixels

# Address-mode names as they appear in 3Dmigoto's sampler_params.csv
# (e.g. AddressU="Wrap"). Maps to the D3D11_TEXTURE_ADDRESS_* constants.
ADDRESS_NAME_MAP = {
    "WRAP": D3D11_TEXTURE_ADDRESS_WRAP,
    "MIRROR": D3D11_TEXTURE_ADDRESS_MIRROR,
    "CLAMP": D3D11_TEXTURE_ADDRESS_CLAMP,
    "BORDER": D3D11_TEXTURE_ADDRESS_BORDER,
    "MIRRORONCE": D3D11_TEXTURE_ADDRESS_MIRROR_ONCE,
    "MIRROR_ONCE": D3D11_TEXTURE_ADDRESS_MIRROR_ONCE,
    # RenderDoc-style names used by the new dumps' sampler_params.csv —
    # unrecognized they silently fell back to WRAP, so a clamp sampler
    # wrapped u>1 to the OPPOSITE texture edge (sekiro4 irradiance volume).
    "CLAMPEDGE": D3D11_TEXTURE_ADDRESS_CLAMP,
    "CLAMPBORDER": D3D11_TEXTURE_ADDRESS_BORDER,
    "REPEAT": D3D11_TEXTURE_ADDRESS_WRAP,
}

# Filter component values used internally by Sampler._get_filter_mode:
# 0 == point, 1 == linear.
FILTER_POINT = 0
FILTER_LINEAR = 1

# Comparison-function names as they appear in sampler_params.csv (CompareFunc).
COMPARISON_NAME_MAP = {
    "NEVER": D3D11_COMPARISON_NEVER,
    "LESS": D3D11_COMPARISON_LESS,
    "EQUAL": D3D11_COMPARISON_EQUAL,
    "LESSEQUAL": D3D11_COMPARISON_LESS_EQUAL,
    "LESS_EQUAL": D3D11_COMPARISON_LESS_EQUAL,
    "GREATER": D3D11_COMPARISON_GREATER,
    "NOTEQUAL": D3D11_COMPARISON_NOT_EQUAL,
    "NOT_EQUAL": D3D11_COMPARISON_NOT_EQUAL,
    "GREATEREQUAL": D3D11_COMPARISON_GREATER_EQUAL,
    "GREATER_EQUAL": D3D11_COMPARISON_GREATER_EQUAL,
    "ALWAYS": D3D11_COMPARISON_ALWAYS,
}

COMPARISON_MAP = {
    "D3D11_COMPARISON_NEVER": 0,
    "D3D11_COMPARISON_LESS": 1,
    "D3D11_COMPARISON_EQUAL": 2,
    "D3D11_COMPARISON_LESS_EQUAL": 3,
    "D3D11_COMPARISON_GREATER": 4,
    "D3D11_COMPARISON_NOT_EQUAL": 5,
    "D3D11_COMPARISON_GREATER_EQUAL": 6,
    "D3D11_COMPARISON_ALWAYS": 7,
}

FORMAT_MAP = {
    "DXGI_FORMAT_B8G8R8A8_UNORM": 0x57,
    "DXGI_FORMAT_R8G8B8A8_UNORM": 0x56,
    "DXGI_FORMAT_R8G8B8A8_UINT": 0x6C,
    "DXGI_FORMAT_R32G32B32A32_FLOAT": 0x5A,
    "DXGI_FORMAT_R32G32B32A32_UINT": 0x5B,
    "DXGI_FORMAT_R16G16B16A16_FLOAT": 0x5E,
    "DXGI_FORMAT_R16G16B16A16_UNORM": 0x5C,
    "DXGI_FORMAT_R32_FLOAT": 0x52,
    "DXGI_FORMAT_R8_UNORM": 0x51,
}

USAGE_MAP = {
    "D3D11_USAGE_DEFAULT": 0,
    "D3D11_USAGE_IMMUTABLE": 1,
    "D3D11_USAGE_DYNAMIC": 2,
    "D3D11_USAGE_STAGING": 3,
}

BINDFLAGS_MAP = {
    "D3D11_BIND_SHADER_RESOURCE": 0x8,
    "D3D11_BIND_RENDER_TARGET": 0x4,
    "D3D11_BIND_DEPTH_STENCIL": 0x2,
    "D3D11_BIND_VERTEX_BUFFER": 0x1,
    "D3D11_BIND_INDEX_BUFFER": 0x2,
}


def _convert_filter(val):
    if isinstance(val, int):
        return val
    return FILTER_MAP.get(val, D3D11_FILTER_MIN_MAG_LINEAR_MIP_POINT)


def _convert_address(val):
    if isinstance(val, int):
        return val
    return ADDRESS_MAP.get(val, D3D11_TEXTURE_ADDRESS_WRAP)


def _convert_comparison(val):
    if isinstance(val, int):
        return val
    return COMPARISON_MAP.get(val, D3D11_COMPARISON_NEVER)


def _convert_format(val):
    if isinstance(val, int):
        return val
    return FORMAT_MAP.get(val, 0x57)


def _convert_usage(val):
    if isinstance(val, int):
        return val
    return USAGE_MAP.get(val, 0)


def _convert_bindflags(val):
    if isinstance(val, int):
        return val
    return BINDFLAGS_MAP.get(val, 0x8)


def _parse_address_name(val) -> int:
    """Resolve a sampler_params.csv address string (e.g. "Wrap") to a
    D3D11_TEXTURE_ADDRESS_* constant. Falls back to the existing converters
    for D3D11_*-style names / raw ints, then defaults to WRAP."""
    if isinstance(val, int):
        return val
    if val is None:
        return D3D11_TEXTURE_ADDRESS_WRAP
    key = str(val).strip().upper()
    if key in ADDRESS_NAME_MAP:
        return ADDRESS_NAME_MAP[key]
    return ADDRESS_MAP.get(str(val), D3D11_TEXTURE_ADDRESS_WRAP)


def _parse_filter_component(token: str) -> int:
    """Map a filter component word ("Linear"/"Point"/"Anisotropic") to the
    internal point(0)/linear(1) mode. Anisotropic degrades to linear."""
    t = str(token).strip().upper()
    if t == "POINT":
        return FILTER_POINT
    # Linear / Anisotropic / anything else -> linear filtering
    return FILTER_LINEAR


def _parse_filter_string(val) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """Parse a 3Dmigoto Filter string like
    "Min=Linear,Mag=Linear,Mip=Point" into explicit (min, mag, mip) filter
    modes. Returns (None, None, None) when the value isn't a parseable string
    (so the caller keeps the bit-packed Filter behaviour)."""
    if not isinstance(val, str):
        return None, None, None
    min_f = mag_f = mip_f = None
    for part in val.split(','):
        if '=' not in part:
            continue
        key, _, value = part.partition('=')
        key = key.strip().upper()
        mode = _parse_filter_component(value)
        if key == 'MIN':
            min_f = mode
        elif key == 'MAG':
            mag_f = mode
        elif key == 'MIP':
            mip_f = mode
    return min_f, mag_f, mip_f


def _parse_lod(val, default: float) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


class Sampler:
    def __init__(
        self,
        Filter: int = D3D11_FILTER_MIN_MAG_LINEAR_MIP_POINT,
        AddressU: int = D3D11_TEXTURE_ADDRESS_WRAP,
        AddressV: int = D3D11_TEXTURE_ADDRESS_WRAP,
        AddressW: int = D3D11_TEXTURE_ADDRESS_WRAP,
        MipLODBias: float = 0.0,
        MaxAnisotropy: int = 1,
        ComparisonFunc: int = D3D11_COMPARISON_NEVER,
        BorderColor: Optional[List[float]] = None,
        MinLOD: float = -3.40282e38,
        MaxLOD: float = 3.40282e38,
        MinFilter: Optional[int] = None,
        MagFilter: Optional[int] = None,
        MipFilter: Optional[int] = None
    ):
        self.Filter = Filter
        self.AddressU = AddressU
        self.AddressV = AddressV
        self.AddressW = AddressW
        self.MipLODBias = MipLODBias
        self.MaxAnisotropy = MaxAnisotropy
        self.ComparisonFunc = ComparisonFunc
        self.BorderColor = BorderColor if BorderColor is not None else [0.0, 0.0, 0.0, 0.0]
        self.MinLOD = MinLOD
        self.MaxLOD = MaxLOD
        # Explicit per-stage filter modes (0=point, 1=linear). When set these
        # take precedence over the bit-packed `Filter` field in
        # _get_filter_mode — used when the sampler is built from a
        # 3Dmigoto "Min=..,Mag=..,Mip=.." filter string.
        self.MinFilter = MinFilter
        self.MagFilter = MagFilter
        self.MipFilter = MipFilter

    @classmethod
    def from_params_row(cls, row: Dict[str, str]) -> 'Sampler':
        """Build a Sampler from one sampler_params.csv row (3Dmigoto dump).

        Columns: AddressU, AddressV, AddressW, Filter
        ("Min=Linear,Mag=Linear,Mip=Point"), MinLOD, MaxLOD, MipLODBias,
        MaxAnisotropy, CompareFunc.
        """
        min_f, mag_f, mip_f = _parse_filter_string(row.get('Filter'))
        return cls(
            AddressU=_parse_address_name(row.get('AddressU', 'Wrap')),
            AddressV=_parse_address_name(row.get('AddressV', 'Wrap')),
            AddressW=_parse_address_name(row.get('AddressW', 'Wrap')),
            MipLODBias=_parse_lod(row.get('MipLODBias', 0.0), 0.0),
            MaxAnisotropy=int(_parse_lod(row.get('MaxAnisotropy', 1), 1)),
            ComparisonFunc=COMPARISON_NAME_MAP.get(
                str(row.get('CompareFunc', 'Never')).strip().upper(),
                D3D11_COMPARISON_NEVER),
            MinLOD=_parse_lod(row.get('MinLOD', -3.40282e38), -3.40282e38),
            MaxLOD=_parse_lod(row.get('MaxLOD', 3.40282e38), 3.40282e38),
            MinFilter=min_f,
            MagFilter=mag_f,
            MipFilter=mip_f,
        )

    @classmethod
    def from_config(cls, config_path: str, sampler_id: int) -> 'Sampler':
        with open(config_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        sampler_data = data[str(sampler_id)]
        return cls(
            Filter=_convert_filter(sampler_data.get('Filter', D3D11_FILTER_MIN_MAG_LINEAR_MIP_POINT)),
            AddressU=_convert_address(sampler_data.get('AddressU', D3D11_TEXTURE_ADDRESS_WRAP)),
            AddressV=_convert_address(sampler_data.get('AddressV', D3D11_TEXTURE_ADDRESS_WRAP)),
            AddressW=_convert_address(sampler_data.get('AddressW', D3D11_TEXTURE_ADDRESS_WRAP)),
            MipLODBias=sampler_data.get('MipLODBias', 0.0),
            MaxAnisotropy=sampler_data.get('MaxAnisotropy', 1),
            ComparisonFunc=_convert_comparison(sampler_data.get('ComparisonFunc', D3D11_COMPARISON_NEVER)),
            BorderColor=sampler_data.get('BorderColor', [0.0, 0.0, 0.0, 0.0]),
            MinLOD=sampler_data.get('MinLOD', -3.40282e38),
            MaxLOD=sampler_data.get('MaxLOD', 3.40282e38)
        )

    def _get_filter_mode(self) -> Tuple[int, int, int]:
        # Explicit modes (parsed from a "Min=..,Mag=..,Mip=.." string) win
        # over the bit-packed D3D11_FILTER value when present.
        if self.MinFilter is not None and self.MagFilter is not None and self.MipFilter is not None:
            return self.MinFilter, self.MagFilter, self.MipFilter
        min_filter = (self.Filter >> 0) & 0x03
        mag_filter = (self.Filter >> 4) & 0x03
        mip_filter = (self.Filter >> 8) & 0x03
        return min_filter, mag_filter, mip_filter

    def _address_mode_to_func(self, address_mode: int):
        if address_mode == D3D11_TEXTURE_ADDRESS_WRAP:
            return self._wrap_address
        elif address_mode == D3D11_TEXTURE_ADDRESS_MIRROR:
            return self._mirror_address
        elif address_mode == D3D11_TEXTURE_ADDRESS_CLAMP:
            return self._clamp_address
        elif address_mode == D3D11_TEXTURE_ADDRESS_BORDER:
            return self._border_address
        elif address_mode == D3D11_TEXTURE_ADDRESS_MIRROR_ONCE:
            return self._mirror_once_address
        return self._wrap_address

    def _wrap_address(self, coord: float) -> float:
        if coord < 0.0:
            coord = coord - math.floor(coord)
        elif coord >= 1.0:
            coord = coord - math.floor(coord)
        return coord

    def _mirror_address(self, coord: float) -> float:
        if coord < 0.0:
            coord = -coord
        int_part = int(coord)
        frac = coord - int_part
        if int_part % 2 == 0:
            return frac
        else:
            return 1.0 - frac

    def _clamp_address(self, coord: float) -> float:
        return max(0.0, min(1.0, coord))

    def _border_address(self, coord: float) -> float:
        return coord

    def _mirror_once_address(self, coord: float) -> float:
        if coord < 0.0:
            return -coord
        elif coord > 1.0:
            return 2.0 - coord
        return coord

    def transform_coordinates(self, u: float, v: float, w: float) -> Tuple[float, float, float]:
        # A NaN/inf UV (e.g. a divide-by-zero upstream feeding a sample, as in
        # witcher event16834) would make the address functions throw —
        # math.floor(inf)/int(inf) raise OverflowError. Clamp non-finite coords
        # to 0 so the sample degrades to a defined texel instead of crashing the
        # whole pipeline; the bad value still surfaces as a golden mismatch.
        u, v, w = (c if math.isfinite(c) else 0.0 for c in (u, v, w))
        address_u_func = self._address_mode_to_func(self.AddressU)
        address_v_func = self._address_mode_to_func(self.AddressV)
        address_w_func = self._address_mode_to_func(self.AddressW)
        return address_u_func(u), address_v_func(v), address_w_func(w)


class TextureDesc:
    def __init__(
        self,
        Width: int = 512,
        Height: int = 512,
        MipLevels: int = 1,
        ArraySize: int = 1,
        Format: int = 0x57,
        SampleDesc_Count: int = 1,
        SampleDesc_Quality: int = 0,
        Usage: int = 0,
        BindFlags: int = 0x8,
        CPUAccessFlags: int = 0,
        MiscFlags: int = 0,
        DataPath: str = "",
        MipDataPaths: Optional[List[str]] = None,
        FormatStr: str = "",
        ArrayMipDataPaths: Optional[List[List[str]]] = None,
        Depth: int = 1,
        Kind: str = ""
    ):
        # Raw DXGI format name (e.g. 'R8G8B8A8_UNORM', 'R32G32B32A32_FLOAT')
        # used to decode raw .img texel data. Empty → rely on BMP / default.
        self.FormatStr = FormatStr
        self.Width = Width
        self.Height = Height
        self.Depth = Depth
        self.Kind = Kind
        self.MipLevels = MipLevels
        self.ArraySize = ArraySize
        self.Format = Format
        self.SampleDesc_Count = SampleDesc_Count
        self.SampleDesc_Quality = SampleDesc_Quality
        self.Usage = Usage
        self.BindFlags = BindFlags
        self.CPUAccessFlags = CPUAccessFlags
        self.MiscFlags = MiscFlags
        self.DataPath = DataPath
        # Ordered list of BMP paths, one per mip level (mip0, mip1, ...). When
        # provided (and longer than one entry) the real captured mip chain is
        # loaded directly instead of being regenerated from mip0. Defaults to
        # [DataPath] so single-image textures keep the regenerate-from-base
        # behaviour.
        if MipDataPaths:
            self.MipDataPaths = list(MipDataPaths)
        elif DataPath:
            self.MipDataPaths = [DataPath]
        else:
            self.MipDataPaths = []
        # Per-array-slice mip chains for a Texture2DArray / TextureCube[Array]:
        # ArrayMipDataPaths[slice] = [mip0_path, mip1_path, ...]. Slice 0 is the
        # same chain as MipDataPaths, so a non-array texture leaves this as a
        # single-slice list and the existing slice-0 path is unchanged.
        if ArrayMipDataPaths:
            self.ArrayMipDataPaths = [list(s) for s in ArrayMipDataPaths]
        else:
            self.ArrayMipDataPaths = [self.MipDataPaths] if self.MipDataPaths else []

    @classmethod
    def from_config(cls, config_path: str, texture_id: int) -> 'TextureDesc':
        with open(config_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        tex_data = data[str(texture_id)]
        return cls(
            Width=tex_data.get('Width', 512),
            Height=tex_data.get('Height', 512),
            MipLevels=tex_data.get('MipLevels', 1),
            ArraySize=tex_data.get('ArraySize', 1),
            Format=_convert_format(tex_data.get('Format', 'DXGI_FORMAT_B8G8R8A8_UNORM')),
            SampleDesc_Count=tex_data.get('SampleDesc', {}).get('Count', 1),
            SampleDesc_Quality=tex_data.get('SampleDesc', {}).get('Quality', 0),
            Usage=_convert_usage(tex_data.get('Usage', 'D3D11_USAGE_DEFAULT')),
            BindFlags=_convert_bindflags(tex_data.get('BindFlags', 'D3D11_BIND_SHADER_RESOURCE')),
            CPUAccessFlags=tex_data.get('CPUAccessFlags', 0),
            MiscFlags=tex_data.get('MiscFlags', 0),
            DataPath=tex_data.get('Image', '')
        )


class Texture:
    def __init__(self):
        self._mip_levels_cache: Dict[Tuple[str, ...], List[List[List[List[float]]]]] = {}

    def _get_mip_levels(self, texture_desc: TextureDesc,
                        array_slice: int = 0) -> List[List[List[List[float]]]]:
        arr = getattr(texture_desc, 'ArrayMipDataPaths', None)
        if arr:
            idx = array_slice if 0 <= array_slice < len(arr) else 0
            mip_paths = arr[idx]
        else:
            mip_paths = texture_desc.MipDataPaths or (
                [texture_desc.DataPath] if texture_desc.DataPath else [])
        cache_key = tuple(mip_paths)
        if cache_key in self._mip_levels_cache:
            return self._mip_levels_cache[cache_key]

        mip_levels = self._load_mip_levels(mip_paths, texture_desc)
        self._mip_levels_cache[cache_key] = mip_levels
        return mip_levels

    def _load_mip_levels(self, mip_paths: List[str],
                        texture_desc: TextureDesc) -> List[List[List[List[float]]]]:
        """Load the mip chain.

        Prefers raw .img data (decoded per the texture's DXGI format), falling
        back to the sibling .bmp when a level is a block-compressed format the
        raw decoder doesn't support. When more than one level was captured,
        load each directly so sampling reflects real captured data; with a
        single image, parse the base level and regenerate the rest."""
        base_w = max(1, int(texture_desc.Width or 1))
        base_h = max(1, int(texture_desc.Height or 1))
        fmt = getattr(texture_desc, 'FormatStr', '') or ''

        if len(mip_paths) > 1:
            levels = []
            for i, path in enumerate(mip_paths):
                w = max(1, base_w >> i)
                h = max(1, base_h >> i)
                pixels = self._load_level_pixels(path, fmt, w, h)
                if pixels is None:
                    # A missing/invalid level truncates the chain; stop here so
                    # we never index past a real captured level.
                    break
                levels.append(pixels)
            if levels:
                return levels
            return self._create_placeholder_texture(texture_desc)

        # Single source image: parse base + regenerate mips.
        base = self._load_level_pixels(mip_paths[0], fmt, base_w, base_h) if mip_paths else None
        if base is None:
            return self._create_placeholder_texture(texture_desc)
        mip_levels = [base]
        mip_levels.extend(self._generate_mipmaps(base))
        return mip_levels

    def _load_level_pixels(self, path: str, fmt: str, width: int, height: int):
        """Load one mip level. Raw .img is decoded by DXGI format; on an
        unsupported (e.g. block-compressed) format it falls back to the sibling
        .bmp. Non-.img paths are parsed as BMP."""
        if path.lower().endswith('.img'):
            pixels = self._load_img_pixels(path, fmt, width, height)
            if pixels is not None:
                return pixels
            # Unsupported raw format → use the sibling BMP (RenderDoc's decode).
            sibling_bmp = path[:-4] + '.bmp'
            if os.path.exists(sibling_bmp):
                return self._load_bmp_pixels(sibling_bmp)
            return None
        return self._load_bmp_pixels(path)

    def _load_img_pixels(self, path: str, fmt: str, width: int,
                         height: int) -> Optional[List[List[List[float]]]]:
        """Read a raw .img texel dump and decode it per its DXGI format into a
        top-left-origin RGBA-float grid. Returns None for unsupported formats
        (so the caller can fall back to the BMP)."""
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except Exception:
            return None
        return self._decode_raw_texels(data, fmt, width, height)

    # DXGI format (name without DXGI_FORMAT_ prefix) → (component type,
    # channel order). Block-compressed formats are intentionally absent so the
    # loader falls back to the captured BMP for those.
    _FMT_SPECS = {
        'R8G8B8A8_UNORM': ('unorm8', 'RGBA'),
        'R8G8B8A8_UNORM_SRGB': ('unorm8', 'RGBA'),
        'R8G8B8A8_SNORM': ('snorm8', 'RGBA'),
        'B8G8R8A8_UNORM': ('unorm8', 'BGRA'),
        'B8G8R8A8_UNORM_SRGB': ('unorm8', 'BGRA'),
        'B8G8R8X8_UNORM': ('unorm8', 'BGRX'),
        'R8G8_UNORM': ('unorm8', 'RG'),
        'R8_UNORM': ('unorm8', 'R'),
        'A8_UNORM': ('unorm8', 'A'),
        'R16G16B16A16_FLOAT': ('float16', 'RGBA'),
        'R16G16B16A16_UNORM': ('unorm16', 'RGBA'),
        'R16G16_FLOAT': ('float16', 'RG'),
        'R16G16_UNORM': ('unorm16', 'RG'),
        'R16_FLOAT': ('float16', 'R'),
        'R16_UNORM': ('unorm16', 'R'),
        'R32G32B32A32_FLOAT': ('float32', 'RGBA'),
        'R32G32B32_FLOAT': ('float32', 'RGB'),
        'R32G32_FLOAT': ('float32', 'RG'),
        'R32_FLOAT': ('float32', 'R'),
    }
    _COMP_SIZE = {'unorm8': 1, 'snorm8': 1, 'float16': 2, 'unorm16': 2, 'float32': 4}

    @staticmethod
    def _decode_r11g11b10(data: bytes, width: int,
                          height: int) -> Optional[List[List[List[float]]]]:
        """R11G11B10_FLOAT: one uint32 per texel — R = bits 0..10, G = bits
        11..21 (float11: 5-bit exp, 6-bit mantissa), B = bits 22..31
        (float10: 5-bit exp, 5-bit mantissa). No sign bits."""
        if len(data) < width * height * 4:
            return None

        def _small_float(bits: int, mant_bits: int) -> float:
            exp = (bits >> mant_bits) & 0x1F
            mant = bits & ((1 << mant_bits) - 1)
            if exp == 0:
                if mant == 0:
                    return 0.0
                return (mant / (1 << mant_bits)) * 2.0 ** -14
            if exp == 31:
                return math.inf if mant == 0 else math.nan
            return (1.0 + mant / (1 << mant_bits)) * 2.0 ** (exp - 15)

        grid = []
        off = 0
        for _ in range(height):
            row = []
            for _ in range(width):
                packed = struct.unpack_from('<I', data, off)[0]
                off += 4
                row.append([
                    _small_float(packed & 0x7FF, 6),
                    _small_float((packed >> 11) & 0x7FF, 6),
                    _small_float((packed >> 22) & 0x3FF, 5),
                    1.0,
                ])
            grid.append(row)
        return grid

    def _decode_raw_texels(self, data: bytes, fmt: str, width: int,
                           height: int) -> Optional[List[List[List[float]]]]:
        """Decode raw row-major (top-left origin) texel bytes for an
        uncompressed DXGI format (or BC7) into an RGBA-float grid."""
        if not fmt:
            return None
        name = fmt.strip().upper()
        if name.startswith('DXGI_FORMAT_'):
            name = name[len('DXGI_FORMAT_'):]
        if name.startswith('BC7_UNORM'):
            return decode_bc7_image(data, width, height, 0)
        # BC1/BC3/BC4/BC5 (block compression). _SRGB variants decode to the
        # same stored values (matching the previous BMP-fallback behaviour —
        # golden comparisons are calibrated to sRGB-space values).
        for bck in ('BC1', 'BC3', 'BC4', 'BC5'):
            if name.startswith(bck + '_'):
                return _decode_bc_image(data, width, height, bck)
        if name == 'R11G11B10_FLOAT':
            return self._decode_r11g11b10(data, width, height)
        spec = self._FMT_SPECS.get(name)
        if spec is None and name.endswith('_TYPELESS'):
            spec = self._FMT_SPECS.get(name[:-len('_TYPELESS')] + '_FLOAT') \
                or self._FMT_SPECS.get(name[:-len('_TYPELESS')] + '_UNORM')
        if spec is None:
            return None  # unsupported (e.g. block-compressed) → BMP fallback

        comp_type, order = spec
        csize = self._COMP_SIZE[comp_type]
        nch = len(order)
        bpt = csize * nch
        if len(data) < width * height * bpt:
            return None

        def read_comp(off: int) -> float:
            if comp_type == 'unorm8':
                return data[off] / 255.0
            if comp_type == 'snorm8':
                v = data[off]
                v = v - 256 if v >= 128 else v
                return max(-1.0, v / 127.0)
            if comp_type == 'unorm16':
                return struct.unpack_from('<H', data, off)[0] / 65535.0
            if comp_type == 'float16':
                return struct.unpack_from('<e', data, off)[0]
            if comp_type == 'float32':
                return struct.unpack_from('<f', data, off)[0]
            return 0.0

        pixels = []
        for y in range(height):
            row = []
            for x in range(width):
                base = (y * width + x) * bpt
                ch = {'R': 0.0, 'G': 0.0, 'B': 0.0, 'A': 1.0}
                for i, c in enumerate(order):
                    val = read_comp(base + i * csize)
                    if c != 'X':
                        ch[c] = val
                row.append([ch['R'], ch['G'], ch['B'], ch['A']])
            pixels.append(row)
        return pixels

    def _load_bmp_pixels(self, path: str) -> Optional[List[List[List[float]]]]:
        """Read one BMP file and return its pixels as a single mip level, or
        None on any failure (so callers can fall back)."""
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except Exception:
            return None
        return self._parse_bmp_pixels(data)

    def _parse_bmp_pixels(self, data: bytes) -> Optional[List[List[List[float]]]]:
        """Parse a 24/32-bit BMP into a top-left-origin pixel grid (one mip
        level). Returns None when the data isn't a supported BMP."""
        if len(data) < 54 or data[0:2] != b'BM':
            return None

        offset = struct.unpack('<I', data[10:14])[0]
        header_size = struct.unpack('<I', data[14:18])[0]
        if header_size != 40:
            return None

        width = struct.unpack('<I', data[18:22])[0]
        height = struct.unpack('<I', data[22:26])[0]
        bits_per_pixel = struct.unpack('<H', data[28:30])[0]

        if bits_per_pixel != 24 and bits_per_pixel != 32:
            return None

        bytes_per_pixel = bits_per_pixel // 8
        row_size = ((bits_per_pixel * width + 31) // 32) * 4

        pixels = []
        for y in range(height):
            row = []
            for x in range(width):
                pos = offset + y * row_size + x * bytes_per_pixel
                if pos + bytes_per_pixel > len(data):
                    row.append([0.0, 0.0, 0.0, 1.0])
                    continue

                if bits_per_pixel == 24:
                    b, g, r = data[pos], data[pos + 1], data[pos + 2]
                else:
                    b, g, r, a = data[pos], data[pos + 1], data[pos + 2], data[pos + 3]

                row.append([
                    r / 255.0,
                    g / 255.0,
                    b / 255.0,
                    a / 255.0 if bits_per_pixel == 32 else 1.0
                ])
            pixels.append(row)

        # BMP scanlines are stored bottom-up (positive height). Flip
        # vertically so texture row 0 (v=0) maps to the top of the image,
        # matching D3D's top-left UV origin used by the sampler.
        pixels.reverse()
        return pixels

    def _create_placeholder_texture(self, texture_desc: TextureDesc) -> List[List[List[List[float]]]]:
        width = 4
        height = 4
        checkerboard = []
        for y in range(height):
            row = []
            for x in range(width):
                if (x + y) % 2 == 0:
                    color = [1.0, 1.0, 1.0, 1.0]
                else:
                    color = [0.0, 0.0, 0.0, 1.0]
                row.append(color)
            checkerboard.append(row)
        mip_levels = [checkerboard]
        mip_levels.extend(self._generate_mipmaps(checkerboard))
        return mip_levels

    def _generate_mipmaps(self, base_level: List[List[List[float]]]) -> List[List[List[List[float]]]]:
        mip_levels = []
        prev_level = base_level
        h = len(prev_level)
        w = len(prev_level[0])

        while True:
            if w <= 1 or h <= 1:
                break

            new_h = max(1, h // 2)
            new_w = max(1, w // 2)
            new_level = []

            for y in range(new_h):
                new_row = []
                for x in range(new_w):
                    c00 = prev_level[y * 2][x * 2]
                    c10 = prev_level[min(y * 2 + 1, h - 1)][x * 2]
                    c01 = prev_level[y * 2][min(x * 2 + 1, w - 1)]
                    c11 = prev_level[min(y * 2 + 1, h - 1)][min(x * 2 + 1, w - 1)]

                    new_color = [
                        (c00[0] + c10[0] + c01[0] + c11[0]) / 4.0,
                        (c00[1] + c10[1] + c01[1] + c11[1]) / 4.0,
                        (c00[2] + c10[2] + c01[2] + c11[2]) / 4.0,
                        (c00[3] + c10[3] + c01[3] + c11[3]) / 4.0
                    ]
                    new_row.append(new_color)
                new_level.append(new_row)

            mip_levels.append(new_level)
            prev_level = new_level
            h = new_h
            w = new_w

        return mip_levels

    def _sample_nearest(self, mip_level: List[List[List[float]]], u: float, v: float) -> List[float]:
        h = len(mip_level)
        w = len(mip_level[0])

        # Guard NaN/Inf UV so int() can't crash the draw (see _sample_linear).
        if u != u or u in (float('inf'), float('-inf')):
            u = 0.0
        if v != v or v in (float('inf'), float('-inf')):
            v = 0.0

        x = int(u * w) % w
        y = int(v * h) % h

        x = max(0, min(w - 1, x))
        y = max(0, min(h - 1, y))

        return mip_level[y][x]

    def _sample_linear(self, mip_level: List[List[List[float]]], u: float, v: float,
                       address_u: int = D3D11_TEXTURE_ADDRESS_WRAP,
                       address_v: int = D3D11_TEXTURE_ADDRESS_WRAP) -> List[float]:
        h = len(mip_level)
        w = len(mip_level[0])

        # A NaN/Inf UV (e.g. from a divide-by-zero in the shader) must not crash
        # the int() conversion below; treat it as 0 like the GPU's well-defined
        # out-of-range behaviour rather than aborting the whole draw.
        if u != u or u in (float('inf'), float('-inf')):
            u = 0.0
        if v != v or v in (float('inf'), float('-inf')):
            v = 0.0

        # D3D11 bilinear: the sample point in texel space is u*w - 0.5 (texel
        # centers sit at integer+0.5). The previous u*w convention was half a
        # texel off, blending neighbours at exact texel-center samples.
        fu = u * w - 0.5
        fv = v * h - 0.5

        u0 = math.floor(fu)
        v0 = math.floor(fv)
        s = fu - u0
        t = fv - v0
        u0 = int(u0)
        v0 = int(v0)

        def _nb(i, n, mode):
            if mode == D3D11_TEXTURE_ADDRESS_WRAP:
                return i % n
            return max(0, min(n - 1, i))
        u1 = _nb(u0 + 1, w, address_u)
        v1 = _nb(v0 + 1, h, address_v)
        u0 = _nb(u0, w, address_u)
        v0 = _nb(v0, h, address_v)

        c00 = mip_level[v0][u0]
        c10 = mip_level[v0][u1]
        c01 = mip_level[v1][u0]
        c11 = mip_level[v1][u1]

        color = [
            c00[0] * (1 - s) * (1 - t) + c10[0] * s * (1 - t) + c01[0] * (1 - s) * t + c11[0] * s * t,
            c00[1] * (1 - s) * (1 - t) + c10[1] * s * (1 - t) + c01[1] * (1 - s) * t + c11[1] * s * t,
            c00[2] * (1 - s) * (1 - t) + c10[2] * s * (1 - t) + c01[2] * (1 - s) * t + c11[2] * s * t,
            c00[3] * (1 - s) * (1 - t) + c10[3] * s * (1 - t) + c01[3] * (1 - s) * t + c11[3] * s * t
        ]
        return color

    def _sample_mip_point(self, mip_level: List[List[List[float]]], u: float, v: float) -> List[float]:
        return self._sample_nearest(mip_level, u, v)

    def sample_cmp_lz(self, u: float, v: float, ref: float,
                      texture_desc: TextureDesc, sampler: Sampler,
                      array_slice: int = 0) -> float:
        """SampleCmpLevelZero: hardware PCF on mip 0. Each of the 4 bilinear
        neighbours' R channel is compared against `ref` with the sampler's
        ComparisonFunc (1 on pass), and the 0/1 results are bilinearly
        blended — NOT the texel values (shadow-map depth compare)."""
        mip_levels = self._get_mip_levels(texture_desc, array_slice)
        if not mip_levels:
            return 1.0
        level = mip_levels[0]
        h = len(level)
        w = len(level[0])
        if not math.isfinite(u):
            u = 0.0
        if not math.isfinite(v):
            v = 0.0
        tu = sampler._address_mode_to_func(sampler.AddressU)(u)
        tv = sampler._address_mode_to_func(sampler.AddressV)(v)

        cmpf = sampler.ComparisonFunc

        def _passes(texel: float) -> float:
            if cmpf == D3D11_COMPARISON_LESS:
                ok = ref < texel
            elif cmpf == D3D11_COMPARISON_LESS_EQUAL:
                ok = ref <= texel
            elif cmpf == D3D11_COMPARISON_GREATER:
                ok = ref > texel
            elif cmpf == D3D11_COMPARISON_GREATER_EQUAL:
                ok = ref >= texel
            elif cmpf == D3D11_COMPARISON_EQUAL:
                ok = ref == texel
            elif cmpf == D3D11_COMPARISON_NOT_EQUAL:
                ok = ref != texel
            elif cmpf == D3D11_COMPARISON_ALWAYS:
                ok = True
            else:  # NEVER
                ok = False
            return 1.0 if ok else 0.0

        min_filter, _, _ = sampler._get_filter_mode()
        if min_filter == FILTER_POINT:
            x = max(0, min(w - 1, int(tu * w)))
            y = max(0, min(h - 1, int(tv * h)))
            return _passes(level[y][x][0])

        fu = tu * w - 0.5
        fv = tv * h - 0.5
        u0 = int(math.floor(fu))
        v0 = int(math.floor(fv))
        s = fu - u0
        t = fv - v0

        def _nb(i, n, mode):
            if mode == D3D11_TEXTURE_ADDRESS_WRAP:
                return i % n
            return max(0, min(n - 1, i))
        u1 = _nb(u0 + 1, w, sampler.AddressU)
        v1 = _nb(v0 + 1, h, sampler.AddressV)
        u0 = _nb(u0, w, sampler.AddressU)
        v0 = _nb(v0, h, sampler.AddressV)
        p00 = _passes(level[v0][u0][0])
        p10 = _passes(level[v0][u1][0])
        p01 = _passes(level[v1][u0][0])
        p11 = _passes(level[v1][u1][0])
        return (p00 * (1 - s) * (1 - t) + p10 * s * (1 - t)
                + p01 * (1 - s) * t + p11 * s * t)

    def load(self, x: int, y: int, mip: int, texture_desc: TextureDesc,
             array_slice: int = 0) -> List[float]:
        """HLSL Texture2D.Load: fetch the texel at integer coords (x, y) on mip
        level `mip`, with NO filtering or address wrapping. Out-of-bounds reads
        return 0 (D3D returns 0 for Load outside the resource). The decoded grid
        is top-left origin, so [y][x] maps directly to D3D texel coords."""
        mip_levels = self._get_mip_levels(texture_desc, array_slice)
        if not mip_levels:
            return [0.0, 0.0, 0.0, 0.0]
        if mip < 0 or mip >= len(mip_levels):
            return [0.0, 0.0, 0.0, 0.0]
        level = mip_levels[mip]
        h = len(level)
        w = len(level[0]) if h else 0
        if y < 0 or y >= h or x < 0 or x >= w:
            return [0.0, 0.0, 0.0, 0.0]
        return list(level[y][x])

    def _get_volume_slices(self, texture_desc: TextureDesc):
        """Decode a Texture3D's .img (Depth slices of Width x Height, each
        slice compressed/packed independently) into a list of 2D pixel grids.
        Returns [] when the volume can't be decoded."""
        path = texture_desc.DataPath
        key = ('volume', path)
        if key in self._mip_levels_cache:
            return self._mip_levels_cache[key]
        slices = []
        w = max(1, int(texture_desc.Width or 1))
        h = max(1, int(texture_desc.Height or 1))
        d = max(1, int(getattr(texture_desc, 'Depth', 1) or 1))
        fmt = (getattr(texture_desc, 'FormatStr', '') or '').strip().upper()
        if fmt.startswith('DXGI_FORMAT_'):
            fmt = fmt[len('DXGI_FORMAT_'):]
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except Exception:
            data = b''
        if data and path.lower().endswith('.img'):
            if fmt.startswith('BC7_UNORM'):
                ssize = ((w + 3) // 4) * ((h + 3) // 4) * 16
                for i in range(d):
                    px = decode_bc7_image(data, w, h, i * ssize)
                    if px is None:
                        break
                    slices.append(px)
            else:
                spec = self._FMT_SPECS.get(fmt)
                if spec:
                    ssize = self._COMP_SIZE[spec[0]] * len(spec[1]) * w * h
                    for i in range(d):
                        px = self._decode_raw_texels(
                            data[i * ssize:(i + 1) * ssize], fmt, w, h)
                        if px is None:
                            break
                        slices.append(px)
        if not slices:
            # Fall back to the 2D pipeline (BMP slice 0) so behaviour degrades
            # to the pre-volume state instead of black.
            mips = self._get_mip_levels(texture_desc)
            slices = [mips[0]] if mips else []
        self._mip_levels_cache[key] = slices
        return slices

    def sample_volume(self, u: float, v: float, w: float,
                      texture_desc: TextureDesc, sampler: Sampler) -> List[float]:
        """Sample a Texture3D at (u, v, w) with the sampler's filter/address
        modes: bilinear within the two bracketing depth slices + linear across
        w (trilinear), or full point sampling for a point filter."""
        slices = self._get_volume_slices(texture_desc)
        if not slices:
            return [0.0, 0.0, 0.0, 0.0]
        depth = len(slices)
        tu, tv, tw = sampler.transform_coordinates(u, v, w)
        _, mag_filter, _ = sampler._get_filter_mode()
        z = tw * depth - 0.5
        if mag_filter == 0:
            zi = min(max(int(round(z)), 0), depth - 1)
            return self._sample_nearest(slices[zi], tu, tv)
        z0 = math.floor(z)
        frac = z - z0
        if sampler.AddressW == D3D11_TEXTURE_ADDRESS_WRAP:
            z0c = int(z0) % depth
            z1c = (int(z0) + 1) % depth
        else:
            z0c = min(max(int(z0), 0), depth - 1)
            z1c = min(max(int(z0) + 1, 0), depth - 1)
        c0 = self._sample_linear(slices[z0c], tu, tv,
                                 sampler.AddressU, sampler.AddressV)
        c1 = (self._sample_linear(slices[z1c], tu, tv,
                                  sampler.AddressU, sampler.AddressV)
              if z1c != z0c else c0)
        return [c0[i] * (1 - frac) + c1[i] * frac for i in range(4)]

    def sample(self, u: float, v: float, w: float, texture_desc: TextureDesc, sampler: Sampler,
                ddx_uv: Optional[List[float]] = None, ddy_uv: Optional[List[float]] = None,
                name: str = '', array_slice: int = 0) -> List[float]:
        tu, tv, tw = sampler.transform_coordinates(u, v, w)

        min_filter, mag_filter, mip_filter = sampler._get_filter_mode()

        mip_levels = self._get_mip_levels(texture_desc, array_slice)
        level_count = len(mip_levels)

        # LOD selection. When screen-space UV derivatives are supplied (the PS
        # quad path), compute LOD the D3D11 way:
        #   rho = max(|ddx_uv * texsize|, |ddy_uv * texsize|);  LOD = log2(rho)
        # Otherwise fall back to treating the 3rd coordinate as an explicit LOD
        # (SampleLevel-style / 3-component coords / points & lines).
        if ddx_uv is not None and ddy_uv is not None:
            tex_h = len(mip_levels[0])
            tex_w = len(mip_levels[0][0]) if tex_h > 0 else 1
            dx_u, dx_v = ddx_uv[0] * tex_w, ddx_uv[1] * tex_h
            dy_u, dy_v = ddy_uv[0] * tex_w, ddy_uv[1] * tex_h
            rho_sq = max(dx_u * dx_u + dx_v * dx_v, dy_u * dy_u + dy_v * dy_v)
            lod = 0.5 * math.log2(rho_sq) if rho_sq > 1e-20 else 0.0
        else:
            # Explicit LOD (SampleLevel). Use the RAW w — `tw` has the
            # sampler's AddressW transform applied, which is only meaningful
            # for a Texture3D z coordinate; a Clamp AddressW would clamp
            # LOD 2.0 to 1.0 (Octopath terrain sampled the wrong mip on
            # every morphing vertex).
            lod = w if math.isfinite(w) else 0.0

        lod = lod + sampler.MipLODBias
        lod = max(sampler.MinLOD, min(sampler.MaxLOD, lod))

        if level_count == 1:
            single = (self._sample_nearest(mip_levels[0], tu, tv) if mag_filter == 0
                        else self._sample_linear(mip_levels[0], tu, tv,
                                                 sampler.AddressU, sampler.AddressV))
            if TRACE.texture_lod:
                TRACE.texture_sample(u, v, lod, ddx_uv, ddy_uv, single, name)
            return single

        lod_level = max(0.0, min(lod, float(level_count - 1)))
        level0 = int(lod_level)
        level1 = min(level0 + 1, level_count - 1)
        s = lod_level - level0

        # Within a mip level, D3D selects the Min filter when minifying
        # (lod > 0) and the Mag filter when magnifying. Point(0) → nearest
        # texel, Linear(1) → bilinear.
        level_filter = min_filter if lod_level > 0.0 else mag_filter

        def _within(level: int) -> List[float]:
            if level_filter == 0:
                return self._sample_nearest(mip_levels[level], tu, tv)
            return self._sample_linear(mip_levels[level], tu, tv,
                                       sampler.AddressU, sampler.AddressV)

        # Blend the two bracketing mip levels by the LOD fraction. NOTE: the
        # captured sampler here is Mip=Point, but our quad-derivative LOD runs
        # slightly high on tiny minified triangles where this texture has a
        # sharp level2→level3 step; trilinear blending tracks the GPU's result
        # there more closely than snapping to the rounded level, so we always
        # blend (s == 0 collapses to a single level anyway when LOD is integral).
        color0 = _within(level0)
        color1 = _within(level1)
        # No [0,1] clamp: the GPU returns the stored value as-is — a signed
        # R16G16_FLOAT detail normal (x = -0.083) or an HDR R11G11B10 probe
        # (> 1) must survive. UNORM/SNORM decodes are range-limited already.
        out = [color0[i] * (1 - s) + color1[i] * s for i in range(4)]
        if TRACE.texture_lod:
            TRACE.texture_sample(u, v, lod, ddx_uv, ddy_uv, out, name)
        return out