import os
import re
import csv
import sys
import time
import json
import zipfile
import tempfile
import shutil
import threading

from hlsl_interpreter import HLSLInterpreter
from rasterizer import Rasterizer
from d3d import D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST
from output_merger import Depth, ComparisonFunc
from debug_trace import TRACE
import d3d


def print_and_compare_results(interpreter, results, output_struct_name, float_tolerance, execute_count, interpret_time, load_golden_time, execute_time, total_start):
    if interpreter.print_interpreter_result:
        interpreter.log_output("HLSL Interpreter Result:")
        interpreter.log_output("=" * 40)
        if results:
            for idx, result in enumerate(results):
                interpreter.log_output(f"\n--- Row {idx} ---")
                if result:
                    for key, value in result.items():
                        if isinstance(value, list):
                            if len(value) == 4:
                                interpreter.log_output(f"{key}: [{value[0]:.4f}, {value[1]:.4f}, {value[2]:.4f}, {value[3]:.4f}]")
                            elif len(value) == 3:
                                interpreter.log_output(f"{key}: [{value[0]:.4f}, {value[1]:.4f}, {value[2]:.4f}]")
                            elif len(value) == 2:
                                interpreter.log_output(f"{key}: [{value[0]:.4f}, {value[1]:.4f}]")
                            else:
                                interpreter.log_output(f"{key}: {value}")
                        else:
                            interpreter.log_output(f"{key}: {value}")
        else:
            interpreter.log_output("No result produced")

        if results and results[-1] and 'Color' in results[-1]:
            color = results[-1]['Color']
            if color and isinstance(color, list) and len(color) == 4:
                interpreter.log_output("\nFinal Output Color (RGBA):")
                interpreter.log_output(f"  R: {color[0]:.4f}")
                interpreter.log_output(f"  G: {color[1]:.4f}")
                interpreter.log_output(f"  B: {color[2]:.4f}")
                interpreter.log_output(f"  A: {color[3]:.4f}")

        interpreter.log_output("\n" + "=" * 40)
    interpreter.log_output("Comparing with golden data...")
    interpreter.log_output("=" * 40)
    compare_start = time.time()
    interpreter.compare_vs_output_with_golden(results, output_struct_name=output_struct_name, float_tolerance=float_tolerance, execute_count=execute_count)
    compare_time = time.time() - compare_start

    total_time = time.time() - total_start

    interpreter.log_output("\n" + "=" * 40)
    interpreter.log_output("Timing Summary:")
    interpreter.log_output("=" * 40)
    interpreter.log_output(f"interpreter.interpret():             {interpret_time:.4f}s")
    interpreter.log_output(f"interpreter.load_vs_output_golden_from_csv(): {load_golden_time:.4f}s")
    interpreter.log_output(f"interpreter.executeVS():           {execute_time:.4f}s")
    interpreter.log_output(f"compare_vs_output_with_golden():    {compare_time:.4f}s")
    interpreter.log_output(f"Total execution time:               {total_time:.4f}s")


def _make_interpreter(config: dict, shader_stage: int = d3d.SHADER_STAGE_VS, log_file_path: str = None, primitive_topology: int = D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST,
                    texture_exec=None, texture_desc_list=None, sampler_list=None) -> HLSLInterpreter:
    """Create an HLSLInterpreter from config dict."""
    config_log_file_mode = config.get('log_file_mode', 'a')
    if shader_stage != d3d.SHADER_STAGE_VS and shader_stage != d3d.SHADER_STAGE_CS:
        config_log_file_mode = 'a'

    return HLSLInterpreter(
        log_to_file=config.get('log_to_file', True),
        log_file_path=log_file_path or config.get('log_file_path', 'hlsl_interpreter.log'),
        log_file_mode=config_log_file_mode,
        print_sequence=config.get('print_sequence', 1),
        printSyntaxTree=config.get('printSyntaxTree', True),
        print_interpreter_result=config.get('print_interpreter_result', True),
        max_workers=config.get('max_workers', 1),
        primitive_topology=primitive_topology,
        texture_list=[texture_exec] if texture_exec else [],
        texture_desc_list=texture_desc_list or [],
        sampler_list=sampler_list or [],
        # float32 rounding adds per-op overhead; the precision-sensitive hash
        # outputs are all in the VS, so only emulate there (keeps the per-pixel
        # PS fast on full-screen draws like event7358).
        f32_emulation=(config.get('float32_emulation', False)
                       and shader_stage == d3d.SHADER_STAGE_VS),
    )


# 3Dmigoto dumps shader-resource textures, one BMP per mip/array slice, as
# <Stage>_slot_<slot>_res_<resid>_mip<mip>_arr<arr>.bmp
def _texture_file_re(stage: str):
    return re.compile(
        r'^' + re.escape(stage) + r'_slot_(\d+)_res_(\d+)_mip(\d+)_arr(\d+)\.bmp$',
        re.IGNORECASE,
    )


def _read_csv_rows(path: str):
    """Read a CSV into a list of dict rows; [] if absent/unreadable."""
    rows = []
    try:
        with open(path, 'r', encoding='utf-8', newline='') as f:
            for row in csv.DictReader(f):
                rows.append(row)
    except Exception:
        pass
    return rows


def _as_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _fmt4(vals) -> str:
    """Format a numeric sequence as a 4-decimal list for logs."""
    try:
        return "[" + ", ".join(f"{float(v):.4f}" for v in vals) + "]"
    except (TypeError, ValueError):
        return str(vals)


# ----------------------------------------------------------------------------
# Golden pipeline-statistics comparison (pipeline_statistics.csv)
# ----------------------------------------------------------------------------
# Maps each golden RawCounter to the matching key in render.py's pipeline_stats
# dict. Counters for stages we don't emulate (HS/DS/GS/CS) are intentionally
# absent — they're skipped rather than warned on.
_PIPELINE_STAT_MAP = {
    'InputVerticesRead': 'vertices',
    'VSInvocations': 'vertices',
    'IAPrimitives': 'primitives',
    'RasterizerInvocations': 'primitives',
    'RasterizedPrimitives': 'not_culled',
    'PSInvocations': 'ps_pixels',
    'SamplesPassed': 'depth_passed',
}
# Per the spec, only these two counters are treated as errors; the rest are
# warnings. The prefixes deliberately do NOT start with "Error:" so the
# regression runner (which gates on VS-vs-golden "Error:" lines) is unaffected.
_PIPELINE_STAT_ERRORS = ('VSInvocations', 'SamplesPassed')


def _load_golden_pipeline_statistics(path: str) -> dict:
    """Parse pipeline_statistics.csv → {RawCounter: int Value}. {} if absent.

    Columns are ``RawCounter,Description,Value`` (plus trailing empties)."""
    stats = {}
    for row in _read_csv_rows(path):
        name = (row.get('RawCounter') or '').strip()
        if not name:
            continue
        stats[name] = _as_int(row.get('Value'), 0)
    return stats


def _resolve_triangle_topology(csv_topo, vertex_count: int, golden_iaprim: int, log) -> int:
    """Disambiguate triangle LIST vs STRIP using the captured primitive count.

    These RenderDoc/3Dmigoto dumps have an unreliable topology enum in
    pipeline_state.csv: it reports TRIANGLESTRIP (5) even for draws that are
    actually triangle lists (verified — golden IAPrimitives == vertex_count/3
    for every captured draw, not vertex_count-2). The captured pipeline
    statistics' IAPrimitives count, by contrast, is ground truth.

    So when the CSV claims a triangle topology and the golden IAPrimitives is
    available, pick the interpretation whose primitive count matches:
        list  → vertex_count // 3 triangles
        strip → vertex_count - 2 triangles
    If the count is ambiguous (matches both, e.g. a single triangle) or matches
    neither (no golden / partial capture), keep the CSV value unchanged.
    """
    tri_topos = (D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST,
                d3d.D3D_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP)
    if csv_topo not in tri_topos or vertex_count <= 0 or golden_iaprim <= 0:
        return csv_topo
    list_tris = vertex_count // 3
    strip_tris = vertex_count - 2
    matches_list = (golden_iaprim == list_tris)
    matches_strip = (golden_iaprim == strip_tris)
    if matches_list and not matches_strip:
        resolved = D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST
    elif matches_strip and not matches_list:
        resolved = d3d.D3D_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP
    else:
        return csv_topo
    if resolved != csv_topo:
        log(f"Topology: CSV reported {csv_topo} but golden IAPrimitives="
            f"{golden_iaprim} matches {vertex_count}/3 → using TRIANGLELIST "
            f"({resolved}) instead (capture enum unreliable).")
    return resolved


def _compare_pipeline_statistics(golden: dict, pipeline_stats: dict, log,
                                samples_passed_tolerance: int = 500) -> None:
    """Compare our pipeline_stats against the golden capture, counter by counter.

    VSInvocations / SamplesPassed mismatches log an error-level line; every
    other mapped counter mismatch logs a warning.

    ``SamplesPassed`` (mapped from depth_passed) is allowed a tolerance band:
    our rasterizer's fragment count and ordering differ slightly from the real
    GPU, so an exact match is unrealistic. A difference within
    ``samples_passed_tolerance`` is treated as OK rather than an error."""
    log("\n" + "=" * 50)
    log("Pipeline Statistics vs Golden (pipeline_statistics.csv):")
    log("=" * 50)
    mismatches = 0
    for counter, our_key in _PIPELINE_STAT_MAP.items():
        if counter not in golden:
            continue
        golden_val = golden[counter]
        our_val = pipeline_stats.get(our_key, 0)
        if golden_val == our_val:
            log(f"  OK  {counter}: output={our_val} golden={golden_val}")
            continue
        # SamplesPassed: accept any difference within the configured tolerance.
        if counter == 'SamplesPassed' and abs(our_val - golden_val) <= samples_passed_tolerance:
            log(f"  OK  {counter}: output={our_val} golden={golden_val} "
                f"(diff={abs(our_val - golden_val)} within tolerance {samples_passed_tolerance})")
            continue
        mismatches += 1
        kind = "Error" if counter in _PIPELINE_STAT_ERRORS else "Warning"
        log(f"{kind} [PipelineStats]: {counter} mismatch: "
            f"output={our_val} golden={golden_val} "
            f"(mapped from pipeline_stats['{our_key}'])")
    if mismatches == 0:
        log("  All compared pipeline statistics match golden.")
    log("=" * 50)


# ----------------------------------------------------------------------------
# Golden output-merger pixel comparison (diff_ps_output_rt0.csv)
# ----------------------------------------------------------------------------
def _load_golden_ps_output(path: str) -> dict:
    """Parse diff_ps_output_rt0.csv → {(x, y): {'color': [r,g,b,a], 'depth': f}}.

    Columns are ``X,Y,B,G,R,A,Depth,Stencil`` — note the B,G,R channel order in
    the capture; we re-order to RGBA to match Pixel.ps_output_color."""
    golden = {}
    for row in _read_csv_rows(path):
        try:
            x = int(float(row['X']))
            y = int(float(row['Y']))
        except (KeyError, TypeError, ValueError):
            continue

        def f(key):
            try:
                return float(row.get(key))
            except (TypeError, ValueError):
                return 0.0

        golden[(x, y)] = {
            'color': [f('R'), f('G'), f('B'), f('A')],
            'depth': f('Depth'),
        }
    return golden


def _compare_ps_output(golden: dict, pixels, tolerance: float, log, max_report: int = 50) -> None:
    """Compare final output-merger pixels against the golden RT0 dump.

    Color (RGBA) and depth are compared per pixel within ``tolerance``. Where
    several rendered pixels land on the same (x, y) — the interpreter has no
    true depth occlusion — the nearest (smallest depth) is taken as the winner,
    matching what a standard LESS depth test would keep."""
    log("\n" + "=" * 50)
    log(f"Output-Merger Pixels vs Golden (diff_ps_output_rt0.csv, tolerance={tolerance}):")
    log("=" * 50)
    if not golden:
        log("  No golden pixel data (diff_ps_output_rt0.csv empty or absent) — skipped.")
        log("=" * 50)
        return

    # Collapse rendered pixels to one winner per (x, y): nearest depth wins.
    ours = {}
    for p in pixels:
        key = (int(p.x), int(p.y))
        prev = ours.get(key)
        if prev is None or p.depth < prev.depth:
            ours[key] = p

    matched = mismatched = missing = 0
    reported = 0
    for key, g in golden.items():
        p = ours.get(key)
        if p is None:
            missing += 1
            if reported < max_report:
                log(f"Error [PixelDiff]: ({key[0]},{key[1]}) missing in output "
                    f"(golden color={_fmt4(g['color'])} depth={g['depth']:.6f})")
                reported += 1
            continue
        our_color = p.ps_output_color if p.ps_output_color else (p.color or [0.0, 0.0, 0.0, 0.0])
        our_color = (list(our_color) + [0.0] * 4)[:4]
        cdiff = [abs(our_color[i] - g['color'][i]) for i in range(4)]
        ddiff = abs(float(p.depth) - g['depth'])
        if max(cdiff) <= tolerance and ddiff <= tolerance:
            matched += 1
        else:
            mismatched += 1
            if reported < max_report:
                log(f"Error [PixelDiff]: ({key[0]},{key[1]}) "
                    f"color out={_fmt4(our_color)} golden={_fmt4(g['color'])} cdiff={_fmt4(cdiff)} | "
                    f"depth out={float(p.depth):.6f} golden={g['depth']:.6f} ddiff={ddiff:.6f}")
                reported += 1

    extra = sum(1 for k in ours if k not in golden)
    suppressed = (mismatched + missing) - reported
    if suppressed > 0:
        log(f"  ... ({suppressed} more mismatch/missing line(s) suppressed)")
    log(f"  Golden pixels: {len(golden)} | matched: {matched} | "
        f"mismatched: {mismatched} | missing: {missing} | "
        f"extra (ours not in golden): {extra}")
    log("=" * 50)


# ----------------------------------------------------------------------------
# Golden depth comparison (diff_depth_output.csv)
# ----------------------------------------------------------------------------
def _load_golden_depth_output(path: str) -> dict:
    """Parse diff_depth_output.csv → {(x, y): depth}.

    Columns are ``X,Y,Depth,Stencil`` — the captured post-draw depth buffer for
    every pixel this draw wrote. Used to validate the interpreter's per-pixel
    output depth when the depth test is enabled."""
    golden = {}
    for row in _read_csv_rows(path):
        try:
            x = int(float(row['X']))
            y = int(float(row['Y']))
            depth = float(row['Depth'])
        except (KeyError, TypeError, ValueError):
            continue
        golden[(x, y)] = depth
    return golden


def _compare_depth_output(golden: dict, pixels, tolerance: float, log, max_report: int = 50) -> None:
    """Compare final per-pixel output depth against the golden depth dump.

    Only depth is compared, within ``tolerance``. As in the RT0 comparison,
    several rendered fragments may land on the same (x, y); the nearest (smallest
    depth) is taken as the winner, matching a standard LESS depth test."""
    log("\n" + "=" * 50)
    log(f"Output Depth vs Golden (diff_depth_output.csv, tolerance={tolerance}):")
    log("=" * 50)
    if not golden:
        log("  No golden depth data (diff_depth_output.csv empty or absent) — skipped.")
        log("=" * 50)
        return

    # Collapse rendered pixels to one winner per (x, y): nearest depth wins.
    ours = {}
    for p in pixels:
        key = (int(p.x), int(p.y))
        prev = ours.get(key)
        if prev is None or p.depth < prev.depth:
            ours[key] = p

    matched = mismatched = missing = 0
    reported = 0
    for key, g_depth in golden.items():
        p = ours.get(key)
        if p is None:
            missing += 1
            if reported < max_report:
                log(f"Error [DepthDiff]: ({key[0]},{key[1]}) missing in output "
                    f"(golden depth={g_depth:.6f})")
                reported += 1
            continue
        ddiff = abs(float(p.depth) - g_depth)
        if ddiff <= tolerance:
            matched += 1
        else:
            mismatched += 1
            if reported < max_report:
                log(f"Error [DepthDiff]: ({key[0]},{key[1]}) "
                    f"depth out={float(p.depth):.6f} golden={g_depth:.6f} ddiff={ddiff:.6f}")
                reported += 1

    extra = sum(1 for k in ours if k not in golden)
    suppressed = (mismatched + missing) - reported
    if suppressed > 0:
        log(f"  ... ({suppressed} more mismatch/missing line(s) suppressed)")
    log(f"  Golden depth pixels: {len(golden)} | matched: {matched} | "
        f"mismatched: {mismatched} | missing: {missing} | "
        f"extra (ours not in golden): {extra}")
    log("=" * 50)


def _rt_format_to_clamp_mode(fmt):
    """Map a render-target format string (from pipeline_state.csv,
    e.g. "R8G8B8A8_UNORM") to an output-merger write-clamp mode.

    Returns one of 'unorm' / 'snorm' / 'float' / 'none', or None when the
    format is absent/typeless/unrecognised (caller falls back to config).
      UNORM, UNORM_SRGB → 'unorm' ([0,1])
      SNORM             → 'snorm' ([-1,1])
      FLOAT             → 'float' (no clamp)
      UINT, SINT        → 'none'  (integer RT; float clamp doesn't apply)
    """
    if not fmt:
        return None
    u = fmt.upper()
    if 'TYPELESS' in u:
        return None
    if 'SNORM' in u:
        return 'snorm'
    if 'UNORM' in u or 'SRGB' in u:
        return 'unorm'
    if 'FLOAT' in u:
        return 'float'
    if 'UINT' in u or 'SINT' in u:
        return 'none'
    return None


def _clamp_output_colors(pixels, mode, log) -> None:
    """Output-merger write: clamp each pixel's PS output color to the render
    target's representable range, modelling D3D11's format conversion on write.

    D3D clamps the shader output when converting to a fixed-point target
    (UNORM → [0,1], SNORM → [-1,1]) and leaves FLOAT targets unclamped. The
    capture RTs here are R8G8B8A8_UNORM, so the default clamps to [0,1].
      mode True / "unorm" → [0,1] (default)
      mode "snorm"        → [-1,1]
      mode False / "float"/"none" → no clamp (HDR / float RT)
    """
    if mode in (False, 'float', 'none', 'None') or mode is None:
        return
    lo, hi = (-1.0, 1.0) if str(mode).lower() == 'snorm' else (0.0, 1.0)
    clamped = 0
    for p in pixels:
        c = p.ps_output_color
        if not c:
            continue
        new = [max(lo, min(hi, v)) if isinstance(v, (int, float)) else v for v in c]
        if new != list(c):
            clamped += 1
        p.ps_output_color = new
    log(f"Output-Merger write clamp [{lo},{hi}]: {clamped} pixel(s) had a "
        f"channel outside range (clamped).")


def _color_to_byte(v):
    """Quantize a float color component in [0,1] to an 8-bit value, clamped.

    A pixel shader can legitimately emit a non-finite channel (e.g. a 1/w that
    blows up on a sliver triangle); clamp inf/-inf/NaN here rather than letting
    int(round(inf)) raise OverflowError and abort the whole bitmap dump."""
    try:
        f = float(v) * 255.0
    except (TypeError, ValueError):
        return 0
    if f != f:            # NaN
        return 0
    if f == float('inf'):
        return 255
    if f == float('-inf'):
        return 0
    return max(0, min(255, int(round(f))))


def _write_bmp24(frame, width, height, path, log) -> bool:
    """Write a top-down ``height × width`` frame of (r, g, b) byte triples to a
    24-bit BI_RGB BMP at ``path``.

    BMP stores scanlines bottom-up, so the last frame row is written first
    (the Y-flip), and each row is padded to a 4-byte boundary. Returns True on
    success.
    """
    import struct

    row_bytes = width * 3
    padding = (4 - (row_bytes % 4)) % 4
    pad = b'\x00' * padding
    pixel_array_size = (row_bytes + padding) * height
    try:
        with open(path, 'wb') as f:
            # BITMAPFILEHEADER (14 bytes)
            f.write(b'BM')
            f.write(struct.pack('<I', 54 + pixel_array_size))
            f.write(struct.pack('<HH', 0, 0))
            f.write(struct.pack('<I', 54))
            # BITMAPINFOHEADER (40 bytes); positive height → bottom-up rows
            f.write(struct.pack('<IiiHHIIiiII',
                                40, width, height, 1, 24, 0,
                                pixel_array_size, 2835, 2835, 0, 0))
            # BMP scanlines are bottom-up: emit the last screen row first.
            for py in range(height - 1, -1, -1):
                row = frame[py]
                f.write(b''.join(bytes((b, g, r)) for (r, g, b) in row))
                f.write(pad)
    except Exception as e:
        log(f"Failed to write bitmap {path}: {e}")
        return False
    return True


def _save_output_pixels_bitmap(pixels, viewport, path, log) -> bool:
    """Save the pipeline's final output-merger pixel colors to a 24-bit BMP.

    The image is sized to the viewport (width × height). Each output pixel is
    placed at (x − viewport.x, y − viewport.y); where several fragments land on
    the same pixel the nearest (smallest depth) wins, matching the standard LESS
    depth test (and the golden PixelDiff collapse). Pixels never written stay
    black. Color is taken from ``ps_output_color`` (falling back to the
    interpolated ``color``), clamped to [0,1] and quantized to 8-bit BGR.

    Returns True on success.
    """
    width = int(round(viewport.width))
    height = int(round(viewport.height))
    if width <= 0 or height <= 0:
        log(f"Output bitmap skipped: invalid viewport size {width}x{height}.")
        return False
    x0 = int(round(viewport.x))
    y0 = int(round(viewport.y))

    # Collapse to one winner per (x, y): nearest depth wins.
    winners = {}
    for p in pixels:
        key = (int(p.x), int(p.y))
        prev = winners.get(key)
        if prev is None or p.depth < prev.depth:
            winners[key] = p

    # Framebuffer: height rows (top-down) of width (r, g, b) byte triples.
    black = (0, 0, 0)
    frame = [[black] * width for _ in range(height)]

    written = 0
    for (x, y), p in winners.items():
        px, py = x - x0, y - y0
        if not (0 <= px < width and 0 <= py < height):
            continue
        c = p.ps_output_color if p.ps_output_color else (p.color or black)
        c = (list(c) + [0.0, 0.0, 0.0])[:3]
        frame[py][px] = (_color_to_byte(c[0]), _color_to_byte(c[1]), _color_to_byte(c[2]))
        written += 1

    if not _write_bmp24(frame, width, height, path, log):
        return False
    log(f"Saved output-merger bitmap: {path} ({width}x{height}, {written} pixel(s) written)")
    return True


def _save_golden_pixels_bitmap(golden, viewport, path, log) -> bool:
    """Save the golden output-merger pixel colors (diff_ps_output_rt0.csv) to a
    24-bit BMP, sized to the viewport (width × height).

    ``golden`` is the dict from :func:`_load_golden_ps_output`:
    ``{(x, y): {'color': [r, g, b, a], 'depth': f}}`` — one color per screen
    pixel already (no depth collapse needed). Each entry is placed at
    (x − viewport.x, y − viewport.y); untouched pixels stay black. Colors are
    clamped to [0,1] and quantized to 8-bit BGR. Returns True on success.
    """
    width = int(round(viewport.width))
    height = int(round(viewport.height))
    if width <= 0 or height <= 0:
        log(f"Golden bitmap skipped: invalid viewport size {width}x{height}.")
        return False
    x0 = int(round(viewport.x))
    y0 = int(round(viewport.y))

    black = (0, 0, 0)
    frame = [[black] * width for _ in range(height)]

    written = 0
    for (x, y), g in golden.items():
        px, py = x - x0, y - y0
        if not (0 <= px < width and 0 <= py < height):
            continue
        c = (list(g.get('color') or black) + [0.0, 0.0, 0.0])[:3]
        frame[py][px] = (_color_to_byte(c[0]), _color_to_byte(c[1]), _color_to_byte(c[2]))
        written += 1

    if not _write_bmp24(frame, width, height, path, log):
        return False
    log(f"Saved golden bitmap: {path} ({width}x{height}, {written} pixel(s) written)")
    return True


def _save_depth_bitmap(pixels, viewport, path, log) -> bool:
    """Save the pipeline's per-pixel depth buffer as a grayscale 24-bit BMP.

    The image is sized to the viewport (width × height). As in
    :func:`_save_output_pixels_bitmap`, fragments are collapsed to one winner
    per (x, y) with the nearest (smallest) depth, matching the LESS depth test.

    Depth values are commonly clustered very close to 0 (near plane) or 1 (far
    plane), so a naive ``gray = depth * 255`` would collapse the whole image to
    black or white. To keep contrast, the written depths are auto-scaled: the
    actual [min, max] range across the buffer is mapped linearly onto [0, 255]
    (min→black, max→white). When the range is degenerate (all depths
    effectively equal) every covered pixel is rendered mid-gray. Pixels never
    written stay black. Returns True on success.
    """
    width = int(round(viewport.width))
    height = int(round(viewport.height))
    if width <= 0 or height <= 0:
        log(f"Depth bitmap skipped: invalid viewport size {width}x{height}.")
        return False
    x0 = int(round(viewport.x))
    y0 = int(round(viewport.y))

    # Collapse to one winner per (x, y): nearest depth wins.
    winners = {}
    for p in pixels:
        d = p.depth
        if not isinstance(d, (int, float)):
            continue
        key = (int(p.x), int(p.y))
        prev = winners.get(key)
        if prev is None or d < prev.depth:
            winners[key] = p

    # Keep only winners that land inside the viewport, with their depth.
    covered = []  # (px, py, depth)
    for (x, y), p in winners.items():
        px, py = x - x0, y - y0
        if 0 <= px < width and 0 <= py < height:
            covered.append((px, py, float(p.depth)))

    if not covered:
        log("Depth bitmap skipped: no covered pixels with depth.")
        return False

    depths = [d for (_, _, d) in covered]
    dmin, dmax = min(depths), max(depths)
    rng = dmax - dmin

    # Auto-scale: map [dmin, dmax] -> [0, 255] so a buffer whose depths all sit
    # near 0 or near 1 still shows contrast. Degenerate range -> flat mid-gray.
    EPS = 1e-9
    black = (0, 0, 0)
    frame = [[black] * width for _ in range(height)]
    for (px, py, d) in covered:
        if rng > EPS:
            g = _color_to_byte((d - dmin) / rng)
        else:
            g = 128
        frame[py][px] = (g, g, g)

    if not _write_bmp24(frame, width, height, path, log):
        return False
    if rng > EPS:
        log(f"Saved depth bitmap: {path} ({width}x{height}, {len(covered)} pixel(s) "
            f"written; depth range [{dmin:.6g}, {dmax:.6g}] auto-scaled to [0,255])")
    else:
        log(f"Saved depth bitmap: {path} ({width}x{height}, {len(covered)} pixel(s) "
            f"written; depth ~constant {dmin:.6g}, rendered mid-gray)")
    return True


def _collect_mip_paths(data_folder, stage, slot, resource_id, first_mip, num_mips):
    """Ordered texture data paths for a texture's view mip range (mip0, mip1,
    ...). Prefers raw .img data (decoded per the texture's DXGI format),
    falling back to the .bmp for a level when no .img was dumped.

    Stops at the first level with neither .img nor .bmp so the chain never
    indexes past real captured data."""
    paths = []
    for m in range(first_mip, first_mip + max(1, num_mips)):
        stem = f"{stage}_slot_{slot}_res_{resource_id}_mip{m}_arr0"
        img = os.path.join(data_folder, stem + '.img')
        bmp = os.path.join(data_folder, stem + '.bmp')
        if os.path.exists(img):
            paths.append(img)
        elif os.path.exists(bmp):
            paths.append(bmp)
        else:
            break
    return paths


def _discover_stage_textures_from_bmp(data_folder, stage, log=None):
    """Fallback texture discovery: scan dumped BMPs directly when
    texture_params.csv is absent. Groups every mip slice (arr0) per slot so
    the real captured mip chain is loaded, and pairs each texture with a
    default sampler at the same slot index."""
    try:
        from texture import TextureDesc, Sampler, Texture
    except Exception:
        return None, [], []

    pattern = _texture_file_re(stage)
    slot_mips = {}  # slot -> {mip: path}
    slot_res = {}   # slot -> resource id
    for name in os.listdir(data_folder):
        m = pattern.match(name)
        if not m:
            continue
        slot, resid, mip, arr = (int(m.group(1)), int(m.group(2)),
                                 int(m.group(3)), int(m.group(4)))
        if arr != 0:
            continue
        slot_mips.setdefault(slot, {})[mip] = os.path.join(data_folder, name)
        slot_res[slot] = resid

    if not slot_mips:
        return None, [], []

    max_slot = max(slot_mips)
    texture_desc_list = [None] * (max_slot + 1)
    sampler_list = [None] * (max_slot + 1)
    for slot, mips in sorted(slot_mips.items()):
        mip_paths = [mips[m] for m in sorted(mips)]
        texture_desc_list[slot] = TextureDesc(
            MipLevels=len(mip_paths), DataPath=mip_paths[0], MipDataPaths=mip_paths)
        sampler_list[slot] = Sampler()  # default linear/wrap sampler
        if log:
            log(f"  {stage} texture t{slot}: res {slot_res[slot]}, "
                f"{len(mip_paths)} mip level(s) ({os.path.basename(mip_paths[0])})")

    return Texture(), texture_desc_list, sampler_list


def _load_stage_textures(data_folder, stage, log=None):
    """Build per-stage texture and sampler descriptors from the zip's
    texture_params.csv and sampler_params.csv.

    - texture_params.csv → TextureDesc per texture (t) slot, with the full
      captured mip chain (ViewFirstMip .. ViewFirstMip+ViewNumMips-1).
    - sampler_params.csv → Sampler per sampler (s) slot, configured with the
      captured address modes, filter (Min/Mag/Mip), LOD range and bias.

    texture_desc_list is indexed by texture register (t-slot); sampler_list
    is indexed by sampler register (s-slot). Falls back to scanning dumped
    BMPs when texture_params.csv is missing.

    Returns (texture_exec, texture_desc_list, sampler_list) or (None, [], []).
    """
    try:
        from texture import TextureDesc, Sampler, Texture
    except Exception:
        return None, [], []

    stage = stage.upper()
    tex_csv = os.path.join(data_folder, 'texture_params.csv')
    samp_csv = os.path.join(data_folder, 'sampler_params.csv')

    texture_by_slot = {}
    for row in _read_csv_rows(tex_csv):
        if (row.get('Stage') or '').strip().upper() != stage:
            continue
        bind = (row.get('BindType') or '').strip().upper()
        if bind and bind != 'SRV':
            continue
        if 'Slot' not in row or 'ResourceId' not in row:
            continue
        slot = _as_int(row.get('Slot'), -1)
        resource_id = _as_int(row.get('ResourceId'), -1)
        if slot < 0 or resource_id < 0:
            continue
        mips_num = _as_int(row.get('MipsNum'), 1)
        first_mip = _as_int(row.get('ViewFirstMip'), 0)
        view_mips = _as_int(row.get('ViewNumMips'), mips_num) or mips_num
        mip_paths = _collect_mip_paths(
            data_folder, stage, slot, resource_id, first_mip, view_mips)
        if not mip_paths:
            # SRV bound but no BMP dumped (e.g. injected 3Dmigoto resource) —
            # nothing to sample, skip it.
            continue
        texture_by_slot[slot] = TextureDesc(
            Width=_as_int(row.get('Width'), 512) or 512,
            Height=_as_int(row.get('Height'), 512) or 512,
            MipLevels=len(mip_paths),
            ArraySize=_as_int(row.get('ArraySize'), 1) or 1,
            DataPath=mip_paths[0],
            MipDataPaths=mip_paths,
            FormatStr=(row.get('Format') or '').strip(),
        )
        if log:
            log(f"  {stage} texture t{slot}: res {resource_id}, "
                f"{len(mip_paths)} mip level(s) ({os.path.basename(mip_paths[0])})")

    # No texture_params.csv usable — fall back to scanning dumped BMPs.
    if not texture_by_slot:
        return _discover_stage_textures_from_bmp(data_folder, stage, log)

    sampler_by_slot = {}
    for row in _read_csv_rows(samp_csv):
        if (row.get('Stage') or '').strip().upper() != stage:
            continue
        if 'Slot' not in row:
            continue
        slot = _as_int(row.get('Slot'), -1)
        if slot < 0:
            continue
        sampler_by_slot[slot] = Sampler.from_params_row(row)
        if log:
            log(f"  {stage} sampler s{slot}: Filter={row.get('Filter','')} "
                f"Address={row.get('AddressU')}/{row.get('AddressV')}/{row.get('AddressW')}")

    max_t = max(texture_by_slot)
    texture_desc_list = [None] * (max_t + 1)
    for slot, desc in texture_by_slot.items():
        texture_desc_list[slot] = desc

    if sampler_by_slot:
        max_s = max(sampler_by_slot)
        sampler_list = [None] * (max_s + 1)
        for slot, samp in sampler_by_slot.items():
            sampler_list[slot] = samp
    else:
        # No sampler params captured — default linear/wrap sampler at slot 0.
        sampler_list = [Sampler()]

    return Texture(), texture_desc_list, sampler_list


def _run_zip_workflow(config: dict, data_path: str, config_path: str):
    """Execute the full rendering pipeline from a zip archive."""
    config_dir = os.path.dirname(os.path.abspath(config_path))
    if os.path.isabs(data_path):
        abs_data_path = data_path
    elif os.path.exists(data_path):
        abs_data_path = os.path.abspath(data_path)
    else:
        abs_data_path = os.path.join(config_dir, data_path)

    if not os.path.exists(abs_data_path):
        print(f"Error: data_path not found: {abs_data_path}")
        sys.exit(1)

    temp_dir = tempfile.mkdtemp(prefix='hlsl_interp_')
    try:
        print(f"Extracting {abs_data_path} ...")
        with zipfile.ZipFile(abs_data_path, 'r') as z:
            z.extractall(temp_dir)

        # Find top-level folder inside the zip
        items = os.listdir(temp_dir)
        if len(items) == 1 and os.path.isdir(os.path.join(temp_dir, items[0])):
            data_folder = os.path.join(temp_dir, items[0])
        else:
            data_folder = temp_dir

        _execute_pipeline(config, config_path, data_folder)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _execute_pipeline(config: dict, config_path: str, data_folder: str):
    """Execute VS → Rasterizer → Depth → PS pipeline from extracted data folder."""
    # Config parameters
    log_file_path = config.get('log_file_path', '')
    float_tolerance = config.get('float_tolerance', 0.0001)
    execute_count = config.get('execute_count', None)
    if execute_count == -1:
        execute_count = None
    early_z = config.get('early_z', True)
    mesh_view_enabled = config.get('mesh_view_enabled', False)
    primitive_topology = config.get('primitive_topology', D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST)
    # Tolerance for the golden output-merger pixel comparison (color + depth).
    pixel_tolerance = config.get('pixel_tolerance', 0.01)
    # Tolerance for the golden depth comparison (diff_depth_output.csv). Separate
    # from pixel_tolerance so depth can be validated more tightly than color.
    depth_tolerance = config.get('depth_tolerance', 0.01)
    # Tolerance (in samples) for the golden SamplesPassed/depth_passed compare.
    # Our rasterizer's fragment count/ordering differs slightly from the GPU, so
    # an exact match is unrealistic; differences within this band are accepted.
    samples_passed_tolerance = config.get('samples_passed_tolerance', 500)
    # Output-merger write clamp. D3D converts the PS output to the render-target
    # format on write: fixed-point targets clamp (UNORM→[0,1], SNORM→[-1,1]),
    # FLOAT targets don't. These captures use R8G8B8A8_UNORM → default [0,1].
    output_color_clamp = config.get('output_color_clamp', True)

    # Resolve log path relative to config
    if log_file_path:
        config_dir = os.path.dirname(os.path.abspath(config_path))
        log_file_path = log_file_path if os.path.isabs(log_file_path) else os.path.join(config_dir, log_file_path)

    # Optional intermediate-data tracing (PS pixels / texture LOD / quad
    # derivatives) to a file. No-op unless the config has a "debug_trace" block
    # with "enabled": true. Debug file is written next to the log file (or the
    # config dir when no log is set); see debug_trace.py for config keys.
    _trace_dir = os.path.dirname(log_file_path) if log_file_path else os.path.dirname(os.path.abspath(config_path))
    _trace_stem = os.path.splitext(os.path.basename(config.get('data_path', '')))[0] or 'pipeline'
    TRACE.configure(config, base_dir=_trace_dir, default_stem=_trace_stem)

    # Data file paths
    vs_hlsl = os.path.join(data_folder, 'VS_shader.hlsl')
    ps_hlsl = os.path.join(data_folder, 'PS_shader.hlsl')
    vs_cb_csv = os.path.join(data_folder, 'VS_constant_buffers.csv')
    ps_cb_csv = os.path.join(data_folder, 'PS_constant_buffers.csv')
    vs_sig_csv = os.path.join(data_folder, 'VS_input_output_signature.csv')
    ps_sig_csv = os.path.join(data_folder, 'PS_input_output_signature.csv')
    ia_vertex_csv = os.path.join(data_folder, 'ia_vertex_data.csv')
    pipeline_state_csv = os.path.join(data_folder, 'pipeline_state.csv')
    # Golden VS output: 3Dmigoto/RenderDoc dumps name this file either
    # 'MeshOut_vs_mesh.csv' or '<capture-prefix>_vs_mesh.csv'. Prefer the
    # canonical name, otherwise pick up any '*_vs_mesh.csv' in the folder so
    # the VS-vs-golden comparison is not silently skipped.
    golden_vs_csv = os.path.join(data_folder, 'MeshOut_vs_mesh.csv')
    if not os.path.exists(golden_vs_csv):
        mesh_candidates = sorted(
            f for f in os.listdir(data_folder) if f.lower().endswith('_vs_mesh.csv')
        )
        if mesh_candidates:
            golden_vs_csv = os.path.join(data_folder, mesh_candidates[0])

    total_start = time.time()

    # ============================================================
    # VS setup and execution
    # ============================================================
    vs_interp = _make_interpreter(config, d3d.SHADER_STAGE_VS, log_file_path, primitive_topology)

    if mesh_view_enabled:
        vs_interp.enable_mesh_view(True)

    if not os.path.exists(vs_hlsl):
        print(f"Error: VS shader not found: {vs_hlsl}")
        return

    with open(vs_hlsl, 'r', encoding='utf-8') as f:
        vs_code_raw = f.read()
    vs_code = vs_interp.preprocess_hlsl(vs_code_raw)
    vs_interp.hlsl_code = vs_code

    # Load VS shader-resource textures + samplers (vertex texture fetch) from
    # the zip's texture_params.csv / sampler_params.csv. No-op when the VS
    # stage binds no textures.
    vs_tex_exec, vs_tex_desc_list, vs_samp_list = _load_stage_textures(
        data_folder, 'VS', vs_interp.log_output
    )
    if vs_tex_exec:
        vs_interp.set_texture_and_sampler(vs_tex_exec, vs_tex_desc_list, vs_samp_list)
        vs_interp.log_output(
            f"Loaded {sum(1 for t in vs_tex_desc_list if t)} VS texture(s) from zip"
        )

    # Parse cbuffers, texture/sampler bindings, and functions from VS code
    vs_interp._parse_texture_and_sampler_bindings(vs_code)
    vs_interp._parse_structured_buffers(vs_code)
    for cb_match in vs_interp.patterns['cbuffer_finditer'].finditer(vs_code):
        cb_def = vs_interp.parse_cbuffer(cb_match.group())
        if cb_def:
            vs_interp.cbuffers[cb_def.name] = cb_def
    vs_interp.parse_all_functions(vs_code)

    # Load cbuffer data
    if os.path.exists(vs_cb_csv):
        vs_interp.load_all_cbuffers_from_combined_csv(vs_cb_csv)
    # Prefer the raw capture binary so integer bit patterns survive for
    # asint/asuint (no-op when *_constant_buffer_info.csv is absent).
    vs_interp.override_cbuffers_from_binary(data_folder, 'VS')

    # Load StructuredBuffer data (e.g. skinning bone palette t0)
    vs_interp.load_structured_buffer_data(data_folder)
    # Load typed Buffer<T> data (e.g. Buffer<float4> t1 texcoord table)
    vs_interp.load_typed_buffer_data(data_folder)

    # Load VS signature
    vs_sig = vs_interp.load_signature_from_csv(vs_sig_csv)

    # Parse VS main function parameters
    vs_params = vs_interp.parse_main_params_with_semantics(vs_code, 'main')
    if not vs_params:
        print("Error: Could not parse VS main() parameters")
        return

    vs_input_params = vs_params['inputs']
    vs_output_params = vs_params['outputs']

    vs_interp.map_params_to_signature(vs_input_params, vs_sig['inputs'])
    vs_interp.map_params_to_signature(vs_output_params, vs_sig['outputs'])

    vs_interp.log_output(f"VS inputs:  {[(p['name'], p['type'], p['semantic']) for p in vs_input_params]}")
    vs_interp.log_output(f"VS outputs: {[(p['name'], p['type'], p['semantic'], 'slot', p['slot']) for p in vs_output_params]}")

    # Load vertex data
    vertex_data = vs_interp.load_ia_vertex_data(ia_vertex_csv, vs_input_params)
    vs_interp.log_output(f"Loaded {len(vertex_data)} vertices from ia_vertex_data.csv")

    # Load per-instance inputs (INSTANCE_TRANSFORM*, etc.) from the binary
    # vertex buffers. ia_vertex_data.csv only carries the per-vertex stream;
    # instanced inputs come from a separate slot and would otherwise be zero.
    ia_layouts_csv = os.path.join(data_folder, 'ia_input_layouts.csv')
    instance_inputs = vs_interp.load_per_instance_data(
        ia_layouts_csv, data_folder, vs_input_params, instance_index=0
    )
    if instance_inputs:
        for vtx in vertex_data:
            vtx.update(instance_inputs)
        vs_interp.log_output(
            f"Loaded per-instance inputs (instance 0): "
            f"{ {k: (v if not isinstance(v, list) else [round(x, 4) for x in v]) for k, v in instance_inputs.items()} }"
        )

    # Re-decode per-vertex inputs that ia_vertex_data.csv stored as zeros
    # because RenderDoc could not represent their format (e.g. NORMAL/TANGENT
    # dumped as 'R0G0B0A0_UNORM'). These come from a separate VB slot and are
    # fetched by index-buffer value, then override the zero CSV columns.
    idx_list = vs_interp.load_index_column(ia_vertex_csv)
    # SV_VertexID is a per-vertex system value (the index-buffer value for an
    # indexed draw, +BaseVertex=0). Used e.g. to index a Buffer<float4> texcoord
    # table (Octopath). Without it every vertex reads element 0.
    sv_vid_params = [p['name'] for p in vs_input_params
                     if p['semantic_base'].upper() == 'SV_VERTEXID']
    if sv_vid_params and idx_list:
        for i, vtx in enumerate(vertex_data):
            vid = idx_list[i] if i < len(idx_list) else i
            for pname in sv_vid_params:
                vtx[pname] = vid
    pv_overrides = vs_interp.load_per_vertex_binary_data(
        ia_layouts_csv, data_folder, vs_input_params, idx_list,
        csv_vertex_data=vertex_data,
    )
    if pv_overrides:
        overridden = set()
        for i, ov in enumerate(pv_overrides):
            if i < len(vertex_data) and ov:
                vertex_data[i].update(ov)
                overridden.update(ov.keys())
        if overridden:
            vs_interp.log_output(
                f"Re-decoded per-vertex inputs from binary VB: {sorted(overridden)}"
            )

    # Load golden VS output (optional)
    golden_vs_rows = []
    if os.path.exists(golden_vs_csv):
        golden_vs_rows = vs_interp.load_vs_golden_from_mesh_csv(golden_vs_csv, vs_output_params)
        vs_interp.log_output(f"Loaded {len(golden_vs_rows)} golden VS output rows")

    # Execute VS
    vs_interp.log_output("=" * 50)
    vs_interp.log_output("Executing Vertex Shader...")
    vs_start = time.time()
    vs_results = vs_interp.executeVS_with_params(
        'main', vs_input_params, vs_output_params, vertex_data, execute_count=execute_count
    )
    vs_time = time.time() - vs_start
    vs_interp.log_output(f"VS executed {len(vs_results)} vertices in {vs_time:.4f}s")

    # Print sample results
    if vs_interp.print_interpreter_result and vs_results:
        vs_interp.log_output("\nSample VS output (first row):")
        first = vs_results[0]
        for k, v in first.items():
            if isinstance(v, list):
                vs_interp.log_output(f"  {k}: [{', '.join(f'{x:.4f}' for x in v)}]")
            elif isinstance(v, (int, float)):
                vs_interp.log_output(f"  {k}: {v:.4f}")

    # Compare with golden
    if golden_vs_rows:
        vs_interp.log_output("\nComparing VS output with golden data...")
        vs_interp.compare_vs_output_with_golden_params(
            vs_results, vs_output_params, golden_vs_rows,
            float_tolerance=float_tolerance,
            execute_count=execute_count,
        )

    # ============================================================
    # Rasterizer
    # ============================================================
    rast = Rasterizer()
    if os.path.exists(pipeline_state_csv):
        topo_from_csv = rast.load_config_from_pipeline_state_csv(pipeline_state_csv)
        # Use config override if explicitly set, otherwise use CSV value
        if 'primitive_topology' not in config and topo_from_csv is not None:
            primitive_topology = topo_from_csv

    # The capture's topology enum is unreliable (reports STRIP for list draws);
    # disambiguate against the golden primitive count when not overridden.
    if 'primitive_topology' not in config:
        _golden_iaprim = _load_golden_pipeline_statistics(
            os.path.join(data_folder, 'pipeline_statistics.csv')).get('IAPrimitives', 0)
        primitive_topology = _resolve_triangle_topology(
            primitive_topology, len(vs_results), _golden_iaprim, vs_interp.log_output)

    # Output-merger write clamp mode: prefer the RT0 format from
    # pipeline_state.csv (UNORM→[0,1], SNORM→[-1,1], FLOAT/UINT→no clamp); fall
    # back to the output_color_clamp config when no format row is present.
    rt_format = rast.config.render_target_format
    fmt_clamp_mode = _rt_format_to_clamp_mode(rt_format)
    effective_clamp = fmt_clamp_mode if fmt_clamp_mode is not None else output_color_clamp
    vs_interp.log_output(
        f"Render target format: {rt_format or 'unknown'} "
        f"→ output-merger clamp mode: {effective_clamp}"
        + ("" if fmt_clamp_mode is not None else "  (from config fallback)")
    )

    # Mesh view: display input + VS output meshes (topology now finalized)
    if mesh_view_enabled and vs_interp._mesh_view:
        vs_interp.primitive_topology = primitive_topology
        # Enable "Re-execute Vertex Shader" for the selected vertex (param-based workflow)
        vs_interp._mesh_view.set_hlsl_interpreter_params(
            vs_interp, vs_input_params, vs_output_params, 'main'
        )
        vs_interp.log_output("Displaying input/output mesh...")
        # Align input mesh to the executed vertex count (execute_count may be < total IA buffer)
        vs_interp.show_input_mesh_from_params(vs_input_params, vertex_data[:len(vs_results)])
        vs_interp.show_result_mesh_from_params(vs_results)

    vs_interp.log_output(f"\nRasterizing {len(vs_results)} vertices (topology={primitive_topology})...")
    rast_start = time.time()
    pixels = rast.rasterize(vs_results, primitive_topology)
    rast_time = time.time() - rast_start
    rast_stats = dict(rast.get_stats())
    vs_interp.log_output(f"Rasterized → {len(pixels)} pixels in {rast_time:.4f}s")

    # Pipeline statistics accumulated across the stages, reported at the end and
    # mirrored into the MeshView status bar.
    rast_pixel_count = len(pixels)
    ps_pixel_count = 0
    depth_failed = 0

    # Depth/stencil. The zip ships the depth/stencil buffer contents from
    # *before* this draw (pre_draw_depth_stencil.csv). Loading it lets the
    # depth test compare each fragment against the real pre-existing depth
    # (geometry already on screen) rather than a synthetic clear value, so
    # fragments occluded by earlier draws are correctly rejected.
    depth = Depth()
    pre_draw_ds_csv = os.path.join(data_folder, 'pre_draw_depth_stencil.csv')
    if os.path.exists(pre_draw_ds_csv):
        loaded = depth.load_pre_draw_depth_stencil(pre_draw_ds_csv)
        # Enable the depth test now that there is a real depth buffer to test
        # against. pipeline_state.csv does not capture DepthEnable/DepthFunc for
        # these dumps, so use D3D's standard depth state (LESS, depth-write on),
        # which is what the captured SamplesPassed counter reflects.
        depth.config.depth_enable = True
        depth.config.depth_write_mask = True
        depth.config.depth_func = ComparisonFunc.LESS
        vs_interp.log_output(
            f"Loaded pre-draw depth buffer: {loaded} pixels; "
            f"depth test enabled (func=LESS, write=on)"
        )

    if early_z:
        pixels = depth.execute(pixels, early_z=True)
        depth_failed = rast_pixel_count - len(pixels)
        vs_interp.log_output(f"Early-Z: {len(pixels)} pixels after depth test")

    pixels_for_ps = pixels

    # ============================================================
    # PS setup and execution
    # ============================================================
    no_ps = True
    if os.path.exists(ps_hlsl) and pixels_for_ps:
        no_ps = False
        ps_interp = _make_interpreter(config, d3d.SHADER_STAGE_PS, log_file_path)

        with open(ps_hlsl, 'r', encoding='utf-8') as f:
            ps_code_raw = f.read()
        ps_code = ps_interp.preprocess_hlsl(ps_code_raw)
        ps_interp.hlsl_code = ps_code

        # Load PS shader-resource textures + samplers from the zip's
        # texture_params.csv / sampler_params.csv so Texture2D.Sample resolves
        # to real texel data with the captured sampler state and mip chain.
        tex_exec, tex_desc_list, samp_list = _load_stage_textures(
            data_folder, 'PS', ps_interp.log_output
        )
        if tex_exec:
            ps_interp.set_texture_and_sampler(tex_exec, tex_desc_list, samp_list)
            ps_interp.log_output(
                f"Loaded {sum(1 for t in tex_desc_list if t)} PS texture(s) from zip"
            )

        ps_interp._parse_texture_and_sampler_bindings(ps_code)
        for cb_match in ps_interp.patterns['cbuffer_finditer'].finditer(ps_code):
            cb_def = ps_interp.parse_cbuffer(cb_match.group())
            if cb_def:
                ps_interp.cbuffers[cb_def.name] = cb_def
        ps_interp.parse_all_functions(ps_code)

        if os.path.exists(ps_cb_csv):
            ps_interp.load_all_cbuffers_from_combined_csv(ps_cb_csv)
        ps_interp.override_cbuffers_from_binary(data_folder, 'PS')

        ps_params = ps_interp.parse_main_params_with_semantics(ps_code, 'main')
        if ps_params:
            ps_input_params = ps_params['inputs']
            ps_output_params = ps_params['outputs']

            if os.path.exists(ps_sig_csv):
                ps_sig = ps_interp.load_signature_from_csv(ps_sig_csv)
                ps_interp.map_params_to_signature(ps_input_params, ps_sig['inputs'])
                ps_interp.map_params_to_signature(ps_output_params, ps_sig['outputs'])

            ps_interp.log_output(f"\nExecuting Pixel Shader on {len(pixels_for_ps)} pixels...")
            ps_pixel_count = len(pixels_for_ps)
            ps_start = time.time()
            ps_interp.executePS_with_params(
                'main', ps_input_params, ps_output_params, pixels_for_ps, ps_code=ps_code
            )
            ps_time = time.time() - ps_start
            ps_interp.log_output(f"PS executed in {ps_time:.4f}s")

    # Output-merger write: clamp PS output to the render-target's range
    # (mode derived from the RT0 format when available).
    _clamp_output_colors(pixels, effective_clamp, vs_interp.log_output)

    if not early_z:
        pixels = depth.execute(pixels, early_z=False)
        depth_failed = rast_pixel_count - len(pixels)
        vs_interp.log_output(f"Late-Z: {len(pixels)} pixels after depth test")

    total_time = time.time() - total_start
    vs_interp.log_output("\n" + "=" * 50)
    vs_interp.log_output("Pipeline Summary:")
    vs_interp.log_output(f"  VS:          {len(vs_results)} vertices in {vs_time:.4f}s")
    vs_interp.log_output(f"  Rasterizer:  {len(rast.get_pixels())} pixels in {rast_time:.4f}s")
    vs_interp.log_output(f"  Total:       {total_time:.4f}s")

    # ============================================================
    # Pipeline statistics (vertices, primitives, clip/cull, pixels)
    # ============================================================
    pipeline_stats = {
        'vertices': len(vs_results),
        'primitives': rast_stats.get('primitives', 0),
        'clipped': rast_stats.get('clipped', 0),
        'not_clipped': rast_stats.get('not_clipped', 0),
        'culled': rast_stats.get('culled', 0),
        'not_culled': rast_stats.get('not_culled', 0),
        'rast_pixels': rast_pixel_count,
        'depth_failed': depth_failed,
        'depth_passed': rast_pixel_count - depth_failed,
        'ps_pixels': ps_pixel_count,
    }
    vs_interp.log_output("\n" + "=" * 50)
    vs_interp.log_output("Pipeline Statistics:")
    vs_interp.log_output("=" * 50)
    vs_interp.log_output(f"  Vertices executed:        {pipeline_stats['vertices']}")
    vs_interp.log_output(f"  Primitives assembled:     {pipeline_stats['primitives']}")
    vs_interp.log_output(f"  Primitives clipped:       {pipeline_stats['clipped']}  (not clipped: {pipeline_stats['not_clipped']})")
    vs_interp.log_output(f"  Primitives culled:        {pipeline_stats['culled']}  (not culled: {pipeline_stats['not_culled']})")
    vs_interp.log_output(f"  Rasterizer pixels:        {pipeline_stats['rast_pixels']}")
    vs_interp.log_output(f"  Pixels failed depth test: {pipeline_stats['depth_failed']}  (passed: {pipeline_stats['depth_passed']})")
    vs_interp.log_output(f"  Pixel shader executed:    {pipeline_stats['ps_pixels']} pixels")
    vs_interp.log_output("=" * 50)

    # ============================================================
    # Golden comparisons: pipeline statistics + output-merger pixels
    # ============================================================
    golden_stats = _load_golden_pipeline_statistics(
        os.path.join(data_folder, 'pipeline_statistics.csv'))
    if golden_stats:
        _compare_pipeline_statistics(golden_stats, pipeline_stats, vs_interp.log_output,
                                    samples_passed_tolerance=samples_passed_tolerance)

    golden_ps = _load_golden_ps_output(
        os.path.join(data_folder, 'diff_ps_output_rt0.csv'))
    if golden_ps:
        _compare_ps_output(golden_ps, pixels, pixel_tolerance, vs_interp.log_output)
        # Save the golden pixel colors as a viewport-sized bitmap so the
        # reference image can be eyeballed next to our rendered output. Path:
        # 'golden_bitmap_path' from config, else '<zip-stem>_golden.bmp' beside
        # the log file (matching the output-bitmap convention below).
        _gdir = os.path.dirname(os.path.abspath(config_path))
        golden_bmp = config.get('golden_bitmap_path', '')
        if golden_bmp:
            golden_bmp = golden_bmp if os.path.isabs(golden_bmp) else os.path.join(_gdir, golden_bmp)
        else:
            _data_path = config.get('data_path', '')
            _stem = os.path.splitext(os.path.basename(_data_path))[0] if _data_path else 'output'
            _out_dir = os.path.dirname(log_file_path) if log_file_path else _gdir
            golden_bmp = os.path.join(_out_dir, f"{_stem}_golden.bmp")
        _save_golden_pixels_bitmap(golden_ps, rast.config.viewport, golden_bmp, vs_interp.log_output)

    # Golden depth comparison (diff_depth_output.csv). Only meaningful when the
    # depth test ran (a pre-draw depth buffer was loaded), since otherwise the
    # interpreter's per-pixel depth was never resolved against real occlusion.
    if depth.config.depth_enable:
        golden_depth = _load_golden_depth_output(
            os.path.join(data_folder, 'diff_depth_output.csv'))
        if golden_depth:
            _compare_depth_output(golden_depth, pixels, depth_tolerance, vs_interp.log_output)

    # ============================================================
    # Save the final output-merger pixel colors as a viewport-sized bitmap.
    # Path: 'output_bitmap_path' from config (resolved relative to the config
    # file). Otherwise default to '<zip-stem>_output.bmp' next to the log file
    # (so headless/regression runs keep their output beside the case log)
    # falling back to the config directory when no log path is set.
    # ============================================================
    config_dir = os.path.dirname(os.path.abspath(config_path))
    data_path = config.get('data_path', '')
    stem = os.path.splitext(os.path.basename(data_path))[0] if data_path else 'output'
    out_dir = os.path.dirname(log_file_path) if log_file_path else config_dir
    # Which bitmaps to dump is driven by what the output merger has bound, per
    # pipeline_state.csv: a RenderTarget → the color bitmap, a DepthStencil →
    # the depth bitmap. A depth-only pass (e.g. shadow/Z-prepass) has no render
    # target, so no color bitmap is written. Both still require surviving
    # fragments to have anything to draw.
    has_render_target = rast.config.render_target_format is not None
    has_depth_stencil = rast.config.depth_stencil_format is not None

    if not pixels:
        # No fragments survived the pipeline: nothing to render.
        vs_interp.log_output("Output/depth bitmaps skipped: pipeline produced no pixels.")
    else:
        # Output-color bitmap — only when a render target is bound and the PS ran.
        if has_render_target and no_ps == False:
            out_bmp = config.get('output_bitmap_path', '')
            if out_bmp:
                out_bmp = out_bmp if os.path.isabs(out_bmp) else os.path.join(config_dir, out_bmp)
            else:
                out_bmp = os.path.join(out_dir, f"{stem}_output.bmp")
            _save_output_pixels_bitmap(pixels, rast.config.viewport, out_bmp, vs_interp.log_output)
        elif not has_render_target:
            vs_interp.log_output(
                "Output color bitmap skipped: no render target bound (pipeline_state.csv RenderTarget absent).")

        # Depth bitmap — only when a depth-stencil is bound. Path:
        # 'depth_bitmap_path' from config (resolved like output_bitmap_path),
        # else '<zip-stem>_depth.bmp' beside the output bitmap.
        if has_depth_stencil:
            depth_bmp = config.get('depth_bitmap_path', '')
            if depth_bmp:
                depth_bmp = depth_bmp if os.path.isabs(depth_bmp) else os.path.join(config_dir, depth_bmp)
            else:
                depth_bmp = os.path.join(out_dir, f"{stem}_depth.bmp")
            _save_depth_bitmap(pixels, rast.config.viewport, depth_bmp, vs_interp.log_output)
        else:
            vs_interp.log_output(
                "Depth bitmap skipped: no depth-stencil bound (pipeline_state.csv DepthStencil absent).")

    # Flush/close the optional debug trace file (no-op when tracing was off).
    if TRACE.enabled:
        vs_interp.log_output(f"Debug trace written: {getattr(TRACE, 'path', '?')}")
    TRACE.close()

    # ============================================================
    # Mesh view: feed rasterizer/PS/output-merger pixels and stay open
    # ============================================================
    if mesh_view_enabled and vs_interp._mesh_view:
        vs_interp._mesh_view.set_pipeline_stats(pipeline_stats)
        if pixels:
            vs_interp._mesh_view.set_rasterizer_pixels(pixels)
            vs_interp._mesh_view.set_output_merger_pixels(pixels)
            vs_interp._mesh_view._draw_rasterizer_pixels()
            vs_interp._mesh_view._draw_pixel_shader_pixels()
            vs_interp._mesh_view._draw_output_merger_pixels()
        vs_interp._mesh_view.show(blocking=False)

        while True:
            user_input = input("\nEnter 'x' to exit, 'o' to open MeshView: ").strip().lower()
            if user_input == 'x':
                vs_interp._mesh_view.close()
                break
            elif user_input == 'o':
                vs_interp._mesh_view.show(blocking=False)


def _run_legacy_workflow(config: dict, config_path: str):
    """Original struct-based workflow for old JSON format."""
    config_dir = os.path.dirname(os.path.abspath(config_path))

    def resolve(p):
        return p if (not p or os.path.isabs(p)) else os.path.join(config_dir, p)

    hlsl_file_path = resolve(config.get('hlsl_file_path', ''))
    csv_folder_path = resolve(config.get('csv_folder_path', ''))
    log_file_path = resolve(config.get('log_file_path', 'hlsl_interpreter.log'))
    log_file_mode = config.get('log_file_mode', 'a')
    print_sequence = config.get('print_sequence', 1)
    log_to_file = config.get('log_to_file', True)
    printSyntaxTree = config.get('printSyntaxTree', True)
    print_interpreter_result = config.get('print_interpreter_result', True)
    float_tolerance = config.get('float_tolerance', 0.0001)
    output_struct_name = config.get('output_struct_name', 'VS_OUTPUT')
    execute_count = config.get('execute_count', None)
    max_workers = config.get('max_workers', 1)
    primitive_topology = config.get('primitive_topology', D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST)
    mesh_view_enabled = config.get('mesh_view_enabled', False)
    texture_desc_path = resolve(config.get('texture_desc', ''))
    sampler_config_path = resolve(config.get('sampler_config', ''))
    depth_stencil_config_path = resolve(config.get('depth_stencil_config', ''))
    early_z = config.get("early_z", True)

    texture_desc_list = []
    sampler_list = []
    texture_exec = None
    if texture_desc_path and sampler_config_path:
        from texture import TextureDesc, Sampler, Texture
        with open(texture_desc_path, 'r', encoding='utf-8') as f:
            texture_data = json.load(f)
        with open(sampler_config_path, 'r', encoding='utf-8') as f:
            sampler_data = json.load(f)
        for tex_id in texture_data:
            texture_desc_list.append(TextureDesc.from_config(texture_desc_path, int(tex_id)))
        for samp_id in sampler_data:
            sampler_list.append(Sampler.from_config(sampler_config_path, int(samp_id)))
        texture_exec = Texture()

    interpreter = HLSLInterpreter(
        log_to_file=log_to_file,
        log_file_path=log_file_path,
        log_file_mode=log_file_mode,
        print_sequence=print_sequence,
        printSyntaxTree=printSyntaxTree,
        print_interpreter_result=print_interpreter_result,
        max_workers=max_workers,
        primitive_topology=primitive_topology,
        texture_list=[texture_exec] if texture_exec else [],
        texture_desc_list=texture_desc_list,
        sampler_list=sampler_list)

    if mesh_view_enabled:
        interpreter.enable_mesh_view(True)

    total_start = time.time()

    interpret_start = time.time()
    interpreter.interpret(hlsl_file_path, csv_folder_path)
    interpret_time = time.time() - interpret_start

    if mesh_view_enabled and interpreter._mesh_view:
        interpreter._mesh_view.set_hlsl_interpreter(interpreter, "main", "VS_INPUT")

    golden_csv_path = os.path.join(csv_folder_path, 'VS_OUTPUT.csv') if csv_folder_path else None
    load_golden_start = time.time()
    if golden_csv_path and os.path.exists(golden_csv_path):
        interpreter.load_vs_output_golden_from_csv(golden_csv_path)
    load_golden_time = time.time() - load_golden_start

    execute_start = time.time()
    results = interpreter.executeVS("vs_main", "VS_INPUT", execute_count=execute_count)
    execute_time = time.time() - execute_start

    if mesh_view_enabled:
        interpreter.log_output("Displaying input mesh before executeVS...")
        interpreter.show_input_mesh("VS_INPUT")

    print_and_compare_results(interpreter, results, output_struct_name, float_tolerance, execute_count, interpret_time, load_golden_time, execute_time, total_start)

    if mesh_view_enabled and results:
        interpreter.log_output("Displaying result mesh after executeVS...")
        interpreter.show_result_mesh(results)

    # Rasterization
    raster_param_path = resolve(config.get('rasterizer_param', 'rasterizer_param.json'))
    r = Rasterizer(raster_param_path if os.path.exists(raster_param_path) else None)
    pixels = r.rasterize(results, D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST)

    depth = Depth(depth_stencil_config_path) if depth_stencil_config_path and os.path.exists(depth_stencil_config_path) else Depth()

    if early_z:
        depth_pixels = depth.execute(pixels, early_z=True)
        interpreter.log_output(f"Early-Z: Depth processed {len(pixels)} rasterizer pixels to {len(depth_pixels)} pixels")
    else:
        interpreter.log_output(f"Late-Z: Depth will process pixels after PS")
        depth_pixels = pixels

    if mesh_view_enabled and interpreter._mesh_view:
        interpreter._mesh_view.set_rasterizer_pixels(depth_pixels)

    pixels_for_ps = depth_pixels if early_z else pixels

    interpreter.executePS("ps_main", "PS_INPUT_BASIC", pixels_for_ps)

    if not early_z:
        depth_pixels = depth.execute(pixels, early_z=False)
        interpreter.log_output(f"Late-Z: Depth processed {len(pixels)} PS pixels to {len(depth_pixels)} pixels")

    if mesh_view_enabled and interpreter._mesh_view:
        interpreter._mesh_view.set_rasterizer_pixels(depth_pixels)
        interpreter._mesh_view.set_output_merger_pixels(depth_pixels)
        interpreter._mesh_view._draw_rasterizer_pixels()
        interpreter._mesh_view._draw_pixel_shader_pixels()
        interpreter._mesh_view._draw_output_merger_pixels()

    while True:
        user_input = input("\nEnter 'x' to exit, 'o' to open MeshView, 'r' to rerun executeVS: ")
        user_input = user_input.strip().lower()
        if user_input == 'x':
            if interpreter._mesh_view:
                interpreter._mesh_view.close()
            break
        elif user_input == 'o':
            if interpreter._mesh_view:
                interpreter._mesh_view.show(blocking=False)
        elif user_input == 'r':
            execute_start = time.time()
            results = interpreter.executeVS("main", "VS_INPUT", execute_count=execute_count)
            execute_time = time.time() - execute_start
            interpreter.log_output(f"Re-executed executeVS in {execute_time:.4f}s")


def main():
    if len(sys.argv) < 2:
        print("Usage: python render.py <config.json>")
        config_path = './wrong_constant_attenuation.json'
    else:
        config_path = sys.argv[1]

    if not os.path.exists(config_path):
        print(f"Error: Config file not found: {config_path}")
        sys.exit(1)

    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)

    data_path = config.get('data_path', '')
    mesh_view_enabled = config.get('mesh_view_enabled', False)

    if mesh_view_enabled and sys.platform == 'darwin':
        # On macOS, tkinter's Cocoa backend requires the event loop to run on the
        # main thread.  Create the root here (main thread), hand it to MeshView via
        # set_main_thread_root(), run the pipeline in a daemon thread, then block
        # on mainloop() until the user closes the window or the pipeline finishes.
        import tkinter as tk
        import mesh_view as mv_module
        root = tk.Tk()
        root.withdraw()
        mv_module.set_main_thread_root(root)

        def _run_pipeline():
            try:
                if data_path:
                    _run_zip_workflow(config, data_path, config_path)
                else:
                    _run_legacy_workflow(config, config_path)
            finally:
                try:
                    root.after(0, root.quit)
                except Exception:
                    pass

        t = threading.Thread(target=_run_pipeline, daemon=True)
        t.start()
        root.mainloop()
    else:
        if data_path:
            _run_zip_workflow(config, data_path, config_path)
        else:
            _run_legacy_workflow(config, config_path)


if __name__ == '__main__':
    main()
