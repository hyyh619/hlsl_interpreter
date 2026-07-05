"""
dxbc_interp.py — a small DXBC (vs_5_0) disassembly interpreter.

Purpose: a *register-level golden* reference for the HLSL-source interpreter.
3Dmigoto's decompiled `VS_shader.hlsl` is lossy (it drops matrix major-order /
struct selectors and occasionally reorders), whereas `VS_shader_disasm.txt` is
the exact instruction stream the GPU ran. Executing the disasm faithfully gives
an independent reference: if this VM's final outputs match the golden
`*_vs_mesh.csv` but the HLSL interpreter's don't, the HLSL decompile is the
problem; diffing the two register traces localizes the exact divergence.

Scope: the witcher3 VS opcode set (ALU + int/bit + control flow + sample_l /
sample_c_lz / ld_structured / ld_raw). Registers are typeless 32-bit; we store
each component as a Python float and reinterpret bits for integer/bitwise ops.

This is a diagnostic tool, not part of the render pipeline.
"""

import struct
import math
import re

__all__ = ["DXBCInterpreter", "f2u", "f2i", "u2f", "i2f"]


# ---- bit reinterpretation between the float / int views of a 32-bit lane ----
def f2u(f):
    return struct.unpack('<I', struct.pack('<f', _as_f32(f)))[0]


def f2i(f):
    return struct.unpack('<i', struct.pack('<f', _as_f32(f)))[0]


def u2f(u):
    return struct.unpack('<f', struct.pack('<I', u & 0xFFFFFFFF))[0]


def i2f(i):
    return struct.unpack('<f', struct.pack('<I', int(i) & 0xFFFFFFFF))[0]


def _as_f32(f):
    try:
        return struct.unpack('<f', struct.pack('<f', f))[0]
    except (OverflowError, struct.error):
        return float('inf') if f > 0 else float('-inf')


_TRUE = u2f(0xFFFFFFFF)   # DXBC comparison "true" = all-ones bit pattern
_FALSE = 0.0

_SWZ = {'x': 0, 'y': 1, 'z': 2, 'w': 3}


class DXBCInterpreter:
    def __init__(self, disasm_text, cbuffers, resources=None, log_trace=False):
        """
        cbuffers: {slot:int -> list of [x,y,z,w] float rows}
        resources: optional dict with callables for memory ops:
          'sample_l'(slot, coords, lod) -> [r,g,b,a]
          'sample_c_lz'(slot, coords, cmp) -> float
          'ld_structured'(slot, index, byte_off, n) -> [..]
          'ld_raw'(slot, byte_addr, n) -> [..]
        """
        self.cb = cbuffers
        self.res = resources or {}
        self.icb = []             # dcl_immediateConstantBuffer rows
        self.ntemps = 0
        self.instrs = []          # list of (line_no, opcode, sat, [operand strs])
        self.log_trace = log_trace
        self.trace = []           # list of (line_no, opcode, dest_name, [vals])
        self._parse(disasm_text)
        self._build_cf()

    # ------------------------------------------------------------------ parse
    def _parse(self, text):
        # immediate constant buffer: dcl_immediateConstantBuffer { {a,b,c,d}, ... }
        mpos = text.find('dcl_immediateConstantBuffer')
        if mpos >= 0:
            depth = 0
            start = end = -1
            for i in range(mpos, len(text)):
                if text[i] == '{':
                    if depth == 0:
                        start = i
                    depth += 1
                elif text[i] == '}':
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            if start >= 0 and end > start:
                for row in re.findall(r'\{([^{}]*)\}', text[start + 1:end]):
                    vals = []
                    for tok in row.split(','):
                        tok = tok.strip()
                        try:
                            vals.append(float(tok))
                        except ValueError:
                            vals.append(0.0)
                    self.icb.append((vals + [0.0] * 4)[:4])
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            mt = re.match(r'dcl_temps\s+(\d+)', line)
            if mt:
                self.ntemps = int(mt.group(1))
                continue
            m = re.match(r'(\d+):\s+(.*)$', line)
            if not m:
                continue
            lineno = int(m.group(1))
            body = m.group(2).strip()
            # opcode token: letters/underscores/digits, may carry (...) resource
            # decoration for sample/ld — strip the parenthetical type tags.
            mo = re.match(r'([a-z_0-9]+)(\([^)]*\))*\s*(.*)$', body)
            opcode = mo.group(1)
            rest = mo.group(3)
            sat = False
            if opcode.endswith('_sat'):
                sat = True
                opcode = opcode[:-4]
            operands = self._split_operands(rest)
            self.instrs.append([lineno, opcode, sat, operands])

    @staticmethod
    def _split_operands(s):
        """Split an operand list on top-level commas (ignoring those inside
        l(...) literals, abs()/parens, and [..] index brackets)."""
        out, depth, cur = [], 0, ''
        for ch in s:
            if ch in '([':
                depth += 1
                cur += ch
            elif ch in ')]':
                depth -= 1
                cur += ch
            elif ch == ',' and depth == 0:
                out.append(cur.strip())
                cur = ''
            else:
                cur += ch
        if cur.strip():
            out.append(cur.strip())
        return out

    def _build_cf(self):
        """Match if/else/endif and loop/endloop so execution can jump."""
        self.jump = {}     # instr index -> matching index
        stack = []
        for i, (ln, op, sat, ops) in enumerate(self.instrs):
            if op in ('if_nz', 'if_z'):
                stack.append(('if', i, None))
            elif op == 'else':
                kind, ifidx, _ = stack.pop()
                self.jump[ifidx] = i      # if -> else
                stack.append(('else', i, ifidx))
            elif op == 'endif':
                kind, idx, prev = stack.pop()
                self.jump[idx] = i        # if/else -> endif
                if kind == 'else':
                    pass
            elif op == 'loop':
                stack.append(('loop', i, None))
            elif op == 'endloop':
                kind, idx, _ = stack.pop()
                self.jump[idx] = i        # loop -> endloop
                self.jump[i] = idx        # endloop -> loop (back-edge)

    # --------------------------------------------------------------- operands
    def _reg_vec4(self, name, regs, inp, out):
        """Return the [x,y,z,w] float vector for a register/input/output/cb base
        (no swizzle applied). `name` may be e.g. r5, v2, o6, cb13[35],
        cb13[r5.w + 39]."""
        m = re.match(r'(cb\d+)\[(.+)\]$', name)
        if m:
            cbname = m.group(1)
            slot = int(cbname[2:])
            idx = self._eval_index(m.group(2), regs, inp, out)
            rows = self.cb.get(slot, [])
            if 0 <= idx < len(rows):
                return list(rows[idx])
            return [0.0, 0.0, 0.0, 0.0]
        m = re.match(r'icb\[(.+)\]$', name)
        if m:
            idx = self._eval_index(m.group(1), regs, inp, out)
            if 0 <= idx < len(self.icb):
                return list(self.icb[idx])
            return [0.0, 0.0, 0.0, 0.0]
        if name.startswith('r'):
            return regs[int(name[1:])]
        if name.startswith('v'):
            return inp.get(name, [0.0, 0.0, 0.0, 0.0])
        if name.startswith('o'):
            return out.get(name, [0.0, 0.0, 0.0, 0.0])
        # bare immediate index target like 'l(..)' shouldn't reach here
        return [0.0, 0.0, 0.0, 0.0]

    def _eval_index(self, expr, regs, inp, out):
        # A relative cbuffer index register holds an INTEGER bit pattern (DXBC
        # requires an int operand for indexing), so always reinterpret via f2i.
        expr = expr.strip()
        # forms: 35   |   r5.w + 39   |   r0.x
        m = re.match(r'([a-z]\w*\.[xyzw])\s*\+\s*(\d+)$', expr)
        if m:
            return f2i(self._scalar(m.group(1), regs, inp, out)) + int(m.group(2))
        m = re.match(r'([a-z]\w*\.[xyzw])$', expr)
        if m:
            return f2i(self._scalar(m.group(1), regs, inp, out))
        try:
            return int(expr)
        except ValueError:
            return 0

    def _scalar(self, token, regs, inp, out):
        """Evaluate a single 'reg.comp' scalar (for index expressions)."""
        name, _, comp = token.partition('.')
        vec = self._reg_vec4(name, regs, inp, out)
        return vec[_SWZ.get(comp[0], 0)]

    def read_src(self, op, dest_lanes, regs, inp, out):
        """Return {lane: value} for each lane in dest_lanes, applying swizzle and
        the negate/abs source modifiers."""
        neg = False
        absv = False
        s = op.strip()
        if s.startswith('-'):
            neg = True
            s = s[1:].strip()
        ma = re.match(r'abs\((.*)\)$', s)
        if ma:
            absv = True
            s = ma.group(1).strip()
            if s.startswith('-'):
                neg = True
                s = s[1:].strip()
        # literal
        if s.startswith('l('):
            lit = self._parse_literal(s)
            base = lit
            swz = None
        else:
            # split trailing swizzle from base (handle cb13[..].xy and r5.w)
            base_name, swz = self._split_swizzle(s)
            base = self._reg_vec4(base_name, regs, inp, out)
        res = {}
        for p in dest_lanes:
            if swz is None:
                v = base[p] if p < len(base) else 0.0
            else:
                comp = self._swz_for_lane(swz, dest_lanes, p)
                v = base[_SWZ[comp]]
            if absv:
                v = abs(v)
            if neg:
                v = -v
            res[p] = v
        return res

    def read_src_n(self, op, n, regs, inp, out):
        """Read the first n swizzle components (for dp2/dp3/dp4)."""
        neg = False
        absv = False
        s = op.strip()
        if s.startswith('-'):
            neg = True
            s = s[1:].strip()
        ma = re.match(r'abs\((.*)\)$', s)
        if ma:
            absv = True
            s = ma.group(1).strip()
        if s.startswith('l('):
            lit = self._parse_literal(s)
            vals = lit[:n]
        else:
            base_name, swz = self._split_swizzle(s)
            base = self._reg_vec4(base_name, regs, inp, out)
            if swz is None:
                swz = 'xyzw'
            chars = (swz + swz[-1] * 4)[:n]
            vals = [base[_SWZ[c]] for c in chars]
        if absv:
            vals = [abs(v) for v in vals]
        if neg:
            vals = [-v for v in vals]
        return vals

    @staticmethod
    def _split_swizzle(s):
        """'r5.xyz' -> ('r5','xyz'); 'cb13[35].x' -> ('cb13[35]','x');
        'cb13[r5.w + 39].zw' -> ('cb13[r5.w + 39]','zw'); 'r5' -> ('r5',None)."""
        # find the swizzle: a trailing .<xyzw...> not inside brackets
        depth = 0
        for i in range(len(s)):
            if s[i] in '[':
                depth += 1
            elif s[i] in ']':
                depth -= 1
        # locate last '.' at depth 0
        depth = 0
        dotpos = -1
        for i, ch in enumerate(s):
            if ch == '[':
                depth += 1
            elif ch == ']':
                depth -= 1
            elif ch == '.' and depth == 0:
                dotpos = i
        if dotpos < 0:
            return s, None
        swz = s[dotpos + 1:]
        if swz and all(c in 'xyzw' for c in swz):
            return s[:dotpos], swz
        return s, None

    @staticmethod
    def _swz_for_lane(swz, dest_lanes, p):
        if len(swz) == 4:
            return swz[p]
        if len(swz) == 1:
            return swz[0]
        if len(swz) == len(dest_lanes):
            return swz[dest_lanes.index(p)]
        # pad with last char and index by absolute lane
        padded = (swz + swz[-1] * 4)[:4]
        return padded[p]

    @staticmethod
    def _parse_literal(s):
        # DXBC is typeless; a literal carries its type by FORMAT. Float literals
        # print with a decimal point (l(1.000000)); integer literals print bare
        # (l(1), l(-1)). They are DIFFERENT 32-bit values: l(1)=0x00000001 vs
        # l(1.0)=0x3F800000. Store an int-format literal as the float whose BITS
        # equal that int (i2f) so integer ops recover it via f2i, while
        # float-format literals are stored as their float value.
        inner = s[s.index('(') + 1:s.rindex(')')]
        parts = [x.strip() for x in inner.split(',')]
        vals = []
        for x in parts:
            try:
                if '.' not in x and 'e' not in x and 'E' not in x:
                    vals.append(i2f(int(x)))      # integer-format literal
                else:
                    vals.append(float(x))         # float-format literal
            except ValueError:
                vals.append(0.0)
        if len(vals) == 1:
            vals = vals * 4
        while len(vals) < 4:
            vals.append(0.0)
        return vals

    @staticmethod
    def _dest_lanes(dest):
        name, swz = DXBCInterpreter._split_swizzle(dest)
        if swz is None:
            return name, [0, 1, 2, 3]
        return name, [_SWZ[c] for c in swz]

    def write_dst(self, dest, lane_vals, regs, out, sat):
        name, lanes = self._dest_lanes(dest)
        if sat:
            lane_vals = {p: min(1.0, max(0.0, v)) if isinstance(v, float)
                         and not math.isnan(v) else v for p, v in lane_vals.items()}
        if name.startswith('r'):
            target = regs[int(name[1:])]
        elif name.startswith('o['):
            # dynamic output index (HS fork: mov o[r0.x + 0].x, ...)
            idx = self._eval_index(name[2:-1], regs, {}, out)
            target = out.setdefault(f'o{idx}', [0.0, 0.0, 0.0, 0.0])
        elif name.startswith('o'):
            target = out.setdefault(name, [0.0, 0.0, 0.0, 0.0])
        else:
            return
        for p, v in lane_vals.items():
            target[p] = _as_f32(v) if isinstance(v, float) else v

    # ----------------------------------------------------------------- execute
    def run(self, inputs):
        regs = [[0.0, 0.0, 0.0, 0.0] for _ in range(max(self.ntemps, 16))]
        inp = {k: (list(v) + [0.0, 0.0, 0.0, 0.0])[:4] for k, v in inputs.items()}
        out = {}
        self.trace = []
        pc = 0
        loop_guard = 0
        while pc < len(self.instrs):
            loop_guard += 1
            if loop_guard > 2_000_000:
                raise RuntimeError("instruction budget exceeded (infinite loop?)")
            lineno, op, sat, ops = self.instrs[pc]
            pc = self._exec(pc, lineno, op, sat, ops, regs, inp, out)
        return out

    def _exec(self, pc, lineno, op, sat, ops, regs, inp, out):
        nxt = pc + 1

        def src(i, lanes):
            return self.read_src(ops[i], lanes, regs, inp, out)

        def emit(dest, vals):
            self.write_dst(dest, vals, regs, out, sat)
            if self.log_trace:
                name, lanes = self._dest_lanes(dest)
                self.trace.append((lineno, op, dest,
                                   [round(vals[p], 6) for p in sorted(vals)]))

        # ----- control flow -----
        if op in ('if_nz', 'if_z'):
            cond = self.read_src(ops[0], [0], regs, inp, out)[0]
            truth = (f2u(cond) != 0)
            taken = truth if op == 'if_nz' else (not truth)
            if not taken:
                tgt = self.jump.get(pc, pc)
                # jump to else (execute else body) or endif
                _, top, _, _ = self.instrs[tgt]
                return tgt + 1 if self.instrs[tgt][1] == 'else' else tgt + 1 \
                    if self.instrs[tgt][1] == 'endif' else tgt
            return nxt
        if op == 'else':
            # reached by falling through the taken if-body: jump to endif
            return self.jump.get(pc, pc) + 1
        if op == 'endif':
            return nxt
        if op == 'loop':
            return nxt
        if op == 'endloop':
            return self.jump.get(pc, pc)   # back-edge to loop
        if op in ('break',):
            return self._break_to_endloop(pc)
        if op in ('breakc_nz', 'breakc_z'):
            cond = self.read_src(ops[0], [0], regs, inp, out)[0]
            truth = (f2u(cond) != 0)
            do = truth if op == 'breakc_nz' else (not truth)
            return self._break_to_endloop(pc) if do else nxt
        if op == 'ret':
            return len(self.instrs)

        dest = ops[0] if ops else None
        name, lanes = self._dest_lanes(dest) if dest else (None, [])

        # ----- moves / select -----
        if op == 'mov':
            s0 = src(1, lanes)
            emit(dest, s0)
            return nxt
        if op == 'movc':
            c = src(1, lanes); a = src(2, lanes); b = src(3, lanes)
            emit(dest, {p: (a[p] if f2u(c[p]) != 0 else b[p]) for p in lanes})
            return nxt

        # ----- dot products (scalar result, replicated to dest lanes) -----
        if op in ('dp2', 'dp3', 'dp4'):
            n = {'dp2': 2, 'dp3': 3, 'dp4': 4}[op]
            a = self.read_src_n(ops[1], n, regs, inp, out)
            b = self.read_src_n(ops[2], n, regs, inp, out)
            # GPU dpN = mul + mad chain with one float32 rounding per step
            # (a double sum rounded once drifts ULPs on position chains).
            d = _as_f32(a[0] * b[0])
            for i in range(1, n):
                d = _as_f32(a[i] * b[i] + d)
            emit(dest, {p: d for p in lanes})
            return nxt

        # ----- float ALU (component-wise) -----
        farith = {
            'add': lambda a, b: a + b,
            'mul': lambda a, b: a * b,
            'max': lambda a, b: max(a, b),
            'min': lambda a, b: min(a, b),
            'div': lambda a, b: (a / b) if b != 0 else (math.inf if a > 0 else
                                                        (-math.inf if a < 0 else math.nan)),
        }
        if op in farith:
            a = src(1, lanes); b = src(2, lanes)
            emit(dest, {p: farith[op](a[p], b[p]) for p in lanes})
            return nxt
        if op == 'mad':
            a = src(1, lanes); b = src(2, lanes); c = src(3, lanes)
            emit(dest, {p: a[p] * b[p] + c[p] for p in lanes})
            return nxt
        funary = {
            'sqrt': lambda v: math.sqrt(v) if v >= 0 else math.nan,
            'rsq': lambda v: (1.0 / math.sqrt(v)) if v > 0 else math.inf,
            'rcp': lambda v: (1.0 / v) if v != 0 else math.inf,
            'exp': lambda v: 2.0 ** v,
            'log': lambda v: math.log2(v) if v > 0 else -math.inf,
            'frc': lambda v: v - math.floor(v),
            'round_z': lambda v: math.trunc(v),
            'round_ni': lambda v: math.floor(v),
            'sqrt_': None,
        }
        if op in funary and funary[op] is not None:
            a = src(1, lanes)
            emit(dest, {p: funary[op](a[p]) for p in lanes})
            return nxt
        if op == 'sincos':
            # sincos dest_sin, dest_cos, src  (either dest may be 'null')
            sdst, cdst, srcop = ops[0], ops[1], ops[2]
            for d in (sdst, cdst):
                if d == 'null':
                    continue
            ln = self._dest_lanes(sdst)[1] if sdst != 'null' else \
                self._dest_lanes(cdst)[1]
            a = self.read_src(srcop, ln, regs, inp, out)
            if sdst != 'null':
                emit(sdst, {p: math.sin(a[p]) for p in ln})
            if cdst != 'null':
                self.write_dst(cdst, {p: math.cos(a[p]) for p in ln},
                               regs, out, sat)
            return nxt

        # ----- conversions -----
        if op == 'itof':
            a = src(1, lanes)
            emit(dest, {p: float(f2i(a[p])) for p in lanes})
            return nxt
        if op == 'utof':
            a = src(1, lanes)
            emit(dest, {p: float(f2u(a[p])) for p in lanes})
            return nxt
        if op == 'ftoi':
            a = src(1, lanes)
            emit(dest, {p: i2f(int(_trunc_to_int(a[p]))) for p in lanes})
            return nxt
        if op == 'ftou':
            a = src(1, lanes)
            emit(dest, {p: u2f(int(max(0, _trunc_to_int(a[p])))) for p in lanes})
            return nxt
        if op == 'f16tof32':
            a = src(1, lanes)
            emit(dest, {p: _half_bits_to_float(f2u(a[p]) & 0xFFFF) for p in lanes})
            return nxt

        # ----- float comparisons (write 0xFFFFFFFF / 0) -----
        fcmp = {
            'lt': lambda a, b: a < b,
            'ge': lambda a, b: a >= b,
            'eq': lambda a, b: a == b,
            'ne': lambda a, b: a != b,
        }
        if op in fcmp:
            a = src(1, lanes); b = src(2, lanes)
            emit(dest, {p: (_TRUE if fcmp[op](a[p], b[p]) else _FALSE) for p in lanes})
            return nxt

        # ----- integer ALU / comparisons / bitwise -----
        icmp = {
            'ilt': lambda a, b: a < b, 'ige': lambda a, b: a >= b,
            'ieq': lambda a, b: a == b,
        }
        if op in icmp:
            a = src(1, lanes); b = src(2, lanes)
            emit(dest, {p: (_TRUE if icmp[op](f2i(a[p]), f2i(b[p])) else _FALSE)
                        for p in lanes})
            return nxt
        ucmp = {'uge': lambda a, b: a >= b, 'ult': lambda a, b: a < b}
        if op in ucmp:
            a = src(1, lanes); b = src(2, lanes)
            emit(dest, {p: (_TRUE if ucmp[op](f2u(a[p]), f2u(b[p])) else _FALSE)
                        for p in lanes})
            return nxt
        iarith = {
            'iadd': lambda a, b: a + b,
            'imul': lambda a, b: a * b,        # low 32 bits (imul lo)
            'imax': lambda a, b: max(a, b),
            'imin': lambda a, b: min(a, b),
            'ishl': lambda a, b: a << (b & 31),
            'ishr': lambda a, b: a >> (b & 31),
            'and': lambda a, b: a & b,
            'or': lambda a, b: a | b,
            'xor': lambda a, b: a ^ b,
        }
        if op in iarith:
            a = src(1, lanes); b = src(2, lanes)
            emit(dest, {p: i2f(_wrap32(iarith[op](f2i(a[p]), f2i(b[p]))))
                        for p in lanes})
            return nxt
        if op == 'ushr':
            a = src(1, lanes); b = src(2, lanes)
            emit(dest, {p: u2f((f2u(a[p]) >> (f2u(b[p]) & 31)) & 0xFFFFFFFF)
                        for p in lanes})
            return nxt
        if op in ('umin', 'umax'):
            a = src(1, lanes); b = src(2, lanes)
            f = min if op == 'umin' else max
            emit(dest, {p: u2f(f(f2u(a[p]), f2u(b[p]))) for p in lanes})
            return nxt
        if op == 'ineg':
            a = src(1, lanes)
            emit(dest, {p: i2f(_wrap32(-f2i(a[p]))) for p in lanes})
            return nxt
        if op == 'imad':
            a = src(1, lanes); b = src(2, lanes); c = src(3, lanes)
            emit(dest, {p: i2f(_wrap32(f2i(a[p]) * f2i(b[p]) + f2i(c[p])))
                        for p in lanes})
            return nxt
        if op == 'ubfe':
            # ubfe dst, width, offset, src
            w = src(1, lanes); o = src(2, lanes); a = src(3, lanes)
            emit(dest, {p: u2f(_ubfe(f2u(w[p]) & 31, f2u(o[p]) & 31, f2u(a[p])))
                        for p in lanes})
            return nxt
        if op == 'bfi':
            # bfi dst, width, offset, insert, base
            w = src(1, lanes); o = src(2, lanes); ins = src(3, lanes); base = src(4, lanes)
            emit(dest, {p: u2f(_bfi(f2u(w[p]) & 31, f2u(o[p]) & 31,
                                    f2u(ins[p]), f2u(base[p]))) for p in lanes})
            return nxt
        if op == 'bfrev':
            a = src(1, lanes)
            emit(dest, {p: u2f(_bitrev32(f2u(a[p]))) for p in lanes})
            return nxt
        if op == 'firstbit_lo':
            a = src(1, lanes)
            emit(dest, {p: u2f(_firstbit_lo(f2u(a[p]))) for p in lanes})
            return nxt

        # ----- memory -----
        if op in ('sample_l', 'sample_c_lz', 'ld_structured', 'ld_raw',
                  'ld', 'ld_indexable', 'ld_structured_indexable'):
            return self._exec_mem(pc, lineno, op, sat, ops, regs, inp, out, emit)
        if op in ('resinfo', 'resinfo_indexable'):
            # resinfo dst, mipLevel, tN.swz -> (w, h, depth/elems, mips)
            mip = f2i(self.read_src(ops[1], [0], regs, inp, out)[0])
            tslot = _res_slot(ops[2])
            fn = self.res.get('resinfo')
            dims = fn(tslot, mip) if fn else [0.0, 0.0, 0.0, 0.0]
            dims = _swizzle_result(dims, ops[2])
            emit(dest, {p: dims[i] for i, p in enumerate(lanes)})
            return nxt

        raise NotImplementedError(f"opcode '{op}' (line {lineno}) not implemented")

    # --------------------------------------------------------------- memory
    def _exec_mem(self, pc, lineno, op, sat, ops, regs, inp, out, emit):
        nxt = pc + 1
        dest = ops[0]
        name, lanes = self._dest_lanes(dest)
        if op == 'sample_l':
            # sample_l dst, coords, tN.swizzle, sS, lod
            coords = self.read_src_n(ops[1], 3, regs, inp, out)
            tslot = _res_slot(ops[2])
            lod = self.read_src(ops[4], [0], regs, inp, out)[0] if len(ops) > 4 else 0.0
            fn = self.res.get('sample_l')
            rgba = fn(tslot, coords, lod) if fn else [0.0, 0.0, 0.0, 0.0]
            rgba = _swizzle_result(rgba, ops[2])
            emit(dest, {p: rgba[i] for i, p in enumerate(lanes)})
            return nxt
        if op == 'sample_c_lz':
            coords = self.read_src_n(ops[1], 3, regs, inp, out)
            tslot = _res_slot(ops[2])
            cmpv = self.read_src(ops[4], [0], regs, inp, out)[0] if len(ops) > 4 else 0.0
            fn = self.res.get('sample_c_lz')
            v = fn(tslot, coords, cmpv) if fn else 1.0
            emit(dest, {p: v for p in lanes})
            return nxt
        if op in ('ld_structured', 'ld_structured_indexable'):
            idx = f2i(self.read_src(ops[1], [0], regs, inp, out)[0])
            boff = f2i(self.read_src(ops[2], [0], regs, inp, out)[0])
            tslot = _res_slot(ops[3])
            fn = self.res.get('ld_structured')
            vals = fn(tslot, idx, boff, len(lanes)) if fn else [0.0] * len(lanes)
            emit(dest, {p: vals[i] for i, p in enumerate(lanes)})
            return nxt
        if op in ('ld_raw',):
            addr = f2i(self.read_src(ops[1], [0], regs, inp, out)[0])
            tslot = _res_slot(ops[2])
            fn = self.res.get('ld_raw')
            vals = fn(tslot, addr, len(lanes)) if fn else [0.0] * len(lanes)
            emit(dest, {p: vals[i] for i, p in enumerate(lanes)})
            return nxt
        if op in ('ld', 'ld_indexable'):
            # ld dst, coords(int4: x,y[,slice],mip), tN.swz
            coords = self.read_src_n(ops[1], 4, regs, inp, out)
            tslot = _res_slot(ops[2])
            fn = self.res.get('ld')
            if fn is not None:
                rgba = fn(tslot, [f2i(c) for c in coords])
                rgba = _swizzle_result(rgba, ops[2])
                emit(dest, {p: rgba[i] for i, p in enumerate(lanes)})
                return nxt
        # unknown texture op: approximate as 0
        emit(dest, {p: 0.0 for p in lanes})
        return nxt

    def _break_to_endloop(self, pc):
        depth = 0
        i = pc
        while i < len(self.instrs):
            op = self.instrs[i][1]
            if op == 'loop':
                depth += 1
            elif op == 'endloop':
                depth -= 1
                if depth < 0:
                    return i + 1
            i += 1
        return len(self.instrs)


# ----------------------------------------------------------------- helpers
def _looks_int(f):
    # heuristic: index registers hold itof'd ints or raw small ints; treat the
    # bit pattern as int when the float value is non-integral or huge.
    return False


def _trunc_to_int(f):
    if math.isnan(f):
        return 0
    if math.isinf(f):
        return 0x7FFFFFFF if f > 0 else -0x80000000
    return int(math.trunc(f))


def _wrap32(i):
    i &= 0xFFFFFFFF
    return i - 0x100000000 if i >= 0x80000000 else i


def _ubfe(width, offset, val):
    if width == 0:
        return 0
    mask = (1 << width) - 1
    return (val >> offset) & mask


def _bfi(width, offset, ins, base):
    mask = ((1 << width) - 1) << offset
    return ((ins << offset) & mask) | (base & (~mask & 0xFFFFFFFF))


def _bitrev32(u):
    r = 0
    for _ in range(32):
        r = (r << 1) | (u & 1)
        u >>= 1
    return r & 0xFFFFFFFF


def _firstbit_lo(u):
    if u == 0:
        return 0xFFFFFFFF
    n = 0
    while not (u & 1):
        u >>= 1
        n += 1
    return n


def _half_bits_to_float(h):
    return struct.unpack('<e', struct.pack('<H', h & 0xFFFF))[0]


def _res_slot(token):
    m = re.match(r't(\d+)', token)
    return int(m.group(1)) if m else 0


def _swizzle_result(rgba, token):
    _, swz = DXBCInterpreter._split_swizzle(token)
    rgba = (list(rgba) + [0.0, 0.0, 0.0, 0.0])[:4]
    if not swz:
        return rgba
    return [rgba[_SWZ[c]] for c in swz]
