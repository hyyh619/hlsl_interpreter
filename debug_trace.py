"""Optional pipeline debug tracing.

A single, globally-shared :class:`_DebugTrace` instance (`TRACE`) that, when
enabled via the run JSON config, dumps intermediate per-pixel / per-sample
pipeline data to a file. This is the instrumentation that cracked the event399
vertex-color/LOD mismatch (see Sessions/hlsl-interpreter-step98-*), kept in the
source behind a control flag so future color/LOD debugging doesn't need ad-hoc
prints.

OFF by default — every trace call is a cheap boolean-attribute check when the
corresponding channel is disabled, so leaving this in costs nothing on normal
runs.

Enable from the config JSON (resolved relative to the config file)::

    "debug_trace": {
        "enabled": true,
        "file": "pipeline_debug.log",   // default "<stem>_debug.log" / this name
        "ps_pixels": true,              // PS per-pixel inputs + output color
        "texture_lod": true,            // Texture.Sample u,v,LOD,derivatives,result
        "derivatives": false,           // quad screen-space UV gradients
        "target_pixels": ["366,354", "367,353"]  // empty/absent = all pixels
    }

The three channels mirror the three things worth inspecting when a pixel color
is wrong: what the PS received and emitted, what the texture unit sampled, and
what gradients drove the mip selection.
"""

import os
import threading
from typing import Optional


class _DebugTrace:
    def __init__(self):
        # Master switch + per-channel switches (all gated by `enabled`).
        self.enabled = False
        self.ps_pixels = False
        self.texture_lod = False
        self.derivatives = False
        # When non-empty, only these integer (x, y) pixels are traced. Empty
        # means "every pixel" (can be very large — use target_pixels in anger).
        self.target_pixels = set()
        # Context set by the PS loop so texture/derivative traces (which don't
        # otherwise know which pixel they belong to) can be attributed/filtered.
        self.current_pixel = None      # (x, y) or None
        self.phase = 'main'            # 'main' | 'deriv' (neighbor-lane re-exec)
        self._fh = None
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------
    def configure(self, config: dict, base_dir: str = '', default_stem: str = 'pipeline') -> None:
        """(Re)configure from a run config dict. Safe to call once per pipeline
        run; closes any previously-open trace file first. No-op when the
        `debug_trace` block is absent or `enabled` is false."""
        self.close()
        self.enabled = False
        self.ps_pixels = self.texture_lod = self.derivatives = False
        self.target_pixels = set()
        self.current_pixel = None
        self.phase = 'main'

        cfg = (config or {}).get('debug_trace')
        if not isinstance(cfg, dict) or not cfg.get('enabled'):
            return

        path = cfg.get('file') or f'{default_stem}_debug.log'
        if not os.path.isabs(path) and base_dir:
            path = os.path.join(base_dir, path)
        try:
            self._fh = open(path, 'w', encoding='utf-8')
        except OSError as e:
            # Tracing is best-effort: never break a run because the debug file
            # couldn't be opened.
            print(f"debug_trace: could not open {path}: {e}")
            return

        self.enabled = True
        self.ps_pixels = bool(cfg.get('ps_pixels', True))
        self.texture_lod = bool(cfg.get('texture_lod', False))
        self.derivatives = bool(cfg.get('derivatives', False))
        for spec in cfg.get('target_pixels', []) or []:
            try:
                xs, ys = str(spec).split(',')
                self.target_pixels.add((int(xs), int(ys)))
            except (ValueError, TypeError):
                continue
        self.path = path
        self._write_raw(
            f"# pipeline debug trace  ps_pixels={self.ps_pixels} "
            f"texture_lod={self.texture_lod} derivatives={self.derivatives} "
            f"targets={sorted(self.target_pixels) or 'ALL'}\n"
        )

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    # -- context -----------------------------------------------------------
    def set_pixel(self, x, y) -> None:
        """Record the pixel the PS is currently shading (used to attribute and
        filter texture/derivative traces)."""
        self.current_pixel = (int(x), int(y))

    def set_phase(self, phase: str) -> None:
        self.phase = phase

    def _pixel_selected(self) -> bool:
        """True when the current pixel should be traced (no targets ⇒ all)."""
        if not self.target_pixels:
            return True
        return self.current_pixel in self.target_pixels

    # -- channels ----------------------------------------------------------
    def ps_pixel(self, lane, inputs: dict, output) -> None:
        """One traced line per shaded pixel: its interpolated PS inputs and the
        emitted output color."""
        if not (self.enabled and self.ps_pixels) or not self._pixel_selected():
            return
        x, y = self.current_pixel if self.current_pixel else ('?', '?')
        fields = ' '.join(f'{k}={_fmt(v)}' for k, v in inputs.items())
        self._write_raw(f"PS  ({x},{y}) lane={lane} {fields} out={_fmt(output)}\n")

    def texture_sample(self, u, v, lod, ddx_uv, ddy_uv, result, name: str = '') -> None:
        """One traced line per Texture.Sample evaluation."""
        if not (self.enabled and self.texture_lod) or not self._pixel_selected():
            return
        px = self.current_pixel if self.current_pixel else ('?', '?')
        self._write_raw(
            f"TEX ({px[0]},{px[1]}) phase={self.phase} {name} "
            f"uv=({u:.5f},{v:.5f}) lod={_fmtf(lod)} "
            f"ddx={_fmt(ddx_uv)} ddy={_fmt(ddy_uv)} result={_fmt(result)}\n"
        )

    def deriv(self, coords_desc: str, ddx_uv, ddy_uv) -> None:
        """One traced line per quad screen-space UV gradient."""
        if not (self.enabled and self.derivatives) or not self._pixel_selected():
            return
        px = self.current_pixel if self.current_pixel else ('?', '?')
        self._write_raw(
            f"DDX ({px[0]},{px[1]}) {coords_desc} ddx={_fmt(ddx_uv)} ddy={_fmt(ddy_uv)}\n"
        )

    # -- low level ---------------------------------------------------------
    def _write_raw(self, line: str) -> None:
        if self._fh is None:
            return
        with self._lock:
            self._fh.write(line)
            # Flush each line so a trace is inspectable mid-run (these runs can
            # take a while) and survives an interrupted/killed process.
            self._fh.flush()


def _fmt(v) -> str:
    """Compact formatter for scalars / vectors / None."""
    if v is None:
        return 'None'
    if isinstance(v, (list, tuple)):
        return '[' + ','.join(_fmtf(x) for x in v) + ']'
    return _fmtf(v)


def _fmtf(x) -> str:
    if isinstance(x, (int, float)):
        return f'{float(x):.5f}'
    return str(x)


# Process-wide singleton shared by the interpreter, rasterizer and texture unit.
TRACE = _DebugTrace()
