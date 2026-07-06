"""Dynamic web mesh viewer — a live, browser-based twin of mesh_view.MeshView.

Where html_mesh_view.HtmlMeshView bakes the whole capture into ONE static HTML
file that is only written once, at the end of the run, WebMeshView runs a tiny
stdlib HTTP server on a background thread and the browser page *polls* it while
the pipeline executes.  This makes progress live:

  * VS execution progress  -> vertices appear on the Output canvas as each
    vertex is shaded (deliverable 2);
  * PS execution progress   -> pixels fill in on the Pixel-Shader canvas as
    each fragment is shaded (deliverable 3);
  * a "Show Normals" toggle draws/hides the per-vertex normal vectors live
    (deliverable 4).

Public setter API matches HtmlMeshView / MeshView so it is a drop-in
(set_input_data / set_output_data / set_rasterizer_pixels /
set_pixel_shader_output / set_output_merger_pixels / set_pipeline_stats /
set_primitive_topology / clear / show / close, plus the _draw_* no-ops).

Extra hooks the interpreter calls for LIVE progress (no-ops on the other
viewers, so callers guard with hasattr):
  * bind_vs_results(results_ref)       -> share the list VS appends to
  * set_vs_progress(done, total)       -> update the VS progress counter
  * bind_ps_pixels(pixels_ref)         -> share the pixel list PS colours
  * set_ps_progress(done, total)       -> update the PS progress counter
  * set_phase(name)                    -> 'init'|'vs'|'rasterizer'|'ps'|'done'

The server binds 127.0.0.1 on an OS-assigned free port and serves two routes:
  GET /        -> the poller page (HTML+JS, self-contained)
  GET /state   -> a JSON snapshot built under a lock on every request
"""
import json
import os
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List

try:
    from d3d import (
        D3D_PRIMITIVE_TOPOLOGY_UNDEFINED,
        D3D_PRIMITIVE_TOPOLOGY_POINTLIST,
        D3D_PRIMITIVE_TOPOLOGY_LINELIST,
        D3D_PRIMITIVE_TOPOLOGY_LINESTRIP,
        D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST,
        D3D_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP,
    )
except ImportError:
    (D3D_PRIMITIVE_TOPOLOGY_UNDEFINED, D3D_PRIMITIVE_TOPOLOGY_POINTLIST,
     D3D_PRIMITIVE_TOPOLOGY_LINELIST, D3D_PRIMITIVE_TOPOLOGY_LINESTRIP,
     D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST,
     D3D_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP) = (0, 1, 2, 3, 4, 5)


class WebMeshView:
    """Live browser twin of MeshView. Same setter API + progress hooks; the
    page polls /state so VS/PS progress animates as the pipeline runs."""

    def __init__(self, vertices: List = None,
                 primitive_topology: int = D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST,
                 title: str = "HLSL Interpreter - Mesh View (Web)",
                 host: str = "127.0.0.1", port: int = 0):
        self.title = title
        self.primitive_topology = primitive_topology
        self._host = host
        self._want_port = port
        self._log_output = print

        self._lock = threading.RLock()
        self._seq = 0
        self._phase = 'init'

        self._input = []           # serialized vertex dicts
        self._output = []          # final serialized output (set_output_data)
        self._rasterizer_pixels = []
        self._output_merger_pixels = []
        self._pipeline_stats = {}

        # Live progress: share the interpreter's own growing lists so /state can
        # slice a snapshot without the interpreter paying an O(n) copy per item.
        self._vs_results_ref = None
        self._vs_done = 0
        self._vs_total = 0
        self._rast_pixels_ref = None    # rasterizer's growing self._pixels list
        self._rast_done = 0             # primitives rasterized so far
        self._rast_total = 0            # total primitives
        self._rast_pixel_count = 0      # pixels emitted so far (report cap)
        self._ps_pixels_ref = None
        self._ps_done = 0
        self._ps_total = 0

        # Per-item animation delays (seconds). The pipeline reads these LIVE each
        # iteration (get_delay), so the browser sliders (POST /set) can pace the
        # VS/Rasterizer/PS animation while it runs. 0 = full speed.
        self._delays = {'vertex': 0.0, 'primitive': 0.0, 'pixel': 0.0}

        self._server = None
        self._thread = None
        self._opened = False

        # Pipeline controller (set by render.py) enabling stage replay and
        # per-vertex / per-pixel instruction tracing from the browser.
        self._controller = None

    def set_controller(self, controller):
        """Attach a pipeline controller exposing replay(stage) / trace_vertex(i)
        / trace_pixel(x, y). Enables the Replay buttons and Info-panel traces."""
        self._controller = controller

    # ------------------------------------------------------------ bookkeeping
    def _bump(self):
        self._seq += 1

    # --------------------------------------------------------------- setters
    def set_primitive_topology(self, primitive_topology: int):
        with self._lock:
            self.primitive_topology = primitive_topology
            self._bump()

    def clear(self):
        # Reset geometry/pixel buffers and the shared live-list refs, but KEEP
        # the numeric VS/PS progress counters: render.py calls clear() (via
        # show_input_mesh_from_params) AFTER the VS loop finishes, and zeroing
        # here would snap the completed VS progress bar back to 0/0. The next
        # run rebinds via bind_vs_results / bind_ps_pixels, which reset them.
        with self._lock:
            self._input = []
            self._output = []
            self._rasterizer_pixels = []
            self._output_merger_pixels = []
            self._vs_results_ref = None
            self._rast_pixels_ref = None
            self._ps_pixels_ref = None
            self._bump()

    @staticmethod
    def _pack_vertices(positions, normals, colors, tex_coords, tex_coords2):
        out = []
        for i, pos in enumerate(positions):
            def _at(lst):
                return list(lst[i]) if lst and i < len(lst) and lst[i] is not None else None
            out.append({
                'p': [float(c) for c in pos[:3]],
                'n': _at(normals),
                'c': _at(colors),
                't': _at(tex_coords),
                't2': _at(tex_coords2),
            })
        return out

    def set_input_data(self, positions, normals=None, colors=None,
                       tex_coords=None, tex_coords2=None):
        with self._lock:
            self._input = self._pack_vertices(positions, normals, colors,
                                              tex_coords, tex_coords2)
            self._bump()

    def set_output_data(self, positions, normals=None, colors=None,
                        tex_coords=None, tex_coords2=None):
        with self._lock:
            self._output = self._pack_vertices(positions, normals, colors,
                                               tex_coords, tex_coords2)
            self._bump()

    def set_input_vertices(self, vertices):
        with self._lock:
            self._input = [self._vd(v) for v in (vertices or [])]
            self._bump()

    def set_output_vertices(self, vertices):
        with self._lock:
            self._output = [self._vd(v) for v in (vertices or [])]
            self._bump()

    def set_vertices(self, vertices):
        self.set_input_vertices(vertices)

    @staticmethod
    def _vd(v):
        return {'p': list(getattr(v, 'position', [0, 0, 0])[:3]),
                'n': list(v.normal) if getattr(v, 'normal', None) else None,
                'c': list(v.color) if getattr(v, 'color', None) else None,
                't': list(v.tex_coord) if getattr(v, 'tex_coord', None) else None,
                't2': list(v.tex_coord2) if getattr(v, 'tex_coord2', None) else None}

    @staticmethod
    def _row_to_vertex(row):
        """Serialize one canonical VS-result dict (as executeVS_with_params
        returns) to the viewer's compact vertex shape — mirrors
        show_result_mesh_from_params' attribute picks."""
        pos = row.get('sv_position')
        if not (isinstance(pos, list) and len(pos) >= 3):
            return None
        norm = row.get('Normal')
        col = row.get('Color')
        if isinstance(col, list) and len(col) >= 4:
            col = col[:4]
        elif isinstance(col, list) and len(col) >= 3:
            col = col[:3] + [1.0]
        else:
            col = None
        tc = row.get('TexCoord')
        return {
            'p': [float(c) for c in pos[:3]],
            'n': [float(c) for c in norm[:3]] if isinstance(norm, list) and len(norm) >= 3 else None,
            'c': col,
            't': tc[:2] if isinstance(tc, list) and len(tc) >= 2 else None,
            't2': None,
        }

    @staticmethod
    def _pack_pixels(pixels):
        """Compact each Pixel to [x, y, prim_id, psR, psG, psB] (ps -1 if none)."""
        packed = []
        for p in pixels or []:
            ps = getattr(p, 'ps_output_color', None)
            if ps:
                r = int(min(255, max(0, ps[0] * 255)))
                g = int(min(255, max(0, ps[1] * 255)))
                b = int(min(255, max(0, ps[2] * 255)))
            else:
                r = g = b = -1
            packed.append([int(p.x), int(p.y), int(p.primitive_id), r, g, b])
        return packed

    def set_rasterizer_pixels(self, pixels):
        with self._lock:
            self._rasterizer_pixels = self._pack_pixels(pixels)
            self._bump()

    def set_pixel_shader_output(self, pixels):
        with self._lock:
            self._rasterizer_pixels = self._pack_pixels(pixels)
            self._bump()

    def set_output_merger_pixels(self, pixels):
        with self._lock:
            self._output_merger_pixels = self._pack_pixels(pixels)
            self._bump()

    def set_pipeline_stats(self, stats: dict):
        with self._lock:
            self._pipeline_stats = dict(stats or {})
            self._bump()

    # ---------------------------------------------------- live progress hooks
    def set_phase(self, name: str):
        with self._lock:
            self._phase = name
            self._bump()

    def bind_vs_results(self, results_ref, total: int = 0):
        """Share the list executeVS_with_params appends to, so /state can slice
        a live snapshot of the vertices shaded so far without any per-vertex copy."""
        with self._lock:
            self._vs_results_ref = results_ref
            self._vs_total = total or 0
            self._vs_done = 0
            self._phase = 'vs'
            self._bump()

    def set_vs_progress(self, done: int, total: int = None):
        with self._lock:
            self._vs_done = done
            if total is not None:
                self._vs_total = total
            self._bump()

    def bind_rast_pixels(self, pixels_ref, total_primitives: int = 0):
        """Share the rasterizer's growing self._pixels list so /state animates
        pixels appearing as each primitive is scan-converted (deliverable 1)."""
        with self._lock:
            self._rast_pixels_ref = pixels_ref
            self._rast_total = total_primitives or 0
            self._rast_done = 0
            self._rast_pixel_count = 0
            # Drop the previous run's final pixels so the snapshot shows the live
            # growing list (matters on replay, where finals were already set).
            self._rasterizer_pixels = []
            self._output_merger_pixels = []
            self._phase = 'rasterizer'
            self._bump()

    def set_rast_progress(self, done_primitives: int, total_primitives: int = None,
                          pixel_count: int = None):
        with self._lock:
            self._rast_done = done_primitives
            if total_primitives is not None:
                self._rast_total = total_primitives
            if pixel_count is not None:
                self._rast_pixel_count = pixel_count
            elif self._rast_pixels_ref is not None:
                self._rast_pixel_count = len(self._rast_pixels_ref)
            self._bump()

    def bind_ps_pixels(self, pixels_ref, total: int = 0):
        """Share the pixel list executePS_with_params colours in place."""
        with self._lock:
            self._ps_pixels_ref = pixels_ref
            self._ps_total = total or 0
            self._ps_done = 0
            # On replay the previous final pixels would mask the live PS slice.
            self._rasterizer_pixels = []
            self._output_merger_pixels = []
            self._phase = 'ps'
            self._bump()

    def set_ps_progress(self, done: int, total: int = None):
        with self._lock:
            self._ps_done = done
            if total is not None:
                self._ps_total = total
            self._bump()

    # ------------------------------------------------------- animation delays
    def set_delays(self, vertex=None, primitive=None, pixel=None):
        """Set per-item animation delays in SECONDS (None leaves a value as-is)."""
        with self._lock:
            if vertex is not None:
                self._delays['vertex'] = max(0.0, float(vertex))
            if primitive is not None:
                self._delays['primitive'] = max(0.0, float(primitive))
            if pixel is not None:
                self._delays['pixel'] = max(0.0, float(pixel))
            self._bump()

    def get_delay(self, kind: str) -> float:
        """Current delay (seconds) for 'vertex' | 'primitive' | 'pixel'. Read
        LIVE by the pipeline each iteration so slider changes take effect at once."""
        return self._delays.get(kind, 0.0)

    # ------------------------------------------------- controller-routed actions
    def _route_replay(self, stage: str) -> dict:
        c = self._controller
        if c is None:
            return {'ok': False, 'error': 'no controller (replay unavailable)'}
        try:
            return c.replay(stage)
        except Exception as e:
            return {'ok': False, 'error': f'{type(e).__name__}: {e}'}

    def _route_trace(self, kind: str, a: int, b: int) -> dict:
        c = self._controller
        if c is None:
            return {'ok': False, 'error': 'no controller (trace unavailable)'}
        try:
            if kind == 'vertex':
                return c.trace_vertex(a)
            return c.trace_pixel(a, b)
        except Exception as e:
            return {'ok': False, 'error': f'{type(e).__name__}: {e}'}

    # ------------------------------------------------- tk-parity no-op hooks
    def _draw_rasterizer_pixels(self):
        pass

    def _draw_pixel_shader_pixels(self):
        pass

    def _draw_output_merger_pixels(self):
        pass

    def set_hlsl_interpreter(self, *a, **k):
        pass

    def set_hlsl_interpreter_params(self, *a, **k):
        pass

    # ------------------------------------------------------------- snapshot
    def _snapshot(self) -> dict:
        with self._lock:
            # Output: prefer the final set_output_data payload; while VS runs,
            # slice the live results list up to the shaded count.
            if self._output:
                output = self._output
            elif self._vs_results_ref is not None:
                rows = self._vs_results_ref[:self._vs_done]
                output = [v for v in (self._row_to_vertex(r) for r in rows) if v]
            else:
                output = []

            # Rasterizer pixels: the final full list (set after rasterize) wins;
            # while the rasterizer runs, slice the live growing list so pixels
            # animate in as each primitive is scan-converted.
            if self._rasterizer_pixels:
                rasterizer = self._rasterizer_pixels
            elif self._rast_pixels_ref is not None:
                rasterizer = self._pack_pixels(self._rast_pixels_ref[:self._rast_pixel_count])
            else:
                rasterizer = []

            # PS pixels: final override wins; else the in-flight coloured slice.
            if self._rasterizer_pixels and self._phase == 'done':
                ps_pixels = self._rasterizer_pixels
            elif self._ps_pixels_ref is not None:
                ps_pixels = self._pack_pixels(self._ps_pixels_ref[:self._ps_done])
            else:
                ps_pixels = rasterizer

            return {
                'seq': self._seq,
                'phase': self._phase,
                'title': self.title,
                'topology': self.primitive_topology,
                'topo_names': {
                    D3D_PRIMITIVE_TOPOLOGY_UNDEFINED: 'Undefined',
                    D3D_PRIMITIVE_TOPOLOGY_POINTLIST: 'Point List',
                    D3D_PRIMITIVE_TOPOLOGY_LINELIST: 'Line List',
                    D3D_PRIMITIVE_TOPOLOGY_LINESTRIP: 'Line Strip',
                    D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST: 'Triangle List',
                    D3D_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP: 'Triangle Strip',
                },
                'topo_enum': {
                    'POINTLIST': D3D_PRIMITIVE_TOPOLOGY_POINTLIST,
                    'LINELIST': D3D_PRIMITIVE_TOPOLOGY_LINELIST,
                    'LINESTRIP': D3D_PRIMITIVE_TOPOLOGY_LINESTRIP,
                    'TRIANGLELIST': D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST,
                    'TRIANGLESTRIP': D3D_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP,
                },
                'input': self._input,
                'output': output,
                'rasterizer': rasterizer,
                'ps_pixels': ps_pixels,
                'output_merger': self._output_merger_pixels,
                'stats': self._pipeline_stats,
                'vs': {'done': self._vs_done, 'total': self._vs_total},
                'rast': {'done': self._rast_done, 'total': self._rast_total,
                         'pixels': self._rast_pixel_count},
                'ps': {'done': self._ps_done, 'total': self._ps_total},
                'delays': dict(self._delays),
            }

    # --------------------------------------------------------------- server
    def _ensure_server(self):
        if self._server is not None:
            return
        view = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence per-request stderr noise
                pass

            def _send(self, code, body, ctype):
                self.send_response(code)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                parts = self.path.split('?', 1)
                path = parts[0]
                if path == '/' or path == '/index.html':
                    safe_title = (view.title or 'Mesh View').replace('<', '').replace('>', '')
                    html = _PAGE.replace('__TITLE__', safe_title)
                    self._send(200, html.encode('utf-8'), 'text/html; charset=utf-8')
                elif path == '/state':
                    body = json.dumps(view._snapshot(), separators=(',', ':')).encode('utf-8')
                    self._send(200, body, 'application/json')
                elif path == '/set':
                    # Live animation-delay control: /set?vertex=..&primitive=..&pixel=..
                    # (values in seconds). The pipeline reads these each iteration.
                    q = urllib.parse.parse_qs(parts[1] if len(parts) > 1 else '')
                    def _f(k):
                        try:
                            return float(q[k][0]) if k in q else None
                        except (ValueError, IndexError):
                            return None
                    view.set_delays(vertex=_f('vertex'), primitive=_f('primitive'),
                                    pixel=_f('pixel'))
                    body = json.dumps({'delays': dict(view._delays)},
                                      separators=(',', ':')).encode('utf-8')
                    self._send(200, body, 'application/json')
                elif path == '/replay':
                    # Re-run from a stage onward: /replay?stage=vs|rasterizer|ps|om
                    q = urllib.parse.parse_qs(parts[1] if len(parts) > 1 else '')
                    stage = (q.get('stage', ['vs'])[0]).lower()
                    result = view._route_replay(stage)
                    self._send(200, json.dumps(result, separators=(',', ':')).encode('utf-8'),
                               'application/json')
                elif path == '/trace_vertex':
                    q = urllib.parse.parse_qs(parts[1] if len(parts) > 1 else '')
                    try:
                        i = int(q['i'][0])
                    except (KeyError, ValueError, IndexError):
                        i = -1
                    result = view._route_trace('vertex', i, 0)
                    self._send(200, json.dumps(result, separators=(',', ':')).encode('utf-8'),
                               'application/json')
                elif path == '/trace_pixel':
                    q = urllib.parse.parse_qs(parts[1] if len(parts) > 1 else '')
                    try:
                        x = int(q['x'][0]); y = int(q['y'][0])
                    except (KeyError, ValueError, IndexError):
                        x = y = -1
                    result = view._route_trace('pixel', x, y)
                    self._send(200, json.dumps(result, separators=(',', ':')).encode('utf-8'),
                               'application/json')
                else:
                    self._send(404, b'not found', 'text/plain')

        self._server = ThreadingHTTPServer((self._host, self._want_port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name='WebMeshView', daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        if self._server is None:
            return ''
        host, port = self._server.server_address[0], self._server.server_address[1]
        return f'http://{host}:{port}/'

    def show(self, blocking: bool = False):
        """Start the server (idempotent) and open the browser once."""
        try:
            self._ensure_server()
        except Exception as e:
            self._log_output(f"Web mesh view failed to start server: {e}")
            return
        if not self._opened:
            self._opened = True
            self._log_output(f"Web mesh view live at: {self.url}")
            try:
                webbrowser.open(self.url)
            except Exception:
                pass

    def close(self):
        srv = self._server
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass
            self._server = None


# The live poller page. Reuses html_mesh_view's projection/wireframe/pixel-image
# rendering, but wraps it so DATA is refreshed from /state on a timer instead of
# baked in. Adds VS/PS progress bars and a "Show Normals" toggle.
_PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>__TITLE__</title>
<style>
  :root{color-scheme:dark;}
  body{margin:0;background:#12121c;color:#e6e6ef;font:13px/1.4 Consolas,monospace;}
  header{padding:6px 10px;background:#1a1a2e;border-bottom:1px solid #2a2a44;}
  #stats{white-space:pre-wrap;color:#9fb;font-size:12px;margin-top:4px;}
  .bars{display:flex;gap:16px;margin-top:6px;flex-wrap:wrap;align-items:center;}
  .bar{display:flex;align-items:center;gap:6px;font-size:12px;color:#aab;}
  .track{width:180px;height:10px;background:#22223a;border:1px solid #2a2a44;border-radius:5px;overflow:hidden;}
  .fill{height:100%;width:0;background:#4a8;transition:width .12s linear;}
  #rastfill{background:#48a;}
  #psfill{background:#a84;}
  .phase{color:#fd6;font-weight:bold;}
  .delays{display:flex;gap:14px;margin-top:6px;flex-wrap:wrap;align-items:center;
    padding:5px 8px;background:#20203a;border:1px solid #2a2a44;border-radius:4px;}
  .delays b{color:#cdeeff;font-weight:bold;margin-right:2px;}
  .delays .d{display:flex;align-items:center;gap:5px;font-size:12px;color:#aab;}
  .delays input[type=range]{width:120px;}
  .delays .val{color:#9fb;min-width:48px;display:inline-block;}
  #live{color:#6c9;}
  .wrap{display:flex;gap:8px;padding:8px;align-items:flex-start;flex-wrap:wrap;}
  .panel{background:#1a1a2e;border:1px solid #2a2a44;border-radius:4px;}
  .panel h3{margin:0;padding:5px 8px;font-size:12px;background:#22223a;
    border-bottom:1px solid #2a2a44;border-radius:4px 4px 0 0;}
  canvas{display:block;background:#1a1a2e;cursor:crosshair;}
  .tabs{display:flex;gap:2px;padding:4px 6px 0;}
  .tabs button{background:#22223a;color:#bbc;border:1px solid #2a2a44;
    border-bottom:none;padding:3px 10px;cursor:pointer;font:inherit;border-radius:3px 3px 0 0;}
  .tabs button.active{background:#33335a;color:#fff;}
  .ctl{padding:4px 8px;font-size:12px;color:#aab;}
  .ctl label{margin-right:10px;}
  .ctl button{background:#33335a;color:#fff;border:1px solid #2a2a44;border-radius:3px;
    cursor:pointer;padding:2px 8px;font:inherit;margin-right:3px;}
  .replay{display:flex;gap:8px;margin-top:6px;flex-wrap:wrap;align-items:center;
    padding:5px 8px;background:#20203a;border:1px solid #2a2a44;border-radius:4px;}
  .replay b{color:#cdeeff;font-weight:bold;margin-right:2px;}
  .replay button{background:#2a4a3a;color:#cfe;border:1px solid #3a5a4a;border-radius:3px;
    cursor:pointer;padding:3px 10px;font:inherit;}
  .replay button:hover{background:#356048;}
  .replay button:disabled{opacity:.5;cursor:default;}
  #replaymsg{color:#9fb;}
  #info,#pxinfo{width:340px;padding:8px;white-space:pre-wrap;
    font-size:12px;overflow:auto;max-height:640px;}
  #info{min-height:250px;}
  #pxinfo{min-height:250px;}
  .trace{margin-top:6px;font-size:11px;color:#bcd;border-top:1px solid #2a2a44;padding-top:5px;}
  .trace .stmt{color:#8fd;}
  .trace .res{color:#fd9;}
  .k{color:#8ad;}
</style></head><body>
<header>
  <b id="ttl">__TITLE__</b>
  <div id="stats"></div>
  <div class="bars">
    <div class="bar">VS <div class="track"><div class="fill" id="vsfill"></div></div><span id="vstxt">0/0</span></div>
    <div class="bar">Rast <div class="track"><div class="fill" id="rastfill"></div></div><span id="rasttxt">0/0</span></div>
    <div class="bar">PS <div class="track"><div class="fill" id="psfill"></div></div><span id="pstxt">0/0</span></div>
    <div class="bar">Phase: <span class="phase" id="phase">init</span></div>
    <div class="bar"><label><input type="checkbox" id="shownormals"> Show Normals</label></div>
    <div class="bar"><label><input type="checkbox" id="livepoll" checked> Live</label> <span id="live"></span></div>
  </div>
  <div class="delays">
    <b>Animation delay per item:</b>
    <span class="d">Vertex (VS) <input type="range" id="dvertex" min="0" max="100" step="1" value="0"><span class="val" id="dvertexv">0 ms</span></span>
    <span class="d">Primitive (Rast) <input type="range" id="dprimitive" min="0" max="200" step="1" value="0"><span class="val" id="dprimitivev">0 ms</span></span>
    <span class="d">Pixel (PS) <input type="range" id="dpixel" min="0" max="50" step="0.5" value="0"><span class="val" id="dpixelv">0 ms</span></span>
    <button id="dreset" style="background:#33335a;color:#fff;border:1px solid #2a2a44;border-radius:3px;cursor:pointer;padding:2px 8px;font:inherit;">Reset</button>
  </div>
  <div class="replay">
    <b>Replay stage:</b>
    <button data-stage="vs">&#9654; Vertex Shader</button>
    <button data-stage="rasterizer">&#9654; Rasterizer</button>
    <button data-stage="ps">&#9654; Pixel Shader</button>
    <button data-stage="om">&#9654; Output Merger</button>
    <span id="replaymsg"></span>
  </div>
</header>
<div class="wrap">
  <div class="panel">
    <h3>Input Vertices</h3>
    <div class="ctl">
      <label>Zoom <input type="range" id="izoom" min="0.1" max="5" step="0.05" value="1"></label>
      drag = rotate &nbsp; right-click = select
    </div>
    <canvas id="incanvas" width="620" height="360"></canvas>
  </div>
  <div class="panel">
    <h3>Output</h3>
    <div class="tabs" id="otabs">
      <button data-tab="vs" class="active">VS Result</button>
      <button data-tab="rast">Rasterizer</button>
      <button data-tab="ps">Pixel Shader</button>
      <button data-tab="om">Output Merger</button>
    </div>
    <div class="ctl" id="octl">
      <label>Zoom <input type="range" id="ozoom" min="0.1" max="5" step="0.05" value="1"></label>
    </div>
    <div class="ctl" id="pctl" style="display:none;">
      Zoom <button id="pzin">&#43;</button><button id="pzout">&#8722;</button><button id="pzreset">Reset</button>
      <span id="pzval">1.0x</span> &nbsp; click a pixel to trace its PS
    </div>
    <canvas id="outcanvas" width="620" height="360"></canvas>
  </div>
  <div class="panel">
    <h3>Selected Vertex Info</h3>
    <div id="info">Right-click a vertex to inspect its VS instruction trace.</div>
    <h3>Selected Pixel Info</h3>
    <div id="pxinfo">Click a pixel (Rasterizer / Pixel Shader / Output Merger tab) to trace its PS instructions.</div>
  </div>
</div>
<script>
var DATA = {input:[],output:[],rasterizer:[],ps_pixels:[],output_merger:[],
  stats:{},topo_names:{},topo_enum:{},topology:4,vs:{done:0,total:0},ps:{done:0,total:0},phase:'init'};
var SHOW_NORMALS = false;
var curTab = 'vs';

function primColor(id){
  var hue=(id*37)%360, d=Math.PI/180;
  var r=Math.floor(127+127*Math.sin(hue*d));
  var g=Math.floor(127+127*Math.sin((hue+120)*d));
  var b=Math.floor(127+127*Math.sin((hue+240)*d));
  return "rgb("+r+","+g+","+b+")";
}
function vcolor(v){
  var c=v.c;
  if(!c) return "#88aaff";
  var r=Math.max(0,Math.min(255,Math.floor(c[0]*255)));
  var g=Math.max(0,Math.min(255,Math.floor(c[1]*255)));
  var b=Math.max(0,Math.min(255,Math.floor(c[2]*255)));
  return "rgb("+r+","+g+","+b+")";
}
// A view over one canvas; verts is re-read from DATA each draw so live updates show.
function makeView(canvas, getVerts, zoomEl, onSelect, isActive){
  // Start with a mild tilt so the mesh isn't dead-on axis-aligned: with rx=ry=0
  // the camera looks straight down z, so normals facing +/-z (very common)
  // project to zero length and are invisible. The tilt gives them screen extent.
  var rx=22, ry=-32, ox=0, oy=0, zoom=1, sel=-1, dragging=false, last=null;
  function active(){ return isActive ? isActive() : true; }
  function transform(p){
    var ax=rx*Math.PI/180, ay=ry*Math.PI/180;
    var cx=Math.cos(ax), sx=Math.sin(ax), cy=Math.cos(ay), sy=Math.sin(ay);
    var y1=p[1]*cx - p[2]*sx, z1=p[1]*sx + p[2]*cx;
    var x2=p[0]*cy + z1*sy;
    return [x2, y1];
  }
  function bounds(verts){
    var mn=[1e30,1e30], mx=[-1e30,-1e30];
    for(var i=0;i<verts.length;i++){var t=transform(verts[i].p);
      mn[0]=Math.min(mn[0],t[0]);mn[1]=Math.min(mn[1],t[1]);
      mx[0]=Math.max(mx[0],t[0]);mx[1]=Math.max(mx[1],t[1]);}
    if(mn[0]>mx[0]){mn=[-1,-1];mx=[1,1];}
    return [mn,mx];
  }
  function makeProject(W,H,b){
    var span=Math.max(b[1][0]-b[0][0], b[1][1]-b[0][1], 1e-6);
    var margin=40, usable=Math.min(W,H)-2*margin;
    var sc=zoom*usable/span;
    var cx=(b[0][0]+b[1][0])/2, cy=(b[0][1]+b[1][1])/2;
    return function(p){var t=transform(p);
      return [(t[0]-cx)*sc + W/2 + ox, -(t[1]-cy)*sc + H/2 + oy];};
  }
  function draw(){
    var verts=getVerts()||[];
    var ctx=canvas.getContext("2d"), W=canvas.width, H=canvas.height;
    ctx.fillStyle="#1a1a2e"; ctx.fillRect(0,0,W,H);
    if(!verts.length){ctx.fillStyle="#667";ctx.fillText("(no vertices yet)",20,20);return;}
    var b=bounds(verts), project=makeProject(W,H,b);
    var topo=DATA.topology, E=DATA.topo_enum;
    ctx.lineWidth=1;
    function seg(a,c,col){var pa=project(verts[a].p),pc=project(verts[c].p);
      ctx.strokeStyle=col;ctx.beginPath();ctx.moveTo(pa[0],pa[1]);ctx.lineTo(pc[0],pc[1]);ctx.stroke();}
    if(topo===E.TRIANGLELIST){for(var i=0;i+2<verts.length;i+=3){var col=vcolor(verts[i]);seg(i,i+1,col);seg(i+1,i+2,col);seg(i+2,i,col);}}
    else if(topo===E.TRIANGLESTRIP){for(var i=0;i+2<verts.length;i++){var col=vcolor(verts[i]);seg(i,i+1,col);seg(i+1,i+2,col);seg(i+2,i,col);}}
    else if(topo===E.LINELIST){for(var i=0;i+1<verts.length;i+=2)seg(i,i+1,vcolor(verts[i]));}
    else if(topo===E.LINESTRIP){for(var i=0;i+1<verts.length;i++)seg(i,i+1,vcolor(verts[i]));}
    // normal vectors (deliverable 4): draw each normal as a FIXED-length screen
    // segment along its projected direction, so it is visible at any zoom/units.
    // (Adding n*len in object space then projecting collapses to nothing when the
    //  normal faces the camera and vanishes entirely at small on-screen scale.)
    if(SHOW_NORMALS){
      var nlen=20;                 // on-screen normal length, pixels
      var drew=0;
      ctx.strokeStyle="#7fd7ff"; ctx.lineWidth=1.5; ctx.fillStyle="#cdeeff";
      for(var i=0;i<verts.length;i++){var v=verts[i];if(!v.n)continue;
        var p0=project(v.p);
        // screen-space direction of the normal: transform() applies the same
        // rotation project() does; project flips y, so negate ty.
        var td=transform(v.n), dx=td[0], dy=-td[1];
        var len=Math.sqrt(dx*dx+dy*dy);
        if(len<1e-4){ // normal points at/away from the camera: mark with a tip dot
          ctx.beginPath();ctx.arc(p0[0],p0[1],2.4,0,7);ctx.fill();drew++;continue;}
        dx=dx/len*nlen; dy=dy/len*nlen;
        ctx.beginPath();ctx.moveTo(p0[0],p0[1]);ctx.lineTo(p0[0]+dx,p0[1]+dy);ctx.stroke();
        ctx.beginPath();ctx.arc(p0[0]+dx,p0[1]+dy,1.8,0,7);ctx.fill();drew++;}
      ctx.lineWidth=1;
      if(!drew){ctx.fillStyle="#fd6";ctx.fillText("(selected mesh has no normal data)",20,H-12);}
    }
    // vertex dots
    for(var i=0;i<verts.length;i++){var p=project(verts[i].p);
      ctx.fillStyle=(i===sel)?"#ffdd33":vcolor(verts[i]);
      ctx.beginPath();ctx.arc(p[0],p[1],(i===sel)?5:2.5,0,7);ctx.fill();
      if(i===sel){ctx.strokeStyle="#fff";ctx.stroke();}}
  }
  function pick(mx,my){
    var verts=getVerts()||[],W=canvas.width,H=canvas.height,b=bounds(verts),
        project=makeProject(W,H,b),best=-1,bd=400;
    for(var i=0;i<verts.length;i++){var p=project(verts[i].p);
      var d=(p[0]-mx)*(p[0]-mx)+(p[1]-my)*(p[1]-my);if(d<bd){bd=d;best=i;}}
    return best;
  }
  canvas.addEventListener("mousedown",function(e){if(!active())return;dragging=true;last=[e.offsetX,e.offsetY];});
  canvas.addEventListener("mousemove",function(e){if(!active())return;if(dragging&&last){ry+=(e.offsetX-last[0])*0.5;rx+=(e.offsetY-last[1])*0.5;last=[e.offsetX,e.offsetY];draw();}});
  window.addEventListener("mouseup",function(){dragging=false;last=null;});
  canvas.addEventListener("contextmenu",function(e){if(!active())return;e.preventDefault();
    sel=pick(e.offsetX,e.offsetY);draw();if(onSelect)onSelect(sel);});
  canvas.addEventListener("wheel",function(e){if(!active())return;e.preventDefault();
    zoom*=(e.deltaY<0?1.1:0.9);if(zoomEl)zoomEl.value=zoom;draw();},{passive:false});
  if(zoomEl)zoomEl.addEventListener("input",function(){zoom=parseFloat(zoomEl.value);draw();});
  return {draw:draw, setSel:function(i){sel=i;draw();}, getSel:function(){return sel;}};
}

function fmt(a){return a?("("+a.map(function(x){return x.toFixed(4);}).join(", ")+")"):null;}
function vinfo(idx){
  var out="";
  if(idx>=0 && idx<DATA.input.length){var v=DATA.input[idx];
    out+="--- Input Vertex ["+idx+"] ---\n";
    out+="Position: "+fmt(v.p)+"\n";
    if(v.n)out+="Normal:   "+fmt(v.n)+"\n";
    if(v.c)out+="Color:    "+fmt(v.c)+"\n";
    if(v.t)out+="TexCoord: "+fmt(v.t)+"\n";
  }
  if(idx>=0 && idx<DATA.output.length){var v=DATA.output[idx];
    out+="\n--- Output Vertex ["+idx+"] ---\n";
    out+="Position: "+fmt(v.p)+"\n";
    if(v.n)out+="Normal:   "+fmt(v.n)+"\n";
    if(v.c)out+="Color:    "+fmt(v.c)+"\n";
    if(v.t)out+="TexCoord: "+fmt(v.t)+"\n";
  }
  document.getElementById("info").textContent = out || "No vertex selected.";
}

// Render a captured instruction trace (list of [STMT]/=> lines) into an element.
function renderTrace(el, header, res){
  if(!res){el.textContent=header+"\n(no response)";return;}
  if(!res.ok){el.textContent=header+"\nTrace unavailable: "+(res.error||"?");return;}
  var lines=res.lines||[];
  var html="<b>"+header+"</b>\n<div class='trace'>";
  for(var i=0;i<lines.length;i++){
    var ln=lines[i];
    var cls = ln.indexOf("=>")>=0 ? "res" : (ln.indexOf("[STMT]")>=0 ? "stmt" : "");
    var esc = ln.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
    html += cls ? ("<span class='"+cls+"'>"+esc+"</span>\n") : (esc+"\n");
  }
  html+="</div>";
  el.innerHTML=html;
}
function fetchJSON(p, cb){
  fetch(p,{cache:"no-store"}).then(function(r){return r.json();}).then(cb).catch(function(){cb(null);});
}

var inView, outVs, octx;
function syncSel(i){ if(outVs)outVs.setSel(i); vinfo(i); traceVertex(i); }
function syncSelO(i){ if(inView)inView.setSel(i); vinfo(i); traceVertex(i); }
function traceVertex(i){
  if(i<0){return;}
  var el=document.getElementById("info");
  el.textContent+="\n\n(loading VS instruction trace for vertex "+i+"...)";
  fetchJSON("/trace_vertex?i="+i,function(res){
    var head="VS instruction trace — vertex "+i;
    vinfo(i);  // reset the info text, then append trace below it
    var base=document.getElementById("info").textContent;
    document.getElementById("info").innerHTML="";
    var pre=document.createTextNode(base+"\n\n");
    document.getElementById("info").appendChild(pre);
    var span=document.createElement("span");
    renderTrace(span, head, res);
    document.getElementById("info").appendChild(span);
  });
}

// Pixel-view zoom/pan state (Rasterizer/PS/OM tabs) + last transform (for
// click->pixel inversion) + selected pixel marker.
var pxZoom=1, pxOx=0, pxOy=0, pxXform=null, pxSel=null;
function drawPixels(pixels, shaded){
  var cv=document.getElementById("outcanvas"), W=cv.width, H=cv.height;
  octx.fillStyle="#1a1a2e"; octx.fillRect(0,0,W,H);
  if(!pixels||!pixels.length){octx.fillStyle="#667";octx.fillText("(no pixels yet)",20,20);pxXform=null;return;}
  var mnx=1e9,mny=1e9,mxx=-1e9,mxy=-1e9;
  for(var i=0;i<pixels.length;i++){var p=pixels[i];
    mnx=Math.min(mnx,p[0]);mny=Math.min(mny,p[1]);mxx=Math.max(mxx,p[0]);mxy=Math.max(mxy,p[1]);}
  var pw=mxx-mnx+1, ph=mxy-mny+1;
  var sc=Math.min((W-8)/pw,(H-8)/ph); if(sc<=0)sc=1;
  sc*=pxZoom;
  var ox=(W-pw*sc)/2 + pxOx, oy=(H-ph*sc)/2 + pxOy, cell=Math.max(1,sc);
  pxXform={mnx:mnx,mny:mny,sc:sc,ox:ox,oy:oy,cell:cell};
  for(var i=0;i<pixels.length;i++){var p=pixels[i];
    var col;
    if(shaded && p[3]>=0) col="rgb("+p[3]+","+p[4]+","+p[5]+")";
    else col=primColor(p[2]);
    octx.fillStyle=col;
    octx.fillRect(ox+(p[0]-mnx)*sc, oy+(p[1]-mny)*sc, cell, cell);
  }
  // highlight the selected pixel
  if(pxSel){
    octx.strokeStyle="#ffdd33"; octx.lineWidth=2;
    octx.strokeRect(ox+(pxSel[0]-mnx)*sc-1, oy+(pxSel[1]-mny)*sc-1, cell+2, cell+2);
    octx.lineWidth=1;
  }
}
// Invert the last drawPixels transform: screen (mx,my) -> pixel (x,y) or null.
function pickPixel(mx,my){
  if(!pxXform)return null;
  var x=Math.floor((mx-pxXform.ox)/pxXform.sc)+pxXform.mnx;
  var y=Math.floor((my-pxXform.oy)/pxXform.sc)+pxXform.mny;
  return [x,y];
}
function renderOutput(){
  if(curTab==="vs")outVs.draw();
  else if(curTab==="rast")drawPixels(DATA.rasterizer,false);
  else if(curTab==="ps")drawPixels(DATA.ps_pixels&&DATA.ps_pixels.length?DATA.ps_pixels:DATA.rasterizer,true);
  else if(curTab==="om")drawPixels(DATA.output_merger&&DATA.output_merger.length?DATA.output_merger:(DATA.ps_pixels&&DATA.ps_pixels.length?DATA.ps_pixels:DATA.rasterizer),true);
}
function setTab(name){
  curTab=name;
  document.querySelectorAll("#otabs button").forEach(function(b){b.classList.toggle("active",b.dataset.tab===name);});
  document.getElementById("octl").style.display=(name==="vs")?"block":"none";
  document.getElementById("pctl").style.display=(name==="vs")?"none":"block";
  renderOutput();
}
function setPxZoom(z){
  pxZoom=Math.max(0.2,Math.min(40,z));
  document.getElementById("pzval").textContent=pxZoom.toFixed(1)+"x";
  if(curTab!=="vs")renderOutput();
}

function refreshHeader(){
  var s=DATA.stats||{};
  document.getElementById("ttl").textContent=DATA.title||"Mesh View";
  var T=(DATA.topo_names&&DATA.topo_names[DATA.topology])||"Unknown";
  var line="Input: "+DATA.input.length+" vertices | Output: "+DATA.output.length
    +" vertices | Topology: "+T;
  if(Object.keys(s).length){
    line += "\nVerts: "+(s.vertices||0)+" | Prims: "+(s.primitives||0)
      +" | Clipped: "+(s.clipped||0)+"/"+(s.not_clipped||0)
      +" | Culled: "+(s.culled||0)+"/"+(s.not_culled||0)
      +" | Rast px: "+(s.rast_pixels||0)
      +" | Depth fail: "+(s.depth_failed||0)+" (pass "+(s.depth_passed||0)+")"
      +" | PS px: "+(s.ps_pixels||0);
  }
  document.getElementById("stats").textContent=line;
  var vt=DATA.vs.total||0, vd=DATA.vs.done||0;
  var rt=(DATA.rast&&DATA.rast.total)||0, rd=(DATA.rast&&DATA.rast.done)||0;
  var pt=DATA.ps.total||0, pd=DATA.ps.done||0;
  document.getElementById("vsfill").style.width=(vt?100*vd/vt:0)+"%";
  document.getElementById("rastfill").style.width=(rt?100*rd/rt:0)+"%";
  document.getElementById("psfill").style.width=(pt?100*pd/pt:0)+"%";
  document.getElementById("vstxt").textContent=vd+"/"+vt;
  document.getElementById("rasttxt").textContent=rd+"/"+rt+" ("+((DATA.rast&&DATA.rast.pixels)||0)+" px)";
  document.getElementById("pstxt").textContent=pd+"/"+pt;
  document.getElementById("phase").textContent=DATA.phase||"?";
}

var lastSeq=-1, delaysTouched=false;
function syncDelaySliders(d){
  // Reflect server delays on the sliders, but don't fight the user mid-drag.
  if(delaysTouched||!d)return;
  var m={vertex:'dvertex',primitive:'dprimitive',pixel:'dpixel'};
  for(var k in m){var el=document.getElementById(m[k]);
    if(el){el.value=(d[k]*1000);document.getElementById(m[k]+'v').textContent=(d[k]*1000).toFixed(el.step<1?1:0)+" ms";}}
}
function applyState(st){
  DATA=st;
  refreshHeader();
  syncDelaySliders(st.delays);
  // Auto-follow the active stage so progress is visible without clicking tabs.
  if(DATA.phase==="rasterizer" && curTab==="vs") setTab("rast");
  if(DATA.phase==="ps" && (curTab==="vs"||curTab==="rast")) setTab("ps");
  inView.draw();
  renderOutput();
}
function poll(){
  if(!document.getElementById("livepoll").checked){return;}
  fetch("/state",{cache:"no-store"}).then(function(r){return r.json();}).then(function(st){
    document.getElementById("live").textContent="";
    if(st.seq!==lastSeq){lastSeq=st.seq;applyState(st);}
    else{ // still refresh live progress bars/output during an active phase
      if(st.phase==="vs"||st.phase==="rasterizer"||st.phase==="ps"){applyState(st);}
    }
  }).catch(function(){document.getElementById("live").textContent="(disconnected)";});
}
// Push slider values (ms -> seconds) to the server; pipeline reads them live.
function pushDelays(){
  var v=parseFloat(document.getElementById("dvertex").value)/1000;
  var p=parseFloat(document.getElementById("dprimitive").value)/1000;
  var x=parseFloat(document.getElementById("dpixel").value)/1000;
  fetch("/set?vertex="+v+"&primitive="+p+"&pixel="+x,{cache:"no-store"}).catch(function(){});
}
function wireDelay(id){
  var el=document.getElementById(id), lbl=document.getElementById(id+"v");
  el.addEventListener("input",function(){
    delaysTouched=true;
    lbl.textContent=parseFloat(el.value).toFixed(el.step<1?1:0)+" ms";
    pushDelays();
  });
}

// Selected-pixel PS instruction trace.
function tracePixel(x,y){
  pxSel=[x,y];
  var el=document.getElementById("pxinfo");
  el.textContent="Pixel ("+x+","+y+")\n(loading PS instruction trace...)";
  if(curTab!=="vs")renderOutput();
  fetchJSON("/trace_pixel?x="+x+"&y="+y,function(res){
    renderTrace(el, "PS instruction trace — pixel ("+x+","+y+")", res);
  });
}

(function(){
  octx=document.getElementById("outcanvas").getContext("2d");
  inView=makeView(document.getElementById("incanvas"), function(){return DATA.input;},
    document.getElementById("izoom"), syncSel);
  outVs=makeView(document.getElementById("outcanvas"), function(){return DATA.output;},
    document.getElementById("ozoom"), syncSelO, function(){return curTab==="vs";});
  document.querySelectorAll("#otabs button").forEach(function(b){
    b.addEventListener("click",function(){setTab(b.dataset.tab);});});
  document.getElementById("shownormals").addEventListener("change",function(e){
    SHOW_NORMALS=e.target.checked; inView.draw(); if(curTab==="vs")outVs.draw();});
  wireDelay("dvertex"); wireDelay("dprimitive"); wireDelay("dpixel");
  document.getElementById("dreset").addEventListener("click",function(){
    ["dvertex","dprimitive","dpixel"].forEach(function(id){
      var el=document.getElementById(id);el.value=0;
      document.getElementById(id+"v").textContent="0 ms";});
    delaysTouched=true; pushDelays();});

  // Pixel-tab zoom buttons (deliverable 1).
  document.getElementById("pzin").addEventListener("click",function(){setPxZoom(pxZoom*1.4);});
  document.getElementById("pzout").addEventListener("click",function(){setPxZoom(pxZoom/1.4);});
  document.getElementById("pzreset").addEventListener("click",function(){pxOx=0;pxOy=0;setPxZoom(1);});

  // Pixel-tab interactions on the output canvas (only when a pixel tab is active).
  var oc=document.getElementById("outcanvas"), pdrag=false, plast=null, pmoved=false;
  oc.addEventListener("wheel",function(e){
    if(curTab==="vs")return; e.preventDefault();
    setPxZoom(pxZoom*(e.deltaY<0?1.1:0.9));
  },{passive:false});
  oc.addEventListener("mousedown",function(e){
    if(curTab==="vs")return; pdrag=true; pmoved=false; plast=[e.offsetX,e.offsetY];});
  oc.addEventListener("mousemove",function(e){
    if(curTab==="vs"||!pdrag||!plast)return;
    var dx=e.offsetX-plast[0], dy=e.offsetY-plast[1];
    if(Math.abs(dx)+Math.abs(dy)>3)pmoved=true;
    pxOx+=dx; pxOy+=dy; plast=[e.offsetX,e.offsetY]; renderOutput();});
  window.addEventListener("mouseup",function(){pdrag=false;plast=null;});
  oc.addEventListener("click",function(e){
    if(curTab==="vs"||pmoved)return;
    var xy=pickPixel(e.offsetX,e.offsetY);
    if(xy)tracePixel(xy[0],xy[1]);});

  // Replay buttons (deliverable 2).
  document.querySelectorAll(".replay button").forEach(function(b){
    b.addEventListener("click",function(){
      var stage=b.dataset.stage, msg=document.getElementById("replaymsg");
      msg.textContent="replaying "+stage+"...";
      document.querySelectorAll(".replay button").forEach(function(x){x.disabled=true;});
      fetchJSON("/replay?stage="+stage,function(res){
        document.querySelectorAll(".replay button").forEach(function(x){x.disabled=false;});
        if(res&&res.ok)msg.textContent="replayed "+stage+" ✓ ("+(res.vertices||0)+" verts, "+(res.pixels||0)+" px)";
        else msg.textContent="replay failed: "+((res&&res.error)||"?");
      });
    });});

  setTab("vs");
  poll();
  setInterval(poll, 250);
})();
</script>
</body></html>
"""
