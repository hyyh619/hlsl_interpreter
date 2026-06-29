"""
dxbc_diff.py — run the DXBC VM (dxbc_interp) on a captured VS and compare its
outputs against the golden *_vs_mesh.csv, to decide whether a divergence is in
the HLSL decompile (DXBC matches golden, HLSL doesn't) or in shared infra.

Usage:
    python dxbc_diff.py <case.zip-or-folder> [vertex_index] [--textures]
"""

import sys
import os
import re
import csv
import struct
import zipfile
import tempfile

from dxbc_interp import DXBCInterpreter


def _load_cbuffers(folder):
    info = os.path.join(folder, 'VS_constant_buffer_info.csv')
    cb = {}
    if not os.path.exists(info):
        return cb
    with open(info, newline='') as f:
        for row in csv.DictReader(f):
            try:
                slot = int(row['Slot'])
            except (KeyError, ValueError):
                continue
            binf = row.get('BinFile', '').strip()
            path = os.path.join(folder, binf)
            if not binf or not os.path.exists(path):
                continue
            data = open(path, 'rb').read()
            nrows = len(data) // 16
            rows = [list(struct.unpack_from('<4f', data, i * 16)) for i in range(nrows)]
            cb[slot] = rows
    return cb


def _load_signature_inputs(folder):
    sig = os.path.join(folder, 'VS_input_output_signature.csv')
    inputs = {}  # slot -> (semantic, index)
    with open(sig, newline='') as f:
        for row in csv.DictReader(f):
            if row.get('Type', '').strip().lower() != 'input':
                continue
            slot = int(row['Slot'])
            inputs[slot] = (row['SemanticName'].strip().upper(), int(row['Index']))
    return inputs


def _load_signature_outputs(folder):
    sig = os.path.join(folder, 'VS_input_output_signature.csv')
    outs = {}  # slot -> 'SEMANTICn'
    with open(sig, newline='') as f:
        for row in csv.DictReader(f):
            if row.get('Type', '').strip().lower() != 'output':
                continue
            slot = int(row['Slot'])
            sem = row['SemanticName'].strip()
            idx = int(row['Index'])
            outs[slot] = f'{sem}{idx}' if not sem.upper().startswith('SV_') else sem
    return outs


def _load_vertex_inputs_via_interp(folder):
    """Load per-vertex inputs the way render.py does — binary VBs (incl. the
    per-instance buffer) overlaid on the decoded CSV — so instanced cases get
    the real v1 transform. Returns (list of {vN:[x,y,z,w]}, slot_sem) or None."""
    try:
        from hlsl_interpreter import HLSLInterpreter
    except Exception:
        return None
    vs_hlsl = os.path.join(folder, 'VS_shader.hlsl')
    sig_csv = os.path.join(folder, 'VS_input_output_signature.csv')
    ia_layouts = os.path.join(folder, 'ia_input_layouts.csv')
    ia_vertex = os.path.join(folder, 'ia_vertex_data.csv')
    draw_info = os.path.join(folder, 'draw_call_info.csv')
    if not (os.path.exists(vs_hlsl) and os.path.exists(ia_layouts)):
        return None
    itp = HLSLInterpreter()
    code = open(vs_hlsl).read()
    params = itp.parse_main_params_with_semantics(itp.preprocess_hlsl(code))
    if not params:
        return None
    vin = params['inputs']
    sig = itp.load_signature_csv(sig_csv) if hasattr(itp, 'load_signature_csv') else None
    # map params -> signature slots (same call render.py uses)
    import csv as _csv
    sig_inputs = []
    with open(sig_csv, newline='') as f:
        for row in _csv.DictReader(f):
            if row.get('Type', '').strip().lower() == 'input':
                sig_inputs.append({'slot': int(row['Slot']),
                                   'semantic': row['SemanticName'].strip(),
                                   'index': int(row['Index'])})
    itp.map_params_to_signature(vin, sig_inputs)
    idx_list = itp.load_index_list_from_binary(ia_layouts, folder, draw_info)
    if not idx_list:
        idx_list = itp.load_index_column(ia_vertex)
    csv_vd = itp.load_ia_vertex_data(ia_vertex, vin)
    if csv_vd and len(csv_vd) == len(idx_list):
        vertex_data = csv_vd
    else:
        vertex_data = (csv_vd[:len(idx_list)] if csv_vd
                       else [{} for _ in range(len(idx_list))])
    binov = itp.load_per_vertex_binary_data(ia_layouts, folder, vin, idx_list,
                                            csv_vertex_data=None)
    if binov:
        for i, ov in enumerate(binov):
            if ov and i < len(vertex_data):
                vertex_data[i].update(ov)
    # vertex_data is keyed by param name (v0,v1,..); normalize to 4-vectors
    verts = []
    for vd in vertex_data:
        vin_row = {}
        for p in vin:
            val = vd.get(p['name'])
            if val is None:
                vec = [0.0, 0.0, 0.0, 0.0]
            elif isinstance(val, list):
                vec = (list(val) + [0.0, 0.0, 0.0, 0.0])[:4]
            else:
                vec = [float(val), 0.0, 0.0, 0.0]
            vin_row[p['name']] = vec
        verts.append(vin_row)
    slot_sem = {p['slot']: (p['semantic_base'].upper(), p['semantic_index'])
                for p in vin if p.get('slot', -1) >= 0}
    return verts, slot_sem


def _load_vertex_inputs(folder, slot_sem):
    """Map each vN -> [x,y,z,w] from ia_vertex_data.csv (RenderDoc-decoded)."""
    path = os.path.join(folder, 'ia_vertex_data.csv')
    rows = list(csv.reader(open(path, newline='')))
    header = [h.strip() for h in rows[0]]
    # build semantic -> {comp: col}
    col_of = {}
    for i, name in enumerate(header):
        base, _, comp = name.rpartition('.')
        if comp.lower() in ('x', 'y', 'z', 'w'):
            col_of.setdefault(base.upper(), {})[comp.lower()] = i
    verts = []
    for r in rows[1:]:
        vin = {}
        for slot, (sem, idx) in slot_sem.items():
            key = f'{sem}{idx}' if f'{sem}{idx}' in col_of else sem
            cols = col_of.get(key, col_of.get(sem, {}))
            vec = []
            for c in ('x', 'y', 'z', 'w'):
                if c in cols and cols[c] < len(r):
                    try:
                        vec.append(float(r[cols[c]]))
                    except ValueError:
                        vec.append(0.0)
                else:
                    vec.append(0.0)
            vin[f'v{slot}'] = vec
        verts.append(vin)
    return verts


def _build_resources(folder):
    """Wire texture/buffer memory ops by reusing render.py's loader. Returns a
    resources dict for DXBCInterpreter (sample_l / sample_c_lz / ld_raw /
    ld_structured), or {} if textures can't be loaded."""
    try:
        import render
        tex_exec, desc_list, samp_list = render._load_stage_textures(folder, 'VS')
    except Exception as e:
        print(f"[textures] load failed: {e}")
        return {}
    if tex_exec is None:
        return {}
    default_samp = next((s for s in samp_list if s is not None), None)
    if default_samp is None:
        from texture import Sampler
        default_samp = Sampler()
    nz = [i for i, d in enumerate(desc_list) if d is not None]
    print(f"[textures] VS desc slots loaded: {nz}")

    def _desc(slot):
        return desc_list[slot] if 0 <= slot < len(desc_list) else None

    def sample_l(slot, coords, lod):
        d = _desc(slot)
        if d is None:
            return [0.0, 0.0, 0.0, 0.0]
        u = coords[0] if coords else 0.0
        v = coords[1] if len(coords) > 1 else 0.0
        try:
            return tex_exec.sample(u, v, float(lod or 0.0), d, default_samp,
                                   None, None)
        except Exception:
            return [0.0, 0.0, 0.0, 0.0]

    def sample_c_lz(slot, coords, cmp):
        d = _desc(slot)
        if d is None:
            return 1.0
        u = coords[0] if coords else 0.0
        v = coords[1] if len(coords) > 1 else 0.0
        try:
            texel = tex_exec.sample(u, v, 0.0, d, default_samp, None, None)
            depth = texel[0] if isinstance(texel, list) else texel
            return 1.0 if cmp <= depth else 0.0
        except Exception:
            return 1.0

    raw_buffers = {}

    def _raw(slot):
        if slot in raw_buffers:
            return raw_buffers[slot]
        import glob
        cand = glob.glob(os.path.join(folder, f'VS_slot_{slot}_*_buffer.bin'))
        data = open(cand[0], 'rb').read() if cand else b''
        raw_buffers[slot] = data
        return data

    def ld_raw(slot, byte_addr, n):
        data = _raw(slot)
        out = []
        for i in range(n):
            off = byte_addr + i * 4
            out.append(struct.unpack_from('<f', data, off)[0]
                       if 0 <= off and off + 4 <= len(data) else 0.0)
        return out

    def ld_structured(slot, index, byte_off, n):
        return [0.0] * n

    return {'sample_l': sample_l, 'sample_c_lz': sample_c_lz,
            'ld_raw': ld_raw, 'ld_structured': ld_structured}


def _load_golden(folder):
    import glob
    cands = glob.glob(os.path.join(folder, '*_vs_mesh.csv'))
    if not cands:
        return None, None
    rows = list(csv.reader(open(cands[0], newline='')))
    header = [h.strip() for h in rows[0]]
    return header, [r for r in rows[1:]]


def _parse_disasm_outputs(disasm):
    """Return ordered [(reg_name, semantic, ncomp_declared)] from dcl_output*."""
    outs = []
    for line in disasm.splitlines():
        s = line.strip()
        m = re.match(r'dcl_output(_siv)?\s+(o\d+)\.([xyzw]+)(?:,\s*(\w+))?', s)
        if m:
            reg = m.group(2)
            swz = m.group(3)
            siv = m.group(4)  # e.g. 'position'
            sem = 'SV_Position' if (m.group(1) and siv == 'position') else None
            outs.append([reg, sem, len(swz)])
    return outs


def _golden_for_outputs(header, row, disasm, sig_outputs):
    """Map golden physical columns to o0..oN, SV_Position FIRST (matching the
    HLSL interpreter's loader), returning {reg: [vals]}."""
    comp_cols = [i for i, n in enumerate(header)
                 if n.rsplit('.', 1)[-1].lower() in ('x', 'y', 'z', 'w')]
    # dumped component count per header base group
    hdr_count = {}
    for n in header:
        base, _, comp = n.rpartition('.')
        if comp.lower() in ('x', 'y', 'z', 'w') and base:
            hdr_count[base.upper()] = hdr_count.get(base.upper(), 0) + 1
    decl = _parse_disasm_outputs(disasm)  # [(reg, sem_or_None, ncomp)]
    # attach semantic from signature (slot order == o-index order)
    reg_sem = {}
    for reg, sem, nc in decl:
        idx = int(reg[1:])
        if sem is None:
            sem = sig_outputs.get(idx)
        reg_sem[reg] = (sem, nc)

    def dumped(reg):
        sem, nc = reg_sem[reg]
        if sem:
            for cand in (sem.upper(),):
                if cand in hdr_count:
                    return hdr_count[cand]
        return nc

    ordered = ([r for r, _, _ in decl if (reg_sem[r][0] or '').upper().startswith('SV_POSITION')]
               + [r for r, _, _ in decl if not (reg_sem[r][0] or '').upper().startswith('SV_POSITION')])
    out = {}
    cur = 0
    for reg in ordered:
        n = dumped(reg)
        cols = comp_cols[cur:cur + n]
        cur += n
        vals = []
        for c in cols:
            try:
                vals.append(float(row[c]))
            except (ValueError, IndexError):
                vals.append(0.0)
        out[reg] = vals
    return out


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    src = sys.argv[1]
    vtx = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 0

    tmp = None
    if src.endswith('.zip'):
        tmp = tempfile.mkdtemp()
        with zipfile.ZipFile(src) as z:
            z.extractall(tmp)
        sub = [d for d in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, d))]
        folder = os.path.join(tmp, sub[0]) if sub else tmp
    else:
        folder = src

    disasm = open(os.path.join(folder, 'VS_shader_disasm.txt')).read()
    cb = _load_cbuffers(folder)
    via = None if '--csv-inputs' in sys.argv else _load_vertex_inputs_via_interp(folder)
    if via:
        verts, slot_sem = via
        print("[inputs] loaded via interpreter loaders (binary VBs + instance)")
    else:
        slot_sem = _load_signature_inputs(folder)
        verts = _load_vertex_inputs(folder, slot_sem)
        print("[inputs] loaded from ia_vertex_data.csv")
    print(f"cbuffers: {{slot: nrows}} = "
          f"{ {k: len(v) for k, v in sorted(cb.items())} }")
    print(f"inputs slot->sem: {slot_sem}")
    print(f"vertices: {len(verts)}")

    resources = _build_resources(folder) if '--no-tex' not in sys.argv else {}
    vm = DXBCInterpreter(disasm, cb, resources=resources, log_trace=True)
    print(f"parsed {len(vm.instrs)} instructions, dcl_temps {vm.ntemps}")

    out = vm.run(verts[vtx])
    print(f"\n=== DXBC VM outputs for vertex {vtx} ===")
    for k in sorted(out):
        print(f"  {k} = {[round(x, 5) for x in out[k]]}")

    hdr, golden = _load_golden(folder)
    if golden and vtx < len(golden):
        sig_out = _load_signature_outputs(folder)
        gmap = _golden_for_outputs(hdr, golden[vtx], disasm, sig_out)
        print(f"\n=== DXBC vs golden (vertex {vtx}; SV_Position-first mapping) ===")
        tol = 0.02
        for reg in sorted(gmap):
            gv = gmap[reg]
            ov = out.get(reg, [])
            n = min(len(gv), len(ov))
            ok = all(abs(ov[i] - gv[i]) <= tol or
                     (ov[i] != ov[i])  # nan never matches
                     and False for i in range(n))
            ok = all(abs(ov[i] - gv[i]) <= tol for i in range(n)) if n else False
            flag = 'OK ' if ok else 'DIFF'
            print(f"  [{flag}] {reg}: dxbc={[round(x,4) for x in ov[:n]]} "
                  f"golden={[round(x,4) for x in gv[:n]]}")


if __name__ == '__main__':
    main()
