import json
import math
import time
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple
from enum import Enum

from pixel import Pixel
from d3d import (
    D3D_PRIMITIVE_TOPOLOGY_UNDEFINED,
    D3D_PRIMITIVE_TOPOLOGY_POINTLIST,
    D3D_PRIMITIVE_TOPOLOGY_LINELIST,
    D3D_PRIMITIVE_TOPOLOGY_LINESTRIP,
    D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST,
    D3D_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP,
    D3D_PRIMITIVE_TOPOLOGY_TRIANGLEFAN
)


# Epsilon for the homogeneous w>0 clip plane. Vertices with w below this lie on
# or behind the camera plane; clipping against w>=eps keeps the perspective
# divide finite regardless of the depth-clip setting. This plane is always
# active (D3D's clip volume 0<=z<=w implies w>0); near/far depth clipping is
# layered on top of it only when depth_clip_enable is True.
_CLIP_W_EPS = 1e-7


class CullMode(Enum):
    NONE = 0
    FRONT = 1
    BACK = 2


class FillMode(Enum):
    # D3D11_FILL_MODE: only wireframe and solid exist. Point/line rendering
    # comes from primitive topology, not the rasterizer fill mode.
    WIREFRAME = 2
    SOLID = 3


class FrontFace(Enum):
    COUNTER_CLOCKWISE = 0
    CLOCKWISE = 1


@dataclass
class Viewport:
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    min_depth: float = 0.0
    max_depth: float = 1.0

    def contains(self, x: float, y: float) -> bool:
        """Check if point is inside viewport"""
        return (self.x <= x < self.x + self.width and
                self.y <= y < self.y + self.height)

    def transform_to_screen(self, clip_x: float, clip_y: float, clip_w: float) -> Tuple[int, int]:
        """Transform clip coordinates to integer screen pixel coordinates.

        Used by the point/line paths, which walk whole pixels. The triangle path
        uses transform_to_screen_subpixel instead (D3D11 §3.4.1 snapping)."""
        if abs(clip_w) < 1e-8:
            return (int(self.x + self.width / 2), int(self.y + self.height / 2))
        ndc_x = clip_x / clip_w
        ndc_y = clip_y / clip_w
        screen_x = int((ndc_x + 1.0) * 0.5 * self.width + self.x)
        screen_y = int((1.0 - (ndc_y + 1.0) * 0.5) * self.height + self.y)
        return (screen_x, screen_y)

    def transform_to_screen_subpixel(self, clip_x: float, clip_y: float, clip_w: float) -> Tuple[float, float]:
        """Transform clip coordinates to sub-pixel screen coordinates per the
        D3D11 rasterizer rule §3.4.1 Coordinate Snapping.

        After the perspective divide and viewport scale, the x/y positions are
        snapped to n.8 fixed point — 8 fractional bits, i.e. a 1/256 sub-pixel
        grid. Unlike transform_to_screen this keeps the fractional position
        (does NOT truncate to whole pixels), so triangle edge functions and the
        Top-Left rule operate on the same snapped geometry the hardware uses."""
        if abs(clip_w) < 1e-8:
            return (self.x + self.width / 2.0, self.y + self.height / 2.0)
        ndc_x = clip_x / clip_w
        ndc_y = clip_y / clip_w
        screen_x = (ndc_x + 1.0) * 0.5 * self.width + self.x
        screen_y = (1.0 - (ndc_y + 1.0) * 0.5) * self.height + self.y
        # Snap to the 1/256 sub-pixel grid (8 fractional bits = n.8 fixed point).
        screen_x = round(screen_x * 256.0) / 256.0
        screen_y = round(screen_y * 256.0) / 256.0
        return (screen_x, screen_y)


@dataclass
class ScissorRect:
    left: int = 0
    top: int = 0
    right: int = 0
    bottom: int = 0

    def contains(self, x: int, y: int) -> bool:
        """Check if point is inside scissor rect (exclusive on right/bottom)"""
        return (self.left <= x < self.right and
                self.top <= y < self.bottom)


@dataclass
class RasterizerConfig:
    cull_mode: CullMode = CullMode.BACK
    fill_mode: FillMode = FillMode.SOLID
    front_face: FrontFace = FrontFace.COUNTER_CLOCKWISE
    scissor_enable: bool = False
    scissor_rect: ScissorRect = None
    multisample_enable: bool = False
    antialiasing_line_enable: bool = False
    depth_clip_enable: bool = True
    # Render-target 0 format string from pipeline_state.csv (e.g.
    # "R8G8B8A8_UNORM"). Drives the output-merger write clamp (see render.py
    # _rt_format_to_clamp_mode). None when the capture predates the format row.
    render_target_format: Optional[str] = None
    # Depth-stencil format string from pipeline_state.csv (e.g. "D16_UNORM").
    # None when no depth-stencil view is bound to the output merger.
    depth_stencil_format: Optional[str] = None
    viewport: Viewport = None

    def __post_init__(self):
        if self.scissor_rect is None:
            self.scissor_rect = ScissorRect()
        if self.viewport is None:
            self.viewport = Viewport()


@dataclass
class Triangle:
    """Triangle primitive with vertices and interpolated attributes"""
    v0: Dict[str, Any]  # Vertex 0 output data
    v1: Dict[str, Any]  # Vertex 1 output data
    v2: Dict[str, Any]  # Vertex 2 output data
    primitive_id: int = 0

    def get_position(self, vertex: Dict[str, Any]) -> List[float]:
        """Extract position from vertex output data"""
        if not vertex:
            return [0.0, 0.0, 0.0, 1.0]
        for key in ['sv_position', 'position', 'pos', 'Pos', 'SV_Position']:
            if key in vertex and vertex[key]:
                pos = vertex[key]
                if isinstance(pos, list):
                    if len(pos) == 4:
                        return pos
                    elif len(pos) == 3:
                        return [pos[0], pos[1], pos[2], 1.0]
        return [0.0, 0.0, 0.0, 1.0]

    def get_attribute(self, vertex: Dict[str, Any], attr_name: str) -> Any:
        """Extract attribute from vertex output data"""
        if not vertex:
            return None
        attr_name_lower = attr_name.lower()
        for key, value in vertex.items():
            if key.lower() == attr_name_lower:
                return value
        return None


class Rasterizer:
    """
    D3D11-style rasterizer implementation

    Receives HLSLInterpreter's vertex shader output and performs:
    - Primitive assembly (points, lines, triangles)
    - Vertex post-processing (viewport transform, clipping)
    - Triangle culling
    - Rasterization with barycentric coordinate interpolation
    - Scissor testing
    - MSAA support
    """

    def __init__(self, config_path: str = None):
        self.config = RasterizerConfig()
        self._primitive_id_counter = 0
        self._pixels: List[Pixel] = []
        self.stats = self._new_stats()
        # Optional live-animation view (WebMeshView): when set, rasterize()
        # shares its growing pixel list and reports per-primitive progress so the
        # browser animates pixels appearing, and honours a per-primitive delay.
        self._anim_view = None

        if config_path:
            self.load_config(config_path)

    def set_animation_hook(self, view):
        """Attach a WebMeshView for live rasterizer progress/animation. Pass None
        to detach. Only used when the view exposes bind_rast_pixels."""
        self._anim_view = view if (view is not None and
                                   hasattr(view, 'bind_rast_pixels')) else None

    def _anim_primitive_done(self):
        """Report one finished primitive to the live view and apply the
        per-primitive animation delay (read live so slider changes take effect).
        Uses stats['primitives'] as the running count and the total bound at
        bind_rast_pixels time."""
        view = self._anim_view
        if view is None:
            return
        view.set_rast_progress(self.stats['primitives'], None, len(self._pixels))
        d = view.get_delay('primitive')
        if d > 0:
            time.sleep(d)

    @staticmethod
    def _new_stats() -> Dict[str, int]:
        """Per-rasterize() primitive/pixel counters."""
        return {
            'primitives': 0,    # primitives assembled from the vertex stream
            'clipped': 0,       # primitives discarded before cull (degenerate w,
                                # zero area, or bounding box fully off-viewport)
            'not_clipped': 0,
            'culled': 0,        # primitives discarded by face culling
            'not_culled': 0,    # primitives that reached pixel generation
            'pixels': 0,        # fragments emitted to the pixel list
        }

    def get_stats(self) -> Dict[str, int]:
        """Counters from the most recent rasterize() call."""
        return self.stats

    def _tally(self, status: str):
        """Fold a per-primitive rasterize status into the running stats.
        status is one of 'clipped', 'culled', 'rasterized'."""
        if status == 'clipped':
            self.stats['clipped'] += 1
        elif status == 'culled':
            self.stats['not_clipped'] += 1
            self.stats['culled'] += 1
        else:  # 'rasterized'
            self.stats['not_clipped'] += 1
            self.stats['not_culled'] += 1

    def load_config(self, config_path: str):
        """Load rasterizer configuration from JSON file"""
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = json.load(f)

            cull_mode_map = {
                'none': CullMode.NONE,
                'front': CullMode.FRONT,
                'back': CullMode.BACK
            }
            cull_mode_str = config_data.get('cull_mode', 'back').lower()
            self.config.cull_mode = cull_mode_map.get(cull_mode_str, CullMode.BACK)

            fill_mode_map = {
                'wireframe': FillMode.WIREFRAME,
                'solid': FillMode.SOLID
            }
            fill_mode_str = config_data.get('fill_mode', 'solid').lower()
            self.config.fill_mode = fill_mode_map.get(fill_mode_str, FillMode.SOLID)

            front_face_str = config_data.get('front_face', 'counter_clockwise').lower()
            if front_face_str == 'clockwise':
                self.config.front_face = FrontFace.CLOCKWISE
            else:
                self.config.front_face = FrontFace.COUNTER_CLOCKWISE

            self.config.scissor_enable = config_data.get('scissor_enable', False)

            if 'scissor_rect' in config_data:
                sr = config_data['scissor_rect']
                self.config.scissor_rect = ScissorRect(
                    left=sr.get('left', 0),
                    top=sr.get('top', 0),
                    right=sr.get('right', 0),
                    bottom=sr.get('bottom', 0)
                )

            self.config.multisample_enable = config_data.get('multisample_enable', False)
            self.config.antialiasing_line_enable = config_data.get('antialiasing_line_enable', False)
            self.config.depth_clip_enable = config_data.get('depth_clip_enable', True)

            if 'viewport' in config_data:
                vp = config_data['viewport']
                self.config.viewport = Viewport(
                    x=vp.get('x', 0.0),
                    y=vp.get('y', 0.0),
                    width=vp.get('width', 800.0),
                    height=vp.get('height', 600.0),
                    min_depth=vp.get('min_depth', 0.0),
                    max_depth=vp.get('max_depth', 1.0)
                )

        except Exception as e:
            print(f"Warning: Failed to load rasterizer config from {config_path}: {e}")

    def clear_pixels(self):
        """Clear the pixel output buffer"""
        self._pixels = []

    def get_pixels(self) -> List[Pixel]:
        """Get the rasterized pixels"""
        return self._pixels

    def rasterize(self, results: List[Dict[str, Any]], primitive_topology: int = D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST) -> List[Pixel]:
        """
        Rasterize vertex shader output

        Args:
            results: List of vertex output dictionaries from HLSLInterpreter executorVS
            primitive_topology: D3D_PRIMITIVE_TOPOLOGY_* value

        Returns:
            List of Pixel objects representing rasterized fragments
        """
        self.clear_pixels()
        self.stats = self._new_stats()

        # Live animation: share the growing pixel list + total primitive count.
        if self._anim_view is not None:
            n = len(results)
            if primitive_topology == D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST:
                total_prims = n // 3
            elif primitive_topology == D3D_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP:
                total_prims = max(0, n - 2)
            elif primitive_topology == D3D_PRIMITIVE_TOPOLOGY_LINELIST:
                total_prims = n // 2
            elif primitive_topology == D3D_PRIMITIVE_TOPOLOGY_LINESTRIP:
                total_prims = max(0, n - 1)
            elif primitive_topology == D3D_PRIMITIVE_TOPOLOGY_POINTLIST:
                total_prims = n
            else:
                total_prims = 0
            self._anim_view.bind_rast_pixels(self._pixels, total_prims)

        if primitive_topology == D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST:
            self._rasterize_triangle_list(results)
        elif primitive_topology == D3D_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP:
            self._rasterize_triangle_strip(results)
        elif primitive_topology == D3D_PRIMITIVE_TOPOLOGY_LINELIST:
            self._rasterize_line_list(results)
        elif primitive_topology == D3D_PRIMITIVE_TOPOLOGY_LINESTRIP:
            self._rasterize_line_strip(results)
        elif primitive_topology == D3D_PRIMITIVE_TOPOLOGY_POINTLIST:
            self._rasterize_point_list(results)

        self.stats['pixels'] = len(self._pixels)
        if self._anim_view is not None:
            self._anim_view.set_rast_progress(
                self.stats['primitives'], self.stats['primitives'], len(self._pixels))
        return self._pixels

    def _rasterize_triangle_list(self, vertices: List[Dict[str, Any]]):
        """Rasterize triangle list - every 3 vertices form a triangle"""
        num_primitives = len(vertices) // 3
        for i in range(num_primitives):
            tri = Triangle(
                v0=vertices[i * 3],
                v1=vertices[i * 3 + 1],
                v2=vertices[i * 3 + 2],
                primitive_id=i
            )
            self.stats['primitives'] += 1
            self._tally(self._rasterize_triangle(tri))
            self._anim_primitive_done()

    def _rasterize_triangle_strip(self, vertices: List[Dict[str, Any]]):
        """Rasterize triangle strip"""
        if len(vertices) < 3:
            return
        for i in range(len(vertices) - 2):
            tri = Triangle(
                v0=vertices[i],
                v1=vertices[i + 1],
                v2=vertices[i + 2],
                primitive_id=i
            )
            self.stats['primitives'] += 1
            self._tally(self._rasterize_triangle(tri))
            self._anim_primitive_done()

    def _rasterize_line_list(self, vertices: List[Dict[str, Any]]):
        """Rasterize line list - every 2 vertices form a line"""
        if len(vertices) < 2:
            return
        for i in range(0, len(vertices) - 1, 2):
            self.stats['primitives'] += 1
            self._tally(self._rasterize_line(vertices[i], vertices[i + 1], i // 2))
            self._anim_primitive_done()

    def _rasterize_line_strip(self, vertices: List[Dict[str, Any]]):
        """Rasterize line strip"""
        if len(vertices) < 2:
            return
        for i in range(len(vertices) - 1):
            self.stats['primitives'] += 1
            self._tally(self._rasterize_line(vertices[i], vertices[i + 1], i))
            self._anim_primitive_done()

    def _rasterize_point_list(self, vertices: List[Dict[str, Any]]):
        """Rasterize point list"""
        for i, vertex in enumerate(vertices):
            self.stats['primitives'] += 1
            self._tally(self._rasterize_point(vertex, i))
            self._anim_primitive_done()

    def _rasterize_point(self, vertex: Dict[str, Any], primitive_id: int) -> str:
        """Rasterize a point primitive. Returns 'clipped' or 'rasterized'."""
        pos = self._get_vertex_position(vertex)
        if pos is None:
            return 'clipped'

        clip_w = pos[3] if len(pos) >= 4 else 1.0
        # w-clip: a point on or behind the camera plane has no valid projection.
        if clip_w < _CLIP_W_EPS:
            return 'clipped'

        ndc_z = pos[2] / clip_w
        # Near/far depth clip: discard the point if it falls outside [0,1] in z.
        if self.config.depth_clip_enable and (ndc_z < 0.0 or ndc_z > 1.0):
            return 'clipped'

        screen_x, screen_y = self.config.viewport.transform_to_screen(pos[0], pos[1], clip_w)

        if not self._is_in_viewport(screen_x, screen_y):
            return 'clipped'

        if self.config.scissor_enable and not self._is_in_scissor(screen_x, screen_y):
            return 'clipped'

        pixel = Pixel(
            x=screen_x,
            y=screen_y,
            depth=self._map_depth(ndc_z),
            color=self._interpolate_vertex_attribute(vertex, 'Color'),
            texcoord=self._interpolate_vertex_attribute(vertex, 'TexCoord'),
            texcoord2=self._interpolate_vertex_attribute(vertex, 'TexCoord2'),
            normal=self._interpolate_vertex_attribute(vertex, 'Normal'),
            worldPos=self._interpolate_vertex_attribute(vertex, 'WorldPos'),
            attributes={},
            primitive_id=primitive_id
        )
        self._pixels.append(pixel)
        return 'rasterized'

    def _rasterize_line(self, v0: Dict[str, Any], v1: Dict[str, Any], primitive_id: int) -> str:
        """Rasterize a line primitive using DDA. Returns 'clipped' or 'rasterized'.

        The segment is first clipped in clip space against the w>0 plane (always)
        and the near/far depth planes (when depth_clip_enable is True); the
        clipped endpoints replace the originals before the screen-space walk.
        """
        clipped = self._clip_segment(v0, v1)
        if clipped is None:
            return 'clipped'
        v0, v1 = clipped

        pos0 = self._get_vertex_position(v0)
        pos1 = self._get_vertex_position(v1)

        if pos0 is None or pos1 is None:
            return 'clipped'

        clip_w0 = pos0[3] if len(pos0) >= 4 else 1.0
        clip_w1 = pos1[3] if len(pos1) >= 4 else 1.0

        if abs(clip_w0) < 1e-8 or abs(clip_w1) < 1e-8:
            return 'clipped'

        # Post-clip NDC z at each endpoint; depth is interpolated screen-linearly.
        ndc_z0 = pos0[2] / clip_w0
        ndc_z1 = pos1[2] / clip_w1

        screen_x0, screen_y0 = self.config.viewport.transform_to_screen(pos0[0], pos0[1], clip_w0)
        screen_x1, screen_y1 = self.config.viewport.transform_to_screen(pos1[0], pos1[1], clip_w1)

        dx = abs(screen_x1 - screen_x0)
        dy = abs(screen_y1 - screen_y0)
        steps = max(dx, dy) + 1

        if steps < 1:
            steps = 1

        for i in range(int(steps)):
            t = i / max(steps - 1, 1) if steps > 1 else 0
            screen_x = int(screen_x0 + (screen_x1 - screen_x0) * t)
            screen_y = int(screen_y0 + (screen_y1 - screen_y0) * t)

            if not self._is_in_viewport(screen_x, screen_y):
                continue

            if self.config.scissor_enable and not self._is_in_scissor(screen_x, screen_y):
                continue

            depth = self._map_depth(ndc_z0 + (ndc_z1 - ndc_z0) * t)

            interpolated_attrs = self._interpolate_attributes_line(v0, v1, t, clip_w0, clip_w1)

            pixel = Pixel(
                x=screen_x,
                y=screen_y,
                depth=depth,
                color=interpolated_attrs.get('Color'),
                texcoord=interpolated_attrs.get('TexCoord'),
                texcoord2=interpolated_attrs.get('TexCoord2'),
                normal=interpolated_attrs.get('Normal'),
                worldPos=interpolated_attrs.get('WorldPos'),
                attributes={},
                primitive_id=primitive_id
            )
            self._pixels.append(pixel)
        return 'rasterized'

    def _map_depth(self, ndc_z: float) -> float:
        """Map an NDC z (post perspective-divide) to the viewport depth range.

        depth = MinDepth + ndc_z * (MaxDepth - MinDepth). When depth clipping is
        DISABLED, geometry outside the near/far planes is kept but its depth is
        clamped to the viewport range (D3D11 DepthClipEnable=FALSE semantics).
        When ENABLED, callers have already removed out-of-range geometry, so no
        clamp is applied (and with the default [0,1] range the map is identity)."""
        min_d = self.config.viewport.min_depth
        max_d = self.config.viewport.max_depth
        depth = min_d + ndc_z * (max_d - min_d)
        if not self.config.depth_clip_enable:
            lo, hi = (min_d, max_d) if min_d <= max_d else (max_d, min_d)
            depth = max(lo, min(hi, depth))
        return depth

    def _lerp_vertex(self, va: Dict[str, Any], vb: Dict[str, Any], t: float) -> Dict[str, Any]:
        """Linearly interpolate two vertex dicts at parameter t, producing the
        vertex created where a clip plane crosses edge a->b.

        Interpolation is LINEAR in homogeneous clip space — positions (incl. w)
        and every attribute alike. This is the correct basis for clip-space
        clipping: the rasterizer's later perspective divide (attr/w) restores
        perspective-correct values, because both the attribute and w were
        interpolated the same way here."""
        out: Dict[str, Any] = {}
        for k in set(va.keys()) | set(vb.keys()):
            a = va.get(k)
            b = vb.get(k)
            if isinstance(a, list) and isinstance(b, list):
                n = min(len(a), len(b))
                out[k] = [a[i] + (b[i] - a[i]) * t for i in range(n)]
            elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
                out[k] = a + (b - a) * t
            else:
                out[k] = a if a is not None else b
        return out

    def _clip_planes(self):
        """Active clip-space half-space tests, as dist(pos)>=0 predicates.

        The w>0 plane is always present (the clip volume 0<=z<=w requires w>0,
        and it keeps the perspective divide finite). Near (z>=0) and far (z<=w)
        are added only when depth_clip_enable is True."""
        planes = [lambda p: p[3] - _CLIP_W_EPS]            # w >= eps
        if self.config.depth_clip_enable:
            planes.append(lambda p: p[2])                  # near: z >= 0
            planes.append(lambda p: p[3] - p[2])           # far:  z <= w
        return planes

    def _clip_segment(self, v0: Dict[str, Any], v1: Dict[str, Any]):
        """Liang-Barsky clip of a line segment against the active clip planes.
        Returns (cv0, cv1) clipped vertex dicts, or None if fully outside.
        A fully-inside segment returns its original endpoint objects."""
        p0 = self._get_vertex_position(v0)
        p1 = self._get_vertex_position(v1)
        if p0 is None or p1 is None:
            return None

        t0, t1 = 0.0, 1.0
        for dist in self._clip_planes():
            d0 = dist(p0)
            d1 = dist(p1)
            if d0 < 0.0 and d1 < 0.0:
                return None                 # both endpoints outside this plane
            if d0 >= 0.0 and d1 >= 0.0:
                continue                    # both inside; no constraint
            t = d0 / (d0 - d1)              # crossing along the original segment
            if d0 < 0.0:
                t0 = max(t0, t)             # entering the half-space
            else:
                t1 = min(t1, t)             # leaving the half-space
            if t0 > t1:
                return None
        cv0 = self._lerp_vertex(v0, v1, t0) if t0 > 0.0 else v0
        cv1 = self._lerp_vertex(v0, v1, t1) if t1 < 1.0 else v1
        return cv0, cv1

    def _clip_polygon_against_plane(self, verts, dist):
        """Sutherland-Hodgman clip of a polygon (list of vertex dicts) against a
        single clip-space plane. `dist(pos)>=0` marks the inside half-space."""
        out = []
        n = len(verts)
        for i in range(n):
            cur = verts[i]
            nxt = verts[(i + 1) % n]
            dc = dist(self._get_vertex_position(cur))
            dn = dist(self._get_vertex_position(nxt))
            cur_in = dc >= 0.0
            nxt_in = dn >= 0.0
            if cur_in:
                out.append(cur)
            if cur_in != nxt_in:
                t = dc / (dc - dn)
                out.append(self._lerp_vertex(cur, nxt, t))
        return out

    def _clip_triangle(self, v0: Dict[str, Any], v1: Dict[str, Any], v2: Dict[str, Any]):
        """Clip a triangle against the active clip planes and return a fan of
        (va, vb, vc) sub-triangles. Empty list if fully clipped. A triangle
        fully inside every plane returns [(v0, v1, v2)] with the ORIGINAL vertex
        objects untouched — the common case incurs no new vertices and no
        floating-point perturbation."""
        verts = [v0, v1, v2]
        for dist in self._clip_planes():
            verts = self._clip_polygon_against_plane(verts, dist)
            if len(verts) < 3:
                return []
        return [(verts[0], verts[i], verts[i + 1]) for i in range(1, len(verts) - 1)]

    def _rasterize_triangle(self, triangle: Triangle) -> str:
        """Clip the triangle against the depth-clip planes, then rasterize each
        resulting sub-triangle. Returns the aggregate status ('clipped',
        'culled', or 'rasterized') for the original primitive."""
        if (self._get_vertex_position(triangle.v0) is None or
                self._get_vertex_position(triangle.v1) is None or
                self._get_vertex_position(triangle.v2) is None):
            return 'clipped'

        sub_tris = self._clip_triangle(triangle.v0, triangle.v1, triangle.v2)
        if not sub_tris:
            return 'clipped'

        statuses = [self._raster_triangle_core(a, b, c, triangle.primitive_id)
                    for (a, b, c) in sub_tris]
        if any(s == 'rasterized' for s in statuses):
            return 'rasterized'
        if any(s == 'culled' for s in statuses):
            return 'culled'
        return 'clipped'

    def _raster_triangle_core(self, v0: Dict[str, Any], v1: Dict[str, Any], v2: Dict[str, Any],
                              primitive_id: int) -> str:
        """Rasterize a single (already clip-space-clipped) triangle using
        barycentric coordinates. Returns 'clipped', 'culled', or 'rasterized'."""
        v0_pos = self._get_vertex_position(v0)
        v1_pos = self._get_vertex_position(v1)
        v2_pos = self._get_vertex_position(v2)

        if v0_pos is None or v1_pos is None or v2_pos is None:
            return 'clipped'

        clip_w0 = v0_pos[3] if len(v0_pos) >= 4 else 1.0
        clip_w1 = v1_pos[3] if len(v1_pos) >= 4 else 1.0
        clip_w2 = v2_pos[3] if len(v2_pos) >= 4 else 1.0

        if abs(clip_w0) < 1e-8 or abs(clip_w1) < 1e-8 or abs(clip_w2) < 1e-8:
            return 'clipped'

        # §3.4.1 Coordinate Snapping: snap x/y to the n.8 (1/256) sub-pixel grid
        # AFTER perspective divide + viewport scale, keeping the fractional
        # position. Culling and interpolation are set up on these snapped
        # positions (per spec: "front/back culling is applied after vertices
        # have been snapped"; "interpolation ... is set up based on the snapped
        # vertex positions").
        screen_v0 = self.config.viewport.transform_to_screen_subpixel(v0_pos[0], v0_pos[1], clip_w0)
        screen_v1 = self.config.viewport.transform_to_screen_subpixel(v1_pos[0], v1_pos[1], clip_w1)
        screen_v2 = self.config.viewport.transform_to_screen_subpixel(v2_pos[0], v2_pos[1], clip_w2)

        # Pixel bounding box: the integer pixels whose CENTER sample (x+0.5,y+0.5)
        # could fall inside the snapped triangle. floor/ceil over the sub-pixel
        # extents is conservative; the per-sample coverage test trims the edges.
        min_x = int(math.floor(min(screen_v0[0], screen_v1[0], screen_v2[0])))
        max_x = int(math.ceil(max(screen_v0[0], screen_v1[0], screen_v2[0])))
        min_y = int(math.floor(min(screen_v0[1], screen_v1[1], screen_v2[1])))
        max_y = int(math.ceil(max(screen_v0[1], screen_v1[1], screen_v2[1])))

        min_x = max(min_x, int(self.config.viewport.x))
        max_x = min(max_x, int(self.config.viewport.x + self.config.viewport.width - 1))
        min_y = max(min_y, int(self.config.viewport.y))
        max_y = min(max_y, int(self.config.viewport.y + self.config.viewport.height - 1))

        if min_x > max_x or min_y > max_y:
            return 'clipped'

        v0_ndc = [v0_pos[0] / clip_w0, v0_pos[1] / clip_w0, v0_pos[2] / clip_w0]
        v1_ndc = [v1_pos[0] / clip_w1, v1_pos[1] / clip_w1, v1_pos[2] / clip_w1]
        v2_ndc = [v2_pos[0] / clip_w2, v2_pos[1] / clip_w2, v2_pos[2] / clip_w2]

        area = self._edge_function(screen_v0, screen_v1, screen_v2)
        if abs(area) < 1e-10:
            return 'clipped'

        if self._should_cull_triangle(screen_v0, screen_v1, screen_v2):
            return 'culled'

        # §3.4.2.1 Top-Left Rule setup. Normalize by sign(area) so the triangle
        # interior is the e_i >= 0 half-space for every edge regardless of
        # winding. An edge whose direction (oriented to that canonical winding)
        # points straight left is the "top" edge; one that points downward (in
        # screen y-down space) is a "left" edge. A sample lying exactly on an
        # edge (e_i == 0) is inside only if that edge is top or left — so a
        # shared edge is drawn by exactly one of the two adjacent triangles.
        edge_sign = 1.0 if area > 0 else -1.0

        def _is_top_left(p_start, p_end):
            dx = (p_end[0] - p_start[0]) * edge_sign
            dy = (p_end[1] - p_start[1]) * edge_sign
            is_top = (dy == 0.0 and dx < 0.0)
            is_left = (dy > 0.0)
            return is_top or is_left

        # Edge i is the one opposite vertex i, matching the w_i below:
        #   w0 uses edge v1->v2, w1 uses edge v2->v0, w2 uses edge v0->v1.
        tl0 = _is_top_left(screen_v1, screen_v2)
        tl1 = _is_top_left(screen_v2, screen_v0)
        tl2 = _is_top_left(screen_v0, screen_v1)

        # FillMode.WIREFRAME: draw the triangle as its 3 edges. Culling above
        # still applies, matching D3D11 which only rasterizes wireframe fills for
        # triangles that survive the cull.
        if self.config.fill_mode == FillMode.WIREFRAME:
            self._rasterize_line(v0, v1, primitive_id)
            self._rasterize_line(v1, v2, primitive_id)
            self._rasterize_line(v2, v0, primitive_id)
            return 'rasterized'

        def _shade_lane(x: int, y: int):
            """Interpolate one lane unconditionally (helpers included so derivatives
            stay valid at triangle edges). Returns (interpolated, depth, covered).

            §3.4.2: the coverage test samples the PIXEL CENTER (x+0.5, y+0.5),
            not the integer corner."""
            p = (x + 0.5, y + 0.5)
            w0 = self._edge_function(screen_v1, screen_v2, p)
            w1 = self._edge_function(screen_v2, screen_v0, p)
            w2 = self._edge_function(screen_v0, screen_v1, p)

            bary_x = w0 / area
            bary_y = w1 / area
            bary_z = w2 / area

            interpolated = self._interpolate_with_barycentric(
                v0, v1, v2,
                bary_x, bary_y, bary_z,
                clip_w0, clip_w1, clip_w2
            )
            # Screen-linear NDC z (a convex combination of the post-clip vertex
            # depths, hence within [0,1] when depth clipping is enabled), then
            # mapped to the viewport depth range. Near/far rejection is handled
            # geometrically by _clip_triangle, not per-pixel here.
            ndc_z = bary_x * v0_ndc[2] + bary_y * v1_ndc[2] + bary_z * v2_ndc[2]
            depth = self._map_depth(ndc_z)

            # §3.4.2 + §3.4.2.1: a sample is inside when it is in the interior
            # half-space of every edge (e_i > 0), AND for any edge it lies
            # exactly on (e_i == 0) that edge is a top or left edge. e_i is the
            # winding-normalized edge value (interior is e_i >= 0). All inputs
            # are exact multiples of 1/256 (snapped verts) plus the 0.5 center,
            # so the products/sums are exact dyadic rationals and "== 0.0" is a
            # reliable on-edge test, not a floating-point gamble.
            e0 = w0 * edge_sign
            e1 = w1 * edge_sign
            e2 = w2 * edge_sign
            inside = e0 >= 0.0 and e1 >= 0.0 and e2 >= 0.0
            if inside:
                if e0 == 0.0 and not tl0:
                    inside = False
                elif e1 == 0.0 and not tl1:
                    inside = False
                elif e2 == 0.0 and not tl2:
                    inside = False
            covered = (
                inside
                and min_x <= x <= max_x and min_y <= y <= max_y
                and (not self.config.scissor_enable or self._is_in_scissor(x, y))
            )
            return interpolated, depth, covered

        # Quad-aligned traversal: walk 2x2 blocks snapped to even coords so each
        # covered fragment carries the whole quad's interpolated inputs (lanes in
        # TL,TR,BL,BR order). Helper lanes (not covered) feed derivatives only.
        qy_start = int(min_y) & ~1
        qx_start = int(min_x) & ~1
        for qy in range(qy_start, int(max_y) + 1, 2):
            for qx in range(qx_start, int(max_x) + 1, 2):
                lane_coords = ((qx, qy), (qx + 1, qy), (qx, qy + 1), (qx + 1, qy + 1))
                lanes = [_shade_lane(lx, ly) for (lx, ly) in lane_coords]

                if not any(lane[2] for lane in lanes):
                    continue

                quad_inputs = [lane[0] for lane in lanes]
                for lane_idx, ((lx, ly), (interpolated, depth, covered)) in enumerate(
                    zip(lane_coords, lanes)
                ):
                    if not covered:
                        continue
                    pixel = Pixel(
                        x=lx,
                        y=ly,
                        depth=depth,
                        color=interpolated.get('Color'),
                        texcoord=interpolated.get('TexCoord'),
                        texcoord2=interpolated.get('TexCoord2'),
                        normal=interpolated.get('Normal'),
                        worldPos=interpolated.get('WorldPos'),
                        attributes=interpolated.get('attributes', {}),
                        primitive_id=primitive_id,
                        quad_lane=lane_idx,
                        quad_inputs=quad_inputs
                    )
                    self._pixels.append(pixel)
        return 'rasterized'

    def _should_cull_triangle(self, v0: Tuple[int, int], v1: Tuple[int, int], v2: Tuple[int, int]) -> bool:
        """Determine if triangle should be culled based on cull mode"""
        if self.config.cull_mode == CullMode.NONE:
            return False

        edge1_x = v1[0] - v0[0]
        edge1_y = v1[1] - v0[1]
        edge2_x = v2[0] - v0[0]
        edge2_y = v2[1] - v0[1]

        cross_z = edge1_x * edge2_y - edge1_y * edge2_x

        # NOTE: v0/v1/v2 are screen-space coords AFTER the viewport Y-flip
        # (transform_to_screen: screen_y grows downward). In this Y-down space
        # the 2D cross product's sign is inverted vs. the math (Y-up) convention:
        #   cross_z > 0  <=>  vertices wind CLOCKWISE on screen
        #   cross_z < 0  <=>  vertices wind COUNTER-CLOCKWISE on screen
        # D3D11 FrontCounterClockwise=FALSE (FrontFace=CW) => CW-on-screen is the
        # front face, so front <=> cross_z > 0 (and CCW front <=> cross_z < 0).
        if self.config.cull_mode == CullMode.BACK:
            if self.config.front_face == FrontFace.COUNTER_CLOCKWISE:
                return cross_z > 0   # front is CCW (cross_z<0); cull the CW back faces
            else:
                return cross_z < 0   # front is CW (cross_z>0); cull the CCW back faces
        elif self.config.cull_mode == CullMode.FRONT:
            if self.config.front_face == FrontFace.COUNTER_CLOCKWISE:
                return cross_z < 0
            else:
                return cross_z > 0

        return False

    def _edge_function(self, a: Tuple[int, int], b: Tuple[int, int], c: Tuple[int, int]) -> float:
        """Calculate edge function for barycentric coordinates"""
        return (c[0] - a[0]) * (b[1] - a[1]) - (c[1] - a[1]) * (b[0] - a[0])

    def _interpolate_with_barycentric(self, v0: Dict[str, Any], v1: Dict[str, Any], v2: Dict[str, Any],
                                      bary_x: float, bary_y: float, bary_z: float,
                                      clip_w0: float = 1.0, clip_w1: float = 1.0, clip_w2: float = 1.0) -> Dict[str, Any]:
        """
        Interpolate vertex attributes using barycentric coordinates with D3D11 perspective-correct interpolation.

        Uses perspective-correct (trilinear) interpolation: attributes are divided by w before interpolation,
        then the result is divided by the interpolated 1/w to get the correct perspective value.
        """
        result = {}

        attr_names = set()
        for v in [v0, v1, v2]:
            if v:
                attr_names.update(v.keys())

        inv_w0 = 1.0 / clip_w0 if abs(clip_w0) > 1e-8 else 0.0
        inv_w1 = 1.0 / clip_w1 if abs(clip_w1) > 1e-8 else 0.0
        inv_w2 = 1.0 / clip_w2 if abs(clip_w2) > 1e-8 else 0.0

        interpolated_inv_w = bary_x * inv_w0 + bary_y * inv_w1 + bary_z * inv_w2
        if abs(interpolated_inv_w) < 1e-8:
            interpolated_inv_w = 1.0

        for attr_name in attr_names:
            attr_lower = attr_name.lower()
            if attr_lower in ['sv_position', 'position', 'pos', 'sv_position']:
                continue

            vals = []
            for v in [v0, v1, v2]:
                if v and attr_name in v:
                    vals.append(v[attr_name])
                else:
                    vals.append(None)

            if all(isinstance(v, list) and v is not None for v in vals):
                min_len = min(len(v) for v in vals if isinstance(v, list))
                interpolated = []

                # All PS-input attributes are perspective-correct by default
                # (DXBC `dcl_input_ps linear ...`). COLOR is no exception: D3D
                # neither clamps the interpolant nor drops perspective for it —
                # the VS may legitimately output HDR vertex colors (>1) that the
                # PS scales, and clamping/affine-interpolating here would darken
                # and skew the result. The output-merger clamp (post-PS) is the
                # only place values are clamped to the RT range.
                for i in range(min_len):
                    comp0 = vals[0][i] if len(vals[0]) > i else 0.0
                    comp1 = vals[1][i] if len(vals[1]) > i else 0.0
                    comp2 = vals[2][i] if len(vals[2]) > i else 0.0

                    attr0_normalized = comp0 * inv_w0
                    attr1_normalized = comp1 * inv_w1
                    attr2_normalized = comp2 * inv_w2

                    attr_interpolated_normalized = bary_x * attr0_normalized + bary_y * attr1_normalized + bary_z * attr2_normalized

                    attr_interpolated = attr_interpolated_normalized / interpolated_inv_w
                    interpolated.append(attr_interpolated)

                result[attr_name] = interpolated
            elif all(isinstance(v, (int, float)) and v is not None for v in vals):
                attr0_normalized = vals[0] * inv_w0
                attr1_normalized = vals[1] * inv_w1
                attr2_normalized = vals[2] * inv_w2
                attr_interpolated_normalized = bary_x * attr0_normalized + bary_y * attr1_normalized + bary_z * attr2_normalized
                result[attr_name] = attr_interpolated_normalized / interpolated_inv_w

        return result

    def _interpolate_vertex_attribute(self, vertex: Dict[str, Any], attr_name: str) -> Any:
        """Interpolate a single attribute from vertex"""
        if vertex and attr_name in vertex:
            return vertex[attr_name]
        return None

    def _interpolate_attributes_line(self, v0: Dict[str, Any], v1: Dict[str, Any], t: float,
                                      clip_w0: float = 1.0, clip_w1: float = 1.0) -> Dict[str, Any]:
        """Interpolate attributes for line at parameter t with perspective-correct interpolation"""
        result = {}

        if not v0 or not v1:
            return result

        inv_w0 = 1.0 / clip_w0 if abs(clip_w0) > 1e-8 else 0.0
        inv_w1 = 1.0 / clip_w1 if abs(clip_w1) > 1e-8 else 0.0
        one_minus_t = 1.0 - t
        interpolated_inv_w = one_minus_t * inv_w0 + t * inv_w1
        if abs(interpolated_inv_w) < 1e-8:
            interpolated_inv_w = 1.0

        attr_names = set(v0.keys()) | set(v1.keys())

        for attr_name in attr_names:
            attr_lower = attr_name.lower()
            if attr_lower in ['sv_position', 'position', 'pos', 'sv_position']:
                continue

            val0 = v0.get(attr_name)
            val1 = v1.get(attr_name)

            if val0 is None and val1 is None:
                continue

            if val0 is None:
                val0 = val1
            if val1 is None:
                val1 = val0

            if isinstance(val0, list) and isinstance(val1, list):
                min_len = min(len(val0), len(val1))
                interpolated = []

                # Perspective-correct for all attributes incl. COLOR, with no
                # clamp (see the triangle path for rationale).
                for i in range(min_len):
                    v0_comp = val0[i] if i < len(val0) else 0.0
                    v1_comp = val1[i] if i < len(val1) else 0.0
                    v0_normalized = v0_comp * inv_w0
                    v1_normalized = v1_comp * inv_w1
                    val_normalized = one_minus_t * v0_normalized + t * v1_normalized
                    val = val_normalized / interpolated_inv_w
                    interpolated.append(val)
                result[attr_name] = interpolated
            elif isinstance(val0, (int, float)) and isinstance(val1, (int, float)):
                v0_normalized = val0 * inv_w0
                v1_normalized = val1 * inv_w1
                val_normalized = one_minus_t * v0_normalized + t * v1_normalized
                result[attr_name] = val_normalized / interpolated_inv_w
            elif val0 is not None:
                result[attr_name] = val0

        return result

    def _get_vertex_position(self, vertex: Dict[str, Any]) -> Optional[List[float]]:
        """Extract position from vertex output"""
        if not vertex:
            return None

        pos_candidates = ['sv_position', 'position', 'pos', 'Pos', 'SV_Position']
        for key in pos_candidates:
            if key in vertex and vertex[key]:
                pos = vertex[key]
                if isinstance(pos, list):
                    if len(pos) >= 4:
                        return pos[:4]
                    elif len(pos) == 3:
                        return [pos[0], pos[1], pos[2], 1.0]
                    elif len(pos) == 2:
                        return [pos[0], pos[1], 0.0, 1.0]

        return None

    def _is_in_viewport(self, x: int, y: int) -> bool:
        """Check if pixel is inside viewport"""
        return self.config.viewport.contains(x, y)

    def _is_in_scissor(self, x: int, y: int) -> bool:
        """Check if pixel is inside scissor rect"""
        if not self.config.scissor_enable:
            return True
        return self.config.scissor_rect.contains(x, y)

    def get_primitive_count(self) -> int:
        """Get count of primitives processed"""
        return self._primitive_id_counter

    def get_pixel_count(self) -> int:
        """Get count of pixels generated"""
        return len(self._pixels)

    def load_config_from_pipeline_state_csv(self, csv_path: str) -> Optional[int]:
        """
        Load rasterizer/viewport/scissor config from pipeline_state.csv.
        Returns the primitive topology value if found, else None.

        CSV format:
            Section,Property,Value
            Viewport,X,0.0
            Viewport,Width,640.0
            Topology,Primitive,4
            ...
        """
        try:
            import csv as csv_mod
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv_mod.reader(f)
                rows = list(reader)
        except Exception as e:
            print(f"Warning: Failed to load pipeline_state from {csv_path}: {e}")
            return None

        if not rows or len(rows) < 2:
            return None

        vp = {'x': 0.0, 'y': 0.0, 'width': 800.0, 'height': 600.0,
                'min_depth': 0.0, 'max_depth': 1.0}
        sc = {'x': 0, 'y': 0, 'width': 0, 'height': 0}
        has_viewport = False
        has_scissor = False
        primitive_topology = None

        cull_mode_map = {
            'none': CullMode.NONE, 'front': CullMode.FRONT, 'back': CullMode.BACK,
            '0': CullMode.NONE, '1': CullMode.FRONT, '2': CullMode.BACK,
        }
        fill_mode_map = {
            'wireframe': FillMode.WIREFRAME, 'solid': FillMode.SOLID,
            '2': FillMode.WIREFRAME, '3': FillMode.SOLID,
        }

        for row in rows[1:]:
            if len(row) < 3:
                continue
            section = row[0].strip()
            prop = row[1].strip()
            val = row[2].strip().strip('"') if len(row) > 2 else ''

            if section == 'Viewport':
                has_viewport = True
                try:
                    if prop == 'X': vp['x'] = float(val)
                    elif prop == 'Y': vp['y'] = float(val)
                    elif prop == 'Width': vp['width'] = float(val)
                    elif prop == 'Height': vp['height'] = float(val)
                    elif prop == 'MinDepth': vp['min_depth'] = float(val)
                    elif prop == 'MaxDepth': vp['max_depth'] = float(val)
                except ValueError:
                    pass

            elif section == 'Scissor':
                try:
                    if prop == 'X': sc['x'] = int(float(val)); has_scissor = True
                    elif prop == 'Y': sc['y'] = int(float(val))
                    elif prop == 'Width': sc['width'] = int(float(val))
                    elif prop == 'Height': sc['height'] = int(float(val))
                except ValueError:
                    pass

            elif section == 'Rasterizer':
                if prop == 'CullMode':
                    mode = cull_mode_map.get(val.lower())
                    if mode is not None:
                        self.config.cull_mode = mode
                elif prop == 'FillMode':
                    mode = fill_mode_map.get(val.lower())
                    if mode is not None:
                        self.config.fill_mode = mode
                elif prop == 'FrontFace':
                    if 'ccw' in val.lower():
                        self.config.front_face = FrontFace.COUNTER_CLOCKWISE
                    else:
                        self.config.front_face = FrontFace.CLOCKWISE
                elif prop in ('DepthClip', 'DepthClipEnable'):
                    self.config.depth_clip_enable = val.lower() in ('true', '1', 'yes')

            elif section == 'RenderTarget':
                # Target[0]_Format is the RT0 format that the PS writes; it
                # determines the output-merger write clamp range. Its presence
                # also signals a bound render target (→ dump the color bitmap).
                if prop == 'Target[0]_Format' and val and val.upper() != 'UNKNOWN':
                    self.config.render_target_format = val

            elif section == 'DepthStencil':
                # Format presence signals a bound depth-stencil view
                # (→ dump the depth bitmap).
                if prop == 'Format' and val and val.upper() != 'UNKNOWN':
                    self.config.depth_stencil_format = val

            elif section == 'Topology':
                if prop == 'Primitive':
                    try:
                        primitive_topology = int(val)
                    except ValueError:
                        pass

        if has_viewport:
            self.config.viewport = Viewport(
                x=vp['x'], y=vp['y'],
                width=vp['width'], height=vp['height'],
                min_depth=vp['min_depth'], max_depth=vp['max_depth']
            )

        if has_scissor and (sc['width'] > 0 or sc['height'] > 0):
            self.config.scissor_rect = ScissorRect(
                left=sc['x'], top=sc['y'],
                right=sc['x'] + sc['width'],
                bottom=sc['y'] + sc['height']
            )

        return primitive_topology


def create_default_config() -> Dict[str, Any]:
    """Create default rasterizer configuration"""
    return {
        'cull_mode': 'back',
        'fill_mode': 'solid',
        'front_face': 'counter_clockwise',
        'scissor_enable': False,
        'scissor_rect': {
            'left': 0,
            'top': 0,
            'right': 0,
            'bottom': 0
        },
        'multisample_enable': False,
        'antialiasing_line_enable': False,
        'depth_clip_enable': True,
        'viewport': {
            'x': 0,
            'y': 0,
            'width': 800,
            'height': 600,
            'min_depth': 0.0,
            'max_depth': 1.0
        }
    }


def save_default_config(path: str):
    """Save default rasterizer configuration to JSON file"""
    config = create_default_config()
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4)
    print(f"Default rasterizer config saved to {path}")


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1:
        save_default_config(sys.argv[1])
    else:
        print("Usage: python rasterizer.py <config_output_path.json>")
        print("Creating sample config...")
        save_default_config("rasterizer_config.json")