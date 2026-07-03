"""Fixed-function tessellator — the stage that sits between the Hull Shader
(HS) and the Domain Shader (DS) in the D3D11 tessellation pipeline.

Given a domain type (``tri`` / ``quad`` / ``isoline``), a partitioning mode
(``integer`` / ``pow2`` / ``fractional_odd`` / ``fractional_even``) and the per-
patch tessellation factors produced by the HS patch-constant (fork/join)
phase, it generates:

  * the **domain sample points** (``SV_DomainLocation``) — the coordinates the
    DS is invoked at, and
  * the **connectivity** (index list of triangles / lines) that stitches those
    points into primitives.

This is a *fixed-function* unit: no HLSL is interpreted here. The DS then runs
once per generated domain point (see ``HLSLInterpreter.executeDS_with_params``).

Domain-point coordinate conventions (matching D3D11 SV_DomainLocation):
  * quad     → (u, v)        with u, v in [0, 1]
  * tri      → (u, v, w)     barycentric, u + v + w = 1
  * isoline  → (u, v)        v = line coordinate, u = along-line coordinate

Notes on scope
--------------
Integer / pow2 partitioning is implemented exactly (the common, deterministic
case; factor 1 gives the minimal patch — 3 corners for a tri, 4 for a quad).
Fractional partitioning is approximated by rounding the factor up to the
partitioning's nearest even/odd integer segment count — the intermediate
domain-point placement of a real fractional tessellator (which slides the two
outermost segments) is not reproduced, so fractional factors give the correct
*count* and endpoint placement but evenly-spaced interior points. The captured
RenderDoc DS-output dumps this project validates against carry one DS vertex
per patch, so this approximation does not affect golden comparison; it exists
so the module is usable for full tessellation when factors are known.
"""

import math


def _round_partition(factor: float, partitioning: str) -> int:
    """Round a raw tessellation factor to an integer segment count per the
    partitioning mode. Returns the number of segments an edge is split into
    (>= 1); a factor <= 0 means the patch/edge is culled (0 segments)."""
    if factor is None:
        return 1
    if factor <= 0.0:
        return 0
    p = (partitioning or 'integer').lower()
    if p == 'pow2':
        n = 1
        while n < factor:
            n *= 2
        return max(1, min(64, n))
    if p == 'fractional_odd':
        # Odd segment counts: 1, 3, 5, ... up to 63.
        n = int(math.ceil(factor))
        if n % 2 == 0:
            n += 1
        return max(1, min(63, n))
    if p == 'fractional_even':
        # Even segment counts: 2, 4, 6, ... up to 64 (minimum 2).
        n = int(math.ceil(factor))
        if n < 2:
            n = 2
        if n % 2 == 1:
            n += 1
        return max(2, min(64, n))
    # integer
    return max(1, min(64, int(math.ceil(factor))))


def tessellate_quad(edge_factors, inside_factors, partitioning='integer'):
    """Tessellate a quad domain.

    edge_factors:   [Ueq0, Veq0, Ueq1, Veq1] (the 4 outer edges)
    inside_factors: [Uinside, Vinside]
    Returns (points, triangles): points is a list of (u, v); triangles is a
    list of (i0, i1, i2) index triples into points."""
    # Use the inside factors for the interior grid resolution; fall back to the
    # max edge factor. Real D3D stitches the outer ring to the inner grid; here
    # we use a uniform (nu+1) x (nv+1) grid, which is exact for equal factors.
    if inside_factors and len(inside_factors) >= 2:
        nu = _round_partition(inside_factors[0], partitioning)
        nv = _round_partition(inside_factors[1], partitioning)
    else:
        f = max(edge_factors) if edge_factors else 1
        nu = nv = _round_partition(f, partitioning)
    nu = max(1, nu)
    nv = max(1, nv)
    points = []
    for j in range(nv + 1):
        v = j / nv
        for i in range(nu + 1):
            u = i / nu
            points.append((u, v))
    row = nu + 1
    tris = []
    for j in range(nv):
        for i in range(nu):
            a = j * row + i
            b = j * row + i + 1
            c = (j + 1) * row + i
            d = (j + 1) * row + i + 1
            tris.append((a, b, c))
            tris.append((b, d, c))
    return points, tris


def tessellate_tri(edge_factors, inside_factor, partitioning='integer'):
    """Tessellate a triangle domain into barycentric (u, v, w) points.

    edge_factors:  [e0, e1, e2]
    inside_factor: scalar (or 1-element list)
    Returns (points, triangles)."""
    if isinstance(inside_factor, (list, tuple)):
        inside_factor = inside_factor[0] if inside_factor else 1
    n = _round_partition(inside_factor if inside_factor else
                         (max(edge_factors) if edge_factors else 1), partitioning)
    n = max(1, n)
    # Uniform barycentric subdivision: all (i, j, k) with i+j+k = n.
    points = []
    index_of = {}
    for i in range(n + 1):
        for j in range(n + 1 - i):
            k = n - i - j
            index_of[(i, j)] = len(points)
            # (u, v, w) barycentric
            points.append((i / n, j / n, k / n))
    tris = []
    for i in range(n):
        for j in range(n - i):
            a = index_of[(i, j)]
            b = index_of[(i + 1, j)]
            c = index_of[(i, j + 1)]
            tris.append((a, b, c))
            if j < n - i - 1:
                d = index_of[(i + 1, j + 1)]
                tris.append((b, d, c))
    return points, tris


def tessellate_isoline(factors, partitioning='integer'):
    """Tessellate an isoline domain.

    factors: [detail, density] — density = number of lines, detail = segments
    per line. Returns (points, lines): points (u, v); lines (i0, i1) pairs."""
    detail = factors[0] if factors else 1
    density = factors[1] if factors and len(factors) > 1 else 1
    nseg = max(1, _round_partition(detail, partitioning))
    nlines = max(1, int(math.ceil(density)) if density else 1)
    points = []
    lines = []
    for l in range(nlines):
        v = l / nlines if nlines > 1 else 0.0
        base = len(points)
        for s in range(nseg + 1):
            points.append((s / nseg, v))
        for s in range(nseg):
            lines.append((base + s, base + s + 1))
    return points, lines


def tessellate(domain, edge_factors, inside_factors=None, partitioning='integer'):
    """Dispatch by domain type. Returns (points, prims).

    domain: 'tri' | 'quad' | 'isoline'.
    edge_factors / inside_factors: per-patch factors from the HS fork phase.
    When factors are unavailable (patch-constant phase not decompiled), pass
    [1, 1, 1, 1] / [1, 1] for the minimal patch (quad -> 4 corners, tri -> 3)."""
    d = (domain or 'quad').lower()
    if d.startswith('tri'):
        inside = inside_factors[0] if inside_factors else 1
        return tessellate_tri(edge_factors or [1, 1, 1], inside, partitioning)
    if d.startswith('iso'):
        return tessellate_isoline(edge_factors or [1, 1], partitioning)
    # quad (default)
    return tessellate_quad(edge_factors or [1, 1, 1, 1],
                           inside_factors or [1, 1], partitioning)
