import csv
import math
import re
import os
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Union, Optional

from hlsl_syntax_tree import SyntaxTreeNode, SyntaxTreeParser, _COMPILED_PATTERNS

# Sentinel: "this node is not a fusable multiply-add" (distinct from a real
# None/0 result), returned by _try_fma/_fma so the caller falls back cleanly.
_NO_FMA = object()
_MISS = object()


def _f32s(x):
    return struct.unpack('<f', struct.pack('<f', x))[0]


_TWO_PI = 6.283185307179586
_INV_2PI_F32 = 0.15915493667125702        # float32(1/2π) as a double
# float32 split of 2π: hi = f32(2π), lo = f32(2π − hi)
_PI2_HI_F32 = 6.2831854820251465
_PI2_LO_F32 = 4.866440936774252e-08


def _sin_libm(x):
    return math.sin(x)


def _cos_libm(x):
    return math.cos(x)


def _sin_f32red(x):
    """Naive f32 revolution reduction (step 174 — historically net negative)."""
    if not math.isfinite(x) or abs(x) < _TWO_PI:
        return math.sin(x) if math.isfinite(x) else math.nan
    r = _f32s(x * _INV_2PI_F32)
    r = _f32s(r - math.floor(r))
    return math.sin(_TWO_PI * r)


def _cos_f32red(x):
    if not math.isfinite(x) or abs(x) < _TWO_PI:
        return math.cos(x) if math.isfinite(x) else math.nan
    r = _f32s(x * _INV_2PI_F32)
    r = _f32s(r - math.floor(r))
    return math.cos(_TWO_PI * r)


def _cw2_reduce(x):
    """Cody-Waite 2-term reduction with float32 arithmetic per step:
    k = round(x/2π); r = (x − k·hi) − k·lo (each op f32-rounded)."""
    k = _f32s(round(_f32s(x * _INV_2PI_F32)))
    r = _f32s(x - _f32s(k * _PI2_HI_F32))
    return _f32s(r - _f32s(k * _PI2_LO_F32))


def _sin_cw2(x):
    if not math.isfinite(x) or abs(x) < _TWO_PI:
        return math.sin(x) if math.isfinite(x) else math.nan
    return math.sin(_cw2_reduce(x))


def _cos_cw2(x):
    if not math.isfinite(x) or abs(x) < _TWO_PI:
        return math.cos(x) if math.isfinite(x) else math.nan
    return math.cos(_cw2_reduce(x))


def _cw2fma_reduce(x):
    """Cody-Waite 2-term with FMA-style single rounding per mad
    (product kept exact in double, one rounding at the subtract)."""
    k = _f32s(round(_f32s(x * _INV_2PI_F32)))
    r = _f32s(x - k * _PI2_HI_F32)
    return _f32s(r - k * _PI2_LO_F32)


def _sin_cw2fma(x):
    if not math.isfinite(x) or abs(x) < _TWO_PI:
        return math.sin(x) if math.isfinite(x) else math.nan
    return math.sin(_cw2fma_reduce(x))


def _cos_cw2fma(x):
    if not math.isfinite(x) or abs(x) < _TWO_PI:
        return math.cos(x) if math.isfinite(x) else math.nan
    return math.cos(_cw2fma_reduce(x))


_TRIG_MODELS = {
    'libm': (_sin_libm, _cos_libm),
    'f32red': (_sin_f32red, _cos_f32red),
    'cw2': (_sin_cw2, _cos_cw2),
    'cw2fma': (_sin_cw2fma, _cos_cw2fma),
}


class _RawBits(int):
    """An int that is known to be a REGISTER BIT PATTERN (bfrev result).
    Marks integer-op partners so `(int)float` operands are bit-reinterpreted
    (DXBC iadd/imul consume raw bits) instead of value-converted."""
    __slots__ = ()

try:
    from texture import Texture, Sampler, TextureDesc, Sampler as SamplerClass
    TEXTURE_AVAILABLE = True
except ImportError:
    TEXTURE_AVAILABLE = False


try:
    from mesh_view import MeshView, VertexData
    MESHVIEW_AVAILABLE = True
except ImportError:
    MESHVIEW_AVAILABLE = False

try:
    from html_mesh_view import HtmlMeshView
    HTML_MESHVIEW_AVAILABLE = True
except ImportError:
    HTML_MESHVIEW_AVAILABLE = False

try:
    from web_mesh_view import WebMeshView
    WEB_MESHVIEW_AVAILABLE = True
except ImportError:
    WEB_MESHVIEW_AVAILABLE = False


DATA_TYPE_LIST = [
    'float4x4', 'float3x3',  # 矩阵类型
    'float4', 'float3', 'float2', 'float',  # 浮点向量/标量
    'uint4', 'uint3', 'uint2', 'uint',  # 无符号整数
    'int4', 'int3', 'int2', 'int',  # 有符号整数
    'bool'  # 布尔类型
]

from d3d import (
    D3D_PRIMITIVE_TOPOLOGY_UNDEFINED,
    D3D_PRIMITIVE_TOPOLOGY_POINTLIST,
    D3D_PRIMITIVE_TOPOLOGY_LINELIST,
    D3D_PRIMITIVE_TOPOLOGY_LINESTRIP,
    D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST,
    D3D_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP,
    D3D_PRIMITIVE_TOPOLOGY_TRIANGLEFAN
)
from debug_trace import TRACE


@dataclass
class ShaderVariable:
    """着色器变量定义"""
    name: str       # 变量名
    type: str       # 变量类型
    value: Any      # 变量值


@dataclass
class FieldDefinition:
    """结构体或cbuffer的字段定义"""
    field_type: str      # 字段类型，如 float3, float4x4
    name: str           # 字段名
    semantic: str       # 语义名称，如 POSITION, NORMAL
    data: List[Any] = None  # 字段数据值
    array_size: int = 0  # 数组长度（0表示非数组，如 float4 cb1[4] → 4）
    reg_offset: int = -1  # packoffset(cN) 的寄存器号（16字节单位）；-1 表示未指定
    comp_off: int = 0     # packoffset(cN.x/y/z/w) 的组件偏移（0-3）；用于子寄存器字段
    struct_elem_regs: int = 0  # struct{...} NAME[N] 时每个元素占用的 16 字节寄存器数；0 表示非内嵌结构体
    struct_members: List = None  # struct 成员布局 [(name, type, reg_off, comp_off, arr_size), ...]
    struct_int_data: List = None  # struct 元素的整数寄存器行（用于 uint/int/bool 成员的精确位）
    is_row_major: bool = False  # 声明了 row_major：cbuffer 寄存器即逻辑行，加载时不转置


@dataclass
class TextureBinding:
    """PS中的纹理绑定信息"""
    variable_name: str   # 变量名，如 DiffuseTexture
    register_id: int     # register(t0) 中的 t0，即纹理单元ID
    texture: Optional['Texture'] = None  # 实际的Texture对象
    kind: str = 'Texture2D'  # 资源维度: Texture2D / Texture2DArray / TextureCube / ...


@dataclass
class SamplerBinding:
    """PS中的采样器绑定信息"""
    variable_name: str   # 变量名，如 LinearSampler
    register_id: int     # register(s0) 中的 s0，即采样器ID
    sampler: Optional['Sampler'] = None  # 实际的Sampler对象


@dataclass
class Vertex:
    """顶点对象 - 保存输入和输出顶点数据"""
    index: int = 0                          # 顶点索引（按输入顺序）
    input_data: Dict[str, Any] = None      # 输入顶点数据（所有字段）
    output_data: Dict[str, Any] = None     # 输出顶点数据（所有字段）
    input_position: List[float] = None     # 输入坐标
    input_normal: List[float] = None       # 输入法向量
    input_color: List[float] = None        # 输入颜色
    input_texcoord: List[float] = None    # 输入纹理坐标
    input_texcoord2: List[float] = None   # 输入第二纹理坐标
    output_position: List[float] = None    # 输出坐标
    output_normal: List[float] = None      # 输出法向量
    output_color: List[float] = None       # 输出颜色
    output_texcoord: List[float] = None    # 输出纹理坐标
    output_texcoord2: List[float] = None   # 输出第二纹理坐标

    def __post_init__(self):
        if self.input_data is None:
            self.input_data = {}
        if self.output_data is None:
            self.output_data = {}

@dataclass
class StructDefinition:
    """HLSL结构体定义"""
    name: str                     # 结构体名称
    fields: List[FieldDefinition]  # 结构体字段列表

@dataclass
class CbufferDefinition:
    """HLSL常量缓冲区定义"""
    name: str                     # cbuffer名称
    fields: List[FieldDefinition]  # cbuffer字段列表
    register: Optional[int] = None  # register(bN) 中的 N（用于按槽位匹配二进制）



class VertexPool:
    """顶点池 - 根据输入顺序保存所有顶点对象"""

    def __init__(self):
        self.vertices: List[Vertex] = []
        self._input_struct: Optional[StructDefinition] = None
        self._output_struct: Optional[StructDefinition] = None

    def clear(self):
        """清空顶点池"""
        self.vertices.clear()

    def set_input_struct(self, struct: StructDefinition):
        """设置输入结构体定义"""
        self._input_struct = struct

    def set_output_struct(self, struct: StructDefinition):
        """设置输出结构体定义"""
        self._output_struct = struct

    def add_vertex(self, vertex: Vertex):
        """添加顶点到池中"""
        self.vertices.append(vertex)

    def get_vertex(self, index: int) -> Optional[Vertex]:
        """根据索引获取顶点"""
        if 0 <= index < len(self.vertices):
            return self.vertices[index]
        return None

    def get_input_positions(self) -> List[List[float]]:
        """获取所有输入坐标"""
        return [v.input_position for v in self.vertices if v.input_position]

    def get_input_normals(self) -> List[List[float]]:
        """获取所有输入法向量"""
        return [v.input_normal for v in self.vertices if v.input_normal]

    def get_input_colors(self) -> List[List[float]]:
        """获取所有输入颜色"""
        return [v.input_color for v in self.vertices if v.input_color]

    def get_input_texcoords(self) -> List[List[float]]:
        """获取所有输入纹理坐标"""
        return [v.input_texcoord for v in self.vertices if v.input_texcoord]

    def get_input_texcoords2(self) -> List[List[float]]:
        """获取所有输入第二纹理坐标"""
        return [v.input_texcoord2 for v in self.vertices if v.input_texcoord2]

    def get_output_positions(self) -> List[List[float]]:
        """获取所有输出坐标"""
        return [v.output_position for v in self.vertices if v.output_position]

    def get_output_normals(self) -> List[List[float]]:
        """获取所有输出法向量"""
        return [v.output_normal for v in self.vertices if v.output_normal]

    def get_output_colors(self) -> List[List[float]]:
        """获取所有输出颜色"""
        return [v.output_color for v in self.vertices if v.output_color]

    def get_output_texcoords(self) -> List[List[float]]:
        """获取所有输出纹理坐标"""
        return [v.output_texcoord for v in self.vertices if v.output_texcoord]

    def get_output_texcoords2(self) -> List[List[float]]:
        """获取所有输出第二纹理坐标"""
        return [v.output_texcoord2 for v in self.vertices if v.output_texcoord2]

    def build_from_input(self, vs_input: str, input_data: Dict[str, Any], row_index: int):
        """
        根据输入数据构建顶点
        vs_input: 输入结构体名
        input_data: 输入数据字典
        row_index: 行索引
        """
        input_struct = self._input_struct
        if not input_struct:
            return

        vertex = Vertex(index=row_index, input_data=dict(input_data))

        for field in input_struct.fields:
            field_name_lower = field.name.lower()
            field_semantic_upper = field.semantic.upper()
            value = input_data.get(field.name)

            if value is None:
                continue

            if 'pos' in field_name_lower or 'position' in field_name_lower or field_semantic_upper == 'POSITION':
                if isinstance(value, list) and len(value) >= 3:
                    vertex.input_position = value[:3]
            elif 'normal' in field_name_lower or field_semantic_upper == 'NORMAL':
                if isinstance(value, list) and len(value) >= 3:
                    vertex.input_normal = value[:3]
            elif 'color' in field_name_lower or field_semantic_upper == 'COLOR':
                if isinstance(value, list) and len(value) >= 4:
                    vertex.input_color = value[:4]
                elif isinstance(value, list) and len(value) >= 3:
                    vertex.input_color = value[:3] + [1.0]
            elif 'texcoord' in field_name_lower or 'uv' in field_name_lower or field_semantic_upper.startswith('TEXCOORD'):
                if isinstance(value, list):
                    if 'texcoord2' in field_name_lower or 'texcoord1' in field_name_lower or field_semantic_upper == 'TEXCOORD1':
                        vertex.input_texcoord2 = value[:2] if len(value) >= 2 else value
                    else:
                        vertex.input_texcoord = value[:2] if len(value) >= 2 else value

        self.add_vertex(vertex)

    def update_output(self, row_index: int, result: Dict[str, Any]):
        """
        更新顶点的输出数据
        row_index: 行索引
        result: 输出结果字典
        """
        if row_index >= len(self.vertices):
            return

        vertex = self.vertices[row_index]
        vertex.output_data = dict(result) if result else {}

        output_struct = self._output_struct
        if not output_struct:
            for key, value in result.items() if result else {}.items():
                key_lower = key.lower()
                if 'pos' in key_lower or 'position' in key_lower or key.upper() == 'SV_POSITION':
                    if isinstance(value, list) and len(value) >= 3:
                        vertex.output_position = value[:3]
                elif 'normal' in key_lower:
                    if isinstance(value, list) and len(value) >= 3:
                        vertex.output_normal = value[:3]
                elif 'color' in key_lower:
                    if isinstance(value, list) and len(value) >= 4:
                        vertex.output_color = value[:4]
                    elif isinstance(value, list) and len(value) >= 3:
                        vertex.output_color = value[:3] + [1.0]
                elif 'texcoord' in key_lower or 'uv' in key_lower:
                    if isinstance(value, list):
                        if 'texcoord2' in key_lower or 'texcoord1' in key_lower:
                            vertex.output_texcoord2 = value[:2] if len(value) >= 2 else value
                        else:
                            vertex.output_texcoord = value[:2] if len(value) >= 2 else value
            return

        for field in output_struct.fields:
            field_name_lower = field.name.lower()
            field_semantic_upper = field.semantic.upper()
            value = result.get(field.name) if result else None

            if value is None:
                continue

            if 'pos' in field_name_lower or 'position' in field_name_lower or field_semantic_upper == 'POSITION' or field_semantic_upper == 'SV_POSITION':
                if isinstance(value, list) and len(value) >= 3:
                    vertex.output_position = value[:3]
            elif 'normal' in field_name_lower or field_semantic_upper == 'NORMAL':
                if isinstance(value, list) and len(value) >= 3:
                    vertex.output_normal = value[:3]
            elif 'color' in field_name_lower or field_semantic_upper == 'COLOR':
                if isinstance(value, list) and len(value) >= 4:
                    vertex.output_color = value[:4]
                elif isinstance(value, list) and len(value) >= 3:
                    vertex.output_color = value[:3] + [1.0]
            elif 'texcoord' in field_name_lower or 'uv' in field_name_lower or field_semantic_upper.startswith('TEXCOORD'):
                if isinstance(value, list):
                    if 'texcoord2' in field_name_lower or 'texcoord1' in field_name_lower or field_semantic_upper == 'TEXCOORD1':
                        vertex.output_texcoord2 = value[:2] if len(value) >= 2 else value
                    else:
                        vertex.output_texcoord = value[:2] if len(value) >= 2 else value

    def get_count(self) -> int:
        """获取顶点数量"""
        return len(self.vertices)


class _LoopBreak(Exception):
    """`break` inside a while loop — unwinds through nested if/blocks to the
    innermost executing loop."""


class _LoopContinue(Exception):
    """`continue` inside a while loop."""


class HLSLInterpreter:
    """
    HLSL解释器 - 解析和执行HLSL着色器代码
    支持: 结构体定义、cbuffer定义、函数解析、表达式求值
    """

    def __init__(self,
                log_to_file: bool = True,
                log_file_path: str = "hlsl_interpreter.log",
                print_sequence: int = 1,
                log_file_mode: str = 'a',
                printSyntaxTree: bool = True,
                print_interpreter_result: bool = True,
                max_workers: int = 1,
                primitive_topology: int = D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST,
                log_cache_size: int = 10 * 1024 * 1024,
                texture_list: List['Texture'] = None,
                texture_desc_list: List['TextureDesc'] = None,
                sampler_list: List['Sampler'] = None,
                f32_emulation: bool = False,
                trig_model: str = 'libm'):
        self.structs: Dict[str, StructDefinition] = {}      # 解析的结构体定义
        self.cbuffers: Dict[str, CbufferDefinition] = {}    # 解析的cbuffer定义
        self.variables: Dict[str, Any] = {}                 # 全局变量
        self.debug = True                                   # 调试模式开关
        self.printSyntaxTree = printSyntaxTree              # 打印语法树开关
        self.syntax_parser = SyntaxTreeParser()             # 语法树解析器
        self.log_to_file = log_to_file                      # 是否输出到文件
        self.log_file_path = log_file_path                  # 日志文件路径
        self.log_file_mode = log_file_mode                  # 文件模式: 'a'=追加, 'w'=覆盖
        self.print_sequence = max(1, print_sequence)        # 打印间隔频率
        self.print_interpreter_result = print_interpreter_result  # 是否打印HLSL Interpreter Result
        self._eval_counter = 0                              # evaluate_syntax_tree执行计数器
        self._should_print = True                           # 当前是否应该打印
        self._dbg = True                                    # 快速调试打印开关
        self._log_file = None                               # 日志文件句柄
        self._gs_emit = None                                # GS 执行时的 Append 回调（否则 None）
        self._gs_restart = None                             # GS 执行时的 RestartStrip 回调
        self.hlsl_code = None                               # 加载的HLSL代码
        self.max_workers = max_workers                       # 线程池最大工作线程数
        self._parsed_func_cache = {}                         # 解析过的函数体缓存
        self._stmt_fast = {}                                 # 语句级快派发缓存 {stmt: (kind, ...)}
        self._block_stmts_cache = {}                         # 语句块 → 切分结果缓存
        self._cb_static_cache = {}                           # 字面下标 cbuffer 引用值缓存
        self._static_cb_bases_cache = None                   # 静态 cbuffer 基名集合（惰性）
        self._if_parts_cache = {}                            # if 语句 → (条件, then, after) 缓存
        self._while_parts_cache = {}                         # while 语句 → (条件, body, always) 缓存
        self._all_functions = {}                              # 所有解析的函数定义 {func_name: {'ret_type': ..., 'params': {...}, 'body': ...}}
        self.primitive_topology = primitive_topology         # 图元拓扑类型
        # Names of the current VS input attributes (v0, v1, ...). A (uint)/(int)
        # cast applied directly to one of these REINTERPRETS its float32 bits
        # rather than converting the value: 3Dmigoto writes `(uint2)v1.zw` for a
        # DXBC opcode that consumes the raw register bits (e.g. packed halfs),
        # not an ftou conversion.
        self._vertex_input_names: set = set()
        # Emulate GPU float32 arithmetic: round every binary-op / intrinsic
        # result to float32. Needed for hash-style outputs like frac(K*x*x)
        # where float64 intermediates diverge after amplification.
        self.f32_emulation = f32_emulation
        # GPU trig reduction model for sin/cos/sincos ('libm' = math.sin).
        # Vendor hardware reduces large phases differently from libm; the
        # model is selected per run to fit against golden (step 180).
        self.trig_model = trig_model
        self._sin = _TRIG_MODELS.get(trig_model, _TRIG_MODELS['libm'])[0]
        self._cos = _TRIG_MODELS.get(trig_model, _TRIG_MODELS['libm'])[1]
        self._mesh_view = None                               # MeshView实例(用于显示输入和输出)
        self._mesh_view_enabled = False                      # 是否启用MeshView
        self._trace_sink = None                              # 单项指令追踪缓冲(None=关闭)
        self._trace_only = False                             # 追踪时是否抑制正常日志
        self.vertex_pool = VertexPool()                       # 顶点池
        self._log_cache = []                                 # 日志缓存
        self._log_cache_size = log_cache_size                # 日志缓存大小(字节)
        self._log_cache_bytes = 0                            # 当前缓存已用字节数

        # StructuredBuffer绑定 (如 StructuredBuffer<t0_t> t0 : register(t0))
        # name -> {register, element_type, members:[(name,base_type,count)], stride, data(bytes)}
        self.structured_buffers: Dict[str, dict] = {}
        # Typed-buffer bindings (Buffer<float4> t1 : register(t1)).
        # name -> {register, comp, elem_size, data(bytes)}
        self.typed_buffers: Dict[str, dict] = {}
        # Raw buffer bindings (ByteAddressBuffer g : register(t20)), accessed by
        # byte address via .Load()/ld_raw.  name -> {register, data(bytes)}
        self.byte_address_buffers: Dict[str, dict] = {}
        # Exact int32 bit patterns of binary-loaded cbuffers, so asint/asuint of
        # a direct cbuffer component recovers integers a float cannot round-trip
        # (e.g. -1 stored as NaN).  {'cb1': [[i0,i1,i2,i3], ...]}
        self._cb_raw: Dict[str, list] = {}
        # Cached structured-buffer index for the DXBC split-vector-load hazard
        # (see get_value). {'token': 't0[r1.x]', 'value': int} or None.
        self._sb_index_burst: Optional[dict] = None

        # VS/PS纹理和采样器绑定
        self.texture_bindings: List[TextureBinding] = []     # VS/PS中的纹理绑定列表
        self.sampler_bindings: List[SamplerBinding] = []     # VS/PS中的采样器绑定列表
        self.texture_config_path: str = ""                   # 纹理配置文件路径
        self.sampler_config_path: str = ""                   # 采样器配置文件路径
        self._texture_exec: 'Texture' = texture_list[0] if texture_list else None
        self._texture_desc_list: List['TextureDesc'] = texture_desc_list if texture_desc_list else []
        self._sampler_list: List['Sampler'] = sampler_list if sampler_list else []

        # 2x2 quad context for screen-space derivatives (ddx/ddy → texture LOD).
        # Set per-pixel by the PS loop; None outside a quad-aware PS execution.
        self._quad_inputs: Optional[List[Dict[str, Any]]] = None
        self._quad_lane: int = 0
        self._quad_lane_locals_cache: Dict[int, Optional[dict]] = {}
        self._quad_inputs_id: Optional[int] = None   # identity of the cached quad
        self._in_derivative_eval: bool = False   # reentrancy + log-suppression guard
        # Stashed PS execution context so neighbor lanes can be re-executed.
        self._ps_input_params: Optional[list] = None
        self._ps_output_params: Optional[list] = None
        self._ps_code: Optional[str] = None
        self._ps_main_func: Optional[str] = None

        # 预编译的正则表达式模式字典
        type_pattern = '|'.join(DATA_TYPE_LIST)
        self.patterns: Dict[str, re.Pattern] = {
            # execute_statement: 变量声明语句，如 "float4 pos = ...;"
            'variable_declaration': re.compile(rf'^({type_pattern})\s+(\w+)\s*=\s*(.+?);?$'),

            # execute_statement: output字段赋值语句，如 "output.Color = ...;" 或 "output.Color.r = ...;"
            'output_field_assignment': re.compile(r'output\.(\w+)(?:\.([xyzwrgba]+))?\s*=\s*(.+)'),

            # execute_statement: 一般赋值语句，如 "var = ...;"
            'simple_assignment': re.compile(r'(\w+)\s*=\s*(.+?);?$'),

            # execute_statement: if条件语句，如 "if(condition) { ... }"
            'if_statement': re.compile(r'if\s*\((.+?)\)\s*(.+)$', re.DOTALL),

            # parse_struct: 结构体定义，如 "struct VS_INPUT { ... }"
            'struct_definition': re.compile(r'struct\s+(\w+)\s*\{([^}]+)\}'),

            # parse_cbuffer: cbuffer定义，如 "cbuffer MyBuffer : register(b0) { ... }"
            'cbuffer_definition': re.compile(r'cbuffer\s+(\w+)\s*:.*?\{([^}]+)\}', re.DOTALL),

            # parse_function: 函数定义，如 "float4 main(VS_INPUT input) { ... }"
            'function_definition': re.compile(r'(\w+)\s+(\w+)\s*\(([^)]*)\)\s*\{([^}]+(?:\{[^}]*\}[^}]*)*)\}', re.DOTALL),

            # load_hlsl_code_from_file / executeVS: 查找struct定义（用于finditer）
            'struct_finditer': re.compile(r'struct\s+\w+\s*\{[^}]+\}'),

            # load_hlsl_code_from_file: 查找cbuffer定义（用于finditer）
            'cbuffer_finditer': re.compile(r'cbuffer\s+\w+[^}]+\}'),

            # parse_texture_binding: 纹理绑定，如 "Texture2D DiffuseTexture : register(t0);"
            # group(1)=维度种类(Texture2D/Texture2DArray/TextureCube/...), group(2)=变量名,
            # group(3)=寄存器号。早先只匹配 Texture2D，漏掉了 Texture2DArray/Cube（如 witcher
            # 的 t4 = Texture2DArray<float4>），导致 SampleLevel 找不到绑定而静默返回 0。
            'texture_binding': re.compile(
                r'(Texture(?:1D|2D|3D|Cube)(?:Array)?(?:MS)?)(?:<[^>]+>)?\s+(\w+)\s*:\s*register\(t(\d+)\)\s*;?'),

            # parse_sampler_binding: 采样器绑定，如 "SamplerState LinearSampler : register(s0);"
            # SamplerComparisonState (shadow PCF, e.g. witcher s9_s) declares
            # the same s-register binding as a plain SamplerState.
            'sampler_binding': re.compile(r'Sampler(?:Comparison)?State\s+(\w+)(?:_s)?\s*:\s*register\(s(\d+)\)\s*;?'),

            # multi-variable declaration without initializer: "float4 r0,r1,r2;"
            'multi_var_decl': re.compile(rf'^({type_pattern})\s+([\w][\w\s,]*)\s*;?$'),

            # swizzle assignment to local variable: "r0.xyzw = expr;"
            'swizzle_var_assign': re.compile(r'^(\w+)\.([xyzwrgba]+)\s*=\s*(.+?);?$'),

            # matrix _mRC accessor pattern: _m00_m10_m20_m30
            'matrix_accessor': re.compile(r'^_m\d\d(?:_m\d\d)*$'),
        }

        if self.log_to_file and self.log_file_path:
            self._log_file = open(self.log_file_path, self.log_file_mode, encoding='utf-8')

    def __del__(self):
        """对象销毁时关闭日志文件。进程关停期文件可能已被运行时回收——
        此处只作尽力而为的兜底，确定性的最终 flush 在 render.py 管线收尾处。"""
        try:
            if self._log_cache:
                self._flush_log_cache()
            if self._log_file:
                self._log_file.close()
                self._log_file = None
        except Exception:
            pass

    def enable_mesh_view(self, enable=True, mode: str = None, html_path: str = None):
        """启用/禁用网格视图。

        mode: 'none' 不显示 | 'tk' tkinter MeshView | 'html' 浏览器 HtmlMeshView。
        兼容旧签名 enable_mesh_view(True/False)：True→'tk'、False→'none'
        （若 mode 显式给出则以 mode 为准）。
        """
        if mode is None:
            mode = 'tk' if enable else 'none'
        mode = (mode or 'none').lower()

        if mode == 'none':
            self._mesh_view_enabled = False
            self.log_output("Mesh view disabled")
            return

        if mode == 'html':
            if not HTML_MESHVIEW_AVAILABLE:
                self.log_output("Warning: HtmlMeshView not available")
                self._mesh_view_enabled = False
                return
            self._mesh_view_enabled = True
            if self._mesh_view is None or not isinstance(self._mesh_view, HtmlMeshView):
                self._mesh_view = HtmlMeshView(
                    title="HLSL Interpreter - Mesh View (HTML)", out_path=html_path)
            self.log_output("Mesh view enabled (HTML)")
            return

        if mode == 'web':
            if not WEB_MESHVIEW_AVAILABLE:
                self.log_output("Warning: WebMeshView not available")
                self._mesh_view_enabled = False
                return
            self._mesh_view_enabled = True
            if self._mesh_view is None or not isinstance(self._mesh_view, WebMeshView):
                self._mesh_view = WebMeshView(
                    title="HLSL Interpreter - Mesh View (Web)")
                self._mesh_view._log_output = self.log_output
            self.log_output("Mesh view enabled (web, dynamic)")
            return

        # mode == 'tk'
        if not MESHVIEW_AVAILABLE:
            self.log_output("Warning: MeshView not available (tkinter may not be installed)")
            self._mesh_view_enabled = False
            return
        self._mesh_view_enabled = True
        if self._mesh_view is None:
            self._mesh_view = MeshView(title="HLSL Interpreter - Input/Output Mesh")
        self.log_output("Mesh view enabled (tk)")

    def set_texture_and_sampler(self, texture_exec, texture_desc_list, sampler_list):
        """
        设置纹理采样执行器及其关联的texture_desc和sampler列表
        texture_exec: Texture对象（纹理采样执行器）
        texture_desc_list: TextureDesc对象列表（纹理参数和纹理数据）
        sampler_list: Sampler对象列表（采样参数）
        """
        self._texture_exec = texture_exec
        self._texture_desc_list = texture_desc_list if texture_desc_list else []
        self._sampler_list = sampler_list if sampler_list else []

    def show_input_mesh(self, vs_input: str, row_index: int = None):
        """
        显示当前输入的mesh数据
        vs_input: 输入结构体名
        row_index: 指定行索引，如果为None则显示所有行
        """
        if not self._mesh_view_enabled or not MESHVIEW_AVAILABLE:
            return

        input_struct = self.structs.get(vs_input)
        if not input_struct:
            self.log_output(f"Cannot find vs input struct: {vs_input}")
            return

        positions = self.vertex_pool.get_input_positions()
        normals = self.vertex_pool.get_input_normals()
        colors = self.vertex_pool.get_input_colors()
        texcoords = self.vertex_pool.get_input_texcoords()
        texcoords2 = self.vertex_pool.get_input_texcoords2()

        if not positions:
            self.log_output(f"No input vertices in vertex pool")
            return

        num_rows = len(positions)

        if row_index is not None:
            num_rows = min(row_index + 1, num_rows)
            row_start = row_index
            row_end = row_index + 1
        else:
            row_start = 0
            row_end = num_rows

        positions = positions[row_start:row_end]
        normals = normals[row_start:row_end] if normals and len(normals) >= row_end else None
        colors = colors[row_start:row_end] if colors and len(colors) >= row_end else None
        texcoords = texcoords[row_start:row_end] if texcoords and len(texcoords) >= row_end else None
        texcoords2 = texcoords2[row_start:row_end] if texcoords2 and len(texcoords2) >= row_end else None

        if positions:
            self._mesh_view.clear()
            self._mesh_view.set_primitive_topology(self.primitive_topology)
            self._mesh_view.set_input_data(positions, normals, colors, texcoords, texcoords2)
            self._mesh_view.show(blocking=False)
        else:
            self.log_output(f"No position data found in {vs_input}")

    def show_result_mesh(self, results: List[Dict[str, Any]], output_struct_name: str = None):
        """
        显示executeVS执行完毕后的results mesh数据
        results: executeVS返回的输出结构体字典列表
        output_struct_name: 输出结构体名(可选)
        """
        if not self._mesh_view_enabled or not MESHVIEW_AVAILABLE:
            return

        positions = self.vertex_pool.get_output_positions()
        normals = self.vertex_pool.get_output_normals()
        colors = self.vertex_pool.get_output_colors()
        texcoords = self.vertex_pool.get_output_texcoords()
        texcoords2 = self.vertex_pool.get_output_texcoords2()

        if not positions:
            self.log_output("No output vertices in vertex pool")
            return

        if positions:
            self._mesh_view.set_primitive_topology(self.primitive_topology)
            self._mesh_view.set_output_data(positions, normals, colors, texcoords, texcoords2)
            self._mesh_view.show(blocking=False)
            self.log_output(f"Result mesh displayed: {len(positions)} vertices")
        else:
            self.log_output("No position data found in results")

    def show_input_mesh_from_params(self, input_params: List[Dict[str, Any]],
                                    vertex_data: List[Dict[str, Any]], row_index: int = None):
        """
        显示参数式(void main)工作流的输入mesh数据。

        与struct式的show_input_mesh不同，这里直接从VS输入参数(按语义)和vertex_data
        构建网格，不依赖struct定义或vertex_pool（参数式工作流不填充vertex_pool）。
        input_params: parse_main_params_with_semantics得到的inputs列表
        vertex_data:  load_ia_vertex_data得到的列表，每项是{param_name: value}
        row_index:    指定行索引，None表示显示所有顶点
        """
        if not self._mesh_view_enabled or not MESHVIEW_AVAILABLE or not self._mesh_view:
            return

        positions, normals, colors, texcoords, texcoords2 = [], [], [], [], []
        for vtx in vertex_data:
            pos = norm = col = tc = tc2 = None
            for param in input_params:
                sem = param.get('semantic_base', '').upper()
                sem_idx = param.get('semantic_index', 0)
                val = vtx.get(param['name'])
                if not isinstance(val, list):
                    continue
                if sem == 'POSITION' and len(val) >= 3:
                    pos = val[:3]
                elif sem == 'NORMAL' and len(val) >= 3:
                    norm = val[:3]
                elif sem == 'COLOR':
                    if len(val) >= 4:
                        col = val[:4]
                    elif len(val) >= 3:
                        col = val[:3] + [1.0]
                elif sem == 'TEXCOORD' and len(val) >= 2:
                    if sem_idx == 1:
                        tc2 = val[:2]
                    else:
                        tc = val[:2]
            if pos is None:
                continue
            positions.append(pos)
            normals.append(norm)
            colors.append(col)
            texcoords.append(tc)
            texcoords2.append(tc2)

        if row_index is not None and 0 <= row_index < len(positions):
            sl = slice(row_index, row_index + 1)
            positions, normals = positions[sl], normals[sl]
            colors, texcoords, texcoords2 = colors[sl], texcoords[sl], texcoords2[sl]

        if not positions:
            self.log_output("No input position data for mesh view")
            return

        self._mesh_view.clear()
        self._mesh_view.set_primitive_topology(self.primitive_topology)
        self._mesh_view.set_input_data(positions, normals, colors, texcoords, texcoords2)
        self._mesh_view.show(blocking=False)
        self.log_output(f"Input mesh displayed: {len(positions)} vertices")

    def show_result_mesh_from_params(self, vs_results: List[Dict[str, Any]]):
        """
        显示参数式工作流的VS输出mesh数据。

        vs_results是executeVS_with_params的返回值，已按canonical key组织
        (sv_position / Normal / Color / TexCoord / TexCoord2)。沿用legacy
        update_output的约定：输出位置取SV_POSITION的前3个分量。
        """
        if not self._mesh_view_enabled or not MESHVIEW_AVAILABLE or not self._mesh_view:
            return

        positions, normals, colors, texcoords, texcoords2 = [], [], [], [], []
        for row in vs_results:
            pos = row.get('sv_position')
            if not (isinstance(pos, list) and len(pos) >= 3):
                continue
            positions.append(pos[:3])

            norm = row.get('Normal')
            normals.append(norm[:3] if isinstance(norm, list) and len(norm) >= 3 else None)

            col = row.get('Color')
            if isinstance(col, list) and len(col) >= 4:
                colors.append(col[:4])
            elif isinstance(col, list) and len(col) >= 3:
                colors.append(col[:3] + [1.0])
            else:
                colors.append(None)

            tc = row.get('TexCoord')
            texcoords.append(tc[:2] if isinstance(tc, list) and len(tc) >= 2 else None)
            tc2 = row.get('TexCoord2')
            texcoords2.append(tc2[:2] if isinstance(tc2, list) and len(tc2) >= 2 else None)

        if not positions:
            self.log_output("No output position data for mesh view")
            return

        self._mesh_view.set_primitive_topology(self.primitive_topology)
        self._mesh_view.set_output_data(positions, normals, colors, texcoords, texcoords2)
        self._mesh_view.show(blocking=False)
        self.log_output(f"Result mesh displayed: {len(positions)} vertices")

    def _flush_log_cache(self):
        """将缓存中的日志写入文件"""
        if self._log_cache and self._log_file:
            self._log_file.write(''.join(self._log_cache))
            self._log_file.flush()
            self._log_cache = []
            self._log_cache_bytes = 0

    def log_output(self, *args, **kwargs):
        """输出到stdout和日志文件"""
        msg = ' '.join(str(arg) for arg in args)
        print(*args, **kwargs)
        if self.log_to_file and self._log_file:
            msg_bytes = (msg + '\n').encode('utf-8')
            if self._log_cache_bytes + len(msg_bytes) >= self._log_cache_size:
                self._flush_log_cache()
            self._log_cache.append(msg + '\n')
            self._log_cache_bytes += len(msg_bytes)

    def debug_print(self, msg: str):
        """调试打印"""
        if self.debug and self._should_print and not self._in_derivative_eval:
            # Single-item trace capture (web "Selected Vertex/Pixel Info"): when a
            # sink is active, collect the per-statement lines and (in trace-only
            # mode) skip the normal stdout/log so a trace click doesn't pollute
            # the run log.
            sink = self._trace_sink
            if sink is not None:
                sink.append(msg)
                if self._trace_only:
                    return
            self.log_output(msg)

    @staticmethod
    def _safe_pow(base, exp):
        """pow that saturates to +/-inf instead of raising OverflowError, and
        returns NaN for invalid domains instead of crashing. 3Dmigoto exp2/pow
        on real cbuffer values can blow past float range; downstream consumers
        (color quantizer, golden compare) already tolerate inf/NaN."""
        try:
            return math.pow(base, exp)
        except OverflowError:
            return math.inf if base > 0 else -math.inf
        except (ValueError, ZeroDivisionError):
            return math.nan

    def _format_float(self, val):
        """
        格式化浮点数输出
        val: 值
        返回: 格式化后的字符串(保留4位小数)
        """
        if isinstance(val, float):
            return f"{val:.4f}"
        if isinstance(val, list):
            if val and isinstance(val[0], list):
                return self._format_matrix(val)
            return [self._format_float(v) for v in val]
        return val

    def _format_matrix(self, val):
        """
        格式化矩阵输出
        val: 矩阵(二维列表)
        返回: 格式化后的矩阵字符串
        """
        if not val or not isinstance(val[0], list):
            return str(val)
        formatted = [[self._format_float(v) for v in row] for row in val]
        col_widths = [0] * len(formatted[0])
        for row in formatted:
            for j, cell in enumerate(row):
                col_widths[j] = max(col_widths[j], len(cell))
        lines = []
        for row in formatted:
            cells = [cell.rjust(col_widths[j]) for j, cell in enumerate(row)]
            lines.append("[" + " ".join(cells) + "]")
        return "\n".join(lines)

    def _format_value(self, val):
        """格式化值输出(矩阵或标量/向量)"""
        if isinstance(val, list) and val and isinstance(val[0], list):
            return self._format_matrix(val)
        return self._format_float(val)

    def _format_msg(self, *args):
        """格式化多个值用于调试输出"""
        formatted = []
        for arg in args:
            formatted.append(self._format_float(arg))
        return formatted

    def load_json(self, filepath: str):
        """从JSON文件加载数据"""
        with open(filepath, 'r') as f:
            data = json.load(f)
        return data

    def load_csv(self, filepath: str) -> List[List[str]]:
        """从CSV文件加载数据，返回二维列表"""
        rows = []
        with open(filepath, 'r') as f:
            reader = csv.reader(f)
            for row in reader:
                rows.append(row)
        return rows

    def get_type_size(self, field_type: str) -> int:
        """
        获取HLSL类型的大小(字节数)
        field_type: HLSL类型名，如 float4x4, float3, int
        返回: 类型占用的字节数
        """
        return self._TYPE_SIZE_MAP.get(field_type, 0)

    _TYPE_SIZE_MAP = {
        'float4x4': 64, 'float3x3': 36, 'float4': 16, 'float3': 12,
        'float2': 8, 'float': 4, 'uint4': 16, 'uint3': 12, 'uint2': 8,
        'uint': 4, 'int4': 16, 'int3': 12, 'int2': 8, 'int': 4, 'bool': 4
    }

    def parse_value_by_type(self, value_str: str, field_type: str) -> Any:
        """
        根据类型解析字符串值为对应类型的Python对象
        value_str: 值的字符串表示
        field_type: HLSL类型名
        返回: 解析后的值
        """
        value_str = value_str.strip().strip('"')
        handler = self._PARSE_TYPE_HANDLERS.get(field_type)
        if handler:
            return handler(self, value_str)
        try:
            return float(value_str)
        except:
            return value_str

    def _parse_float4x4(self, value_str):
        parts = value_str.split(',')
        if len(parts) >= 16:
            return [[float(parts[j]) for j in range(i*4, i*4+4)] for i in range(4)]
        return None

    def _parse_float3x3(self, value_str):
        parts = value_str.split(',')
        if len(parts) >= 9:
            return [[float(parts[j]) for j in range(i*3, i*3+3)] for i in range(3)]
        return None

    def _parse_float_vector(self, value_str, count):
        return [float(p) for p in value_str.split(',')[:count]]

    def _parse_int_vector(self, value_str, count):
        return [int(p) for p in value_str.split(',')[:count]]

    def _parse_bool(self, value_str):
        return value_str.lower() in ('true', '1', 'yes')

    _PARSE_TYPE_HANDLERS = {
        'float4x4': _parse_float4x4,
        'float3x3': _parse_float3x3,
        'float4': lambda s, v: s._parse_float_vector(v, 4),
        'float3': lambda s, v: s._parse_float_vector(v, 3),
        'float2': lambda s, v: s._parse_float_vector(v, 2),
        'uint4': lambda s, v: s._parse_int_vector(v, 4),
        'uint3': lambda s, v: s._parse_int_vector(v, 3),
        'uint2': lambda s, v: s._parse_int_vector(v, 2),
        'uint': lambda s, v: int(v),
        'int4': lambda s, v: s._parse_int_vector(v, 4),
        'int3': lambda s, v: s._parse_int_vector(v, 3),
        'int2': lambda s, v: s._parse_int_vector(v, 2),
        'int': lambda s, v: int(v),
        'bool': _parse_bool,
    }

    def parse_type(self, type_str: str) -> str:
        """
        解析HLSL类型字符串为标准类型名
        type_str: 类型字符串，如 "float4x4", "float3", "int2"
        返回: 标准类型名
        """
        type_str = type_str.strip()
        if type_str in DATA_TYPE_LIST:
            return type_str
        if type_str.startswith('float'):
            if 'x3' in type_str:
                return 'float3x3'
            elif 'x4' in type_str:
                return 'float4x4'
            elif type_str == 'float':
                return 'float'
            return 'float'
        elif type_str.startswith('int'):
            if type_str == 'int':
                return 'int'
            elif '2' in type_str:
                return 'int2'
            elif '3' in type_str:
                return 'int3'
            elif '4' in type_str:
                return 'int4'
            return 'int'
        elif type_str.startswith('uint'):
            if type_str == 'uint':
                return 'uint'
            elif '2' in type_str:
                return 'uint2'
            elif '3' in type_str:
                return 'uint3'
            elif '4' in type_str:
                return 'uint4'
            return 'uint'
        elif type_str.startswith('bool'):
            return 'bool'
        return type_str

    def parse_struct(self, code: str) -> StructDefinition:
        """
        解析HLSL结构体定义
        code: 结构体代码，如 "struct VS_INPUT { float3 Pos : POSITION; }"
        返回: StructDefinition对象
        """
        match = self.patterns['struct_definition'].search(code)
        if not match:
            return None
        name = match.group(1)
        fields_str = match.group(2)
        fields = []
        for line in fields_str.split(';'):
            line = line.strip()
            if not line:
                continue
            parts = line.split(':')
            if len(parts) == 2:
                type_and_name = parts[0].strip().split()
                semantic = parts[1].strip()
                if len(type_and_name) >= 2:
                    field_type = type_and_name[0]
                    field_name = type_and_name[-1]
                else:
                    field_type = type_and_name[0]
                    field_name = ''
                fields.append(FieldDefinition(field_type, field_name, semantic))
        return StructDefinition(name, fields)

    def _extract_cbuffer_blocks(self, code: str) -> list:
        """Return each `cbuffer NAME : ... { ... }` block with **balanced** braces.

        The `[^}]+`-based regexes stop at the first `}`, which truncates a cbuffer
        whose body contains a nested `struct { ... }` (3Dmigoto emits these for
        struct-typed constant arrays, e.g. TombRaider's `struct {...} Params[12]`).
        A manual brace scan captures the whole cbuffer including nested braces."""
        blocks = []
        for m in re.finditer(r'cbuffer\s+\w+', code):
            brace_start = code.find('{', m.end())
            if brace_start < 0:
                continue
            depth = 0
            for i in range(brace_start, len(code)):
                c = code[i]
                if c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        blocks.append(code[m.start():i + 1])
                        break
        return blocks

    def parse_cbuffer(self, code: str) -> CbufferDefinition:
        """
        解析HLSL常量缓冲区定义
        code: cbuffer代码
        返回: CbufferDefinition对象
        """
        header_m = re.match(r'cbuffer\s+(\w+)', code)
        if not header_m:
            return None
        name = header_m.group(1)
        # register(bN) so the binary loader can match by slot regardless of the
        # cbuffer's name (cb0 / Constants / cbuffer0 all differ across captures).
        reg_m = re.search(r'register\s*\(\s*b(\d+)\s*\)', code)
        register = int(reg_m.group(1)) if reg_m else None
        fields = []
        # Body = everything between the outermost braces (brace-balanced input).
        body_start = code.find('{')
        body_end = code.rfind('}')
        body = code[body_start + 1:body_end] if 0 <= body_start < body_end else ''

        # Pull out any nested `struct { members } NAME[N] : packoffset(cM);`
        # declarations first, register them as struct-array fields, and strip
        # them from the body so the per-line scan below sees only flat fields.
        def _consume_struct(struct_m):
            members_src = struct_m.group(1)
            field_name = struct_m.group(2)
            arr_n = int(struct_m.group(3)) if struct_m.group(3) else 0
            po_reg = int(struct_m.group(4)) if struct_m.group(4) else -1
            # Walk members under HLSL cbuffer packing rules, recording each one's
            # (name, type, register offset, component offset, count, array size)
            # within a single element so `NAME[i].member[.swizzle]` can be sliced
            # from the binary later. Matrices / float4 align to a register; scalar
            # & small-vector members pack into a register but never straddle it.
            members = []
            cur = 0  # running byte offset within one element
            for mline in members_src.split(';'):
                mline = mline.strip().replace('row_major', '').replace('column_major', '').strip()
                if not mline:
                    continue
                mparts = mline.split()
                if len(mparts) < 2:
                    continue
                mtype = mparts[0]
                mname = mparts[1]
                m_arr = 0
                am = re.match(r'(\w+)\[(\d+)\]', mname)
                if am:
                    mname, m_arr = am.group(1), int(am.group(2))
                sz = self._CB_TYPE_BYTES.get(mtype, 16)
                if sz > 16:                       # matrices align to a register
                    cur = (cur + 15) // 16 * 16
                elif sz <= 16 and (cur % 16) + sz > 16:  # vector would straddle
                    cur = (cur + 15) // 16 * 16
                if m_arr > 0:                     # array element register-aligned
                    cur = (cur + 15) // 16 * 16
                members.append((mname, mtype, cur // 16, (cur % 16) // 4, m_arr))
                if m_arr > 0:
                    cur += m_arr * ((sz + 15) // 16 * 16)
                else:
                    cur += sz
            elem_regs = max(1, (cur + 15) // 16)
            fields.append(FieldDefinition('__struct__', field_name, '',
                                          array_size=arr_n, reg_offset=po_reg,
                                          struct_elem_regs=elem_regs,
                                          struct_members=members))
            return ''
        body = re.sub(
            r'struct\s*\{(.*?)\}\s*(\w+)\s*(?:\[(\d+)\])?\s*(?::\s*packoffset\(\s*c(\d+)[^)]*\))?\s*;',
            _consume_struct, body, flags=re.DOTALL)

        lines = body.split('\n')
        for line in lines:
            line = line.strip().rstrip(';')
            if not line or line.startswith('}'):
                continue
            if any(t in line for t in DATA_TYPE_LIST):
                # Strip the matrix major-order qualifier so it is not mistaken
                # for the type: `row_major float4x4 mW2P : packoffset(c0)` must
                # parse as type=float4x4 / name=mW2P, not type=row_major /
                # name=float4x4 (which leaves the matrix unnamed → never loaded
                # from CSV or binary, e.g. Nobu's mW2P/mW2Pt/mW2S).
                line_nm = re.sub(r'\b(row_major|column_major)\s+', '', line)
                parts = line_nm.split()
                if len(parts) >= 2:
                    field_type = parts[0]
                    field_name = parts[1]
                    array_size = 0
                    # 数组声明: float4 cb1[4] → name=cb1, array_size=4
                    arr_match = re.match(r'(\w+)\[(\d+)\]', field_name)
                    if arr_match:
                        field_name = arr_match.group(1)
                        array_size = int(arr_match.group(2))
                    # packoffset(cN[.xyzw]) → 该字段所在的 16 字节寄存器号。
                    # 多个数组字段(如 mvp[2]@c0 与 texgen[2]@c2)靠它从同一
                    # 二进制 cbuffer 里各取正确的寄存器区间。
                    po_match = re.search(r'packoffset\s*\(\s*c(\d+)(?:\.([xyzwrgba]))?\s*\)', line)
                    if po_match:
                        reg_offset = int(po_match.group(1))
                        _comp_char = po_match.group(2) or 'x'
                        comp_off = {'x': 0, 'r': 0, 'y': 1, 'g': 1,
                                    'z': 2, 'b': 2, 'w': 3, 'a': 3}.get(_comp_char, 0)
                    else:
                        reg_offset = -1
                        comp_off = 0
                    # Record the declared major order: a `row_major` matrix keeps
                    # its cbuffer registers AS rows (no transpose on load), so the
                    # decompiled `M._m00_m01_m02_m03` (row) accessor maps straight
                    # to register 0. A plain/`column_major` matrix stores columns
                    # in registers and is transposed to logical rows on load, so
                    # the decompiled `M._m00_m10_m20_m30` (column) accessor maps to
                    # register 0. Getting this per-matrix is what lets TombRaider's
                    # row_major WorldToPSSM0 and the Collision suite's column_major
                    # WorldViewProj both resolve correctly.
                    is_row_major = bool(re.search(r'\brow_major\b', line))
                    fields.append(FieldDefinition(field_type, field_name, '',
                                                  array_size=array_size, reg_offset=reg_offset,
                                                  comp_off=comp_off, is_row_major=is_row_major))
        return CbufferDefinition(name, fields, register=register)

    # 控制流关键字: header 正则可能把 "else if (...)" 误判成函数定义，需排除
    _CONTROL_KEYWORDS = frozenset({
        'if', 'else', 'for', 'while', 'switch', 'do', 'return', 'case', 'default'
    })

    def parse_all_functions(self, code: str):
        """
        解析代码中所有函数定义并存储到_all_functions字典
        code: HLSL代码

        函数体用大括号配对(brace matching)提取，而非单条正则。早期版本用
        r'...\{([^}]+(?:\{[^}]*\}[^}]*)*)\}' 捕获函数体，只能处理一层嵌套的
        `{...}`；3Dmigoto 反编译出的着色器常有 3~4 层嵌套的 if/else，正则会在
        第一个深层嵌套块处提前截断函数体，导致其后的语句(包括 o0 输出写入)整段丢失，
        VS 输出全部塌成 0。改为定位函数头后逐字符配对大括号，可支持任意嵌套深度。
        """
        header_pattern = re.compile(r'(\w+)\s+(\w+)\s*\(([^)]*)\)\s*(?::\s*\w+)?\s*\{')
        for match in header_pattern.finditer(code):
            ret_type = match.group(1)
            func_name = match.group(2)
            params_str = match.group(3)
            # 排除把控制流语句(如 "else if (cond) {")误当成函数定义
            if ret_type in self._CONTROL_KEYWORDS or func_name in self._CONTROL_KEYWORDS:
                continue
            # 从匹配末尾的 '{' 起逐字符配对，找到对应的 '}'
            brace_start = match.end() - 1
            depth = 0
            body_end = None
            for i in range(brace_start, len(code)):
                c = code[i]
                if c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        body_end = i
                        break
            if body_end is None:
                continue
            body = code[brace_start + 1:body_end]
            params = {}
            if params_str.strip():
                for param in params_str.split(','):
                    param = param.strip()
                    parts = param.split()
                    if len(parts) >= 2:
                        param_type = parts[0]
                        param_name = parts[1]
                        params[param_name] = param_type
            self._all_functions[func_name] = {
                'ret_type': ret_type,
                'params': params,
                'body': body
            }

    def _get_function_body(self, func_name: str) -> Optional[str]:
        """
        根据函数名获取函数体
        func_name: 函数名
        返回: 函数体字符串，如果未找到返回None
        """
        if func_name in self._all_functions:
            return self._all_functions[func_name]['body']
        return None

    def _collect_function_statements(self, func_name: str, visited: set = None, is_main_func: bool = False) -> List[str]:
        """
        递归收集函数及其调用的其他函数的语句
        func_name: 函数名
        visited: 已访问的函数集合（防止循环调用）
        is_main_func: 是否是主函数（主函数的return语句需要保留）
        返回: 语句列表
        """
        if visited is None:
            visited = set()

        if func_name in visited:
            return []
        visited.add(func_name)

        body = self._get_function_body(func_name)
        if body is None:
            return []

        statements = self.GenerateStmts(body.strip())

        result_statements = []
        for stmt in statements:
            if stmt is None:
                continue

            called_funcs = self._find_function_calls_in_statement(stmt)
            for called_func in called_funcs:
                if called_func in self._all_functions and called_func not in visited:
                    nested_statements = self._collect_function_statements(called_func, visited, is_main_func=False)
                    result_statements.extend(nested_statements)

            result_statements.append(stmt)

        return result_statements

    def _find_function_calls_in_statement(self, stmt: str) -> List[str]:
        """
        从语句中查找用户定义的函数调用
        stmt: 语句字符串
        返回: 函数名列表
        """
        func_calls = []
        func_pattern = re.compile(r'(\w+)\s*\(')
        for match in func_pattern.finditer(stmt):
            func_name = match.group(1)
            if func_name not in ['if', 'for', 'while', 'do', 'switch']:
                func_calls.append(func_name)
        return func_calls

    def parse_function(self, code: str) -> tuple:
        """
        解析HLSL函数定义
        code: 函数代码，如 "float4 main(VS_INPUT input) { ... }"
        返回: (返回类型, 函数名, 参数字典, 函数体) 元组
        """
        match = self.patterns['function_definition'].search(code)
        if not match:
            return None, None, None, None
        ret_type = match.group(1)
        func_name = match.group(2)
        params_str = match.group(3)
        body = match.group(4)
        params = {}
        if params_str.strip():
            for param in params_str.split(','):
                param = param.strip()
                parts = param.split()
                if len(parts) >= 2:
                    param_type = parts[0]
                    param_name = parts[1]
                    params[param_name] = param_type
        return ret_type, func_name, params, body

    def execute_unary_op(self, op: str, val: Any) -> Any:
        """
        执行一元运算符
        op: 运算符 '-' 或 '!'
        val: 操作数
        """
        def _neg(v):
            if isinstance(v, bool):
                return -1 if v else 0
            if isinstance(v, (int, float)):
                return -v
            return v

        def _bitnot(v):
            if isinstance(v, (int, float, bool)):
                r = (~int(v)) & 0xFFFFFFFF
                return r - 0x100000000 if r >= 0x80000000 else r
            return v

        if op == '-':
            if isinstance(val, list):
                result = [_neg(v) for v in val]
            else:
                result = _neg(val)
        elif op == '~':
            result = [_bitnot(v) for v in val] if isinstance(val, list) else _bitnot(val)
        else:
            result = not bool(val)
        if self.debug and self._should_print:
            if self._dbg: self.debug_print(f"[UNARY OP] operand={self._format_value(val)}, op={op}, result={self._format_value(result)}")
        return result

    def execute_binary_op(self, op: str, left: Any, right: Any) -> Any:
        """
        执行二元运算符
        op: 运算符 '+', '-', '*', '/', '.'
        left, right: 左右操作数
        """
        if left is None and op == '-':
            # Unary negation encoded as binary with empty left side: -(expr)
            return self.execute_unary_op('-', right)
        if left is None or right is None:
            result = None
            if self._dbg: self.debug_print(f"[BINARY OP] left={self._format_value(left)}, right={self._format_value(right)}, op={op}, result={self._format_value(result)}")
            return None
        # Defensive guard: an arithmetic op on a nested list (a matrix where a
        # vector was expected, e.g. an unresolved cbuffer/struct access) would
        # raise `TypeError: can't multiply sequence ...` and abort the whole draw.
        # Collapse a list-of-lists operand to its first row so we degrade to a
        # wrong-but-finite value instead of crashing.
        if op in ('+', '-', '*', '/'):
            if isinstance(left, list) and left and isinstance(left[0], list):
                left = left[0]
            if isinstance(right, list) and right and isinstance(right[0], list):
                right = right[0]
        if op == '+':
            if isinstance(left, list) and isinstance(right, list):
                result = [l + r for l, r in zip(left, right)]
            elif isinstance(left, list) and isinstance(right, (int, float)):
                result = [v + right for v in left]
            elif isinstance(right, list) and isinstance(left, (int, float)):
                result = [left + v for v in right]
            else:
                result = left + right
        elif op == '-':
            if isinstance(left, list) and isinstance(right, list):
                result = [l - r for l, r in zip(left, right)]
            elif isinstance(left, list) and isinstance(right, (int, float)):
                result = [v - right for v in left]
            elif isinstance(right, list) and isinstance(left, (int, float)):
                result = [left - v for v in right]
            else:
                result = left - right
        elif op == '*':
            if isinstance(left, list) and isinstance(right, (int, float)):
                result = [v * right for v in left]
            elif isinstance(right, list) and isinstance(left, (int, float)):
                result = [v * left for v in right]
            elif isinstance(left, list) and isinstance(right, list):
                result = [l * r for l, r in zip(left, right)]
            else:
                result = left * right
        elif op == '/':
            def _safe_div(a, b):
                if b == 0 or b == 0.0:
                    return float('inf') if (a is None or a >= 0) else float('-inf')
                return a / b
            if isinstance(left, list) and isinstance(right, (int, float)):
                result = [_safe_div(v, right) for v in left]
            elif isinstance(left, list) and isinstance(right, list):
                result = [_safe_div(l, r) for l, r in zip(left, right)]
            elif isinstance(right, (int, float)):
                result = _safe_div(left, right)
            else:
                result = left / right if right else 0.0
        elif op == '.':
            result = (left, right)
        elif op == '==':
            if isinstance(left, list) or isinstance(right, list):
                lv = left if isinstance(left, list) else [left] * (len(right) if isinstance(right, list) else 1)
                rv = right if isinstance(right, list) else [right] * len(lv)
                result = [1 if l == r else 0 for l, r in zip(lv, rv)]
            else:
                result = left == right
        elif op == '!=':
            if isinstance(left, list) or isinstance(right, list):
                lv = left if isinstance(left, list) else [left] * (len(right) if isinstance(right, list) else 1)
                rv = right if isinstance(right, list) else [right] * len(lv)
                result = [1 if l != r else 0 for l, r in zip(lv, rv)]
            else:
                result = left != right
        elif op == '<':
            if isinstance(left, list) or isinstance(right, list):
                lv = left if isinstance(left, list) else [left] * (len(right) if isinstance(right, list) else 1)
                rv = right if isinstance(right, list) else [right] * len(lv)
                result = [1 if l < r else 0 for l, r in zip(lv, rv)]
            else:
                result = left < right
        elif op == '>':
            if isinstance(left, list) or isinstance(right, list):
                lv = left if isinstance(left, list) else [left] * (len(right) if isinstance(right, list) else 1)
                rv = right if isinstance(right, list) else [right] * len(lv)
                result = [1 if l > r else 0 for l, r in zip(lv, rv)]
            else:
                result = left > right
        elif op == '<=':
            if isinstance(left, list) or isinstance(right, list):
                lv = left if isinstance(left, list) else [left] * (len(right) if isinstance(right, list) else 1)
                rv = right if isinstance(right, list) else [right] * len(lv)
                result = [1 if l <= r else 0 for l, r in zip(lv, rv)]
            else:
                result = left <= right
        elif op == '>=':
            if isinstance(left, list) or isinstance(right, list):
                lv = left if isinstance(left, list) else [left] * (len(right) if isinstance(right, list) else 1)
                rv = right if isinstance(right, list) else [right] * len(lv)
                result = [1 if l >= r else 0 for l, r in zip(lv, rv)]
            else:
                result = left >= right
        elif op == '&&':
            result = bool(self._to_bool(left) and self._to_bool(right))
        elif op == '||':
            result = bool(self._to_bool(left) or self._to_bool(right))
        elif op == '|':
            if isinstance(left, list) and isinstance(right, list):
                result = [int(l) | int(r) for l, r in zip(left, right)]
            elif isinstance(left, list):
                result = [int(l) | int(right) for l in left]
            elif isinstance(right, list):
                result = [int(left) | int(r) for r in right]
            else:
                result = int(left) | int(right)
        elif op == '&':
            if isinstance(left, list) and isinstance(right, list):
                result = [int(l) & int(r) for l, r in zip(left, right)]
            elif isinstance(left, list):
                result = [int(l) & int(right) for l in left]
            elif isinstance(right, list):
                result = [int(left) & int(r) for r in right]
            else:
                result = int(left) & int(right)
        elif op in ('^', '<<', '>>', '%'):
            # Bitwise xor / shifts (mask to 32-bit, as HLSL ints are 32-bit) and
            # integer modulo. Used by particle/quaternion shaders for index math.
            def _bit(a, b):
                ia, ib = int(a) & 0xFFFFFFFF, int(b) & 0xFFFFFFFF
                if op == '^':
                    r = ia ^ ib
                elif op == '<<':
                    r = (ia << (ib & 31)) & 0xFFFFFFFF
                elif op == '>>':
                    r = ia >> (ib & 31)
                else:  # '%' — operate on the signed values
                    bi = int(b)
                    return int(a) % bi if bi != 0 else 0
                # Reinterpret as signed 32-bit so subsequent (int) math matches.
                return r - 0x100000000 if r >= 0x80000000 else r
            if isinstance(left, list) or isinstance(right, list):
                lv = left if isinstance(left, list) else [left] * (len(right) if isinstance(right, list) else 1)
                rv = right if isinstance(right, list) else [right] * len(lv)
                result = [_bit(l, r) for l, r in zip(lv, rv)]
            else:
                result = _bit(left, right)
        else:
            result = None
        if self.f32_emulation and op in ('+', '-', '*', '/'):
            result = self._to_f32(result)
        if self._dbg: self.debug_print(f"[BINARY OP] left={self._format_float(left)}, right={self._format_float(right)}, op={op}, result={self._format_float(result)}")
        return result

    def transpose_matrix(self, m: List[List[float]]) -> List[List[float]]:
        """
        矩阵转置
        m: 输入矩阵(4x4或3x3)
        返回: 转置后的矩阵
        """
        n = len(m)
        return [[m[j][i] for j in range(n)] for i in range(n)]

    def _gpu_dot(self, pairs) -> float:
        """Accumulate a dot product the way the GPU does under float32
        emulation: mul then a mad chain, each step rounded ONCE to float32
        (the f32 products are exact in double, and rounding the double
        product+accumulator sum once per step is exactly a fused mad).
        A double-precision sum rounded once at the end drifts a few ULP from
        the GPU on long position chains (sekiro sv_position ~0.005 misses)."""
        t = None
        for x, y in pairs:
            if t is None:
                t = self._to_f32(x * y)
            else:
                t = self._to_f32(x * y + t)
        return t if t is not None else 0.0

    def mul_matrix_vector(self, m: List[List[float]], v: List[float]) -> List[float]:
        """
        矩阵乘向量: result = m * v
        m: 4x4或3x3矩阵
        v: 向量(4维或3维)
        返回: 计算后的向量
        """
        if not v or any(x is None for x in v):
            return [0, 0, 0, 0]
        if not m:
            return [0, 0, 0, 0]
        if self.f32_emulation:
            return [self._gpu_dot((v[i], m[i][j]) for i in range(len(v)))
                    for j in range(len(m[0]))]
        return [sum(v[i] * m[i][j] for i in range(len(v))) for j in range(len(m[0]))]

    def mul_matrix_matrix(self, a: List[List[float]], b: List[List[float]]) -> List[List[float]]:
        """
        矩阵乘法: result = a * b
        a, b: n x n 方阵
        返回: 结果矩阵
        """
        n = len(a)
        return [[sum(a[i][k] * b[k][j] for k in range(n)) for j in range(n)] for i in range(n)]

    def length_vec(self, v: List[float]) -> float:
        """计算向量长度(模)"""
        return math.sqrt(sum(x * x for x in v))

    def normalize_vec(self, v: List[float]) -> List[float]:
        """
        向量归一化
        v: 输入向量
        返回: 归一化后的向量，长度为1
        """
        l = self.length_vec(v)
        if l < 1e-8:
            return v
        return [x / l for x in v]

    def dot_product(self, a: List[float], b: List[float]) -> float:
        """
        向量点积: a · b
        a, b: 同维度向量
        返回: 点积结果
        """
        if not isinstance(a, list) or not isinstance(b, list):
            return 0.0
        if len(a) != len(b):
            return 0.0
        if self.f32_emulation:
            return self._gpu_dot(zip(a, b))
        return sum(x * y for x, y in zip(a, b))

    def reflect_vec(self, I: List[float], N: List[float]) -> List[float]:
        """
        计算反射向量 R = I - 2 * (N · I) * N
        I: 入射向量
        N: 法线向量(需要归一化)
        返回: 反射向量
        """
        if not isinstance(I, list) or not isinstance(N, list):
            return [0, 0, 0]
        dot = self.dot_product(N, I)
        result = []
        for i_val, n_val in zip(I, N):
            result.append(i_val - 2 * n_val * dot)
        return result

    def find_top_level_comma(self, expr: str) -> int:
        """
        查找表达式顶层逗号(不在括号内)
        用于分割函数多参数
        expr: 表达式字符串
        返回: 逗号位置索引，或-1表示未找到
        """
        depth = 0
        for i, char in enumerate(expr):
            if char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
            elif char == ',' and depth == 0:
                return i
        return -1

    def evaluate_expression(self, expr: str, local_vars: Dict[str, Any]) -> Any:
        """
        对HLSL表达式求值
        expr: 表达式字符串
        local_vars: 局部变量字典
        返回: 求值结果
        """
        expr = expr.strip()
        if not expr:
            return None

        if expr == 'return':
            return None

        if expr.startswith('return '):
            return self.evaluate_expression(expr[7:], local_vars)

        # 使用语法树解析器处理所有表达式（包括三元运算符）
        tree = self.syntax_parser.parse(expr)

        # Print syntax tree
        if self.printSyntaxTree == True:
            self.debug_print(f"[SYNTAX TREE]\n{tree}")

        result = self.evaluate_syntax_tree(tree, local_vars)
        return result

    def _try_fma(self, node, local_vars):
        """If `node` is `(a*b) ± c` or `c + (a*b)`, evaluate it as a fused
        multiply-add — the product is NOT rounded to float32 before the add, so
        only the final result is rounded (one rounding, like the GPU's `mad`).
        Returns _NO_FMA when the shape isn't a fusable numeric multiply-add, so
        the caller falls back to the ordinary two-rounding path."""
        op, left, right = node.value, node.left, node.right
        mul, other, c_sign, c_on_left = None, None, 1.0, False
        if left is not None and left.node_type == 'binary_op' and left.value == '*':
            mul, other = left, right          # (a*b) ± c
            c_sign = 1.0 if op == '+' else -1.0
        elif (op == '+' and right is not None
              and right.node_type == 'binary_op' and right.value == '*'):
            mul, other = right, left           # c + (a*b)
        else:
            return _NO_FMA
        a = self.evaluate_syntax_tree(mul.left, local_vars)
        b = self.evaluate_syntax_tree(mul.right, local_vars)
        c = self.evaluate_syntax_tree(other, local_vars)
        return self._fma(a, b, c, c_sign)

    @staticmethod
    def _is_num(x):
        return isinstance(x, (int, float)) and not isinstance(x, bool)

    def _fma(self, a, b, c, c_sign):
        """Component-wise `a*b + c_sign*c` with a single float32 rounding.
        Scalars broadcast against vectors (as HLSL `*`/`+` do). Any non-numeric
        operand (matrix, None, unresolved value) → _NO_FMA fallback."""
        lens = {len(x) for x in (a, b, c) if isinstance(x, list)}
        if len(lens) > 1:
            return _NO_FMA
        if not lens:                                   # all scalar
            if not (self._is_num(a) and self._is_num(b) and self._is_num(c)):
                return _NO_FMA
            return self._to_f32(a * b + c_sign * c)
        n = lens.pop()
        av = a if isinstance(a, list) else [a] * n
        bv = b if isinstance(b, list) else [b] * n
        cv = c if isinstance(c, list) else [c] * n
        if not all(self._is_num(e) for e in (*av, *bv, *cv)):
            return _NO_FMA
        return [self._to_f32(av[i] * bv[i] + c_sign * cv[i]) for i in range(n)]

    def evaluate_syntax_tree(self, node: SyntaxTreeNode, local_vars: Dict[str, Any]) -> Any:
        """
        对语法树节点求值
        node: 语法树节点
        local_vars: 局部变量字典
        返回: 求值结果
        """

        if node is None:
            return None

        if node.node_type == 'value':
            if node.value is None:
                return None
            # Per-node fast accessor, resolved once (trees are memoized per
            # statement): numeric literals become constants; `name` /
            # `name.swz` for plain locals skip get_value's string dissection.
            # Anything else (cbuffers, buffers, struct members) keeps the full
            # get_value path.
            ev = getattr(node, '_ev', None)
            if ev is None:
                ev = self._build_value_accessor(node.value)
                node._ev = ev
            if ev is not False:
                hit, result = ev(local_vars)
                if hit:
                    return result
            result = self.get_value(node.value, local_vars)
            if ev is not False and getattr(ev, '_cbc', False):
                self._cb_static_cache[node.value.strip()] = (
                    list(result) if isinstance(result, list) else result)
            return result

        elif node.node_type == 'binary_op':
            if node.value in self._BITWISE_OPS:
                # A bit operation on a (int/uint) cast of a float vertex input
                # consumes the register's raw bits (3Dmigoto's packed-attribute
                # pattern, e.g. `(uint2)v1.zw >> 16`), so reinterpret the float32
                # bits. Outside a bit op the same cast is a plain ftoi/ftou value
                # conversion (e.g. `(int2)v1.zw` used as a texture coordinate).
                left = self._eval_bitwise_operand(node.left, local_vars)
                right = self._eval_bitwise_operand(node.right, local_vars)
                # DXBC ishr is an ARITHMETIC shift. 3Dmigoto renders both
                # ishr and ushr as `(cast)x >> n`; fix_shift_signedness
                # repairs the cast from the disasm, so an (int)-cast left
                # operand selects the sign-extending path here.
                if (node.value == '>>'
                        and getattr(node.left, 'node_type', None) == 'cast'
                        and str(node.left.value).startswith('int')):
                    def _sar(a, b):
                        ia = self._bitcast_to_int(a, True)
                        ia = ((ia & 0xFFFFFFFF) ^ 0x80000000) - 0x80000000
                        return ia >> (int(b) & 31)
                    if isinstance(left, list):
                        rl = right if isinstance(right, list) else [right] * len(left)
                        result = [_sar(l, r) for l, r in zip(left, rl)]
                    else:
                        result = _sar(left, right)
                    if self._dbg: self.debug_print(f"[BINARY OP] left={self._format_float(left)}, right={self._format_float(right)}, op=>> (arith), result={self._format_float(result)}")
                    return result
            else:
                # Fused multiply-add: the GPU runs `a*b + c` as one `mad` with a
                # SINGLE float32 rounding. Under float32 emulation, evaluating
                # `a*b` then `+ c` rounds twice and drifts from the GPU on
                # precision-sensitive chains (e.g. TombRaider's animated skinning
                # transform). Detect `(a*b) ± c` / `c + (a*b)` and fuse it.
                if self.f32_emulation and node.value in ('+', '-'):
                    fused = self._try_fma(node, local_vars)
                    if fused is not _NO_FMA:
                        return fused
                left = self.evaluate_syntax_tree(node.left, local_vars)
                right = self.evaluate_syntax_tree(node.right, local_vars)
                # Integer-op raw-bits rule: DXBC int arithmetic consumes
                # register raw bits, and 3Dmigoto renders the operands as
                # `(int)x`. When one side is a _RawBits value (bfrev result —
                # a genuine bit pattern) the other side's (int)float cast must
                # ALSO be the float's bit pattern, not ftoi(value): witcher's
                # noise hash iadds bfrev results with round_ni raw bits. The
                # partner cast already value-converted, so re-evaluate its
                # inner and bitcast (pure read, no side effects).
                if node.value in ('+', '-', '*'):
                    lraw = isinstance(left, _RawBits)
                    rraw = isinstance(right, _RawBits)
                    if lraw or rraw:
                        if lraw != rraw:
                            # The partner of a raw-bits operand in an int op is
                            # ALSO raw register bits. The decompiled `(int)`
                            # cast marking it is unreliable (the leading cast
                            # parses greedily around the whole sum), so any
                            # float partner is bit-reinterpreted; the partner
                            # cast may have already ftoi'd it, so re-read the
                            # inner expression when the partner is a cast.
                            onode = node.right if lraw else node.left
                            oval = right if lraw else left
                            if (getattr(onode, 'node_type', None) == 'cast'
                                    and str(onode.value) in self._INT_CAST_TYPES):
                                oval = self.evaluate_syntax_tree(onode.left, local_vars)
                            if isinstance(oval, float):
                                bits = self._bitcast_to_int(oval, True)
                                if lraw:
                                    right = bits
                                else:
                                    left = bits
                        # Pure 32-bit integer op — bypass execute_binary_op,
                        # whose f32_emulation rounding would turn the bit
                        # pattern into a float and break the raw chain.
                        try:
                            a, b = int(left), int(right)
                        except (TypeError, ValueError):
                            return self.execute_binary_op(node.value, left, right)
                        if node.value == '+':
                            res = a + b
                        elif node.value == '-':
                            res = a - b
                        else:
                            res = a * b
                        res = _RawBits(self._wrap_i32(res))
                        if self._dbg: self.debug_print(f"[BINARY OP] left={left}, right={right}, op={node.value} (raw i32), result={res}")
                        return res
            return self.execute_binary_op(node.value, left, right)

        elif node.node_type == 'unary_op':
            child = self.evaluate_syntax_tree(node.left, local_vars)
            return self.execute_unary_op(node.value, child)

        elif node.node_type == 'function':
            return self.execute_function_node(node, local_vars)

        elif node.node_type == 'method_call':
            return self.execute_method_call_node(node, local_vars)

        elif node.node_type == 'swizzle':
            # Component selection on a computed value (e.g. the result of a
            # method call: `tex.Load(...).y`).
            val = self.evaluate_syntax_tree(node.left, local_vars)
            if val is None:
                return None
            if not isinstance(val, list):
                val = [val]
            idx = {'x': 0, 'y': 1, 'z': 2, 'w': 3,
                   'r': 0, 'g': 1, 'b': 2, 'a': 3}
            comps = [val[idx[c]] if idx[c] < len(val) else 0.0
                     for c in str(node.value)]
            return comps[0] if len(comps) == 1 else comps

        elif node.node_type == 'ternary':
            cond = self.evaluate_syntax_tree(node.left, local_vars)
            # HLSL movc: a VECTOR condition selects PER COMPONENT — e.g.
            # r5 = [-1,-1,-1,0] picks the true-branch value for xyz and the
            # false-branch value for w. Collapsing it through _to_bool took
            # one branch for all lanes (witcher CSM cascade masks broke).
            if isinstance(cond, list):
                tv = self.evaluate_syntax_tree(node.right, local_vars)
                fv = self.evaluate_syntax_tree(node.third_child, local_vars)

                def _lane(src, i):
                    if isinstance(src, list):
                        if i < len(src):
                            return src[i]
                        return src[-1] if src else 0.0
                    return src
                return [
                    _lane(tv, i) if self._to_bool(c) else _lane(fv, i)
                    for i, c in enumerate(cond)
                ]
            if self._to_bool(cond):
                return self.evaluate_syntax_tree(node.right, local_vars)
            else:
                return self.evaluate_syntax_tree(node.third_child, local_vars)

        elif node.node_type == 'cast':
            inner = self.evaluate_syntax_tree(node.left, local_vars)
            if inner is None:
                return None
            cast_type = node.value
            # float3x3转换: 从4x4矩阵提取前3x3
            if cast_type == 'float3x3' and isinstance(inner, list) and len(inner) == 4:
                return [row[:3] for row in inner[:3]]
            # float2x2转换: 从4x4矩阵提取前2x2
            if cast_type == 'float2x2' and isinstance(inner, list) and len(inner) == 4:
                return [row[:2] for row in inner[:2]]
            # float2x2转换: 从3x3矩阵提取前2x2
            if cast_type == 'float2x2' and isinstance(inner, list) and len(inner) == 3:
                return [row[:2] for row in inner[:2]]
            # int/uint cast: ftoi/ftou value conversion (round toward zero).
            # A vertex-input attribute cast is a raw-bit reinterpret ONLY when it
            # feeds a bit operation; that case is handled in the binary_op branch
            # (_eval_bitwise_operand), so here we always value-convert — matching
            # `(int2)v1.zw` used directly as e.g. a texture coordinate.
            if cast_type in self._INT_CAST_TYPES:
                # D3D ftou clamps negative floats to 0 (witcher's noise hash
                # feeds reversebits((uint)floor(x)) with negative x — the GPU
                # sees 0 there, not the two's-complement pattern).
                is_unsigned = cast_type.startswith(('uint', 'dword'))

                def _to_int(v):
                    if v is None:
                        return 0
                    if isinstance(v, int):
                        return v
                    try:
                        f = float(v)
                    except (TypeError, ValueError):
                        return 0
                    if math.isnan(f) or math.isinf(f):
                        return 0
                    # A float in the denormal range at an int cast can only be
                    # raw integer BITS that round-tripped through float storage
                    # (GPUs run FTZ — real math never yields denormals). E.g.
                    # sekiro tbForce.x: uint 2 reads as 2.8e-45; (uint) must
                    # give 2, not 0, or the wind loop never runs.
                    if 0.0 < abs(f) < 1.1754943508222875e-38:
                        return self._bitcast_to_int(f, True)
                    if is_unsigned and f < 0.0:
                        return 0
                    return int(f)
                if isinstance(inner, list):
                    return [_to_int(v) for v in inner]
                return _to_int(inner)
            # float cast: 转换为浮点数
            if cast_type in ('float', 'float2', 'float3', 'float4'):
                if isinstance(inner, list):
                    return [float(v) for v in inner]
                return float(inner) if inner is not None else 0.0
            return inner

        return None

    def execute_function_node(self, node: SyntaxTreeNode, local_vars: Dict[str, Any]) -> Any:
        """
        执行函数调用语法树节点
        node: 函数调用节点
        local_vars: 局部变量字典
        返回: 函数执行结果
        """
        func_name = node.value
        args = node.args

        # transpose: 矩阵转置函数
        # 计算矩阵的转置，将行列互换
        if func_name == 'transpose':
            if len(args) != 1:
                self.debug_print(f"[ERROR] transpose requires 1 arg, got {len(args)} at line {node.line_number}")
                return None
            val = self.evaluate_syntax_tree(args[0], local_vars)
            if val is None:
                return None
            result = self.transpose_matrix(val)
            if self._dbg: self.debug_print(f"[FUNC] transpose(\n{self._format_value(val)}) =\n{self._format_value(result)}")
            return result

        # normalize: 向量归一化函数
        # 将输入向量缩放到单位长度，即长度为1
        elif func_name == 'normalize':
            if len(args) != 1:
                self.debug_print(f"[ERROR] normalize requires 1 arg, got {len(args)} at line {node.line_number}")
                return None
            val = self.evaluate_syntax_tree(args[0], local_vars)
            if val is None:
                return None
            if isinstance(val, list):
                result = self.normalize_vec(val)
                if self._dbg: self.debug_print(f"[FUNC] normalize({self._format_float(val)}) = {self._format_float(result)}")
                return result
            return val

        # length: 向量长度函数
        # 计算向量的欧几里得长度(模)
        elif func_name == 'length':
            if len(args) != 1:
                self.debug_print(f"[ERROR] length requires 1 arg, got {len(args)} at line {node.line_number}")
                return None
            val = self.evaluate_syntax_tree(args[0], local_vars)
            if val is None:
                return None
            result = self.length_vec(val)
            if self._dbg: self.debug_print(f"[FUNC] length({self._format_float(val)}) = {self._format_float(result)}")
            return result

        # dot: 向量点积函数
        # 计算两个向量的点积，结果为标量
        elif func_name == 'dot':
            if len(args) != 2:
                self.debug_print(f"[ERROR] dot requires 2 args, got {len(args)} at line {node.line_number}")
                return None
            a = self.evaluate_syntax_tree(args[0], local_vars)
            b = self.evaluate_syntax_tree(args[1], local_vars)
            if a is None or b is None:
                return None
            result = self.dot_product(a, b)
            if self._dbg: self.debug_print(f"[FUNC] dot({self._format_float(a)}, {self._format_float(b)}) = {self._format_float(result)}")
            return result

        # reflect: 反射向量函数
        # 计算光线关于法向量的反射向量，公式: R = I - 2 * N * dot(I, N)
        elif func_name == 'reflect':
            if len(args) != 2:
                self.debug_print(f"[ERROR] reflect requires 2 args, got {len(args)} at line {node.line_number}")
                return None
            I = self.evaluate_syntax_tree(args[0], local_vars)
            N = self.evaluate_syntax_tree(args[1], local_vars)
            if I is None or N is None:
                return None
            result = self.reflect_vec(I, N)
            if self._dbg: self.debug_print(f"[FUNC] reflect({self._format_float(I)}, {self._format_float(N)}) = {self._format_float(result)}")
            return result

        # max: 最大值函数 (支持标量和向量)
        elif func_name == 'max':
            if len(args) != 2:
                self.debug_print(f"[ERROR] max requires 2 args, got {len(args)} at line {node.line_number}")
                return None
            a = self.evaluate_syntax_tree(args[0], local_vars)
            b = self.evaluate_syntax_tree(args[1], local_vars)
            if a is None or b is None:
                return None
            if isinstance(a, list) and isinstance(b, list):
                result = [max(x, y) for x, y in zip(a, b)]
            elif isinstance(a, list):
                result = [max(x, b) for x in a]
            elif isinstance(b, list):
                result = [max(a, y) for y in b]
            else:
                result = max(a, b)
            if self._dbg: self.debug_print(f"[FUNC] max({self._format_float(a)}, {self._format_float(b)}) = {self._format_float(result)}")
            return result

        # min: 最小值函数 (支持标量和向量)
        elif func_name == 'min':
            if len(args) != 2:
                self.debug_print(f"[ERROR] min requires 2 args, got {len(args)} at line {node.line_number}")
                return None
            a = self.evaluate_syntax_tree(args[0], local_vars)
            b = self.evaluate_syntax_tree(args[1], local_vars)
            if a is None or b is None:
                return None
            if isinstance(a, list) and isinstance(b, list):
                result = [min(x, y) for x, y in zip(a, b)]
            elif isinstance(a, list):
                result = [min(x, b) for x in a]
            elif isinstance(b, list):
                result = [min(a, y) for y in b]
            else:
                result = min(a, b)
            if self._dbg: self.debug_print(f"[FUNC] min({self._format_float(a)}, {self._format_float(b)}) = {self._format_float(result)}")
            return result

        # rsqrt: 倒数平方根函数 1/sqrt(x)
        elif func_name == 'rsqrt':
            if len(args) != 1:
                return None
            val = self.evaluate_syntax_tree(args[0], local_vars)
            if val is None:
                return None
            if isinstance(val, list):
                result = [1.0 / math.sqrt(max(v, 1e-30)) if v > 0 else 0.0 for v in val]
            else:
                result = 1.0 / math.sqrt(max(val, 1e-30)) if val > 0 else 0.0
            if self._dbg: self.debug_print(f"[FUNC] rsqrt({self._format_float(val)}) = {self._format_float(result)}")
            return result

        # sqrt: 平方根函数
        elif func_name == 'sqrt':
            if len(args) != 1:
                return None
            val = self.evaluate_syntax_tree(args[0], local_vars)
            if val is None:
                return None
            if isinstance(val, list):
                result = [math.sqrt(max(v, 0.0)) for v in val]
            else:
                result = math.sqrt(max(val, 0.0))
            if self._dbg: self.debug_print(f"[FUNC] sqrt({self._format_float(val)}) = {self._format_float(result)}")
            return result

        # log2: 以2为底的对数
        elif func_name == 'log2':
            if len(args) != 1:
                return None
            val = self.evaluate_syntax_tree(args[0], local_vars)
            if val is None:
                return None
            if isinstance(val, list):
                result = [math.log2(max(v, 1e-30)) for v in val]
            else:
                result = math.log2(max(val, 1e-30)) if val > 0 else -1e30
            if self._dbg: self.debug_print(f"[FUNC] log2({self._format_float(val)}) = {self._format_float(result)}")
            return result

        # exp2: 2的幂次方
        elif func_name == 'exp2':
            if len(args) != 1:
                return None
            val = self.evaluate_syntax_tree(args[0], local_vars)
            if val is None:
                return None
            if isinstance(val, list):
                result = [self._safe_pow(2.0, v) for v in val]
            else:
                result = self._safe_pow(2.0, val)
            if self._dbg: self.debug_print(f"[FUNC] exp2({self._format_float(val)}) = {self._format_float(result)}")
            return result

        # rcp: 倒数 (1/x)。D3D 的 rcp 是近似指令，但硬件误差 << 容差；按精确
        # 1/x 以 float32 舍入实现。x=0 给 ±inf、NaN 透传，与 GPU 语义一致。
        elif func_name == 'rcp':
            if len(args) != 1:
                return None
            val = self.evaluate_syntax_tree(args[0], local_vars)
            if val is None:
                return None

            def _rcp1(x):
                try:
                    x = float(x)
                except (TypeError, ValueError):
                    return math.nan
                if math.isnan(x):
                    return math.nan
                if x == 0.0:
                    return math.copysign(math.inf, x)
                return self._f32(1.0 / x)

            result = [_rcp1(v) for v in val] if isinstance(val, list) else _rcp1(val)
            if self._dbg: self.debug_print(f"[FUNC] rcp({self._format_float(val)}) = {self._format_float(result)}")
            return result

        # floor/ceil/round/trunc/frac: 取整与小数部分函数
        # HLSL frac(x) = x - floor(x)（始终返回 [0,1) 的小数部分，对负数也成立）。
        # 这些在 3Dmigoto 反编译出的着色器里大量出现（如 frac() 周期函数、floor() 量化），
        # 缺失时会让整条表达式塌成 None，进而让整个 VS 输出为 0。
        elif func_name in ('floor', 'ceil', 'round', 'trunc', 'frac'):
            if len(args) != 1:
                return None
            val = self.evaluate_syntax_tree(args[0], local_vars)
            if val is None:
                return None
            if func_name == 'floor':
                op = math.floor
            elif func_name == 'ceil':
                op = math.ceil
            elif func_name == 'trunc':
                op = math.trunc
            elif func_name == 'round':
                # HLSL round() 采用就近偶数舍入(banker's rounding)，与 Python round 一致
                op = lambda v: float(round(v))
            else:  # frac
                op = lambda v: v - math.floor(v)
            # GPU semantics: floor/ceil/trunc/round/frac of NaN is NaN and of
            # ±inf is ±inf (frac(inf) is NaN); math.floor/ceil/trunc raise on
            # them instead — pass non-finite inputs through.
            base_op = op
            op = lambda v: (base_op(v) if math.isfinite(v)
                            else (float('nan') if func_name == 'frac' else v))
            if isinstance(val, list):
                result = [float(op(v)) for v in val]
            else:
                result = float(op(val))
            result = self._f32(result)
            if self._dbg: self.debug_print(f"[FUNC] {func_name}({self._format_float(val)}) = {self._format_float(result)}")
            return result

        # clamp: 限制范围函数
        elif func_name == 'clamp':
            if len(args) != 3:
                return None
            val = self.evaluate_syntax_tree(args[0], local_vars)
            lo = self.evaluate_syntax_tree(args[1], local_vars)
            hi = self.evaluate_syntax_tree(args[2], local_vars)
            if val is None:
                return None
            lo = lo if lo is not None else 0.0
            hi = hi if hi is not None else 1.0
            if isinstance(val, list):
                lo_v = lo if isinstance(lo, list) else [lo] * len(val)
                hi_v = hi if isinstance(hi, list) else [hi] * len(val)
                result = [max(min(v, h), l) for v, l, h in zip(val, lo_v, hi_v)]
            else:
                result = max(min(val, hi), lo)
            return result

        # saturate: 限制在[0,1]范围
        elif func_name == 'saturate':
            if len(args) != 1:
                return None
            val = self.evaluate_syntax_tree(args[0], local_vars)
            if val is None:
                return None
            if isinstance(val, list):
                result = [max(0.0, min(1.0, v)) for v in val]
            else:
                result = max(0.0, min(1.0, val))
            return result

        # lerp: 线性插值
        elif func_name == 'lerp':
            if len(args) != 3:
                return None
            a = self.evaluate_syntax_tree(args[0], local_vars)
            b = self.evaluate_syntax_tree(args[1], local_vars)
            t = self.evaluate_syntax_tree(args[2], local_vars)
            if a is None or b is None or t is None:
                return None
            if isinstance(a, list) and isinstance(b, list):
                t_v = t if isinstance(t, list) else [t] * len(a)
                result = [av + (bv - av) * tv for av, bv, tv in zip(a, b, t_v)]
            else:
                result = a + (b - a) * t
            return result

        # mad: multiply-add, mad(a,b,c) = a*b + c, component-wise with scalar
        # broadcasting. Pervasive in 3Dmigoto output, including integer index
        # math like `mad((int2)v1.xx, int2(36,36), int2(1,3))` that computes
        # StructuredBuffer row indices; unimplemented it returned None and
        # poisoned the whole transform (garbage SV_POSITION).
        elif func_name == 'mad':
            if len(args) != 3:
                self.debug_print(f"[ERROR] mad requires 3 args, got {len(args)} at line {node.line_number}")
                return None
            a = self.evaluate_syntax_tree(args[0], local_vars)
            b = self.evaluate_syntax_tree(args[1], local_vars)
            c = self.evaluate_syntax_tree(args[2], local_vars)
            if a is None or b is None or c is None:
                return None
            lists = [x for x in (a, b, c) if isinstance(x, list)]
            if lists:
                n = max(len(x) for x in lists)

                def _comp(x, i):
                    if isinstance(x, list):
                        return x[i] if i < len(x) else x[-1]
                    return x
                result = [_comp(a, i) * _comp(b, i) + _comp(c, i) for i in range(n)]
            else:
                result = a * b + c
            result = self._f32(result)
            if self._dbg: self.debug_print(f"[FUNC] mad(...) = {self._format_float(result)}")
            return result

        # int2/int3/int4: 整数向量构造
        elif func_name in ('int2', 'int3', 'int4', 'uint2', 'uint3', 'uint4'):
            result = []
            for arg in args:
                val = self.evaluate_syntax_tree(arg, local_vars)
                if isinstance(val, list):
                    result.extend(int(v) for v in val)
                elif val is not None:
                    result.append(int(val))
            if self._dbg: self.debug_print(f"[FUNC] {func_name}(...) = {result}")
            return result

        # pow: 幂函数
        # 计算base的exp次幂，即 base ^ exp
        elif func_name == 'pow':
            if len(args) != 2:
                self.debug_print(f"[ERROR] pow requires 2 args, got {len(args)} at line {node.line_number}")
                return None
            base = self.evaluate_syntax_tree(args[0], local_vars)
            exp = self.evaluate_syntax_tree(args[1], local_vars)
            if base is None or exp is None:
                return None
            if isinstance(base, list) or isinstance(exp, list):
                n = max(len(base) if isinstance(base, list) else 1,
                        len(exp) if isinstance(exp, list) else 1)

                def _c(x, i):
                    return x[i] if isinstance(x, list) else x
                result = [self._safe_pow(_c(base, i), _c(exp, i)) for i in range(n)]
            else:
                result = self._safe_pow(base, exp)
            if self._dbg: self.debug_print(f"[FUNC] pow({self._format_float(base)}, {self._format_float(exp)}) = {self._format_float(result)}")
            return result

        # abs: 绝对值函数
        # 返回数值的绝对值，对列表则对每个元素取绝对值
        elif func_name == 'abs':
            if len(args) != 1:
                self.debug_print(f"[ERROR] abs requires 1 arg, got {len(args)} at line {node.line_number}")
                return None
            val = self.evaluate_syntax_tree(args[0], local_vars)
            if val is None:
                return None
            if isinstance(val, list):
                result = [abs(v) for v in val]
            else:
                result = abs(val)
            if self._dbg: self.debug_print(f"[FUNC] abs({self._format_float(val)}) = {self._format_float(result)}")
            return result

        # asint/asuint/asfloat: bit-pattern reinterpretation (no value conversion).
        # 3Dmigoto uses these for integer/flag packing, e.g.
        # `o2.x = (int)v2.x + asint(cb0[0].x)`. We reinterpret the raw 32-bit
        # pattern, matching HLSL semantics; unimplemented they returned None and
        # poisoned the whole expression.
        elif func_name in ('asint', 'asuint', 'asfloat'):
            if len(args) != 1:
                self.debug_print(f"[ERROR] {func_name} requires 1 arg, got {len(args)} at line {node.line_number}")
                return None
            arg0 = args[0]
            # 3Dmigoto renders an INTEGER negate source modifier (disasm
            # `iadd r0, r0, -cb2[0].yxyx`) as asint(-cbN[i].sw). Evaluated
            # literally that float-negates first, so a raw 0 becomes -0.0 and
            # asint returns 0x80000000 — poisoning integer index math (the
            # Octopath terrain LOD chain). The GPU semantics are integer
            # negation applied AFTER the bit reinterpretation: -asint(x).
            int_negate = False
            if func_name in ('asint', 'asuint') and arg0 is not None and arg0.value == '-':
                # Unary minus parses as unary_op OR as binary_op with an
                # empty-value left node.
                left = getattr(arg0, 'left', None)
                if arg0.node_type == 'unary_op':
                    int_negate = True
                    arg0 = left
                elif (arg0.node_type == 'binary_op'
                      and (left is None or (getattr(left, 'node_type', None) == 'value'
                                            and left.value is None))):
                    int_negate = True
                    arg0 = arg0.right
            # For asint/asuint of a direct cbuffer component, use the exact int32
            # bit pattern from the binary load: a float cannot round-trip values
            # like -1 (stored as NaN) through struct re-packing.
            if func_name in ('asint', 'asuint'):
                raw = self._cbuffer_component_raw_int(arg0)
                if raw is not None:
                    if isinstance(raw, list):
                        result = [-v for v in raw] if int_negate else list(raw)
                        if func_name == 'asuint':
                            result = [v & 0xFFFFFFFF for v in result]
                    else:
                        result = -raw if int_negate else raw
                        if func_name == 'asuint':
                            result &= 0xFFFFFFFF
                    if self._dbg: self.debug_print(f"[FUNC] {func_name}(cbuffer raw) = {result}")
                    return result
            val = self.evaluate_syntax_tree(arg0, local_vars)
            if val is None:
                return None

            def _reinterpret(x):
                try:
                    if func_name == 'asint':
                        return struct.unpack('<i', struct.pack('<f', float(x)))[0]
                    if func_name == 'asuint':
                        return struct.unpack('<I', struct.pack('<f', float(x)))[0]
                    # asfloat: reinterpret an integer bit pattern as float32.
                    xi = int(x)
                    packer = '<i' if xi < 0 else '<I'
                    return struct.unpack('<f', struct.pack(packer, xi & 0xFFFFFFFF if xi >= 0 else xi))[0]
                except (struct.error, ValueError, OverflowError):
                    return x

            result = [_reinterpret(v) for v in val] if isinstance(val, list) else _reinterpret(val)
            if int_negate:
                if isinstance(result, list):
                    result = [-v for v in result]
                    if func_name == 'asuint':
                        result = [v & 0xFFFFFFFF for v in result]
                else:
                    result = -result
                    if func_name == 'asuint':
                        result &= 0xFFFFFFFF
            if self._dbg: self.debug_print(f"[FUNC] {func_name}({self._format_float(val)}) = {self._format_float(result)}")
            return result

        # f16tof32 / f32tof16: half-float (un)packing. 3Dmigoto packs two halfs
        # per 32-bit lane; shaders read e.g. f16tof32((uint)v.zw >> 16). The
        # input/output is the raw 16-bit pattern in the low bits.
        elif func_name in ('f16tof32', 'f32tof16'):
            if len(args) != 1:
                return None
            val = self.evaluate_syntax_tree(args[0], local_vars)
            if val is None:
                return None

            def _conv(x):
                try:
                    if func_name == 'f16tof32':
                        # f16tof32 takes the low 16 bits of a uint register. A
                        # float operand (e.g. a packed-half vertex attribute used
                        # directly) is reinterpreted by its float32 bits, not
                        # truncated; an int is already the raw bits.
                        bits = self._bitcast_to_int(x, False) if isinstance(x, float) else int(x)
                        return struct.unpack('<e', struct.pack('<H', bits & 0xFFFF))[0]
                    # D3D's f32tof16 rounds TOWARD ZERO (struct.pack '<e' would
                    # round to nearest-even and lands one ULP high half the
                    # time — sekiro4 packed GS colors differ by exactly 1 in
                    # the f16 bits against the golden).
                    return self._f32_to_f16_rtz(float(x))
                except (struct.error, ValueError, OverflowError):
                    return 0.0 if func_name == 'f16tof32' else 0
            result = [_conv(v) for v in val] if isinstance(val, list) else _conv(val)
            result = self._f32(result) if func_name == 'f16tof32' else result
            if self._dbg: self.debug_print(f"[FUNC] {func_name}({self._format_float(val)}) = {self._format_float(result)}")
            return result

        # sin: 正弦函数
        # 计算弧度的正弦值，对列表则对每个元素计算
        elif func_name == 'reversebits':
            # DXBC bfrev: reverse the 32 RAW BITS of the register. The disasm
            # applies it directly to a float register (round_ni result) with
            # NO ftou — 3Dmigoto's `reversebits((uint)rX)` cast is a BITCAST
            # notation, not a value conversion. So strip an int-cast argument
            # and reverse the float32 bit pattern of the inner value (an int
            # value is already raw bits). Unimplemented, the statement
            # silently kept the register's OLD value and the witcher noise
            # hash decorrelated from golden.
            arg_node = args[0] if args else None
            if (arg_node is not None and arg_node.node_type == 'cast'
                    and str(arg_node.value) in self._INT_CAST_TYPES):
                arg_node = arg_node.left
            val = self.evaluate_syntax_tree(arg_node, local_vars)

            def _brev(v):
                iv = self._bitcast_to_int(v, False) & 0xFFFFFFFF
                r = int(('{:032b}'.format(iv))[::-1], 2)
                return _RawBits(r - 0x100000000 if r >= 0x80000000 else r)
            result = [_brev(v) for v in val] if isinstance(val, list) else _brev(val)
            if self._dbg: self.debug_print(f"[FUNC] reversebits({self._format_value(val)}) = {result}")
            return result

        elif func_name == 'sin':
            if len(args) != 1:
                self.debug_print(f"[ERROR] sin requires 1 arg, got {len(args)} at line {node.line_number}")
                return None
            val = self.evaluate_syntax_tree(args[0], local_vars)
            if val is None:
                return None
            if isinstance(val, list):
                result = [self._sin(v) for v in val]
            else:
                result = self._sin(val)
            if self._dbg: self.debug_print(f"[FUNC] sin({self._format_float(val)}) = {self._format_float(result)}")
            return result

        # cos: 余弦函数
        # 计算弧度的余弦值，对列表则对每个元素计算
        elif func_name == 'cos':
            if len(args) != 1:
                self.debug_print(f"[ERROR] cos requires 1 arg, got {len(args)} at line {node.line_number}")
                return None
            val = self.evaluate_syntax_tree(args[0], local_vars)
            if val is None:
                return None
            if isinstance(val, list):
                result = [self._cos(v) for v in val]
            else:
                result = self._cos(val)
            if self._dbg: self.debug_print(f"[FUNC] cos({self._format_float(val)}) = {self._format_float(result)}")
            return result

        # mul: 矩阵乘法函数
        # 执行4x4或3x3矩阵乘法运算
        elif func_name == 'mul':
            if len(args) != 2:
                self.debug_print(f"[ERROR] mul requires 2 args, got {len(args)} at line {node.line_number}")
                return None
            left = self.evaluate_syntax_tree(args[0], local_vars)
            right = self.evaluate_syntax_tree(args[1], local_vars)
            if left is None or right is None:
                return None
            if isinstance(left, list) and isinstance(right, list):
                if len(left) == 4 and len(right) == 4:
                    result = self.mul_matrix_vector(right, left)
                    if self._dbg: self.debug_print(f"[FUNC] mul(\nleft={self._format_value(left)},\nright={self._format_value(right)}) =\n{self._format_value(result)}")
                    return result
                elif len(left) == 3 and len(right) == 3:
                    result = self.mul_matrix_vector(right, left)
                    if self._dbg: self.debug_print(f"[FUNC] mul(\nleft={self._format_value(left)},\nright={self._format_value(right)}) =\n{self._format_value(result)}")
                    return result
            return None

        # float2/float3/float4: 向量构造函数
        # 将参数展平合并为指定长度的向量
        elif func_name in ['float2', 'float3', 'float4']:
            # 向量构造函数: 将参数展平合并
            result = []
            for arg in args:
                val = self.evaluate_syntax_tree(arg, local_vars)
                if isinstance(val, list):
                    result.extend(val)
                else:
                    result.append(val)
            if self._dbg: self.debug_print(f"[FUNC] {func_name}(args={self._format_float(args)}) = {self._format_float(result)}")
            return result

        # Texture.Sample: 纹理采样函数
        # 格式: DiffuseTexture.Sample(LinearSampler, input.TexCoord)
        # DiffuseTexture 是 Texture2D，LinearSampler 是 SamplerState
        elif func_name == 'Sample' and len(args) == 2:
            if len(node.args) < 1:
                return None
            texture_node = node.args[0]
            texture_name = texture_node.value if texture_node and texture_node.node_type == 'value' else None
            if texture_name:
                sampler_node = args[0] if isinstance(args[0], SyntaxTreeNode) else None
                sampler_name = (sampler_node.value
                                if sampler_node is not None and sampler_node.node_type == 'value'
                                else None)
                coords_node = args[1] if len(args) > 1 else None
                coords = self.evaluate_syntax_tree(coords_node, local_vars) if coords_node else None
                if coords and isinstance(coords, list) and len(coords) >= 2:
                    u, v = coords[0], coords[1]
                    binding = self._find_texture_binding(texture_name)
                    # For an array resource the 3rd coord is the slice index, not
                    # an explicit LOD, so don't feed it in as w.
                    array_slice = self._array_slice_for(binding, coords)
                    w = 0.0 if array_slice else (coords[2] if len(coords) > 2 else 0.0)
                    if binding and self._texture_exec and self._texture_desc_list:
                        reg_id = binding.register_id
                        if reg_id < len(self._texture_desc_list) and self._texture_desc_list[reg_id]:
                            texture_desc = self._texture_desc_list[reg_id]
                            sampler = self._resolve_sampler(sampler_name, reg_id)
                            ddx_uv, ddy_uv = self._compute_uv_derivatives(coords_node, local_vars)
                            result = self._texture_exec.sample(u, v, w, texture_desc, sampler, ddx_uv, ddy_uv, name=texture_name, array_slice=array_slice)
                            if self._dbg: self.debug_print(f"[FUNC] {texture_name}.Sample({sampler_name}, ({u:.4f}, {v:.4f})) = {self._format_float(result)}")
                            return result
            return None

        return None

    def execute_method_call_node(self, node: SyntaxTreeNode, local_vars: Dict[str, Any]) -> Any:
        """
        执行方法调用语法树节点 (如 Texture.Sample)
        node: 方法调用节点
        local_vars: 局部变量字典
        返回: 方法执行结果
        """
        method_name = node.value
        obj_node = node.left
        args = node.args

        if method_name == 'Sample' and len(args) == 2:
            if obj_node is None:
                return None
            obj_name = obj_node.value if obj_node.node_type == 'value' else None
            if obj_name is None:
                return None
            sampler_node = args[0]
            sampler_name = (sampler_node.value
                            if sampler_node is not None and sampler_node.node_type == 'value'
                            else None)
            coords = self.evaluate_syntax_tree(args[1], local_vars)
            if coords and isinstance(coords, list) and len(coords) >= 2:
                u, v = coords[0], coords[1]
                binding = self._find_texture_binding(obj_name)
                array_slice = self._array_slice_for(binding, coords)
                w = 0.0 if array_slice else (coords[2] if len(coords) > 2 else 0.0)
                if binding and self._texture_exec and self._texture_desc_list:
                    reg_id = binding.register_id
                    if reg_id < len(self._texture_desc_list) and self._texture_desc_list[reg_id]:
                        texture_desc = self._texture_desc_list[reg_id]
                        sampler = self._resolve_sampler(sampler_name, reg_id)
                        if self._is_volume_texture(binding, texture_desc):
                            wc = coords[2] if len(coords) > 2 else 0.0
                            result = self._texture_exec.sample_volume(
                                u, v, wc, texture_desc, sampler)
                            self.debug_print(
                                f"[METHOD] {obj_name}.Sample({sampler_name}, "
                                f"({u:.4f}, {v:.4f}, {wc:.4f})) [3D] = "
                                f"{self._format_float(result)}")
                            return result
                        ddx_uv, ddy_uv = self._compute_uv_derivatives(args[1], local_vars)
                        result = self._texture_exec.sample(u, v, w, texture_desc, sampler, ddx_uv, ddy_uv, name=obj_name, array_slice=array_slice)
                        if self._dbg: self.debug_print(f"[METHOD] {obj_name}.Sample({sampler_name}, ({u:.4f}, {v:.4f})) = {self._format_float(result)}")
                        return result
            return None

        # Texture.SampleLevel: sample at an EXPLICIT LOD (no derivatives).
        # Format: tex.SampleLevel(sampler, coords, lod). Pervasive in vertex
        # shaders (which have no screen-space derivatives) — e.g. Witcher3's
        # ambient/shadow probes sample t22/t19 in the VS. Unhandled, it returned
        # None and silently zeroed the sampled term. coords carries u,v (and for
        # Texture2DArray/Cube a 3rd/4th array-slice or direction component we
        # approximate by sampling slice 0); the LOD is the trailing scalar arg,
        # passed through as the explicit-LOD `w` (derivatives None).
        if method_name == 'SampleLevel' and len(args) >= 2:
            if obj_node is None:
                return None
            obj_name = obj_node.value if obj_node.node_type == 'value' else None
            if obj_name is None:
                return None
            sampler_node = args[0]
            sampler_name = (sampler_node.value
                            if sampler_node is not None and sampler_node.node_type == 'value'
                            else None)
            coords = self.evaluate_syntax_tree(args[1], local_vars)
            lod = self.evaluate_syntax_tree(args[2], local_vars) if len(args) > 2 else 0.0
            if isinstance(lod, list):
                lod = lod[0] if lod else 0.0
            if coords and isinstance(coords, list) and len(coords) >= 2:
                u, v = coords[0], coords[1]
                binding = self._find_texture_binding(obj_name)
                if binding and self._texture_exec and self._texture_desc_list:
                    reg_id = binding.register_id
                    if reg_id < len(self._texture_desc_list) and self._texture_desc_list[reg_id]:
                        texture_desc = self._texture_desc_list[reg_id]
                        sampler = self._resolve_sampler(sampler_name, reg_id)
                        if self._is_volume_texture(binding, texture_desc):
                            wc = coords[2] if len(coords) > 2 else 0.0
                            result = self._texture_exec.sample_volume(
                                u, v, wc, texture_desc, sampler)
                            self.debug_print(
                                f"[METHOD] {obj_name}.SampleLevel({sampler_name}, "
                                f"({u:.4f}, {v:.4f}, {wc:.4f}), {lod}) [3D] = "
                                f"{self._format_float(result)}")
                            return result
                        array_slice = self._array_slice_for(binding, coords)
                        result = self._texture_exec.sample(
                            u, v, float(lod or 0.0), texture_desc, sampler,
                            None, None, name=obj_name, array_slice=array_slice)
                        self.debug_print(
                            f"[METHOD] {obj_name}.SampleLevel({sampler_name}, "
                            f"({u:.4f}, {v:.4f}), {lod}) = {self._format_float(result)}")
                        return result
            return None

        # SampleCmp / SampleCmpLevelZero: shadow-map PCF compare sample.
        # Format: tex.SampleCmpLevelZero(cmp_sampler, coords, ref). For a
        # Texture2DArray the trailing coord component is the array slice.
        # Returns the bilinear blend of per-neighbour compare results.
        if method_name in ('SampleCmpLevelZero', 'SampleCmp') and len(args) >= 3:
            if obj_node is None:
                return None
            obj_name = obj_node.value if obj_node.node_type == 'value' else None
            if obj_name is None:
                return None
            sampler_node = args[0]
            sampler_name = (sampler_node.value
                            if sampler_node is not None and sampler_node.node_type == 'value'
                            else None)
            coords = self.evaluate_syntax_tree(args[1], local_vars)
            ref = self.evaluate_syntax_tree(args[2], local_vars)
            if isinstance(ref, list):
                ref = ref[0] if ref else 0.0
            if coords and isinstance(coords, list) and len(coords) >= 2:
                u, v = coords[0], coords[1]
                binding = self._find_texture_binding(obj_name)
                if binding and self._texture_exec and self._texture_desc_list:
                    reg_id = binding.register_id
                    if reg_id < len(self._texture_desc_list) and self._texture_desc_list[reg_id]:
                        texture_desc = self._texture_desc_list[reg_id]
                        sampler = self._resolve_sampler(sampler_name, reg_id)
                        array_slice = self._array_slice_for(binding, coords)
                        result = self._texture_exec.sample_cmp_lz(
                            u, v, float(ref or 0.0), texture_desc, sampler,
                            array_slice=array_slice)
                        self.debug_print(
                            f"[METHOD] {obj_name}.{method_name}({sampler_name}, "
                            f"({u:.4f}, {v:.4f}), ref={ref}) = {result:.4f}")
                        return result
            return None

        # Texture2D.Load: integer texel fetch, no filtering.
        # Format: t1.Load(int3(x, y, mip)).  location.xy = texel coords,
        # location.z = mip level.
        if method_name == 'Load' and len(args) >= 1:
            if obj_node is None:
                return None
            obj_name = obj_node.value if obj_node.node_type == 'value' else None
            if obj_name is None:
                return None
            location = self.evaluate_syntax_tree(args[0], local_vars)
            # Typed buffer (Buffer<T>): Load(int index) -> element[index].
            if obj_name in self.typed_buffers:
                idx = location[0] if isinstance(location, list) else location
                idx = self._load_coord_to_int(idx)
                result = self._typed_buffer_load(self.typed_buffers[obj_name], idx)
                if self._dbg: self.debug_print(f"[METHOD] {obj_name}.Load({idx}) = {self._format_float(result)}")
                return result
            if not isinstance(location, list):
                location = [location]
            x = self._load_coord_to_int(location[0]) if len(location) > 0 else 0
            y = self._load_coord_to_int(location[1]) if len(location) > 1 else 0
            mip = self._load_coord_to_int(location[2]) if len(location) > 2 else 0
            binding = self._find_texture_binding(obj_name)
            if binding and self._texture_exec and self._texture_desc_list:
                reg_id = binding.register_id
                if reg_id < len(self._texture_desc_list) and self._texture_desc_list[reg_id]:
                    texture_desc = self._texture_desc_list[reg_id]
                    result = self._texture_exec.load(x, y, mip, texture_desc)
                    self.debug_print(
                        f"[METHOD] {obj_name}.Load(({x}, {y}, {mip})) = {self._format_float(result)}")
                    return result
            return None

        self.debug_print(f"[ERROR] Unknown method: {method_name}")
        return None

    _CB_COMPONENT_RE = re.compile(r'^(cb\d+)\[(\d+)\]\.([xyzw]{1,4})$')
    _CB_COMP_IDX = {'x': 0, 'y': 1, 'z': 2, 'w': 3}

    def _cbuffer_component_raw_int(self, node):
        """If `node` is a literal cbuffer access (cbN[i].c or a 2-4 lane
        swizzle cbN[i].sw) whose exact int32 bits were captured by
        override_cbuffers_from_binary, return that signed int (scalar) or
        list of ints (swizzle); otherwise None. Lets asint/asuint recover
        integers that do not survive the float round-trip: small ints are
        stored as denormals the CSV rounds to 0.0 — the Octopath terrain
        tile index asint(cb3[0].yx) read [0,0] instead of [1,7]."""
        if node is None or getattr(node, 'node_type', None) != 'value':
            return None
        m = self._CB_COMPONENT_RE.match(str(node.value).strip())
        if not m:
            return None
        rows = self._cb_raw.get(m.group(1))
        if not rows:
            return None
        idx = int(m.group(2))
        if idx >= len(rows):
            return None
        sw = m.group(3)
        if len(sw) == 1:
            return rows[idx][self._CB_COMP_IDX[sw]]
        return [rows[idx][self._CB_COMP_IDX[c]] for c in sw]

    def _struct_member_access(self, field, elem_idx, rest, local_vars=None):
        """Resolve `NAME[elem_idx]<rest>` for a struct{...} NAME[N] cbuffer field.

        `rest` is the text after the subscript: '' / '._mRC' / '.member' /
        '.member.swizzle' / '.member[k].swizzle'. Named members are sliced from
        the element's register rows using the layout captured in parse_cbuffer;
        uint/int/bool members read the exact integer bits (struct_int_data) so
        they can be used as indices. Returns None to fall back to default handling
        (e.g. a bare `._mRC` matrix accessor on the whole element)."""
        members = getattr(field, 'struct_members', None)
        frows = field.data[elem_idx] if field.data and elem_idx < len(field.data) else None
        irows = (field.struct_int_data[elem_idx]
                 if field.struct_int_data and elem_idx < len(field.struct_int_data) else None)
        if not rest.startswith('.') or not members or frows is None:
            return None
        accessor = rest[1:]
        m = re.match(r'^([A-Za-z_]\w*)(?:\[([^\]]+)\])?(?:\.(\w+))?$', accessor)
        if not m:
            return None
        mname, msub, mswz = m.group(1), m.group(2), m.group(3)
        layout = next((x for x in members if x[0] == mname), None)
        if layout is None:
            return None  # not a named member (likely an _mRC accessor) → fall back
        _, mtype, reg_off, comp_off, m_arr = layout
        # Array member element select: member[k] → advance by k register-aligned slots.
        if msub is not None:
            try:
                k = self._eval_subscript(msub, local_vars if local_vars is not None else {})
            except Exception:
                k = 0
            esz = self._CB_TYPE_BYTES.get(mtype, 16)
            reg_off += k * ((esz + 15) // 16)
        is_int = mtype.startswith(('uint', 'int', 'bool', 'dword'))
        rows = irows if (is_int and irows is not None) else frows
        if reg_off >= len(rows):
            return None
        base = self._CB_TYPE_BYTES.get(mtype, 16)
        if base > 16:  # matrix member → list of its register rows
            nregs = (base + 15) // 16
            val = [rows[reg_off + r] for r in range(nregs) if reg_off + r < len(rows)]
        else:
            ncomp = max(1, min(4, base // 4))
            row = rows[reg_off]
            val = row[comp_off:comp_off + ncomp]
            if len(val) == 1:
                val = val[0]
        if mswz:
            return self.apply_swizzle(val, mswz)
        return val

    def recover_struct_array_matrix_selectors(self, code: str, disasm_text: str) -> str:
        """Re-inject the matrix-member selector that 3Dmigoto drops for a
        struct-array cbuffer.

        For `cbuffer B { struct { row_major float4x4 A, Bm, C; } Arr[N]; }` the
        decompile emits `Arr[i]._m10_m11_m12_m13` with the member (.A/.Bm/.C)
        MISSING — ambiguous, because the GPU may read any of the three. The
        exact instruction stream (VS_shader_disasm.txt) is unambiguous: each such
        access reads one register row via `cb<slot>[<reg> + N]`, so the member's
        element-relative base register is `N - R` (R = the `_mR.` row). We pair
        the member-less HLSL accesses with the disasm reads positionally (both in
        program order, verified 1:1 on TombRaider) and rewrite `Arr[i]._mR...`
        into `Arr[i].<member>._mR...`, after which the normal named-member path
        resolves it correctly. This is the only way to recover the selector — it
        is genuinely absent from the decompiled HLSL.

        No-op when: there is no disasm, no struct-array with >=2 matrix members,
        or the HLSL-access vs disasm-read counts do not reconcile 1:1 (so we
        never corrupt a shader we cannot align)."""
        # struct { float4x4 A, B, C; } Arr[N] -> the decompile emits Arr[i]._mR..
        # with the member (.A/.B/.C) dropped; recovered per-access from the
        # disasm. (The other loss pattern — struct { float4x4 M[K]; } Arr[N]
        # flattened to Arr[flat]._mR.. — is handled at runtime in get_value,
        # because injecting `.M[inner]` here hits an expression-parser limit on
        # `Arr[i].Member[k]._mRC`.)
        if not disasm_text:
            return code
        multi = {}   # name -> (cb_slot, {elem_reg_off: member_name})
        for cb_def in self.cbuffers.values():
            if not isinstance(cb_def, CbufferDefinition):
                continue
            for f in cb_def.fields:
                if getattr(f, 'field_type', '') != '__struct__':
                    continue
                members = getattr(f, 'struct_members', None) or []
                mat = {ro: nm for (nm, mt, ro, co, ar) in members
                       if self._CB_TYPE_BYTES.get(mt, 0) > 16}
                if len(mat) >= 2:
                    multi[f.name] = (cb_def.register, mat)
        if not multi:
            return code
        for name, (slot, mat) in multi.items():
            offs = [int(x) for x in re.findall(
                r'cb%d\[\w+(?:\.\w+)?\s*\+\s*(\d+)\]' % slot, disasm_text)]
            if not offs:
                continue
            pat = re.compile(
                re.escape(name) + r'\s*\[[^\]]*\]\s*\.(_m(\d)\d(?:_m\d\d)*)')
            occ = list(pat.finditer(code))
            if len(occ) != len(offs):
                self.log_output(
                    f"Warning: cannot recover '{name}' matrix selectors "
                    f"({len(occ)} HLSL accesses vs {len(offs)} disasm cb{slot} "
                    f"reads do not align) — leaving source unchanged.")
                continue
            out, last = [], 0
            for m, n in zip(occ, offs):
                member = mat.get(n - int(m.group(2)))
                if member is None:
                    continue
                dot = m.start(1) - 1            # the '.' before `_mR...`
                out.append(code[last:dot])
                out.append('.' + member)
                last = dot
            out.append(code[last:])
            code = ''.join(out)
            self.log_output(
                f"Recovered {len(occ)} dropped matrix-member selector(s) for "
                f"struct-array '{name}' from disasm.")
        return code

    def apply_swizzle(self, obj: Any, swizzle: str) -> Any:
        """
        对向量应用swizzle操作
        obj: 向量对象(列表)
        swizzle: swizzle模式字符串，如 'xyz', 'xxx', 'xxyy', 'xz' 等
        返回: 应用swizzle后的结果
        """
        if obj is None:
            return None

        if not isinstance(obj, list):
            # A scalar has a single component; HLSL allows replicating it with
            # .x / .xx / .xxx / .xxxx (and the .r colour-channel alias), e.g.
            # `(int2)v1.xx` on a scalar uint. Anything else is invalid -> None.
            if swizzle and all(c.lower() in ('x', 'r') for c in swizzle):
                return obj if len(swizzle) == 1 else [obj] * len(swizzle)
            return None

        # _mRC accessor on a matrix element (list of float4 rows): _m00_m10_m20_m30
        if (swizzle and swizzle.startswith('_m') and obj
                and isinstance(obj[0], list)):
            accessor_parts = [p for p in swizzle.split('_') if p and p[0] == 'm' and len(p) == 3]
            result = []
            for ap in accessor_parts:
                try:
                    r, c = int(ap[1]), int(ap[2])
                    result.append(obj[r][c] if r < len(obj) and c < len(obj[r]) else 0.0)
                except (ValueError, IndexError):
                    result.append(0.0)
            if result:
                return result[0] if len(result) == 1 else result
            return 0.0

        result = []
        for c in swizzle:
            if c.lower() in self._SWIZZLE_MAP:
                idx = self._SWIZZLE_MAP[c.lower()]
                result.append(obj[idx] if idx < len(obj) else 0)
            elif c in 'rgb':
                idx = {'r': 0, 'g': 1, 'b': 2}[c]
                result.append(obj[idx] if idx < len(obj) else 0)

        if len(result) == 1:
            return result[0]

        numeric_types = (int, float)
        if all(isinstance(v, numeric_types) for v in result):
            return [int(v) for v in result] if all(isinstance(v, int) for v in result) else result

        return result

    _SWIZZLE_MAP = {'x': 0, 'y': 1, 'z': 2, 'w': 3}

    _SWZ_IDX = {'x': 0, 'y': 1, 'z': 2, 'w': 3, 'r': 0, 'g': 1, 'b': 2, 'a': 3}

    def _build_value_accessor(self, name: str):
        """Build a fast accessor for a value-node name, or False when the name
        needs the full get_value path. The accessor returns (hit, value); a
        miss (base var absent / shape mismatch) falls back to get_value."""
        name = name.strip()
        # Numeric literal → constant (rounding matches get_value's _f32 path).
        try:
            const = self._f32(float(name))
            return lambda lv, _c=const: (True, _c)
        except (ValueError, TypeError):
            pass
        if re.match(r'^[A-Za-z_]\w*$', name):
            def _plain(lv, _n=name):
                v = lv.get(_n, _MISS)
                if v is _MISS:
                    return (False, None)
                return (True, v)
            return _plain
        m = re.match(r'^([A-Za-z_]\w*)\.([xyzwrgba]{1,4})$', name)
        if m:
            base = m.group(1)
            idxs = tuple(self._SWZ_IDX[c] for c in m.group(2).lower())
            single = len(idxs) == 1

            def _swz(lv, _b=base, _i=idxs, _s=single):
                v = lv.get(_b, _MISS)
                if v is _MISS or not isinstance(v, list):
                    return (False, None)
                n = len(v)
                if _s:
                    i = _i[0]
                    return (True, v[i]) if i < n else (False, None)
                if _i[-1] < n and max(_i) < n:
                    return (True, [v[i] for i in _i])
                return (False, None)
            return _swz
        # Literal-indexed cbuffer reference (cb0[24].xyzw / MyField[3].x):
        # constant for the whole draw — first resolution goes through
        # get_value and is stored into _cb_static_cache by the caller (the
        # value-node eval sees the _cbc marker); later hits serve COPIES
        # (lists must not be shared: swizzle-assignment writes in place).
        # NOTE: the closure captures only the cache dict, NOT self — a
        # self-capturing closure stored on parser-cached nodes creates a
        # reference cycle that delays __del__ past runtime file teardown
        # and loses the final log flush (empty output.log).
        m = re.match(r'^([A-Za-z_]\w*)\[(\d+)\](?:\.([xyzw]{1,4}))?$', name)
        if m and m.group(1) in self._static_cb_bases():
            def _cb(lv, _n=name, _cache=self._cb_static_cache):
                v = _cache.get(_n, _MISS)
                if v is _MISS:
                    return (False, None)
                if isinstance(v, list):
                    return (True, list(v))
                return (True, v)
            _cb._cbc = True
            return _cb
        return False

    def _static_cb_bases(self):
        """Names that resolve to per-draw-constant cbuffer data (raw cbN
        aliases and named cbuffer fields, excluding struct instances whose
        member access needs locals for subscripts)."""
        bases = self._static_cb_bases_cache
        if bases is None:
            bases = set()
            for cb_def in self.cbuffers.values():
                if not isinstance(cb_def, CbufferDefinition):
                    continue
                bases.add(f'cb{cb_def.register}')
                for f in cb_def.fields:
                    if getattr(f, 'field_type', '') != '__struct__':
                        bases.add(f.name)
            self._static_cb_bases_cache = bases
        return bases

    def get_value(self, name: str, local_vars: Dict[str, Any]) -> Any:
        """
        获取变量或常量的值
        name: 变量名/常量名，支持结构体字段访问(如 input.Pos)
        local_vars: 局部变量字典
        返回: 变量值，如果未找到返回0.0
        """
        name = name.strip()

        # 处理布尔常量
        if name == 'true':
            return True
        if name == 'false':
            return False

        # Hex / unsigned integer literals (0xffffffff, 0x80000000u, 1u) — keep
        # them as ints so bit ops see the exact pattern; >0x7fffffff is signed.
        m_hex = re.match(r'^0[xX]([0-9a-fA-F]+)[uUlL]*$', name)
        if m_hex:
            v = int(m_hex.group(1), 16)
            return v - 0x100000000 if 0x80000000 <= v <= 0xFFFFFFFF else v
        m_uint = re.match(r'^(\d+)[uUlL]+$', name)
        if m_uint:
            return int(m_uint.group(1))

        # 尝试解析为数字
        try:
            return self._f32(float(name))
        except ValueError:
            pass

        # 数组下标访问: cb1[0], cb2[8].xyz, cb1[i].w, t0[i].val[k]
        if '[' in name:
            arr_m = re.match(r'^(\w+)\[([^\]]+)\](.*)$', name)
            if arr_m:
                arr_base = arr_m.group(1)
                idx_expr = arr_m.group(2).strip()
                rest = arr_m.group(3)  # '' 或 '.xyz' 或 '.val[k]'

                # StructuredBuffer access: t0[i].member or t0[i].member[k]
                if arr_base in self.structured_buffers:
                    sb = self.structured_buffers[arr_base]
                    # DXBC split-vector-load hazard: a single
                    #   ld_structured rN.xyz, rN.x, l(off), t0
                    # reads the index rN.x ONCE, but 3Dmigoto decompiles it into
                    # per-component lines (rN.x = t0[rN.x]…; rN.y = t0[rN.x]…),
                    # so re-reading rN.x after the first write corrupts the
                    # index. Cache the index across a consecutive run of loads
                    # sharing the same textual index (reset between statements in
                    # the executor) so it is resolved once, like the GPU.
                    token = f'{arr_base}[{idx_expr}]'
                    burst = self._sb_index_burst
                    if burst is not None and burst['token'] == token:
                        idx = burst['value']
                    else:
                        idx = self._eval_subscript(idx_expr, local_vars)
                        self._sb_index_burst = {'token': token, 'value': idx}
                    if rest.startswith('.'):
                        rm = re.match(r'^(\w+)(?:\[([^\]]+)\])?(.*)$', rest[1:])
                        if rm:
                            member = rm.group(1)
                            vals = self._structured_buffer_member(sb, idx, member)
                            if vals is None:
                                return 0
                            if rm.group(2) is not None:
                                k = self._eval_subscript(rm.group(2), local_vars)
                                vals = vals[k] if 0 <= k < len(vals) else 0.0
                            # Trailing component swizzle (t0[i].Position.x) —
                            # it was silently dropped, so every component read
                            # the member's full vector (scalarized to .x).
                            trailer = (rm.group(3) or '').strip()
                            # Matrix-member element access
                            # (g_SpotLightBuffer[i].ViewToLightSpaceMatrix._m02):
                            # HLSL matrices store column-major by default, so
                            # element (r, c) of a float4x4 lives at c*4 + r.
                            mm = re.match(r'^\._m(\d)(\d)$', trailer)
                            if mm and isinstance(vals, list) and len(vals) >= 4:
                                r_, c_ = int(mm.group(1)), int(mm.group(2))
                                nrows = 4 if len(vals) in (16, 12) else 3
                                k = c_ * nrows + r_
                                return vals[k] if 0 <= k < len(vals) else 0.0
                            if trailer.startswith('.') and isinstance(vals, list):
                                return self.apply_swizzle(vals, trailer[1:])
                            if isinstance(vals, list):
                                return vals[0] if len(vals) == 1 else vals
                            return vals
                    return 0

                idx = self._eval_subscript(idx_expr, local_vars)
                arr = local_vars.get(arr_base)
                if arr is None:
                    arr = self.variables.get(arr_base)
                struct_field = None
                if arr is None:
                    for cb_def in self.cbuffers.values():
                        if isinstance(cb_def, CbufferDefinition):
                            for field in cb_def.fields:
                                if field.name == arr_base and field.data is not None:
                                    arr = field.data
                                    if field.field_type == '__struct__':
                                        struct_field = field
                                    break
                        if arr is not None:
                            break
                # struct {...} NAME[N] element with a named-member accessor
                # (NAME[i].member[.swizzle]). 3Dmigoto sometimes pre-multiplies the
                # index by the element stride (cb4[idx*17]); map an out-of-range
                # index back through the stride so element 0 still resolves.
                if struct_field is not None:
                    # Flat single-matrix-array struct-array (TombRaider skinning):
                    # `struct { float4x4 M[K]; } Arr[N]` is decompiled as
                    # `Arr[flat]._mR..` with both `.M` and the inner index folded
                    # into one flat matrix index. Since the K*N matrices are
                    # contiguous, recover element = flat//K, inner = flat%K and
                    # resolve M[inner] directly (passing literal indices avoids
                    # the parser limit on `Arr[i].M[k]._mRC`).
                    if rest.startswith('._m'):
                        members = getattr(struct_field, 'struct_members', None) or []
                        mats = [m for m in members
                                if self._CB_TYPE_BYTES.get(m[1], 0) > 16]
                        if len(mats) == 1 and mats[0][4] > 0:
                            mname, K = mats[0][0], mats[0][4]
                            elem_i, inner = idx // K, idx % K
                            if 0 <= elem_i < len(arr):
                                res = self._struct_member_access(
                                    struct_field, elem_i, f'.{mname}[{inner}]{rest}')
                                if res is not None:
                                    return res
                    er = getattr(struct_field, 'struct_elem_regs', 1) or 1
                    eidx = idx
                    if eidx >= len(arr) and er > 0:
                        eidx = eidx // er
                    if 0 <= eidx < len(arr):
                        res = self._struct_member_access(struct_field, eidx, rest)
                        if res is not None:
                            return res
                if isinstance(arr, list) and 0 <= idx < len(arr):
                    elem = arr[idx]
                    # Nested 2D index, e.g. a geometry shader's per-primitive
                    # input `v[i][j].swz` (v[i] = primitive-vertex i's attribute
                    # list, v[i][j] = attribute slot j).
                    if rest.startswith('[') and isinstance(elem, list):
                        m2 = re.match(r'^\[([^\]]+)\](.*)$', rest)
                        if m2:
                            j = self._eval_subscript(m2.group(1), local_vars)
                            inner = elem[j] if 0 <= j < len(elem) else 0.0
                            r2 = m2.group(2)
                            if r2.startswith('.'):
                                return self.apply_swizzle(inner, r2[1:])
                            return inner
                    if rest.startswith('.'):
                        return self.apply_swizzle(elem, rest[1:])
                    return elem
                return 0

        # 检查是否包含swizzle操作 (如 LightPos.xyz, LightPos.xxx, input.Pos.xy)
        if '.' in name:
            parts = name.split('.')
            if len(parts) >= 2:
                base_name = parts[0]

                # Non-array cbuffer struct instance member access:
                # `g_forceParam.LoopNum.x` / `.GustParam[i].z`. Route through
                # _struct_member_access (element 0) so uint/int members read
                # their exact integer bits (stored as float denormals, the
                # float view rounds them to 0 — sekiro wind LoopNum.x=2 read
                # as 0 and the whole wind loop never ran).
                if base_name not in local_vars and base_name not in self.variables:
                    for cb_def in self.cbuffers.values():
                        if not isinstance(cb_def, CbufferDefinition):
                            continue
                        for field in cb_def.fields:
                            if (field.name == base_name
                                    and getattr(field, 'field_type', '') == '__struct__'
                                    and not getattr(field, 'array_size', 0)):
                                res = self._struct_member_access(
                                    field, 0, name[len(base_name):], local_vars)
                                if res is not None:
                                    return res

                # 判断是否为swizzle模式（全是xyzwrgb组成的字符串）
                # 对于 input.Color.g, parts = ['input', 'Color', 'g']
                # 只有当最后一部分是纯swizzle字符时，才认为是swizzle操作
                last_part = parts[-1]
                is_single_swizzle = len(parts) == 2 and last_part and all(c in 'xyzwrgb' for c in last_part.lower())
                is_multi_swizzle = len(parts) == 2 and last_part and all(c in 'xyzwrgb' for c in last_part.lower()) and len(last_part) > 1

                if is_single_swizzle or is_multi_swizzle:
                    # 两级访问: input.Pos 或 input.Color.rgb
                    swizzle_str = last_part
                    # 先检查 base_name + '.' + swizzle_str 是否直接存在
                    full_swizzle_name = f'{base_name}.{swizzle_str}'
                    if full_swizzle_name in local_vars:
                        obj = local_vars[full_swizzle_name]
                        if isinstance(obj, (int, float)):
                            return obj
                        if isinstance(obj, list):
                            return obj

                    obj = local_vars.get(base_name)
                    if obj is None:
                        obj = self.variables.get(base_name)
                    if obj is not None:
                        return self.apply_swizzle(obj, swizzle_str)

                    # 尝试从cbuffer获取
                    for cb_name, cb_def in self.cbuffers.items():
                        if isinstance(cb_def, CbufferDefinition):
                            for field in cb_def.fields:
                                if field.name == base_name:
                                    if field.data is not None:
                                        return self.apply_swizzle(field.data, swizzle_str)
                                    return 0

                    # 检查是否在output对象中
                    if base_name in local_vars:
                        obj = local_vars[base_name]
                        if isinstance(obj, dict):
                            return self.apply_swizzle(obj.get(swizzle_str), swizzle_str) if isinstance(obj.get(swizzle_str), list) else self.apply_swizzle(obj, swizzle_str)
                        return self.apply_swizzle(obj, swizzle_str)

                    return 0
                else:
                    # 检查矩阵 _mRC 访问器: WorldViewProj._m01_m11_m21_m31
                    if len(parts) == 2 and self.patterns['matrix_accessor'].match(parts[1]):
                        base = parts[0]
                        prop = parts[1]
                        matrix = local_vars.get(base) or self.variables.get(base)
                        if matrix is None:
                            for cb_def in self.cbuffers.values():
                                if isinstance(cb_def, CbufferDefinition):
                                    for field in cb_def.fields:
                                        if field.name == base and field.data is not None:
                                            matrix = field.data
                                            break
                                if matrix is not None:
                                    break
                        if matrix is not None and isinstance(matrix, list) and matrix and isinstance(matrix[0], list):
                            accessor_parts = [p for p in prop.split('_') if p and p[0] == 'm' and len(p) == 3]
                            result = []
                            for ap in accessor_parts:
                                try:
                                    r, c = int(ap[1]), int(ap[2])
                                    result.append(matrix[r][c] if r < len(matrix) and c < len(matrix[r]) else 0.0)
                                except (ValueError, IndexError):
                                    result.append(0.0)
                            if len(result) == 1:
                                return result[0]
                            return result if result else 0.0

                    # 多级访问: input.Color.g (Color不是纯swizzle字符)
                    if len(parts) == 2:
                        # 两级访问但不是swizzle模式: input.Color
                        # 直接查local_vars中是否存在 'input.Color'
                        full_name = f'{base_name}.{parts[1]}'
                        if full_name in local_vars:
                            return local_vars[full_name]
                        # 检查 base_name 是否在local_vars中作为dict
                        if base_name in local_vars:
                            obj = local_vars[base_name]
                            if isinstance(obj, dict):
                                return obj.get(parts[1], 0)
                            elif isinstance(obj, list):
                                # base_name是列表(比如input.Pos是float3),parts[1]是访问其元素
                                idx_map = {'x': 0, 'y': 1, 'z': 2, 'w': 3, 'r': 0, 'g': 1, 'b': 2, 'a': 3}
                                if parts[1].lower() in idx_map:
                                    idx = idx_map[parts[1].lower()]
                                    return obj[idx] if idx < len(obj) else 0
                        # 检查cbuffer
                        for cb_name, cb_def in self.cbuffers.items():
                            if isinstance(cb_def, CbufferDefinition):
                                for field in cb_def.fields:
                                    if field.name == base_name:
                                        if field.data is not None:
                                            return self.apply_swizzle(field.data, parts[1])
                                        return 0
                        return 0
                    elif len(parts) == 3:
                        # input.Color.g -> 获取 input.Color, 然后对结果应用 .g
                        # 直接查找 input.Color 是否在local_vars中
                        full_name = f'{base_name}.{parts[1]}'  # 'input.Color'
                        if full_name in local_vars:
                            base_val = local_vars[full_name]
                        else:
                            base_val = self.get_value(f'{base_name}.{parts[1]}', local_vars)
                        if isinstance(base_val, list):
                            idx_map = {'x': 0, 'y': 1, 'z': 2, 'w': 3, 'r': 0, 'g': 1, 'b': 2, 'a': 3}
                            swizzle_ch = parts[2].lower()
                            if swizzle_ch in idx_map:
                                return base_val[idx_map[swizzle_ch]] if idx_map[swizzle_ch] < len(base_val) else 0
                        return 0
                    else:
                        # 超过3级,递归处理
                        return self.get_value('.'.join(parts[1:]), local_vars)

        # 局部变量查找
        if name in local_vars:
            val = local_vars[name]
            return val

        base_name = name.split('.')[0] if '.' in name else name

        # cbuffer字段查找
        for cb_name, cb_def in self.cbuffers.items():
            if isinstance(cb_def, CbufferDefinition):
                for field in cb_def.fields:
                    if field.name == base_name:
                        return field.data if field.data is not None else 0

        # 全局变量查找
        if name in self.variables:
            return self.variables[name]

        # 嵌套cbuffer查找
        try:
            if '.' in name:
                parts = name.split('.')
                base = parts[0]
                for cb_name, cb_data in self.cbuffers.items():
                    if base in cb_data:
                        val = cb_data[base]
                        for p in parts[1:]:
                            if isinstance(val, list) and p in ['x', 'y', 'z', 'w']:
                                idx = ['x', 'y', 'z', 'w'].index(p)
                                val = val[idx] if idx < len(val) else 0
                            else:
                                break
                        return val
        except:
            pass

        return 0.0

    @staticmethod
    def _split_top_level_commas(s: str) -> list:
        """Split on commas not nested inside (), [] or <>."""
        parts, depth, cur = [], 0, []
        for ch in s:
            if ch in '([<':
                depth += 1
            elif ch in ')]>':
                depth -= 1
            if ch == ',' and depth == 0:
                parts.append(''.join(cur))
                cur = []
            else:
                cur.append(ch)
        if cur:
            parts.append(''.join(cur))
        return parts

    def _assign_lvalue(self, lvalue: str, value, local_vars: Dict[str, Any]):
        """Assign to an lvalue that may carry a swizzle (e.g. 'r1.x' or 'r0')."""
        lvalue = lvalue.strip()
        m = re.match(r'^(\w+)\.([xyzwrgba]+)$', lvalue)
        if m:
            self._apply_swizzle_assign(m.group(1), m.group(2), value, local_vars)
        else:
            local_vars[lvalue] = value

    def execute_statement(self, stmt: str, local_vars: Dict[str, Any]):
        """
        执行单条HLSL语句
        stmt: 语句字符串，如 "float3 pos = input.Pos;" 或 "output.Color = float4(1,0,0,1);"
        local_vars: 局部变量字典
        """
        stmt = stmt.strip()
        if not stmt:
            return None

        dbg = self.debug and self._should_print and not self._in_derivative_eval
        if dbg:
            self.debug_print(f"\n[STMT] Executing: {stmt}")

        # Fast dispatch: assignment statements classified once (first vertex)
        # skip the whole special-case regex cascade on every later vertex.
        # Only plain assignments are cached, so a cache hit can never shadow
        # the GS-stream / if / while / intrinsic-statement handling below.
        fast = self._stmt_fast.get(stmt)
        if fast is not None:
            kind = fast[0]
            if kind == 1:      # var.swz = expr
                value = self.evaluate_expression(fast[3], local_vars)
                self._apply_swizzle_assign(fast[1], fast[2], value, local_vars)
                if dbg:
                    self.debug_print(f"[STMT] {stmt} => {fast[1]}.{fast[2]} = {self._format_float(value)}")
                return None
            if kind == 2:      # var = expr
                value = self.evaluate_expression(fast[1], local_vars)
                local_vars[fast[2]] = value
                if dbg:
                    self.debug_print(f"[STMT] {stmt} => {fast[2]} = {value}")
                return None
            if kind == 3:      # <type> var = expr
                value = self.evaluate_expression(fast[1], local_vars)
                local_vars[fast[2]] = value
                if dbg:
                    self.debug_print(f"[STMT] {stmt} => {fast[2]} = {self._format_value(value)}")
                return None

        # Geometry-shader stream ops (only active during GS execution, when
        # self._gs_emit is set). `<stream>.Append(...)` emits the current output
        # register values as one output vertex; `<stream>.RestartStrip()` ends
        # the current output strip. 3Dmigoto decompiles the emit as e.g.
        # `m0.Append(0)` — the argument is a dummy; the real vertex is whatever
        # o0/o1/.. hold at that point.
        if self._gs_emit is not None:
            mgs = re.match(r'^(\w+)\s*\.\s*(Append|RestartStrip)\s*\(', stmt)
            if mgs:
                if mgs.group(2) == 'Append':
                    self._gs_emit(local_vars)
                else:
                    self._gs_restart()
                return None

        # if-else条件语句处理
        if stmt.startswith('if'):
            self.execute_if_statement(stmt, local_vars)
            return None

        # while循环: 3Dmigoto 反编译大量输出 `while (true) { ... if (c) break; }`
        # (灯光遍历/CSM 级联选择)。未实现时整块被当作未知语句跳过——阴影因子
        # 恒为全亮。break/continue 以异常穿透任意嵌套的 if/块。
        if re.match(r'while\s*\(', stmt):
            self.execute_while_statement(stmt, local_vars)
            return None
        if stmt in ('break', 'break;'):
            raise _LoopBreak()
        if stmt in ('continue', 'continue;'):
            raise _LoopContinue()

        # GetDimensions (resinfo): statement-level method with OUT params —
        # `t0.GetDimensions(0, fDest.x, fDest.y, fDest.z, fDest.w);`.
        # Unimplemented it left fDest = 0 and 0.5/width divided by zero
        # (witcher event16834 → inf positions). Values: per-mip width/height,
        # then array-size/depth, then mip count — assigned to the out-args in
        # order via the normal assignment machinery (swizzle-safe).
        mdim = re.match(r'^(\w+)\s*\.\s*GetDimensions\s*\((.*)\)\s*;?$', stmt)
        if mdim:
            obj_name = mdim.group(1)
            arg_list = [a.strip() for a in mdim.group(2).split(',') if a.strip()]
            binding = self._find_texture_binding(obj_name)
            if binding and self._texture_desc_list and len(arg_list) >= 2:
                reg_id = binding.register_id
                desc = (self._texture_desc_list[reg_id]
                        if reg_id < len(self._texture_desc_list) else None)
                if desc is not None:
                    mip = 0
                    try:
                        mv = self.evaluate_expression(arg_list[0], local_vars)
                        mip = int(mv[0] if isinstance(mv, list) else (mv or 0))
                    except Exception:
                        mip = 0
                    w = max(1, int(desc.Width or 1) >> mip)
                    h = max(1, int(desc.Height or 1) >> mip)
                    third = max(int(getattr(desc, 'ArraySize', 1) or 1),
                                int(getattr(desc, 'Depth', 1) or 1))
                    nmips = int(getattr(desc, 'MipLevels', 1) or 1)
                    vals = [float(w), float(h), float(third), float(nmips)]
                    for target, val in zip(arg_list[1:], vals):
                        self.execute_statement(f"{target} = {val!r}", local_vars)
                    return None

        # 3Dmigoto emits raw-buffer loads as a bare DXBC instruction it "could not
        # decompile":  ld_raw[_indexable](raw_buffer)(...) DST, ADDR, tN.xxxx
        # Execute it as DST = bufferAtRegisterN.Load(ADDR). Pervasive in GPU
        # instancing (e.g. Sekiro's per-instance index table); without it the
        # downstream matrix index is garbage and SV_Position is wrong.
        if stmt.startswith('ld_raw'):
            m = re.match(r'ld_raw\w*\s*\([^)]*\)\s*(?:\([^)]*\)\s*)?'
                         r'([^,]+),\s*([^,]+),\s*t(\d+)\.?\w*', stmt)
            if m:
                dst = m.group(1).strip()
                addr_expr = m.group(2).strip()
                reg = int(m.group(3))
                bab = next((b for b in self.byte_address_buffers.values()
                            if b['register'] == reg), None)
                if bab is not None:
                    addr = self._eval_subscript(addr_expr, local_vars)
                    ncomp = len(dst.split('.')[1]) if '.' in dst else 1
                    val = self._byte_address_load(bab, addr, ncomp)
                    self._assign_lvalue(dst, val, local_vars)
                    self.debug_print(f"[STMT] {stmt} => {dst} = {val}")
                # The bare instruction carries no ';', so the statement splitter
                # may have glued the following statement onto it — run that tail.
                tail = stmt[m.end():].strip()
                if tail:
                    self.execute_statement(tail, local_vars)
            return None

        # sincos(angle, s_out, c_out): no return value; writes sin(angle) to the
        # second lvalue and cos(angle) to the third. Pervasive in 3Dmigoto
        # rotation/animation code; without it the out registers keep stale values
        # and the transform is wrong.
        if stmt.startswith('sincos'):
            m = re.match(r'sincos\s*\((.*)\)\s*;?$', stmt, re.DOTALL)
            if m:
                parts = self._split_top_level_commas(m.group(1))
                if len(parts) == 3:
                    angle = self.evaluate_expression(parts[0].strip(), local_vars)
                    if angle is not None:
                        if isinstance(angle, list):
                            s = [self._sin(a) for a in angle]
                            c = [self._cos(a) for a in angle]
                        else:
                            s, c = self._sin(angle), self._cos(angle)
                        self._assign_lvalue(parts[1].strip(), s, local_vars)
                        self._assign_lvalue(parts[2].strip(), c, local_vars)
                    self.debug_print(f"[STMT] {stmt} => sincos written")
                    return None

        # 多变量声明无初始值: float4 r0,r1,r2; 或 uint4 bitmask, uiDest;
        if '=' not in stmt:
            match = self.patterns['multi_var_decl'].match(stmt)
            if match:
                var_type = match.group(1)
                var_names_str = match.group(2)
                var_names = [n.strip() for n in var_names_str.split(',') if n.strip() and re.match(r'^\w+$', n.strip())]
                if var_names:
                    default = self._default_value_for_type(var_type)
                    for vname in var_names:
                        if vname not in local_vars:
                            local_vars[vname] = list(default) if isinstance(default, list) else default
                    self.debug_print(f"[DECL] {var_type} {var_names}")
                    return None

        # const <type> <name>[] = { {...}, {...}, ... }  (3Dmigoto inline constant buffer)
        # e.g. "const float4 icb[] = { { 1,0,0,0 }, { 0,1,0,0 } }"
        if stmt.startswith('const ') and '[' in stmt and '{' in stmt and '=' in stmt:
            m_icb = re.match(r'^const\s+\w+\s+(\w+)\s*\[\s*\d*\s*\]\s*=\s*(\{.+\})\s*;?$', stmt, re.DOTALL)
            if m_icb:
                arr_name = m_icb.group(1)
                arr_body = m_icb.group(2)
                elements = []
                depth = 0
                elem_start = None
                for ki, kc in enumerate(arr_body):
                    if kc == '{':
                        depth += 1
                        if depth == 2:
                            elem_start = ki + 1
                    elif kc == '}':
                        depth -= 1
                        if depth == 1 and elem_start is not None:
                            raw = arr_body[elem_start:ki]
                            try:
                                vals = [float(p.strip()) for p in raw.split(',') if p.strip()]
                            except ValueError:
                                vals = [0.0]
                            elements.append(vals if len(vals) > 1 else (vals[0] if vals else 0.0))
                            elem_start = None
                if elements:
                    local_vars[arr_name] = elements
                    self.debug_print(f"[STMT] icb {arr_name}[{len(elements)}] loaded")
                return None

        # 变量声明语句: float4 pos = ...;
        match = self.patterns['variable_declaration'].match(stmt)
        if match:
            var_name = match.group(2)
            rhs = match.group(3)
            self._stmt_fast[stmt] = (3, rhs, var_name)
            value = self.evaluate_expression(rhs, local_vars)
            local_vars[var_name] = value
            if dbg:
                self.debug_print(f"[STMT] {stmt} => {var_name} = {self._format_value(value)}")
            return None

        # output字段赋值: output.Color = ...; 或 output.Color.r = ...;
        if 'output.' in stmt:
            match = self.patterns['output_field_assignment'].match(stmt)
            if match:
                field_name = match.group(1)
                swizzle = match.group(2)
                value_expr = match.group(3).rstrip(';').strip()
                value = self.evaluate_expression(value_expr, local_vars)

                if 'output' not in local_vars:
                    local_vars['output'] = {}

                if swizzle is None:
                    local_vars['output'][field_name] = value
                else:
                    if field_name not in local_vars['output']:
                        local_vars['output'][field_name] = [0.0, 0.0, 0.0, 0.0]
                    current = local_vars['output'][field_name]
                    if not isinstance(current, list):
                        current = [current, 0.0, 0.0, 0.0]

                    swizzle_map = {'x': 0, 'y': 1, 'z': 2, 'w': 3, 'r': 0, 'g': 1, 'b': 2, 'a': 3}
                    if isinstance(value, list):
                        for i, ch in enumerate(swizzle.lower()):
                            if ch in swizzle_map and i < len(value):
                                current[swizzle_map[ch]] = value[i]
                    else:
                        ch = swizzle.lower()[0] if swizzle else 'x'
                        if ch in swizzle_map:
                            current[swizzle_map[ch]] = value

                    local_vars['output'][field_name] = current
                self.debug_print(f"[STMT] {stmt} => output.{field_name}" + (f".{swizzle}" if swizzle else "") + f" = {self._format_float(value)}")
                return None

        # swizzle分量赋值: r0.xyzw = expr; 或 o0.xyz = expr;
        # Note: don't check count('=') here since RHS may contain >= <= == comparisons
        if '.' in stmt and '=' in stmt:
            match = self.patterns['swizzle_var_assign'].match(stmt)
            if match:
                var_name = match.group(1)
                swizzle = match.group(2)
                rhs = match.group(3).rstrip(';').strip()
                self._stmt_fast[stmt] = (1, var_name, swizzle, rhs)
                value = self.evaluate_expression(rhs, local_vars)
                self._apply_swizzle_assign(var_name, swizzle, value, local_vars)
                if dbg:
                    self.debug_print(f"[STMT] {stmt} => {var_name}.{swizzle} = {self._format_float(value)}")
                return None

        # 一般赋值语句: var = ...;
        if '=' in stmt and stmt.count('=') == 1:
            match = self.patterns['simple_assignment'].match(stmt)
            if match:
                var_name = match.group(1)
                rhs = match.group(2)
                self._stmt_fast[stmt] = (2, rhs, var_name)
                value = self.evaluate_expression(rhs, local_vars)
                local_vars[var_name] = value
                if dbg:
                    self.debug_print(f"[STMT] {stmt} => {var_name} = {value}")
                return None

        self.debug_print(f"[STMT] {stmt} => (no assignment)")
        return None

    def execute_if_statement(self, stmt: str, local_vars: Dict[str, Any]):
        """
        执行if-else条件语句
        stmt: if语句字符串
        local_vars: 局部变量字典
        """
        parts = self._if_parts_cache.get(stmt)
        if parts is None:
            s = stmt.strip()
            if not s.startswith('if'):
                self._if_parts_cache[stmt] = ()
                return
            # 条件表达式: 用括号配对提取, 兼容条件内的嵌套括号(如 cmp() 预处理后的 -(...))
            paren_start = s.find('(')
            if paren_start < 0:
                self._if_parts_cache[stmt] = ()
                return
            depth = 0
            cond_end = None
            for k in range(paren_start, len(s)):
                if s[k] == '(':
                    depth += 1
                elif s[k] == ')':
                    depth -= 1
                    if depth == 0:
                        cond_end = k
                        break
            if cond_end is None:
                self._if_parts_cache[stmt] = ()
                return
            condition_expr = s[paren_start + 1:cond_end].strip()
            rest = s[cond_end + 1:].strip()

            # then 分支与可选的 else 分支: then 用大括号配对切出, 其后剩余部分作为 else 候选
            if rest.startswith('{'):
                depth = 0
                tb_end = None
                for k in range(len(rest)):
                    if rest[k] == '{':
                        depth += 1
                    elif rest[k] == '}':
                        depth -= 1
                        if depth == 0:
                            tb_end = k
                            break
                if tb_end is None:
                    then_branch = rest
                    after = ''
                else:
                    then_branch = rest[:tb_end + 1]
                    after = rest[tb_end + 1:].strip()
            else:
                else_pos = self.find_else_branch(rest)
                if else_pos >= 0:
                    then_branch = rest[:else_pos].strip()
                    after = rest[else_pos:].strip()
                else:
                    then_branch = rest
                    after = ''
            parts = (condition_expr, then_branch, after)
            self._if_parts_cache[stmt] = parts
        elif parts == ():
            return
        condition_expr, then_branch, after = parts

        cond_value = self.evaluate_expression(condition_expr, local_vars)
        if self.debug and self._should_print and not self._in_derivative_eval:
            self.debug_print(f"[IF] condition: {condition_expr} => {cond_value}")

        if cond_value:
            if then_branch.startswith('{'):
                self.execute_block(then_branch, local_vars)
            elif then_branch and not then_branch.startswith('else'):
                self.execute_statement(then_branch, local_vars)
        elif after.startswith('else'):
            else_branch = after[4:].strip()
            if else_branch.startswith('{'):
                self.execute_block(else_branch, local_vars)
            elif else_branch:
                # `else if (...)` 链: 作为语句递归处理
                self.execute_statement(else_branch, local_vars)

    _MAX_LOOP_ITERS = 65536

    def execute_while_statement(self, stmt: str, local_vars: Dict[str, Any]):
        """Execute `while (cond) { body }`. 3Dmigoto emits `while (true)` with
        conditional breaks (light traversal, CSM cascade selection). break /
        continue arrive as _LoopBreak/_LoopContinue exceptions from any nesting
        depth. A hard iteration cap guards against a condition that never turns
        false under interpreted semantics."""
        parts = self._while_parts_cache.get(stmt)
        if parts is None:
            s = stmt.strip()
            paren_start = s.find('(')
            if paren_start < 0:
                self._while_parts_cache[stmt] = ()
                return
            depth = 0
            cond_end = None
            for k in range(paren_start, len(s)):
                if s[k] == '(':
                    depth += 1
                elif s[k] == ')':
                    depth -= 1
                    if depth == 0:
                        cond_end = k
                        break
            if cond_end is None:
                self._while_parts_cache[stmt] = ()
                return
            condition_expr = s[paren_start + 1:cond_end].strip()
            body = s[cond_end + 1:].strip()
            parts = (condition_expr, body, condition_expr == 'true')
            self._while_parts_cache[stmt] = parts
        elif parts == ():
            return
        condition_expr, body, always = parts

        iters = 0
        while iters < self._MAX_LOOP_ITERS:
            if not always:
                cond_value = self.evaluate_expression(condition_expr, local_vars)
                if not self._to_bool(cond_value):
                    break
            try:
                if body.startswith('{'):
                    self.execute_block(body, local_vars)
                elif body:
                    self.execute_statement(body, local_vars)
                else:
                    break
            except _LoopBreak:
                break
            except _LoopContinue:
                pass
            iters += 1
        if iters >= self._MAX_LOOP_ITERS:
            self.log_output(
                f"Warning: while loop hit iteration cap ({self._MAX_LOOP_ITERS})")

    def find_else_branch(self, stmt: str) -> int:
        """
        查找else分支的起始位置(不在嵌套括号内)
        stmt: 语句字符串
        返回: else关键字位置，或-1表示未找到
        """
        depth = 0
        pos = 0
        while pos < len(stmt):
            char = stmt[pos]
            if char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
            elif char == '{':
                depth += 1
            elif char == '}':
                depth -= 1
            elif depth == 0 and stmt[pos:pos+4] == 'else':
                return pos
            pos += 1
        return -1

    def execute_block(self, block: str, local_vars: Dict[str, Any]):
        """
        执行语句块(被大括号包围的语句列表)
        block: 语句块字符串
        local_vars: 局部变量字典
        """
        statements = self._block_stmts_cache.get(block)
        if statements is None:
            b = block.strip()
            if not b.startswith('{') or not b.endswith('}'):
                self._block_stmts_cache[block] = ()
                return
            inner = b[1:-1].strip()
            statements = tuple(self.GenerateStmts(inner)) if inner else ()
            self._block_stmts_cache[block] = statements
        for stmt in statements:
            self.execute_statement(stmt, local_vars)

    @staticmethod
    def _strip_comments(code: str) -> str:
        """Remove C++-style `//` line comments and `/* */` block comments,
        leaving string literals intact. 3Dmigoto emits comment blocks such as
        `// Needs manual fix for instruction:` immediately before a real
        statement; without stripping, GenerateStmts fuses the comment with the
        following assignment (up to the next ';') and the assignment is lost."""
        out = []
        i = 0
        n = len(code)
        in_string = False
        string_char = None
        while i < n:
            c = code[i]
            if in_string:
                out.append(c)
                if c == string_char:
                    in_string = False
                i += 1
            elif c in '"\'':
                in_string = True
                string_char = c
                out.append(c)
                i += 1
            elif c == '/' and i + 1 < n and code[i + 1] == '/':
                # Line comment: drop through end of line, keep the newline so
                # line-number bookkeeping and statement boundaries are preserved.
                while i < n and code[i] != '\n':
                    i += 1
            elif c == '/' and i + 1 < n and code[i + 1] == '*':
                i += 2
                while i + 1 < n and not (code[i] == '*' and code[i + 1] == '/'):
                    i += 1
                i += 2
            else:
                out.append(c)
                i += 1
        return ''.join(out)

    def GenerateStmts(self, code: str):
        code = self._strip_comments(code)
        statements = []
        current_stmt = []
        brace_count = 0
        paren_count = 0
        in_string = False
        string_char = None

        n = len(code)
        i = 0
        while i < n:
            char = code[i]
            if char == '{':
                brace_count += 1
                current_stmt.append(char)
            elif char == '}':
                if brace_count > 0:
                    current_stmt.append(char)
                brace_count -= 1
                if brace_count == 0 and current_stmt:
                    # 关键: 一个 `if (...) { ... }` 块在其闭合 '}' 处 brace_count 归零，
                    # 但其后可能紧跟 `else { ... }`。若此时就切分，else 分支会变成孤立语句，
                    # 永远不会作为该 if 的 else 执行 —— 条件为假时整段 else 被丢弃，
                    # 导致依赖 else 分支的寄存器(如逐顶点变换结果)保持默认值。
                    # 因此向后窥探: 若下一个非空白记号是 else，则继续累积、暂不切分。
                    j = i + 1
                    while j < n and code[j] in ' \t\r\n':
                        j += 1
                    next_is_else = (
                        code[j:j + 4] == 'else'
                        and (j + 4 >= n or not (code[j + 4].isalnum() or code[j + 4] == '_'))
                    )
                    if not next_is_else:
                        stmt = ''.join(current_stmt).strip()
                        if stmt:
                            statements.append(stmt)
                        current_stmt = []
            elif char == '(':
                paren_count += 1
                current_stmt.append(char)
            elif char == ')':
                paren_count -= 1
                current_stmt.append(char)
            elif char in '"\'':
                if not in_string:
                    in_string = True
                    string_char = char
                elif char == string_char:
                    in_string = False
                    string_char = None
                current_stmt.append(char)
            elif char == ';' and brace_count == 0 and paren_count == 0 and not in_string:
                stmt = ''.join(current_stmt).strip()
                if stmt:
                    statements.append(stmt)
                current_stmt = []
            else:
                current_stmt.append(char)
            i += 1

        if current_stmt:
            stmt = ''.join(current_stmt).strip()
            if stmt:
                statements.append(stmt)

        return statements


    def execute_main_function(self, code: str, main_func: str, input_struct_name: str, row_index: int, data: Dict[str, Any], shader_stage: int = None):
        """
        执行HLSL main函数
        code: HLSL代码
        main_func: main函数名
        input_struct_name: 输入结构体名
        row_index: 数据行索引
        data: 输入数据字典
        shader_stage: shader阶段常量 (SHADER_STAGE_VS/HS/DS/GS/PS/CS)
        返回: output结构体字典
        """
        from d3d import SHADER_STAGE_PS
        input_struct = self.structs.get(input_struct_name)
        if not input_struct:
            self.log_output(f"Cannot find input_struct: {input_struct_name}\n")
            return None

        input_fields = {}
        for field in input_struct.fields:
            input_fields[field.name] = field.field_type

        # 查找main函数签名
        func_signature_pattern = r'(\w+)\s+' + re.escape(main_func) + r'\s*\(\s*(\w+)\s+input\s*\)'
        func_signature_match = re.search(func_signature_pattern, code)
        if not func_signature_match:
            return None

        output_struct_name = func_signature_match.group(1)
        input_struct_name_from_func = func_signature_match.group(2)

        if output_struct_name in self.structs:
            output_struct = self.structs[output_struct_name]
        else:
            output_struct = None

        output_fields = {}
        is_ps = (shader_stage == SHADER_STAGE_PS)
        if output_struct is not None:
            for field in output_struct.fields:
                output_fields[field.name] = field.field_type
        else:
            if is_ps:
                output_fields['Color'] = 'float4'

        func_signature = rf'{output_struct_name}\s+{main_func}\s*\(\s*{input_struct_name_from_func}\s+input\s*\)'

        cache_key = f"{output_struct_name}_{main_func}_{input_struct_name_from_func}"
        if cache_key in self._parsed_func_cache:
            cached = self._parsed_func_cache[cache_key]
            body = cached['body']
            statements = cached['statements']
        else:
            statements = self._collect_function_statements(main_func)
            body = ""
            self._parsed_func_cache[cache_key] = {'body': body, 'statements': statements}

        # 初始化局部变量
        local_vars = {'data': data}

        # 设置input字段变量
        for field_name, field_value in data.items():
            local_vars[f'input.{field_name}'] = field_value

        # 初始化output对象
        output_obj = {}
        for field in output_fields:
            output_obj[field] = None
        local_vars['output'] = output_obj

        ret_val = None

        self._eval_counter += 1
        self._should_print = ((self._eval_counter - 1) % self.print_sequence == 0)
        self._dbg = self.debug and self._should_print and not self._in_derivative_eval

        self.debug_print(f"******************************************************")
        self.debug_print(f"**************Begin {self._eval_counter}**************")
        self.debug_print(f"******************************************************\n")

        self.debug_print(f"\n=== INPUT DATA ===")
        for k, v in local_vars.items():
            if k.startswith('input.') or k == 'output':
                self.debug_print(f"  {k} = {self._format_float(v)}")
        self.debug_print(f"==================")

        # 顺序执行语句
        i = 0
        while i < len(statements):
            stmt = statements[i]
            if stmt is None:
                i += 1
                continue

            if 'return' in stmt and ('output' in stmt or is_ps):
                if is_ps:
                    return_val_match = re.search(r'return\s+(.+?)\s*$', stmt)
                    if return_val_match:
                        expr = return_val_match.group(1).strip()
                        ret_val = self.evaluate_expression(expr, local_vars)
                else:
                    ret_val = local_vars.get('output')
                i += 1
                continue

            # 检查是否是if语句，且下一条是else
            if stmt.startswith('if'):
                next_i = i + 1
                # 查找下一个非None的语句
                while next_i < len(statements) and statements[next_i] is None:
                    next_i += 1
                
                if next_i < len(statements) and statements[next_i].startswith('else'):
                    # 合并if和else为完整语句
                    full_if_stmt = stmt + '\n' + statements[next_i]
                    self.execute_if_statement(full_if_stmt, local_vars)
                    statements[next_i] = None  # 标记else已处理
                else:
                    self.execute_if_statement(stmt, local_vars)
            else:
                self.execute_statement(stmt, local_vars)

            i += 1

        self.debug_print(f"******************************************************")
        self.debug_print(f"**************End {self._eval_counter}**************")
        self.debug_print(f"******************************************************\n")

        return ret_val

    def interpret(self, hlsl_file_path: str, csv_folder_path: str = None):
        """
        解释HLSL代码 - 解析结构体和cbuffer定义
        hlsl_file_path: HLSL文件路径
        csv_folder_path: CSV文件夹路径（如果为None则不加载CSV数据）
        """
        if not os.path.exists(hlsl_file_path):
            self.log_output(f"Error: HLSL file not found: {hlsl_file_path}")
            return

        with open(hlsl_file_path, 'r', encoding='utf-8') as f:
            self.hlsl_code = f.read()

        code = self.hlsl_code

        if csv_folder_path is None:
            csv_folder_path = os.path.dirname(hlsl_file_path)

        self._parse_texture_and_sampler_bindings(code)

        # 解析struct定义
        for struct_match in self.patterns['struct_finditer'].finditer(code):
            struct_def = self.parse_struct(struct_match.group())
            if struct_def:
                self.structs[struct_def.name] = struct_def

        # 解析cbuffer定义（用大括号配平提取，以支持内嵌 struct 的 cbuffer）
        for cb_block in self._extract_cbuffer_blocks(code):
            cb_def = self.parse_cbuffer(cb_block)
            if cb_def:
                self.cbuffers[cb_def.name] = cb_def

        # 从CSV加载struct数据
        for struct_name in self.structs:
            csv_path = os.path.join(csv_folder_path, f'{struct_name}.csv')
            if os.path.exists(csv_path):
                self.load_struct_data_from_csv(struct_name, csv_path)

        # 从CSV加载cbuffer数据
        for cb_name in self.cbuffers:
            csv_path = os.path.join(csv_folder_path, f'{cb_name}.csv')
            if os.path.exists(csv_path):
                self.load_cbuffer_data_from_csv(cb_name, csv_path)

        self.parse_all_functions(code)

    def executeVS(self, main_func: str, vs_input: str, code: str = None, execute_count: int = None):
        """
        执行顶点着色器
        main_func: 入口函数名
        vs_input: 输入结构体名
        code: HLSL代码（如果为None则使用self.hlsl_code）
        execute_count: 执行次数（如果为None则使用input_struct.fields计算行数）
        返回: 输出结构体字典列表
        """
        if code is None:
            code = self.hlsl_code
        else:
            if not self._all_functions:
                self.parse_all_functions(code)
        self._last_executeVS_code = code
        input_struct = self.structs.get(vs_input)
        if not input_struct:
            self.log_output(f"Cannot find vs input: {vs_input}\n")
            return None

        output_struct_name = None
        func_signature_pattern = r'(\w+)\s+' + re.escape(main_func) + r'\s*\(\s*(\w+)\s+input\s*\)'
        func_signature_match = re.search(func_signature_pattern, code)
        if func_signature_match:
            output_struct_name = func_signature_match.group(1)

        output_struct = self.structs.get(output_struct_name) if output_struct_name else None

        self.vertex_pool.clear()
        self.vertex_pool.set_input_struct(input_struct)
        self.vertex_pool.set_output_struct(output_struct)

        # clear eval counter
        self._eval_counter = 0

        if execute_count is None:
            num_rows = 0
            for field in input_struct.fields:
                if field.data:
                    num_rows = max(num_rows, len(field.data))
            execute_count = num_rows

        if self.max_workers > 1:
            def execute_row(row_index: int):
                data = {}
                for field in input_struct.fields:
                    if field.data and row_index < len(field.data):
                        data[field.name] = field.data[row_index]
                self.vertex_pool.build_from_input(vs_input, data, row_index)
                from d3d import SHADER_STAGE_VS
                result = self.execute_main_function(code, main_func, vs_input, row_index, data, SHADER_STAGE_VS)
                self.vertex_pool.update_output(row_index, result)
                return row_index, result

            print(f"Run thread workers")
            results = [None] * execute_count
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = [executor.submit(execute_row, i) for i in range(execute_count)]
                for future in futures:
                    idx, result = future.result()
                    results[idx] = result
        else:
            print(f"Run single thread")
            results = []
            for row_index in range(execute_count):
                data = {}
                for field in input_struct.fields:
                    if field.data and row_index < len(field.data):
                        data[field.name] = field.data[row_index]
                self.vertex_pool.build_from_input(vs_input, data, row_index)
                from d3d import SHADER_STAGE_VS
                result = self.execute_main_function(code, main_func, vs_input, row_index, data, SHADER_STAGE_VS)
                self.vertex_pool.update_output(row_index, result)
                results.append(result)

        return results

    def executePS(self, main_func: str, ps_input: str, pixels: List['Pixel']):
        """
        执行像素着色器
        code: HLSL代码
        main_func: 入口函数名
        ps_input: 输入结构体名
        pixels: 光栅化后的像素列表
        返回: 更新了ps_output_color的像素列表
        """
        code = self.hlsl_code
        if not self._all_functions:
            self.parse_all_functions(code)

        input_struct = self.structs.get(ps_input)
        if not input_struct:
            self.log_output(f"Cannot find ps input: {ps_input}\n")
            return pixels

        output_struct_name = None
        func_signature_pattern = r'(\w+)\s+' + re.escape(main_func) + r'\s*\(\s*(\w+)\s+input\s*\)'
        func_signature_match = re.search(func_signature_pattern, code)
        if func_signature_match:
            output_struct_name = func_signature_match.group(1)

        output_struct = self.structs.get(output_struct_name) if output_struct_name else None

        self._eval_counter = 0

        for pixel in pixels:
            pixel.ps_output_color = None

            data = {
                'Color': pixel.color if pixel.color else [1.0, 1.0, 1.0, 1.0],
                'TexCoord': pixel.texcoord if pixel.texcoord else [0.0, 0.0],
                'TexCoord2': pixel.texcoord2 if pixel.texcoord2 else [0.0, 0.0],
                'Normal': pixel.normal if pixel.normal else [0.0, 0.0, 1.0],
                'WorldPos': pixel.worldPos if pixel.worldPos else [0.0, 0.0, 0.0],
            }
            data.update(pixel.attributes)

            from d3d import SHADER_STAGE_PS
            result = self.execute_main_function(code, main_func, ps_input, 0, data, SHADER_STAGE_PS)

            pixel.ps_output_color = result

        return pixels

    def _parse_texture_and_sampler_bindings(self, code: str):
        """
        解析HLSL代码中的纹理和采样器绑定
        code: HLSL代码
        """
        self.texture_bindings = []
        self.sampler_bindings = []

        if not TEXTURE_AVAILABLE:
            self.log_output("Warning: texture module not available")
            return

        for match in self.patterns['texture_binding'].finditer(code):
            kind = match.group(1)
            var_name = match.group(2)
            reg_id = int(match.group(3))
            binding = TextureBinding(variable_name=var_name, register_id=reg_id,
                                     kind=kind)
            self.texture_bindings.append(binding)

        for match in self.patterns['sampler_binding'].finditer(code):
            var_name = match.group(1)
            reg_id = int(match.group(2))
            binding = SamplerBinding(variable_name=var_name, register_id=reg_id)
            self.sampler_bindings.append(binding)

        for binding in self.texture_bindings:
            for sbinding in self.sampler_bindings:
                if binding.register_id == sbinding.register_id:
                    reg_id = binding.register_id
                    if self._texture_exec and reg_id < len(self._texture_desc_list) and reg_id < len(self._sampler_list):
                        binding.texture = self._texture_exec
                        binding.texture_desc = self._texture_desc_list[reg_id]
                        binding.sampler = self._sampler_list[reg_id]

    _SB_COMPONENTS = {
        'float': 1, 'float2': 2, 'float3': 3, 'float4': 4,
        'int': 1, 'int2': 2, 'int3': 3, 'int4': 4,
        'uint': 1, 'uint2': 2, 'uint3': 3, 'uint4': 4,
        # Matrix members inside StructuredBuffer structs are stored tightly
        # packed (sekiro4's SpotLight.ViewToLightSpaceMatrix): count their
        # full float footprint so later member offsets stay correct.
        'float4x4': 16, 'float3x4': 12, 'float4x3': 12, 'float3x3': 9,
        'float2x2': 4,
    }

    def _parse_structured_buffers(self, code: str):
        """
        Parse StructuredBuffer<T> declarations and the struct layouts they use,
        e.g.  struct t0_t { float val[16]; };
              StructuredBuffer<t0_t> t0 : register(t0);
        Records register, element member layout and byte stride for later
        binary loading + indexed access (t0[i].val[k]).
        """
        # struct layouts: name -> [(member_name, base_type, array_count)]
        struct_layouts = {}
        for m in re.finditer(r'struct\s+(\w+)\s*\{([^}]*)\}', code, re.DOTALL):
            sname = m.group(1)
            members = []
            # Strip line comments first: RenderDoc-derived structs carry
            # trailing `// Offset: N` comments, which after the ';' split land
            # at the START of the next field chunk and silently killed every
            # member but the first (sekiro2/4 particle structs parsed as one
            # float4 -> stride 16 instead of 176).
            body = re.sub(r'//[^\n]*', '', m.group(2))
            for fld in body.split(';'):
                fld = fld.strip()
                if not fld:
                    continue
                fm = re.match(r'(\w+)\s+(\w+)\s*(?:\[\s*(\d+)\s*\])?$', fld)
                if fm:
                    base_type = fm.group(1)
                    member = fm.group(2)
                    count = int(fm.group(3)) if fm.group(3) else 1
                    members.append((member, base_type, count))
            struct_layouts[sname] = members

        for m in re.finditer(
            r'StructuredBuffer\s*<\s*(\w+)\s*>\s*(\w+)\s*:\s*register\s*\(\s*t(\d+)\s*\)', code
        ):
            elem_type, name, reg = m.group(1), m.group(2), int(m.group(3))
            members = struct_layouts.get(elem_type, [])
            # Primitive element type (StructuredBuffer<uint> / <uint3> — the
            # sekiro tile-light index/lookup tables): no struct layout, the
            # element IS the value and `buf[i].x` swizzles it directly.
            elem_prim = elem_type if (not members and
                                      elem_type in self._SB_COMPONENTS) else None
            stride = (self._SB_COMPONENTS[elem_prim] * 4 if elem_prim else
                      sum(self._SB_COMPONENTS.get(bt, 1) * cnt
                          for (_, bt, cnt) in members) * 4)
            self.structured_buffers[name] = {
                'register': reg, 'element_type': elem_type, 'elem_prim': elem_prim,
                'members': members, 'stride': stride, 'data': None,
            }

        # Typed buffers:  Buffer<float4> t1 : register(t1);
        # comp count from the element type (float4 -> 4); the captured view's
        # actual element byte size is filled in by load_typed_buffer_data.
        for m in re.finditer(
            r'\bBuffer\s*<\s*(\w+)\s*>\s*(\w+)\s*:\s*register\s*\(\s*t(\d+)\s*\)', code
        ):
            elem_type, name, reg = m.group(1), m.group(2), int(m.group(3))
            comp = self._SB_COMPONENTS.get(elem_type, 4)
            self.typed_buffers[name] = {
                'register': reg, 'comp': comp, 'elem_size': 0, 'data': None,
            }

        # Raw buffers:  ByteAddressBuffer g_InstanceIndexBuffer : register(t20);
        # Accessed by byte address via .Load()/ld_raw (e.g. GPU instancing index
        # tables). Bytes loaded later from buffer_params.csv.
        for m in re.finditer(
            r'\b(?:RW)?ByteAddressBuffer\s+(\w+)\s*:\s*register\s*\(\s*t(\d+)\s*\)', code
        ):
            name, reg = m.group(1), int(m.group(2))
            self.byte_address_buffers[name] = {'register': reg, 'data': None}

    def _captured_sb_strides(self, data_folder: str) -> dict:
        """Read buffer_params.csv and return the captured per-buffer element
        byte sizes, keyed by resource name AND by register slot. The capture's
        ElementByteSize is authoritative (it includes packing the source-derived
        member sum can miss), so load_structured_buffer_data prefers it."""
        params_path = os.path.join(data_folder, 'buffer_params.csv')
        by_name, by_slot = {}, {}
        if not os.path.exists(params_path):
            return {'name': by_name, 'slot': by_slot}
        rows = self.load_csv(params_path)
        if rows and len(rows) >= 2:
            header = [h.strip() for h in rows[0]]

            def col(c):
                return header.index(c) if c in header else -1
            ci_slot, ci_name, ci_elem = col('Slot'), col('Name'), col('ElementByteSize')
            if ci_slot >= 0 and ci_elem >= 0:
                for row in rows[1:]:
                    if len(row) <= max(ci_slot, ci_elem):
                        continue
                    try:
                        slot = int(row[ci_slot])
                        esize = int(row[ci_elem])
                    except ValueError:
                        continue
                    if esize <= 0:
                        continue
                    if 0 <= ci_name < len(row) and row[ci_name].strip():
                        by_name.setdefault(row[ci_name].strip(), esize)
                    by_slot.setdefault(slot, esize)
        return {'name': by_name, 'slot': by_slot}

    def load_structured_buffer_data(self, data_folder: str):
        """
        Load each parsed StructuredBuffer's contents from the captured binary
        (VS_slot_{reg}_res_*_buffer.bin). Keeps the raw bytes; elements are
        decoded on demand to avoid materialising large palettes. The element
        stride prefers the capture's ElementByteSize (buffer_params.csv) over
        the source-derived member sum.
        """
        import glob
        captured = self._captured_sb_strides(data_folder) if self.structured_buffers else None
        for name, sb in self.structured_buffers.items():
            cap = (captured['name'].get(name) or
                   captured['slot'].get(sb['register'])) if captured else 0
            if cap and cap != sb['stride']:
                self.log_output(
                    f"StructuredBuffer '{name}': stride {sb['stride']}B (from source) "
                    f"overridden by captured ElementByteSize {cap}B")
                sb['stride'] = cap
            if sb['stride'] <= 0:
                continue
            reg = sb['register']
            candidates = (
                glob.glob(os.path.join(data_folder, f'VS_slot_{reg}_res_*_buffer.bin'))
                or glob.glob(os.path.join(data_folder, f'*slot_{reg}_res_*_buffer.bin'))
                or glob.glob(os.path.join(data_folder, f'*slot{reg}_res_*buffer.bin'))
            )
            if not candidates:
                self.log_output(f"Warning: StructuredBuffer '{name}' (t{reg}) data file not found")
                continue
            with open(candidates[0], 'rb') as f:
                sb['data'] = f.read()
            n = len(sb['data']) // sb['stride'] if sb['stride'] else 0
            self.log_output(
                f"Loaded StructuredBuffer '{name}' (t{reg}): {n} elements, "
                f"stride {sb['stride']}B from {os.path.basename(candidates[0])}"
            )

    def load_typed_buffer_data(self, data_folder: str):
        """Load each parsed typed Buffer<T>'s bytes from the capture, using
        buffer_params.csv to map register -> .bin file and element byte size.
        Also loads ByteAddressBuffer raw bytes (same CSV, keyed by register)."""
        params_path = os.path.join(data_folder, 'buffer_params.csv')
        if not os.path.exists(params_path) or (
                not self.typed_buffers and not self.byte_address_buffers):
            return
        rows = self.load_csv(params_path)
        if not rows or len(rows) < 2:
            return
        header = [h.strip() for h in rows[0]]

        def col(name):
            return header.index(name) if name in header else -1
        ci_slot, ci_bin = col('Slot'), col('BinFile')
        ci_elem, ci_type = col('ElementByteSize'), col('DescriptorType')
        ci_fmt, ci_voff = col('ViewFormat'), col('ViewByteOffset')
        if ci_slot < 0 or ci_bin < 0:
            return
        # register -> (binfile, elem_size, view_format, view_offset);
        # prefer TypedBuffer rows.
        by_reg = {}
        for row in rows[1:]:
            if len(row) <= max(ci_slot, ci_bin):
                continue
            try:
                slot = int(row[ci_slot])
            except ValueError:
                continue
            dtype = row[ci_type].strip() if 0 <= ci_type < len(row) else ''
            binfile = row[ci_bin].strip()
            try:
                esize = int(row[ci_elem]) if 0 <= ci_elem < len(row) else 0
            except ValueError:
                esize = 0
            fmt = row[ci_fmt].strip() if 0 <= ci_fmt < len(row) else ''
            try:
                voff = int(row[ci_voff]) if 0 <= ci_voff < len(row) else 0
            except ValueError:
                voff = 0
            if slot not in by_reg or dtype == 'TypedBuffer':
                by_reg[slot] = (binfile, esize, fmt, voff)
        for name, tb in self.typed_buffers.items():
            info = by_reg.get(tb['register'])
            if not info:
                continue
            binfile, esize, fmt, voff = info
            binpath = os.path.join(data_folder, binfile)
            if not binfile or not os.path.exists(binpath):
                continue
            with open(binpath, 'rb') as f:
                data = f.read()
            tb['data'] = data[voff:] if voff else data
            tb['elem_size'] = esize or (tb['comp'] * 4)
            tb['view_format'] = fmt
            n = len(tb['data']) // tb['elem_size'] if tb['elem_size'] else 0
            self.log_output(
                f"Loaded typed Buffer '{name}' (t{tb['register']}): {n} elements, "
                f"{tb['elem_size']}B/elem"
                + (f", view {fmt}" if fmt else "")
                + f" from {os.path.basename(binpath)}"
            )

        for name, bab in self.byte_address_buffers.items():
            info = by_reg.get(bab['register'])
            if not info:
                continue
            binfile = info[0]
            binpath = os.path.join(data_folder, binfile)
            if not binfile or not os.path.exists(binpath):
                continue
            with open(binpath, 'rb') as f:
                bab['data'] = f.read()
            self.log_output(
                f"Loaded ByteAddressBuffer '{name}' (t{bab['register']}): "
                f"{len(bab['data'])}B from {os.path.basename(binpath)}"
            )

    def _byte_address_load(self, bab: dict, byte_addr: int, ncomp: int = 1):
        """Load `ncomp` uint32s from a ByteAddressBuffer at byte address
        `byte_addr` (a multiple of 4). Returns an int or list of ints, 0 on OOB."""
        data = bab.get('data')
        if not data:
            return 0 if ncomp == 1 else [0] * ncomp
        out = []
        for i in range(ncomp):
            off = int(byte_addr) + i * 4
            if 0 <= off and off + 4 <= len(data):
                out.append(struct.unpack_from('<I', data, off)[0])
            else:
                out.append(0)
        return out[0] if ncomp == 1 else out

    @staticmethod
    def _decode_view_format(fmt: str, data: bytes, off: int):
        """Decode one typed-buffer element at byte offset `off` per the SRV's
        actual DXGI view format name from buffer_params.csv (new dumps record
        it in the ViewFormat column, e.g. R16G16_FLOAT, R8G8B8A8_UNORM).
        Returns a list of component values, or None when the name isn't a
        uniform-width RGBA format (caller falls back to byte-level inference).
        """
        if not fmt or '_' not in fmt:
            return None
        comp_part, _, kind = fmt.rpartition('_')
        if kind == 'SRGB':  # R8G8B8A8_UNORM_SRGB → treat as UNORM (no gamma)
            comp_part, _, kind = comp_part.rpartition('_')
        bits = [int(b) for b in re.findall(r'[RGBA](\d+)', comp_part)]
        if not bits or any(b != bits[0] for b in bits):
            return None  # mixed widths (R10G10B10A2, R11G11B10) unsupported
        width, n = bits[0], len(bits)
        if off + n * width // 8 > len(data):
            return None
        if kind == 'FLOAT':
            ch = {32: 'f', 16: 'e'}.get(width)
            return list(struct.unpack_from('<%d%s' % (n, ch), data, off)) if ch else None
        if kind == 'UNORM':
            if width == 8:
                return [b / 255.0 for b in data[off:off + n]]
            if width == 16:
                return [v / 65535.0 for v in struct.unpack_from('<%dH' % n, data, off)]
            return None
        if kind == 'SNORM':
            # D3D SNORM: c / (2^(w-1)-1), clamped to [-1, 1] (min-int -> -1).
            if width == 8:
                return [max(v / 127.0, -1.0)
                        for v in struct.unpack_from('<%db' % n, data, off)]
            if width == 16:
                return [max(v / 32767.0, -1.0)
                        for v in struct.unpack_from('<%dh' % n, data, off)]
            return None
        if kind in ('UINT', 'SINT'):
            ch = {8: 'B', 16: 'H', 32: 'I'}.get(width)
            if ch is None:
                return None
            if kind == 'SINT':
                ch = ch.lower()
            return list(struct.unpack_from('<%d%s' % (n, ch), data, off))
        return None

    @staticmethod
    def _infer_4byte_typed_buffer_fmt(data: bytes) -> str:
        """Infer the view format of a 4-byte-element typed buffer from its byte
        distribution. buffer_params.csv records only the element byte size, and a
        4-byte element is equally an R8G8B8A8 (SNORM normal/tangent/quaternion or
        UNORM colour) or an R16G16_FLOAT (packed texcoord). Returns one of
        'snorm' / 'unorm' / 'half'.

        Discriminator (validated against Octopath captures):
        - R16G16_FLOAT first: texcoords are finite and modestly ranged, so every
          element decodes as two finite halfs with |v| < 1024. Conversely
          R8G8B8A8 normalized data almost always yields Inf/NaN when misread as
          a half, because its high bytes are frequently 0x7F/0xFF/0x80 (SNORM
          +/-1, UNORM +1), whose half exponent field is all-ones. So "every
          half across the whole buffer is finite and in range" is a reliable
          positive signal for R16G16_FLOAT (e.g. foliage texcoords 0.2344,
          0.4575), with false positives effectively excluded by checking all
          elements.
        - Otherwise it is R8G8B8A8; SNORM vs UNORM is decided by whether 0xFF
          (UNORM 1.0) outnumbers 0x7F (SNORM 1.0). Normals (0x7F) / quaternions
          (0x7F + 0x81) -> snorm; colour table (all 0xFF) -> unorm; ties ->
          snorm (the common skinning normal/tangent case)."""
        n = len(data) // 4
        if n:
            all_half = True
            for i in range(n):
                for v in struct.unpack_from('<2e', data, i * 4):
                    if not math.isfinite(v) or abs(v) >= 1024.0:
                        all_half = False
                        break
                if not all_half:
                    break
            if all_half:
                return 'half'
        return 'unorm' if data.count(0xFF) > data.count(0x7F) else 'snorm'

    @staticmethod
    def _infer_halfN_typed_buffer(data: bytes, esize: int) -> str:
        """Decide whether an `esize`-byte typed-buffer element (2 bytes per
        declared component) is R16..._FLOAT (`esize//2` half-floats) or a
        float32 vector RenderDoc reported under a wider float view.

        Finiteness alone is not enough: a 'nice' R32G32 value like 0.4375
        (0x3EE00000) splits into two finite halfs (0.0, 1.719), so it would be
        misread as half4. The decisive extra signal is the FLOAT32 reading: a
        genuine R16..._FLOAT buffer's bytes, read as float32, almost always
        produce a denormal (tiny ~1e-41) because a half's 16 high bits land in
        a float32's mantissa. So call it 'half' only when (a) every element is
        finite/in-range as halfs AND (b) the float32 reading yields a denormal
        somewhere — proving the float32 view is garbage. Otherwise default to
        'float' (the long-standing behaviour), so plausible float32 buffers
        (Octopath's R32G32 e.g. event1320/1828) are never disturbed."""
        nhalf = esize // 2
        nflt = esize // 4
        if esize <= 0 or nhalf <= 0:
            return 'float'
        n = len(data) // esize
        if not n:
            return 'float'
        for i in range(n):
            for v in struct.unpack_from('<%de' % nhalf, data, i * esize):
                if not math.isfinite(v) or abs(v) >= 1024.0:
                    return 'float'
        _FLT_MIN_NORMAL = 1.1754943508222875e-38
        if nflt:
            for i in range(n):
                for v in struct.unpack_from('<%df' % nflt, data, i * esize):
                    if 0.0 < abs(v) < _FLT_MIN_NORMAL:   # denormal → float32 view is garbage
                        return 'half'
        return 'float'

    def _typed_buffer_load(self, tb: dict, index: int):
        """Fetch element `index` of a typed buffer as a float list padded to 4
        with D3D's (0,0,0,1) defaults. buffer_params.csv gives only the element
        byte size, not the view format, so infer from bytes-per-declared-
        component: 1 -> R8G8B8A8 SNORM/UNORM or R16G16_FLOAT (disambiguated by
        byte histogram, see _infer_4byte_typed_buffer_fmt), otherwise float32
        with elem_size//4 components (8B -> R32G32, 16B -> R32G32B32A32)."""
        data = tb.get('data')
        esize = tb.get('elem_size') or 0
        if not data or esize <= 0:
            return None
        off = index * esize
        if index < 0 or off + esize > len(data):
            # D3D robust buffer access: an out-of-bounds typed-buffer read
            # returns zero (not "no value" — returning None here would skip
            # the assignment and leave a stale register value).
            return [0.0, 0.0, 0.0, 0.0]
        comp = tb.get('comp') or 4
        # New dumps record the SRV's actual DXGI view format (ViewFormat column
        # in buffer_params.csv) — decode by it directly, no inference needed.
        view_fmt = tb.get('view_format')
        if view_fmt:
            vals = self._decode_view_format(view_fmt, data, off)
            if vals is not None:
                defaults = (0.0, 0.0, 0.0, 1.0)
                while len(vals) < 4:
                    vals.append(defaults[len(vals)])
                return vals[:4]
        if comp and esize // comp == 1:
            fmt = tb.get('norm_fmt')
            if fmt is None:
                fmt = self._infer_4byte_typed_buffer_fmt(data)
                tb['norm_fmt'] = fmt
            if fmt == 'half':
                vals = list(struct.unpack_from('<2e', data, off))
            elif fmt == 'snorm':
                # D3D SNORM8: c/127 clamped to [-1, 1] (0x80 -> -1.0).
                raw = data[off:off + min(esize, 4)]
                vals = [max((b - 256 if b > 127 else b) / 127.0, -1.0) for b in raw]
            else:
                raw = data[off:off + min(esize, 4)]
                vals = [b / 255.0 for b in raw]
        elif comp and esize // comp == 2:
            # 2 bytes per declared component is AMBIGUOUS for a Buffer<float4>
            # (comp 4, 8-byte element): either R16G16B16A16_FLOAT (4 halfs —
            # Octopath's per-bone matrix rows / packed normals) or R32G32_FLOAT
            # (2 floats RenderDoc reported under a float4 view). Disambiguate by
            # the same finiteness signal used for 4-byte: a float32 bit pattern
            # almost always yields a NaN/inf or huge value in some half lane, so
            # "every element finite and in range as halfs" reliably means half.
            fmt = tb.get('half_fmt2')
            if fmt is None:
                fmt = self._infer_halfN_typed_buffer(data, esize)
                tb['half_fmt2'] = fmt
            if fmt == 'half':
                vals = list(struct.unpack_from('<%de' % min(comp, esize // 2),
                                               data, off))
            else:
                vals = list(struct.unpack_from('<%df' % min(esize // 4, 4),
                                               data, off))
        else:
            ncomp = min(esize // 4, 4)
            vals = list(struct.unpack_from('<%df' % ncomp, data, off))
        defaults = (0.0, 0.0, 0.0, 1.0)
        while len(vals) < 4:
            vals.append(defaults[len(vals)])
        return vals

    # Base byte size of a scalar/vector/matrix field type (pre-packing).
    _CB_TYPE_BYTES = {
        'float': 4, 'int': 4, 'uint': 4, 'bool': 4, 'dword': 4,
        'float2': 8, 'int2': 8, 'uint2': 8,
        'float3': 12, 'int3': 12, 'uint3': 12,
        'float4': 16, 'int4': 16, 'uint4': 16,
        'float3x3': 48, 'float3x4': 48, 'float4x3': 48, 'float4x4': 64,
    }

    def _field_register_offsets(self, fields):
        """Return a per-field 16-byte register offset list for a cbuffer.

        Prefers an explicit packoffset(cN) when the source carried one (3Dmigoto
        always does), otherwise replays HLSL's sequential constant-buffer packing
        rules (vectors never straddle a 16-byte register; arrays and matrices are
        register-aligned). Used to slice the raw binary into per-field windows."""
        offsets = []
        cursor = 0  # next free byte
        for field in fields:
            if getattr(field, 'reg_offset', -1) >= 0:
                reg = field.reg_offset
                offsets.append(reg)
                cursor = reg * 16  # resync the running cursor to the explicit slot
            else:
                offsets.append(cursor // 16 if cursor % 16 == 0 else None)
                reg = cursor // 16
            base = self._CB_TYPE_BYTES.get(field.field_type, 16)
            if getattr(field, 'array_size', 0) > 0:
                elem_stride = (base + 15) // 16 * 16  # each element register-aligned
                cursor = reg * 16 + field.array_size * elem_stride
            else:
                # A vector that would straddle a register boundary bumps to the next.
                if base <= 16 and (cursor % 16) + base > 16:
                    cursor = (cursor + 15) // 16 * 16
                if base > 16:  # matrices align to a register
                    cursor = (cursor + 15) // 16 * 16
                cursor += base
        return offsets

    def override_cbuffers_from_binary(self, data_folder: str, stage_prefix: str):
        """Re-load cbuffers from the raw capture binary (constant_<id>.bin via
        {stage}_constant_buffer_info.csv) so exact bit patterns survive. The
        float CSV destroys integers stored as bits (asint/asuint of e.g. an
        int 1 stored as the float denormal 1.4e-45, or -1 as NaN). float values
        round-trip identically, so this only ever adds precision. No-op when the
        info CSV is absent (e.g. the witcher captures), keeping them untouched."""
        info_path = os.path.join(data_folder, f'{stage_prefix}_constant_buffer_info.csv')
        if not os.path.exists(info_path):
            return
        rows = self.load_csv(info_path)
        if not rows or len(rows) < 2:
            return
        header = [h.strip() for h in rows[0]]

        def col(name):
            return header.index(name) if name in header else -1
        ci_slot, ci_bin, ci_name = col('Slot'), col('BinFile'), col('Name')
        if ci_slot < 0 or ci_bin < 0:
            return
        for row in rows[1:]:
            if len(row) <= max(ci_slot, ci_bin):
                continue
            try:
                slot = int(row[ci_slot])
            except ValueError:
                continue
            binfile = row[ci_bin].strip()
            info_name = row[ci_name].strip() if 0 <= ci_name < len(row) else ''
            # Match the target cbuffer by register slot (robust to naming:
            # cb0 / Constants / cbuffer0 all map to b0), then fall back to the
            # cbN convention and finally the info-CSV name.
            cb_def = None
            for cd in self.cbuffers.values():
                if isinstance(cd, CbufferDefinition) and cd.register == slot:
                    cb_def = cd
                    break
            if cb_def is None:
                cb_def = self.cbuffers.get(f'cb{slot}') or self.cbuffers.get(info_name)
            if cb_def is None or not binfile:
                continue
            binpath = os.path.join(data_folder, binfile)
            if not os.path.exists(binpath):
                continue
            with open(binpath, 'rb') as f:
                data = f.read()
            n4 = len(data) // 16
            decoded = [
                [self._flush_denormal(v) for v in struct.unpack_from('<4f', data, i * 16)]
                for i in range(n4)
            ]
            self._cb_raw[f'cb{slot}'] = [
                list(struct.unpack_from('<4i', data, i * 16)) for i in range(n4)
            ]
            # Fill ALL fields from binary: arrays, matrices, scalars, and vectors.
            # This replaces any values previously loaded from the float CSV so
            # exact bit patterns (needed for asint/asuint) survive.
            total_floats = len(data) // 4
            # Flat decoded float list (with FTZ applied) and raw int/uint views.
            decoded_flat = [v for row in decoded for v in row]
            raw_ints  = list(struct.unpack_from(f'<{total_floats}i', data))
            raw_uints = list(struct.unpack_from(f'<{total_floats}I', data))

            # Walk fields tracking the running byte cursor (same packing rules
            # as _field_register_offsets, but at byte granularity so
            # sub-register fields like float3 / float2 / float are positioned
            # correctly).
            cursor = 0
            for field in cb_def.fields:
                ftype = field.field_type
                array_size = getattr(field, 'array_size', 0)
                base = self._CB_TYPE_BYTES.get(ftype, 16)

                # Struct-array field (struct {...} NAME[N]): each element spans
                # `struct_elem_regs` consecutive registers. Store each element as
                # a list of its raw float4 register rows (no transpose) so a
                # `NAME[i]._mRC` accessor reads register R, component C directly —
                # correct for the row_major matrices these structs hold.
                if ftype == '__struct__':
                    elem_regs = getattr(field, 'struct_elem_regs', 1) or 1
                    if getattr(field, 'reg_offset', -1) >= 0:
                        ri0 = field.reg_offset
                    else:
                        ri0 = (cursor + 15) // 16
                    int_rows_all = self._cb_raw[f'cb{slot}']
                    elements, int_elements = [], []
                    # A non-array struct instance (array_size 0, e.g. sekiro's
                    # `struct {...} g_forceParam`) is one element — without
                    # max(1,·) it got NO data and every member read as 0.
                    for j in range(max(1, array_size)):
                        elem_base = ri0 + j * elem_regs
                        rows = [decoded[elem_base + k]
                                for k in range(elem_regs)
                                if elem_base + k < len(decoded)]
                        irows = [int_rows_all[elem_base + k]
                                 for k in range(elem_regs)
                                 if elem_base + k < len(int_rows_all)]
                        elements.append(rows)
                        int_elements.append(irows)
                    if elements:
                        field.data = elements
                        field.struct_int_data = int_elements
                    cursor = (ri0 + max(1, array_size) * elem_regs) * 16
                    continue

                # Compute this field's byte offset.
                if getattr(field, 'reg_offset', -1) >= 0:
                    byte_off = field.reg_offset * 16 + getattr(field, 'comp_off', 0) * 4
                    cursor = byte_off
                else:
                    if base <= 16 and (cursor % 16) + base > 16:
                        cursor = (cursor + 15) // 16 * 16
                    if base > 16:
                        cursor = (cursor + 15) // 16 * 16
                    byte_off = cursor

                fi = byte_off // 4   # flat float index
                ri = byte_off // 16  # register index

                if array_size > 0:
                    # Array: each element is register-aligned.
                    elem_stride = (base + 15) // 16 * 16
                    elem_regs = elem_stride // 16
                    arr = []
                    for j in range(array_size):
                        row_ri = ri + j * elem_regs
                        if row_ri >= len(decoded):
                            break
                        if base == 64 and row_ri + 3 < len(decoded):
                            # float4x4 array element. Transpose column-major
                            # registers to logical rows; keep row_major registers
                            # as-is (see the non-array branch below).
                            regs = [decoded[row_ri + k] for k in range(4)]
                            if getattr(field, 'is_row_major', False):
                                arr.append(regs)
                            else:
                                arr.append([[regs[c][r] for c in range(4)]
                                            for r in range(4)])
                        elif ftype == 'float4x3' and row_ri + 2 < len(decoded):
                            regs = [decoded[row_ri + k] for k in range(3)]
                            if getattr(field, 'is_row_major', False):
                                arr.append(regs)
                            else:   # column-major: 3 cols -> row-major 4x3
                                arr.append([[regs[c][r] for c in range(3)]
                                            for r in range(4)])
                        elif base == 48 and row_ri + 2 < len(decoded):
                            # float3x4 / float3x3: 3 registers, kept as-is
                            arr.append([decoded[row_ri], decoded[row_ri + 1], decoded[row_ri + 2]])
                        else:
                            arr.append(decoded[row_ri])
                    if arr:
                        field.data = arr
                    # Advance cursor past the whole array.
                    cursor = byte_off + array_size * elem_stride
                else:
                    # Non-array field.
                    if base == 64:      # float4x4
                        if ri + 3 < len(decoded):
                            # A plain/column_major matrix stores columns in the
                            # registers; transpose to logical rows so the element
                            # accessor matches. A `row_major` matrix already stores
                            # rows in registers — keep them as-is so the decompiled
                            # `M._m00_m01_m02_m03` accessor (and mul) map straight to
                            # register R, exactly like the disasm cb[base+R] and the
                            # struct-array path (fixes TombRaider WorldToPSSM0 /
                            # ScreenMatrix without disturbing column_major cases).
                            regs = [decoded[ri], decoded[ri + 1],
                                    decoded[ri + 2], decoded[ri + 3]]
                            if getattr(field, 'is_row_major', False):
                                field.data = regs
                            else:
                                field.data = [[regs[c][r] for c in range(4)]
                                              for r in range(4)]
                    elif ftype == 'float4x3':
                        if ri + 2 < len(decoded):
                            regs = [decoded[ri], decoded[ri + 1], decoded[ri + 2]]
                            if getattr(field, 'is_row_major', False):
                                field.data = regs
                            else:   # column-major: 3 cols -> row-major 4x3
                                field.data = [[regs[c][r] for c in range(3)]
                                              for r in range(4)]
                    elif base == 48:    # float3x4 / float3x3
                        if ri + 2 < len(decoded):
                            field.data = [decoded[ri], decoded[ri+1], decoded[ri+2]]
                    elif base == 16:    # float4 / int4 / uint4
                        if ri < len(decoded):
                            if 'int' in ftype or 'uint' in ftype or 'bool' in ftype:
                                raw = raw_ints if 'uint' not in ftype else raw_uints
                                field.data = raw[fi:fi+4] if fi+4 <= total_floats else decoded[ri]
                            else:
                                field.data = decoded[ri]
                    elif base == 12:    # float3 / int3 / uint3
                        if fi + 2 < total_floats:
                            if 'int' in ftype or 'uint' in ftype:
                                raw = raw_ints if 'uint' not in ftype else raw_uints
                                field.data = raw[fi:fi+3]
                            else:
                                field.data = [decoded_flat[fi], decoded_flat[fi+1],
                                              decoded_flat[fi+2]]
                    elif base == 8:     # float2 / int2 / uint2
                        if fi + 1 < total_floats:
                            if 'int' in ftype or 'uint' in ftype:
                                raw = raw_ints if 'uint' not in ftype else raw_uints
                                field.data = raw[fi:fi+2]
                            else:
                                field.data = [decoded_flat[fi], decoded_flat[fi+1]]
                    elif base == 4:     # float / int / uint / bool / dword
                        if fi < total_floats:
                            if 'int' in ftype and 'uint' not in ftype:
                                field.data = raw_ints[fi]
                            elif 'uint' in ftype or 'bool' in ftype or 'dword' in ftype:
                                field.data = raw_uints[fi]
                            else:
                                field.data = decoded_flat[fi]
                    cursor = byte_off + base

    def _structured_buffer_member(self, sb: dict, index: int, member_name: str):
        """Decode one member of structured-buffer element `index` on demand.
        Returns a list (vectors/arrays) or None if out of range / unknown."""
        raw = sb['data']
        if raw is None:
            return None
        stride = sb['stride']
        base = index * stride
        if base < 0 or base + stride > len(raw):
            return None
        # Primitive element (StructuredBuffer<uint3> etc.): `member_name` is
        # really a component swizzle on the element itself (buf[i].x / .xy).
        prim = sb.get('elem_prim')
        if prim and not sb['members']:
            comps = self._SB_COMPONENTS.get(prim, 1)
            fmt = 'f' if 'float' in prim else ('I' if 'uint' in prim else 'i')
            try:
                vals = list(struct.unpack_from(f'<{comps}{fmt}', raw, base))
            except struct.error:
                return None
            if all(c in 'xyzw' for c in member_name.lower()):
                idx4 = {'x': 0, 'y': 1, 'z': 2, 'w': 3}
                return [vals[idx4[c]] if idx4[c] < len(vals) else 0
                        for c in member_name.lower()]
            return vals
        offset_floats = 0
        for (mname, btype, cnt) in sb['members']:
            comps = self._SB_COMPONENTS.get(btype, 1) * cnt
            if mname == member_name:
                fmt = 'f' if 'float' in btype else ('I' if 'uint' in btype else 'i')
                try:
                    return list(struct.unpack_from(f'<{comps}{fmt}', raw, base + offset_floats * 4))
                except struct.error:
                    return None
            offset_floats += comps
        return None

    def _eval_subscript(self, expr: str, local_vars: Dict[str, Any]) -> int:
        """Evaluate an array-subscript expression to an int. Handles plain
        literals (`12`), constant arithmetic (`48/4`, `48/4+1`) and variable
        references (`r1.y`)."""
        expr = expr.strip()
        try:
            return int(expr)
        except ValueError:
            pass
        try:
            v = self.evaluate_expression(expr, local_vars)
            if isinstance(v, list):
                v = v[0] if v else 0
            return int(v)
        except (ValueError, TypeError, IndexError):
            return 0

    def get_pixel_shader_output(self, pixels: List['Pixel']) -> List[List[float]]:
        """
        获取像素着色器的输出颜色
        pixels: 像素列表
        返回: 输出颜色列表
        """
        return [p.ps_output_color if p.ps_output_color else p.color for p in pixels]

    @staticmethod
    def _is_volume_texture(binding, texture_desc) -> bool:
        """True when the sampled resource is a Texture3D — either declared so
        in the HLSL binding or reported so by texture_params.csv (Kind)."""
        kind = getattr(binding, 'kind', '') if binding else ''
        desc_kind = getattr(texture_desc, 'Kind', '') or ''
        return kind == 'Texture3D' or desc_kind == 'Texture3D'

    @staticmethod
    def _array_slice_for(binding, coords) -> int:
        """The array-slice index a Sample/SampleLevel addresses, from the coord
        that follows the spatial ones: Texture1DArray -> coords[1],
        Texture2DArray -> coords[2], TextureCubeArray -> coords[3]. Non-array
        kinds (and a missing/short coord list) -> slice 0, so ordinary textures
        are unaffected. Witcher3's t4 (Texture2DArray) selects its detail-normal
        slice via coords.z this way."""
        kind = getattr(binding, 'kind', 'Texture2D') if binding else 'Texture2D'
        if 'Array' not in kind:
            return 0
        if kind.startswith('Texture1D'):
            i = 1
        elif kind.startswith('TextureCube'):
            i = 3
        else:                       # Texture2DArray (incl. MS)
            i = 2
        if isinstance(coords, list) and len(coords) > i:
            try:
                return max(0, int(round(float(coords[i]))))
            except (TypeError, ValueError):
                return 0
        return 0

    def _find_texture_binding(self, texture_name: str) -> Optional[TextureBinding]:
        """
        根据纹理变量名查找纹理绑定
        texture_name: 纹理变量名，如 DiffuseTexture
        返回: TextureBinding对象或None
        """
        for binding in self.texture_bindings:
            if binding.variable_name == texture_name:
                return binding
        return None

    def _find_sampler_binding(self, sampler_name: str) -> Optional['SamplerBinding']:
        """根据采样器变量名查找采样器绑定 (用于将 Sample 调用中的采样器实参解析到 s 寄存器)"""
        for binding in self.sampler_bindings:
            if binding.variable_name == sampler_name:
                return binding
        return None

    def _load_coord_to_int(self, v) -> int:
        """Convert a Load coordinate/index to int. 3Dmigoto writes raw integer
        register values as float literals — int 1 becomes the denormal
        1.40129846e-45 (`float2(0,1.40129846e-45)`), which value-truncates to
        0. A float in the denormal range is therefore reinterpreted by its
        bit pattern; everything else is a genuine value (round toward zero)."""
        if v is None:
            return 0
        if isinstance(v, int):
            return v
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0
        if math.isnan(f) or math.isinf(f):
            return 0
        if 0.0 < abs(f) < 1.1754943508222875e-38:
            return self._bitcast_to_int(f, True)
        return int(f)

    def _resolve_sampler(self, sampler_name: Optional[str], texture_reg_id: int):
        """解析纹理采样使用的 Sampler。

        优先用 Sample(samplerVar, coord) 中的采样器实参变量名解析到 s 寄存器，
        再以该寄存器索引 sampler_list；解析失败时回退到纹理寄存器索引 (兼容旧流程)，
        仍失败则用 slot 0 或默认 Sampler。
        """
        sampler = None
        if sampler_name:
            sbinding = self._find_sampler_binding(sampler_name)
            if sbinding is not None and 0 <= sbinding.register_id < len(self._sampler_list):
                sampler = self._sampler_list[sbinding.register_id]
        if sampler is None and 0 <= texture_reg_id < len(self._sampler_list):
            sampler = self._sampler_list[texture_reg_id]
        if sampler is None and self._sampler_list:
            sampler = self._sampler_list[0]
        if sampler is None:
            try:
                from texture import Sampler as _Sampler
                sampler = _Sampler()
            except Exception:
                sampler = None
        return sampler

    def load_struct_data_from_csv(self, struct_name: str, csv_path: str):
        """
        从CSV文件加载struct数据
        struct_name: 结构体名称
        csv_path: CSV文件路径
        """
        if struct_name not in self.structs:
            return
        struct_def = self.structs[struct_name]
        rows = self.load_csv(csv_path)
        if not rows or len(rows) < 2:
            return

        header = rows[0]
        data_rows = rows[1:]

        # 建立字段列索引映射
        field_col_indices = {}
        for i, col in enumerate(header):
            col_clean = col.strip()
            if '.' in col_clean:
                parts = col_clean.split('.')
                base_name = parts[0]
                suffix = parts[1]
                if base_name not in field_col_indices:
                    field_col_indices[base_name] = {}
                field_col_indices[base_name][suffix] = i

        # 填充字段数据
        for field in struct_def.fields:
            if field.semantic in field_col_indices:
                col_dict = field_col_indices[field.semantic]
                values = []
                for row in data_rows:
                    if 'x' in col_dict and 'y' in col_dict and 'z' in col_dict and 'w' in col_dict:
                        x = float(row[col_dict['x']].strip())
                        y = float(row[col_dict['y']].strip())
                        z = float(row[col_dict['z']].strip())
                        w = float(row[col_dict['w']].strip())
                        values.append([x, y, z, w])
                    elif 'x' in col_dict and 'y' in col_dict and 'z' in col_dict:
                        x = float(row[col_dict['x']].strip())
                        y = float(row[col_dict['y']].strip())
                        z = float(row[col_dict['z']].strip())
                        values.append([x, y, z])
                    elif 'x' in col_dict and 'y' in col_dict:
                        x = float(row[col_dict['x']].strip())
                        y = float(row[col_dict['y']].strip())
                        values.append([x, y])
                    else:
                        val_str = row[col_dict['x']].strip().strip('"')
                        values.append(self.parse_value_by_type(val_str, field.field_type))
                field.data = values
                self.log_output(f"Field '{field.semantic}' ({field.field_type}): {values[0] if values else 'N/A'}")

    def load_cbuffer_data_from_csv(self, cb_name: str, csv_path: str):
        """
        从CSV文件加载cbuffer数据
        cb_name: cbuffer名称
        csv_path: CSV文件路径
        """
        if cb_name not in self.cbuffers:
            return
        cb_def = self.cbuffers[cb_name]
        rows = self.load_csv(csv_path)
        if not rows or len(rows) < 2:
            return

        header = rows[0]
        name_idx = header.index('Name') if 'Name' in header else -1
        value_idx = header.index('Value') if 'Value' in header else -1
        type_idx = header.index('Type') if 'Type' in header else -1

        if name_idx == -1 or value_idx == -1:
            return

        matrix_rows = {}
        scalar_vars = {}

        for row in rows[1:]:
            if len(row) <= max(name_idx, value_idx):
                continue
            var_name = row[name_idx].strip().strip('"')
            value_str = row[value_idx].strip().strip('"') if value_idx < len(row) else ''
            type_str = row[type_idx].strip().strip('"') if type_idx != -1 and type_idx < len(row) else ''

            # 跳过空值
            if value_str == '':
                continue

            # 矩阵行处理(如 World.row0, World.row1)
            if '.' in var_name:
                parts = var_name.split('.')
                base_name = parts[0]
                suffix = parts[1]
                if suffix.startswith('row'):
                    row_idx = int(suffix[3:])
                    if base_name not in matrix_rows:
                        matrix_rows[base_name] = {}
                    matrix_rows[base_name][row_idx] = (value_str, type_str)
            else:
                scalar_vars[var_name] = (value_str, type_str)

        # 填充字段数据
        for field in cb_def.fields:
            if getattr(field, 'array_size', 0) > 0:
                # 数组cbuffer: 元素命名为 <name>_v0, <name>_v1, ...
                elements = []
                for i in range(field.array_size):
                    key = f'{field.name}_v{i}'
                    if key in scalar_vars:
                        value_str, type_str = scalar_vars[key]
                        et = type_str if type_str else field.field_type
                        elements.append(self.parse_value_by_type(value_str, et))
                    else:
                        elements.append(None)
                field.data = elements
            elif field.name in matrix_rows:
                row_dict = matrix_rows[field.name]
                if all(i in row_dict for i in range(4)):
                    matrix = []
                    for i in range(4):
                        value_str, type_str = row_dict[i]
                        parts = value_str.split(',')
                        matrix.append([float(p.strip()) for p in parts[:4]])
                    field.data = matrix
            elif field.name in scalar_vars:
                value_str, type_str = scalar_vars[field.name]
                field.data = self.parse_value_by_type(value_str, type_str)

        # 打印cbuffer内容
        cb_n = cb_name
        cb_d = cb_def
        self.log_output(f"Cbuffer {cb_n}:")
        for f in cb_d.fields:
            data = f.data
            ft = f.field_type
            if getattr(f, 'array_size', 0) > 0:
                self.log_output(f"  {f.name} ({ft}[{f.array_size}]):")
                for i, elem in enumerate(data or []):
                    if elem is None:
                        self.log_output(f"    [{i}] <none>")
                    elif isinstance(elem, list):
                        self.log_output(f"    [{i}] [{', '.join(f'{v:.5f}' for v in elem)}]")
                    else:
                        self.log_output(f"    [{i}] {elem}")
                continue
            if data is None:
                self.log_output(f"  {f.name} ({ft}): <not loaded>")
                continue
            if 'float4x4' in ft:
                self.log_output(f"  {f.name} ({ft}):")
                for row in data:
                    row_str = '  '.join(f"{v:12.5f}" for v in row)
                    self.log_output(f"    [{row_str}]")
            elif 'float3x3' in ft:
                self.log_output(f"  {f.name} ({ft}):")
                for row in data:
                    row_str = '  '.join(f"{v:12.5f}" for v in row)
                    self.log_output(f"    [{row_str}]")
            elif 'float4' in ft:
                self.log_output(f"  {f.name} ({ft}): [{', '.join(f'{v:.5f}' for v in data)}]")
            elif 'float3' in ft:
                self.log_output(f"  {f.name} ({ft}): [{', '.join(f'{v:.5f}' for v in data)}]")
            elif 'float2' in ft:
                self.log_output(f"  {f.name} ({ft}): [{', '.join(f'{v:.5f}' for v in data)}]")
            elif 'float' in ft:
                self.log_output(f"  {f.name} ({ft}): {data:.5f}")
            elif 'uint4' in ft:
                self.log_output(f"  {f.name} ({ft}): [{', '.join(str(v) for v in data)}]")
            elif 'uint3' in ft:
                self.log_output(f"  {f.name} ({ft}): [{', '.join(str(v) for v in data)}]")
            elif 'uint2' in ft:
                self.log_output(f"  {f.name} ({ft}): [{', '.join(str(v) for v in data)}]")
            elif 'uint' in ft:
                self.log_output(f"  {f.name} ({ft}): {data}")
            elif 'int4' in ft:
                self.log_output(f"  {f.name} ({ft}): [{', '.join(str(v) for v in data)}]")
            elif 'int3' in ft:
                self.log_output(f"  {f.name} ({ft}): [{', '.join(str(v) for v in data)}]")
            elif 'int2' in ft:
                self.log_output(f"  {f.name} ({ft}): [{', '.join(str(v) for v in data)}]")
            elif 'int' in ft:
                self.log_output(f"  {f.name} ({ft}): {data}")
            elif 'bool' in ft:
                self.log_output(f"  {f.name} ({ft}): {data}")
            else:
                self.log_output(f"  {f.name} ({ft}): {data}")

    def load_vs_output_golden_from_csv(self, csv_path: str):
        """
        从CSV文件加载VS_OUTPUT的golden数据
        csv_path: CSV文件路径
        """
        if "VS_OUTPUT" not in self.structs:
            self.log_output("Error: VS_OUTPUT struct not defined")
            return False

        vs_output_def = self.structs["VS_OUTPUT"]
        rows = self.load_csv(csv_path)
        if not rows or len(rows) < 2:
            self.log_output(f"Error: CSV file {csv_path} is empty or has no data rows")
            return False

        header = rows[0]
        data_rows = rows[1:]

        field_col_indices = {}
        for i, col in enumerate(header):
            col_clean = col.strip()
            if '.' in col_clean:
                parts = col_clean.split('.')
                base_name = parts[0]
                suffix = parts[1]
                if base_name not in field_col_indices:
                    field_col_indices[base_name] = {}
                field_col_indices[base_name][suffix] = i

        for field in vs_output_def.fields:
            if field.semantic in field_col_indices:
                col_dict = field_col_indices[field.semantic]
                values = []
                for row in data_rows:
                    try:
                        if 'x' in col_dict and 'y' in col_dict and 'z' in col_dict and 'w' in col_dict:
                            x = float(row[col_dict['x']].strip())
                            y = float(row[col_dict['y']].strip())
                            z = float(row[col_dict['z']].strip())
                            w = float(row[col_dict['w']].strip())
                            values.append([x, y, z, w])
                        elif 'x' in col_dict and 'y' in col_dict and 'z' in col_dict:
                            x = float(row[col_dict['x']].strip())
                            y = float(row[col_dict['y']].strip())
                            z = float(row[col_dict['z']].strip())
                            values.append([x, y, z])
                        elif 'x' in col_dict and 'y' in col_dict:
                            x = float(row[col_dict['x']].strip())
                            y = float(row[col_dict['y']].strip())
                            values.append([x, y])
                        else:
                            val_str = row[col_dict['x']].strip().strip('"')
                            values.append(self.parse_value_by_type(val_str, field.field_type))
                    except (ValueError, IndexError) as e:
                        self.log_output(f"Warning: Failed to parse {field.semantic} at row: {e}")
                        values.append(None)
                field.data = values

        self.log_output(f"Loaded {len(data_rows)} golden data rows for VS_OUTPUT")
        return True

    def compare_vs_output_with_golden(self, hlsl_output: List[Dict], output_struct_name: str = "VS_OUTPUT", float_tolerance: float = 0.0001, execute_count: int = None) -> bool:
        """
        比较HLSL执行结果与golden数据
        hlsl_output: executeVS返回的输出结构体字典列表
        output_struct_name: 输出结构体名称，用于获取field name (默认"VS_OUTPUT")
        float_tolerance: 浮点类型数据的比较误差容忍度
        execute_count: 执行次数（如果为None则使用golden数据计算行数）
        返回: True表示所有数据匹配, False表示存在不匹配
        """
        if output_struct_name not in self.structs:
            self.log_output(f"Error: {output_struct_name} struct not found")
            return False

        vs_output_def = self.structs[output_struct_name]
        golden_data = {}
        semantic_to_field = {}

        for field in vs_output_def.fields:
            if field.data:
                golden_data[field.semantic] = field.data
            semantic_to_field[field.semantic] = field.name

        num_golden_rows = 0
        for field_data in golden_data.values():
            if field_data:
                num_golden_rows = max(num_golden_rows, len(field_data))

        if execute_count is not None:
            num_golden_rows = execute_count

        if not hlsl_output:
            self.log_output("Error: No HLSL output to compare")
            return False

        if len(hlsl_output) != num_golden_rows:
            self.log_output(f"Error: Row count mismatch - HLSL output has {len(hlsl_output)} rows, golden has {num_golden_rows} rows")
            return False

        all_match = True
        passed_count = 0
        field_type_map = {}
        for field in vs_output_def.fields:
            field_type_map[field.semantic] = field.field_type

        for row_idx in range(len(hlsl_output)):
            output_row = hlsl_output[row_idx]
            row_match = True
            for semantic, golden_values in golden_data.items():
                if row_idx >= len(golden_values):
                    continue

                field_name = semantic_to_field.get(semantic, semantic)
                if field_name not in output_row:
                    continue

                output_value = output_row[field_name]
                golden_value = golden_values[row_idx]

                if output_value is None or golden_value is None:
                    continue

                field_type = field_type_map.get(semantic, '')

                if isinstance(output_value, list) and isinstance(golden_value, list):
                    if len(output_value) != len(golden_value):
                        self.log_output(f"Error: Row {row_idx}, {field_name}: length mismatch output={len(output_value)} golden={len(golden_value)}")
                        row_match = False
                        continue

                    is_float = 'float' in field_type
                    for comp_idx in range(len(output_value)):
                        out_comp = output_value[comp_idx]
                        gold_comp = golden_value[comp_idx]

                        if is_float:
                            if isinstance(out_comp, float) and isinstance(gold_comp, float):
                                if abs(out_comp - gold_comp) > float_tolerance:
                                    self.log_output(f"Error: Row {row_idx}, {field_name}[{comp_idx}]: output={out_comp:.6f} golden={gold_comp:.6f} diff={abs(out_comp - gold_comp):.6f} > tolerance={float_tolerance}")
                                    row_match = False
                            elif out_comp != gold_comp:
                                self.log_output(f"Error: Row {row_idx}, {field_name}[{comp_idx}]: output={out_comp} golden={gold_comp} (float comparison failed)")
                                row_match = False
                        else:
                            if out_comp != gold_comp:
                                self.log_output(f"Error: Row {row_idx}, {field_name}[{comp_idx}]: output={out_comp} golden={gold_comp} (strict equality failed)")
                                row_match = False

            if row_match:
                passed_count += 1
            else:
                all_match = False

        self.log_output(f"Total PASSED rows: {passed_count}/{num_golden_rows}")
        if all_match:
            self.log_output("Comparison PASSED: All output data matches golden data within tolerance")
        else:
            self.log_output("Comparison FAILED: Some output data does not match golden data")

        return all_match

    def get_cbuffer_data(self):
        """
        Get all cbuffer definitions and their data
        Returns: dict of cbuffer_name -> {fields: [{name, field_type, data}, ...]}
        """
        result = {}
        for cb_name, cb_def in self.cbuffers.items():
            if isinstance(cb_def, CbufferDefinition):
                fields_data = []
                for field in cb_def.fields:
                    fields_data.append({
                        'name': field.name,
                        'field_type': field.field_type,
                        'data': field.data
                    })
                result[cb_name] = {'fields': fields_data}
        return result

    def get_hlsl_code(self):
        """
        Get the current HLSL code
        Returns: str - the HLSL code
        """
        return self.hlsl_code

    def get_last_executeVS_code(self):
        """
        Get the last code used in executeVS
        Returns: str - the last code passed to executeVS, or None
        """
        return getattr(self, '_last_executeVS_code', None)

    # =========================================================
    # New param-based execution methods for void main(...) style
    # =========================================================

    def _to_bool(self, val) -> bool:
        """Truthiness for scalars and vectors (non-empty list is true only if any element nonzero)."""
        if val is None:
            return False
        if isinstance(val, list):
            return any(v is not None and v not in (0, 0.0, False) for v in val)
        if isinstance(val, float) and (val != val):  # NaN check
            return False
        return bool(val)

    def _default_value_for_type(self, type_str: str):
        """Return default (zero) value for an HLSL type."""
        defaults = {
            'float4x4': [[0.0, 0.0, 0.0, 0.0]] * 4,
            'float3x3': [[0.0, 0.0, 0.0]] * 3,
            'float4': [0.0, 0.0, 0.0, 0.0],
            'float3': [0.0, 0.0, 0.0],
            'float2': [0.0, 0.0],
            'float': 0.0,
            'uint4': [0, 0, 0, 0], 'uint3': [0, 0, 0], 'uint2': [0, 0], 'uint': 0,
            'int4': [0, 0, 0, 0], 'int3': [0, 0, 0], 'int2': [0, 0], 'int': 0,
            'bool': False,
        }
        v = defaults.get(type_str)
        if isinstance(v, list):
            return [list(row) if isinstance(row, list) else row for row in v]
        return v if v is not None else 0.0

    def _type_component_count(self, type_str: str) -> int:
        """Return number of scalar components for an HLSL type."""
        counts = {
            'float4x4': 16, 'float3x3': 9,
            'float4': 4, 'float3': 3, 'float2': 2, 'float': 1,
            'uint4': 4, 'uint3': 3, 'uint2': 2, 'uint': 1,
            'int4': 4, 'int3': 3, 'int2': 2, 'int': 1,
            'bool': 1,
        }
        return counts.get(type_str, 4)

    def _apply_swizzle_assign(self, var_name: str, swizzle: str, value, local_vars: dict):
        """Assign components of a vector variable via swizzle notation."""
        swizzle_map = {'x': 0, 'y': 1, 'z': 2, 'w': 3, 'r': 0, 'g': 1, 'b': 2, 'a': 3}
        current = local_vars.get(var_name)
        if current is None:
            current = [0.0, 0.0, 0.0, 0.0]
        elif not isinstance(current, list):
            current = [float(current), 0.0, 0.0, 0.0]
        else:
            current = list(current)
        # Extend list if swizzle accesses beyond current length
        indices = [swizzle_map[ch] for ch in swizzle.lower() if ch in swizzle_map]
        if indices:
            needed = max(indices) + 1
            while len(current) < needed:
                current.append(0.0)
        if isinstance(value, list):
            for i, ch in enumerate(swizzle.lower()):
                if ch in swizzle_map and i < len(value):
                    current[swizzle_map[ch]] = value[i]
        elif isinstance(value, (int, float)):
            # Keep ints as ints: an integer register (hash/bit-pattern values,
            # e.g. a _RawBits bfrev result) must not be float-coerced or the
            # raw-bits chain breaks (float(559779) reads back as float bits).
            v = value if (isinstance(value, int) and not isinstance(value, bool)) else float(value)
            for ch in swizzle.lower():
                if ch in swizzle_map:
                    current[swizzle_map[ch]] = v
        local_vars[var_name] = current

    def _dedupe_duplicate_out_params(self, code: str) -> str:
        """3Dmigoto names every slot-shared EXTRA output component `pN` — two
        different semantics on one slot collide (witcher's
        `out float p0 : TESS_BLOCK_SIZE0` and `out float p0 :
        TESS_BLOCK_CM_LEVEL0`): the second body assignment overwrites the
        first and the first semantic's value is silently lost. Rename the
        duplicate declarations p0 -> p0__dup1.. and re-target the body
        assignments to them IN PROGRAM ORDER (the decompiler emits the
        writes in declaration order). Only applies when the assignment
        count matches the declaration count — otherwise the source is left
        untouched."""
        decls = list(re.finditer(r'\bout\s+\w+\s+(p\d+)\s*:\s*(\w+)', code))
        by_name = {}
        for m in decls:
            by_name.setdefault(m.group(1), []).append(m)
        for pname, ms in by_name.items():
            if len(ms) < 2:
                continue
            assigns = [a for a in re.finditer(
                r'\b%s(\.\w+)?\s*=' % re.escape(pname), code)
                if a.start() > ms[-1].end()]
            if len(assigns) != len(ms):
                continue
            # rewrite from the end so positions stay valid; index 0 keeps pname
            for k in range(len(ms) - 1, 0, -1):
                newname = f'{pname}__dup{k}'
                a = assigns[k]
                code = (code[:a.start()] + newname +
                        code[a.start() + len(pname):])
                d = ms[k]
                s = d.group(0).replace(pname, newname, 1)
                code = code[:d.start()] + s + code[d.end():]
            self.log_output(
                f"Renamed {len(ms) - 1} duplicate '{pname}' out-param "
                f"declaration(s) (slot-shared semantics collision).")
        return code

    def preprocess_hlsl(self, code: str) -> str:
        """Preprocess HLSL code: expand #define macros, strip preprocessor directives."""
        code = self._dedupe_duplicate_out_params(code)
        defines = {}
        result_lines = []
        for line in code.split('\n'):
            stripped = line.strip()
            if stripped.startswith('#define'):
                parts = stripped.split(None, 2)
                if len(parts) >= 3:
                    defines[parts[1]] = parts[2].strip()
                elif len(parts) == 2:
                    defines[parts[1]] = ''
                continue
            elif stripped.startswith('#'):
                continue
            for name, replacement in defines.items():
                line = re.sub(r'\b' + re.escape(name) + r'\b', replacement, line)
            result_lines.append(line)
        code = '\n'.join(result_lines)
        # 3Dmigoto decompiler artifact: `and rX, rX, l(0x0000ffff)` (extract a
        # packed 16-bit index, e.g. sekiro4's tile-light index low half) is
        # emitted as the nonsense self-ternary `rX = rX ? 0.000000 : 0;` (the
        # mask printed as a float). A real movc with two zero branches is never
        # generated, so rewrite it back to the bitwise AND.
        artifact = re.compile(
            r'\b(\w+\.\w+)\s*=\s*(\w+\.\w+)\s*\?\s*0\.0+\s*:\s*0\s*;')

        def _fix_and_mask(m):
            if m.group(1) != m.group(2):
                return m.group(0)
            self.log_output(
                f"Repaired decompiler artifact: '{m.group(0).strip()}' -> "
                f"'{m.group(1)} = (uint){m.group(1)} & 0xffff;'")
            return f'{m.group(1)} = (uint){m.group(1)} & 0xffff;'
        return artifact.sub(_fix_and_mask, code)

    def parse_main_params_with_semantics(self, code: str, func_name: str = 'main') -> Optional[dict]:
        """
        Parse void main(...) parameter list.
        Returns {'inputs': [...], 'outputs': [...]} where each entry has:
          name, type, semantic, semantic_base, semantic_index, is_out, slot (-1 until mapped)
        """
        pattern = re.compile(
            r'void\s+' + re.escape(func_name) + r'\s*\(\s*(.*?)\s*\)\s*\{',
            re.DOTALL
        )
        match = pattern.search(code)
        if not match:
            return None

        params_str = match.group(1)
        param_list = [p.strip() for p in params_str.split(',') if p.strip()]
        params = []

        for param_str in param_list:
            param_str = param_str.strip()
            is_out = False
            if param_str.lower().startswith('out '):
                is_out = True
                param_str = param_str[4:].strip()
            elif param_str.lower().startswith('inout '):
                param_str = param_str[6:].strip()

            semantic = ''
            if ':' in param_str:
                colon_pos = param_str.index(':')
                type_name_part = param_str[:colon_pos].strip()
                semantic = param_str[colon_pos + 1:].strip()
            else:
                type_name_part = param_str

            parts = type_name_part.split()
            if len(parts) >= 2:
                param_type = parts[0]
                param_name = parts[-1]
            elif len(parts) == 1:
                param_type = parts[0]
                param_name = '_unnamed'
            else:
                continue

            # Parse semantic index: SV_POSITION0 -> base=SV_POSITION, index=0
            semantic_base = semantic
            semantic_index = 0
            idx_match = re.match(r'^(.*?)(\d+)$', semantic)
            if idx_match:
                semantic_base = idx_match.group(1)
                semantic_index = int(idx_match.group(2))

            params.append({
                'name': param_name,
                'type': param_type,
                'semantic': semantic,
                'semantic_base': semantic_base,
                'semantic_index': semantic_index,
                'is_out': is_out,
                'slot': -1,
            })

        return {
            'inputs': [p for p in params if not p['is_out']],
            'outputs': [p for p in params if p['is_out']],
        }

    def load_signature_from_csv(self, csv_path: str) -> dict:
        """
        Parse VS_input_output_signature.csv or PS_input_output_signature.csv.
        Returns {'inputs': [...], 'outputs': [...]} with slot/index/semantic info.
        """
        if not os.path.exists(csv_path):
            return {'inputs': [], 'outputs': []}
        rows = self.load_csv(csv_path)
        if not rows or len(rows) < 2:
            return {'inputs': [], 'outputs': []}

        header = [h.strip() for h in rows[0]]
        type_idx = header.index('Type') if 'Type' in header else 0
        slot_idx = header.index('Slot') if 'Slot' in header else 1
        index_idx = header.index('Index') if 'Index' in header else 2
        name_idx = header.index('SemanticName') if 'SemanticName' in header else 3

        inputs, outputs = [], []
        for row in rows[1:]:
            if len(row) < 4:
                continue
            sig_type = row[type_idx].strip()
            try:
                slot = int(row[slot_idx].strip())
                idx = int(row[index_idx].strip())
            except ValueError:
                continue
            semantic = row[name_idx].strip()
            entry = {'slot': slot, 'index': idx, 'semantic': semantic}
            if sig_type == 'Input':
                inputs.append(entry)
            elif sig_type == 'Output':
                outputs.append(entry)

        return {'inputs': inputs, 'outputs': outputs}

    def map_params_to_signature(self, params: list, signature_entries: list):
        """Fill in slot field for each param by matching semantic base and index."""
        for param in params:
            sem_base = param['semantic_base'].upper()
            sem_idx = param['semantic_index']
            for sig in signature_entries:
                if sig['semantic'].upper() == sem_base and sig['index'] == sem_idx:
                    param['slot'] = sig['slot']
                    break

    def load_ia_vertex_data(self, csv_path: str, vs_input_params: list) -> list:
        """
        Load ia_vertex_data.csv and map columns to VS input param names by semantic.
        Returns list of dicts {param_name: value} per vertex.
        """
        if not os.path.exists(csv_path):
            return []
        rows = self.load_csv(csv_path)
        if not rows or len(rows) < 2:
            return []

        header = [col.strip() for col in rows[0]]
        col_map = {col: i for i, col in enumerate(header)}

        # Build per-param column mapping
        param_col_map = {}
        for param in vs_input_params:
            sem_base = param['semantic_base']
            sem_idx = param['semantic_index']
            ptype = param['type']

            # Try SEMANTIC0 first, then SEMANTIC, then SEMANTIC + index
            candidates = [
                f'{sem_base}{sem_idx}',
                sem_base,
                f'{sem_base}_{sem_idx}',
            ]
            components = {}
            for cand in candidates:
                for suffix in ['x', 'y', 'z', 'w']:
                    col = f'{cand}.{suffix}'
                    if col in col_map:
                        components[suffix] = col_map[col]
                if components:
                    break

            param_col_map[param['name']] = {'type': ptype, 'components': components}

        vertices = []
        for row in rows[1:]:
            vertex = {}
            for param_name, info in param_col_map.items():
                ptype = info['type']
                comp = info['components']
                try:
                    # Collect the components actually present in the data, in
                    # x,y,z,w order (a vertex element always supplies a
                    # contiguous prefix starting at .x).
                    vals = []
                    for suffix in ('x', 'y', 'z', 'w'):
                        if suffix in comp:
                            vals.append(float(row[comp[suffix]]))
                        else:
                            break
                    if not vals:
                        vertex[param_name] = self._default_value_for_type(ptype)
                        continue
                    # The output width follows the *declared* param type, not
                    # the buffer element's component count. When a buffer
                    # element supplies fewer components than the declared type
                    # (e.g. an R32G32B32 POSITION feeding a float4 v0), D3D
                    # initialises the input register to (0,0,0,1) and overwrites
                    # from .x — so the missing trailing components default to
                    # [0,0,0,1][i], i.e. a missing .w becomes 1.0.
                    target = min(self._type_component_count(ptype), 4)
                    d3d_defaults = (0.0, 0.0, 0.0, 1.0)
                    while len(vals) < target:
                        vals.append(d3d_defaults[len(vals)])
                    if target <= 1:
                        vertex[param_name] = vals[0]
                    else:
                        vertex[param_name] = vals[:target]
                except (ValueError, IndexError):
                    vertex[param_name] = self._default_value_for_type(ptype)
            vertices.append(vertex)

        return vertices

    def _decode_vertex_element(self, raw: bytes, fmt: str, comp_count: int,
                               comp_byte_width: int):
        """
        Decode `comp_count` components of a vertex-buffer element from raw bytes
        according to its DXGI format string (e.g. R32G32B32A32_FLOAT,
        R16G16B16A16_UNORM). Returns a float list (length comp_count), or a
        single float when comp_count == 1.
        """
        fmt_u = fmt.upper()
        vals = []
        try:
            if 'R10G10B10A2' in fmt_u:
                # Packed 10/10/10/2-bit components in a single uint32. Used by
                # The Witcher 3 for NORMAL/TANGENT (RenderDoc reports it as the
                # degenerate 'R0G0B0A0_UNORM'); the 2-bit alpha carries the
                # tangent handedness sign.
                packed = struct.unpack_from('<I', raw)[0]
                comps = [
                    (packed & 0x3FF) / 1023.0,
                    ((packed >> 10) & 0x3FF) / 1023.0,
                    ((packed >> 20) & 0x3FF) / 1023.0,
                    ((packed >> 30) & 0x3) / 3.0,
                ]
                vals = comps[:comp_count] if comp_count <= 4 else comps
            elif 'FLOAT' in fmt_u and comp_byte_width == 4:
                vals = list(struct.unpack_from(f'<{comp_count}f', raw))
            elif 'FLOAT' in fmt_u and comp_byte_width == 2:
                # half float
                ints = struct.unpack_from(f'<{comp_count}H', raw)
                vals = [self._half_to_float(h) for h in ints]
            elif 'UNORM' in fmt_u and comp_byte_width == 2:
                ints = struct.unpack_from(f'<{comp_count}H', raw)
                vals = [v / 65535.0 for v in ints]
            elif 'UNORM' in fmt_u and comp_byte_width == 1:
                ints = struct.unpack_from(f'<{comp_count}B', raw)
                vals = [v / 255.0 for v in ints]
            elif 'SNORM' in fmt_u and comp_byte_width == 2:
                ints = struct.unpack_from(f'<{comp_count}h', raw)
                vals = [max(-1.0, v / 32767.0) for v in ints]
            elif 'SNORM' in fmt_u and comp_byte_width == 1:
                ints = struct.unpack_from(f'<{comp_count}b', raw)
                vals = [max(-1.0, v / 127.0) for v in ints]
            elif ('UINT' in fmt_u or 'SINT' in fmt_u) and comp_byte_width == 4:
                code = 'i' if 'SINT' in fmt_u else 'I'
                vals = list(struct.unpack_from(f'<{comp_count}{code}', raw))
            elif ('UINT' in fmt_u or 'SINT' in fmt_u) and comp_byte_width == 2:
                code = 'h' if 'SINT' in fmt_u else 'H'
                vals = list(struct.unpack_from(f'<{comp_count}{code}', raw))
            elif ('UINT' in fmt_u or 'SINT' in fmt_u) and comp_byte_width == 1:
                code = 'b' if 'SINT' in fmt_u else 'B'
                vals = list(struct.unpack_from(f'<{comp_count}{code}', raw))
            else:
                # Fallback: treat as float32
                vals = list(struct.unpack_from(f'<{comp_count}f', raw))
        except struct.error:
            return [0.0] * comp_count if comp_count > 1 else 0.0
        if comp_count == 1:
            return vals[0]
        return vals

    @staticmethod
    def _half_to_float(h: int) -> float:
        """Convert a 16-bit half-float (as uint16) to a Python float."""
        return struct.unpack('<e', struct.pack('<H', h))[0]

    def load_per_instance_data(self, layouts_csv_path: str, data_folder: str,
                               vs_input_params: list, instance_index: int = 0) -> dict:
        """
        Load per-instance VS inputs (PerInstance=True elements in
        ia_input_layouts.csv) for `instance_index` from the captured binary
        vertex buffers (vb_slot{N}_res_{resid}.bin) and map them to VS input
        param names by semantic.

        ia_vertex_data.csv only contains the per-vertex (slot 0) stream, so
        instanced inputs such as INSTANCE_TRANSFORM* default to zero without
        this. Returns {param_name: value}; empty if no per-instance elements.

        NOTE: assumes a single instance draw (all vertices share instance
        `instance_index`); multi-instance draws are not yet modelled.
        """
        if not os.path.exists(layouts_csv_path):
            return {}
        rows = self.load_csv(layouts_csv_path)
        if not rows:
            return {}

        elements = []          # layout element dicts
        vb_bindings = {}       # input_slot -> {byte_offset, byte_stride, resid}
        section = None
        for row in rows:
            if not row or all(str(c).strip() == '' for c in row):
                section = None
                continue
            c0 = str(row[0]).strip()
            if c0 == 'Index':
                section = 'elements'
                continue
            if c0 == 'VertexBuffer':
                section = 'vbuffers'
                continue
            if c0 == 'IndexBuffer':
                section = 'ibuffer'
                continue
            if section == 'elements':
                # Index,Name,Format,CompCount,CompByteWidth,InputSlot,
                #   VBufferResourceId,ByteOffset,PerInstance,InstanceRate
                try:
                    elements.append({
                        'name': row[1].strip(),
                        'format': row[2].strip(),
                        'comp_count': int(row[3]),
                        'comp_byte_width': int(row[4]),
                        'input_slot': int(row[5]),
                        'resid': self._extract_resid(row[6]),
                        'byte_offset': int(row[7]),
                        'per_instance': row[8].strip().lower() == 'true',
                    })
                except (ValueError, IndexError):
                    pass
            elif section == 'vbuffers':
                # Data rows: Slot,ResourceId,ByteOffset,ByteStride,ByteSize
                # (header has a leading 'VertexBuffer' label column the data omits)
                try:
                    slot = int(row[0])
                    vb_bindings[slot] = {
                        'resid': self._extract_resid(row[1]),
                        'byte_offset': int(row[2]),
                        'byte_stride': int(row[3]),
                    }
                except (ValueError, IndexError):
                    pass

        result = {}
        for elem in elements:
            if not elem['per_instance']:
                continue
            binding = vb_bindings.get(elem['input_slot'])
            if binding is None:
                continue
            resid = elem['resid'] or binding['resid']
            bin_path = os.path.join(
                data_folder, f"vb_slot{elem['input_slot']}_res_{resid}.bin"
            )
            if not os.path.exists(bin_path):
                continue
            stride = binding['byte_stride']
            file_off = binding['byte_offset'] + instance_index * stride + elem['byte_offset']
            elem_bytes = elem['comp_count'] * elem['comp_byte_width']
            with open(bin_path, 'rb') as f:
                f.seek(file_off)
                raw = f.read(elem_bytes)
            if len(raw) < elem_bytes:
                continue
            value = self._decode_vertex_element(
                raw, elem['format'], elem['comp_count'], elem['comp_byte_width']
            )
            # Map element semantic name -> VS input param
            for param in vs_input_params:
                base = param['semantic_base']
                idx = param['semantic_index']
                if elem['name'] == f'{base}{idx}' or (idx == 0 and elem['name'] == base):
                    result[param['name']] = value
                    break

        # SV_InstanceID is a system value, not a buffer element.
        for param in vs_input_params:
            if param['semantic_base'].upper() == 'SV_INSTANCEID':
                result[param['name']] = instance_index

        return result

    def _parse_ia_layouts(self, layouts_csv_path: str):
        """Parse ia_input_layouts.csv into (elements, vb_bindings).

        elements: list of {name, format, comp_count, comp_byte_width,
                           input_slot, resid, byte_offset, per_instance}
        vb_bindings: input_slot -> {resid, byte_offset, byte_stride}
        """
        elements, vb_bindings = [], {}
        if not os.path.exists(layouts_csv_path):
            return elements, vb_bindings
        rows = self.load_csv(layouts_csv_path)
        section = None
        for row in rows:
            if not row or all(str(c).strip() == '' for c in row):
                section = None
                continue
            c0 = str(row[0]).strip()
            if c0 == 'Index':
                section = 'elements'; continue
            if c0 == 'VertexBuffer':
                section = 'vbuffers'; continue
            if c0 == 'IndexBuffer':
                section = 'ibuffer'; continue
            if section == 'elements':
                try:
                    elements.append({
                        'name': row[1].strip(),
                        'format': row[2].strip(),
                        'comp_count': int(row[3]),
                        'comp_byte_width': int(row[4]),
                        'input_slot': int(row[5]),
                        'resid': self._extract_resid(row[6]),
                        'byte_offset': int(row[7]),
                        'per_instance': row[8].strip().lower() == 'true',
                    })
                except (ValueError, IndexError):
                    pass
            elif section == 'vbuffers':
                try:
                    vb_bindings[int(row[0])] = {
                        'resid': self._extract_resid(row[1]),
                        'byte_offset': int(row[2]),
                        'byte_stride': int(row[3]),
                    }
                except (ValueError, IndexError):
                    pass
        return elements, vb_bindings

    def load_index_column(self, csv_path: str) -> list:
        """Return the IDX (index-buffer value) column of ia_vertex_data.csv as
        ints — the per-drawn-vertex index used to fetch from a vertex buffer."""
        if not os.path.exists(csv_path):
            return []
        rows = self.load_csv(csv_path)
        if not rows or len(rows) < 2:
            return []
        header = [c.strip().upper() for c in rows[0]]
        if 'IDX' not in header:
            return []
        j = header.index('IDX')
        out = []
        for row in rows[1:]:
            try:
                out.append(int(float(row[j])))
            except (ValueError, IndexError):
                out.append(0)
        return out

    def load_index_list_from_binary(self, ia_layouts_csv: str, data_folder: str,
                                    draw_call_csv: str) -> list:
        """Load the per-drawn-vertex index list directly from the captured index-buffer
        binary (ib_res_{id}.bin) using ia_input_layouts.csv for the IB resource ID /
        stride and draw_call_info.csv for the draw parameters.

        For non-indexed draws (Indexed=False) returns a sequential list
        [VertexOffset, ..., VertexOffset+N-1].  Returns [] if the binary file is
        absent so the caller can fall back to ia_vertex_data.csv.
        """
        # --- Read draw-call parameters ---
        num_indices = 0
        index_offset = 0   # StartIndexLocation (in index units, NOT bytes)
        base_vertex = 0
        vertex_offset = 0
        is_indexed = False
        if os.path.exists(draw_call_csv):
            for row in self.load_csv(draw_call_csv)[1:]:
                if len(row) < 2:
                    continue
                key, val = row[0].strip(), row[1].strip()
                if key == 'NumIndicesOrVertices':
                    try:
                        num_indices = int(val)
                    except ValueError:
                        pass
                elif key == 'IndexOffset':
                    try:
                        index_offset = int(val)
                    except ValueError:
                        pass
                elif key == 'BaseVertex':
                    try:
                        base_vertex = int(val)
                    except ValueError:
                        pass
                elif key == 'VertexOffset':
                    try:
                        vertex_offset = int(val)
                    except ValueError:
                        pass
                elif key == 'Indexed':
                    is_indexed = val.strip().lower() == 'true'
        if num_indices <= 0:
            return []

        if not is_indexed:
            return list(range(vertex_offset, vertex_offset + num_indices))

        # --- Find IB resource id, binding byte-offset, and stride from
        #     ia_input_layouts.csv.  The binding ByteOffset is the start of
        #     the IB VIEW within the binary file; IndexOffset (from the draw
        #     call) is the additional skip in index units. ---
        ib_resid = None
        ib_bind_byte_off = 0   # ByteOffset from the IB binding row
        ib_stride = 4           # default 4-byte (uint32) indices
        if os.path.exists(ia_layouts_csv):
            section = None
            for row in self.load_csv(ia_layouts_csv):
                if not row or all(str(c).strip() == '' for c in row):
                    section = None
                    continue
                c0 = str(row[0]).strip()
                if c0 == 'IndexBuffer':
                    section = 'ibuffer'
                    continue
                if c0 in ('Index', 'VertexBuffer'):
                    section = c0.lower()
                    continue
                if section == 'ibuffer' and len(row) >= 3:
                    resid = self._extract_resid(row[0])
                    if resid is not None:
                        ib_resid = resid
                        try:
                            ib_bind_byte_off = int(row[1])
                        except (ValueError, IndexError):
                            ib_bind_byte_off = 0
                        try:
                            ib_stride = int(row[2])
                        except (ValueError, IndexError):
                            ib_stride = 4

        if ib_resid is None:
            return []

        bin_path = os.path.join(data_folder, f'ib_res_{ib_resid}.bin')
        if not os.path.exists(bin_path):
            return []

        # The actual read position in the binary:
        #   binding byte-offset  (VIEW start in the resource binary)
        # + StartIndexLocation   (draw-call skip, in index units → convert to bytes)
        actual_byte_off = ib_bind_byte_off + index_offset * ib_stride
        with open(bin_path, 'rb') as f:
            f.seek(actual_byte_off)
            raw = f.read(num_indices * ib_stride)

        if len(raw) < num_indices * ib_stride:
            actual = len(raw) // ib_stride
            self.log_output(
                f"Warning: IB binary too short; got {actual} of {num_indices} indices"
            )
            num_indices = actual

        fmt = '<H' if ib_stride == 2 else '<I'
        indices = [
            struct.unpack_from(fmt, raw, i * ib_stride)[0] + base_vertex
            for i in range(num_indices)
        ]
        return indices

    def load_per_vertex_binary_data(self, layouts_csv_path: str, data_folder: str,
                                    vs_input_params: list, idx_list: list,
                                    csv_vertex_data: list = None) -> list:
        """
        Decode per-vertex VS inputs from the captured binary vertex buffers
        (vb_slot{N}_res_{resid}.bin), fetching each drawn vertex by its
        index-buffer value, and return them as per-row overrides.

        Two reasons to prefer the binary over ia_vertex_data.csv:
          1. RenderDoc sometimes dumps an element with a degenerate format
             (e.g. NORMAL/TANGENT as 'R0G0B0A0_UNORM', CompByteWidth 0) as
             zeros in the CSV even though the real bytes are in the .bin. The
             true byte width is inferred from the gap to the next element in
             the slot (or the slot stride); a 4-component/4-byte element is
             The Witcher 3's R10G10B10A2_UNORM normal/tangent packing.
          2. The CSV rounds floats to ~5 decimals; for a 16-bit-UNORM POSITION
             scaled by a large per-mesh factor that rounding amplifies past the
             golden tolerance. The binary carries the exact bytes the GPU used.

        Decoded values are fit to the declared VS-input register width with the
        same (0,0,0,1) defaults as load_ia_vertex_data. Falls back silently
        (no override for that element) when the .bin file is absent.

        `csv_vertex_data` (the values already loaded from ia_vertex_data.csv)
        gates the precision refinement: for a non-degenerate element the binary
        decode is only used when it AGREES with the CSV value, so a format this
        decoder cannot model (e.g. R8G8B8A8_UINT BLENDINDICES on a skinned mesh)
        never overwrites a column the CSV got right. Degenerate elements always
        use the binary (the CSV holds zeros for them).

        Returns a list (len == len(idx_list)) of {param_name: value} overrides;
        empty when there is nothing to override.
        """
        if not idx_list:
            return []
        elements, vb_bindings = self._parse_ia_layouts(layouts_csv_path)
        if not elements:
            return []

        # Group elements per slot to infer the byte span of degenerate elements.
        slot_elems = {}
        for e in elements:
            slot_elems.setdefault(e['input_slot'], []).append(e)
        for elist in slot_elems.values():
            elist.sort(key=lambda e: e['byte_offset'])

        # Map an element's semantic to the VS input param it feeds (if any).
        def _param_for(elem):
            for param in vs_input_params:
                base = param['semantic_base']
                idx = param['semantic_index']
                if elem['name'] == f'{base}{idx}' or (idx == 0 and elem['name'] == base):
                    return param
            return None

        # Collect the non-instanced elements to (re-)decode from the binary VB,
        # each paired with its VS input param and decode parameters.
        jobs = []
        for slot, elist in slot_elems.items():
            binding = vb_bindings.get(slot)
            if binding is None:
                continue
            stride = binding['byte_stride']
            for i, elem in enumerate(elist):
                param = _param_for(elem)
                if param is None:
                    continue
                cc = elem['comp_count'] or 1
                dec_fmt = elem['format']
                cbw = elem['comp_byte_width']
                degenerate = (cbw == 0)
                if cbw > 0:
                    # Normal element: trust the layout's component byte width.
                    read_bytes = cc * cbw
                else:
                    # Degenerate format (CompByteWidth 0, e.g. NORMAL/TANGENT as
                    # 'R0G0B0A0_UNORM'): infer the element's byte span from the
                    # next element's offset in the same slot (or the slot stride
                    # for the trailing element).
                    if i + 1 < len(elist):
                        span = elist[i + 1]['byte_offset'] - elem['byte_offset']
                    elif stride > 0:
                        span = stride - elem['byte_offset']
                    else:
                        span = 0
                    cbw = span // cc if cc else 0
                    if cbw <= 0:
                        continue
                    read_bytes = cc * cbw
                    # A 4-component element packed into 4 bytes is not R8G8B8A8
                    # (RenderDoc would have named that). The Witcher 3 packs
                    # NORMAL/TANGENT as R10G10B10A2_UNORM (10/10/10/2 bits), so
                    # decode the whole element as one uint32.
                    if cc == 4 and span == 4:
                        dec_fmt = 'R10G10B10A2_UNORM'
                        read_bytes = 4
                resid = elem['resid'] or binding['resid']
                bin_path = os.path.join(
                    data_folder, f"vb_slot{slot}_res_{resid}.bin"
                )
                if not os.path.exists(bin_path):
                    continue
                jobs.append((elem, param, degenerate, cbw, dec_fmt, read_bytes,
                             bin_path, stride, binding['byte_offset'],
                             elem['per_instance']))

        if not jobs:
            return []

        # Cache raw buffer bytes per file.
        file_cache = {}
        overrides = [dict() for _ in idx_list]
        for (elem, param, degenerate, cbw, dec_fmt, read_bytes, bin_path,
             stride, base_off, per_instance) in jobs:
            pname = param['name']
            target = min(self._type_component_count(param['type']), 4)
            if bin_path not in file_cache:
                with open(bin_path, 'rb') as f:
                    file_cache[bin_path] = f.read()
            data = file_cache[bin_path]
            for row_i, vtx_idx in enumerate(idx_list):
                # A per-instance attribute is fetched by instance id, not vertex
                # id. RenderDoc's golden mesh dump (and our single-instance
                # execution) covers instance 0, so every drawn vertex reads the
                # same instance-0 slice at the buffer's bind offset. RenderDoc's
                # ia_vertex_data.csv omits per-instance slots entirely, so the
                # binary is the ONLY source for these inputs (e.g. The Witcher 3
                # packs the per-instance world transform + tangent frame into
                # TEXCOORD0 here).
                fetch_idx = 0 if per_instance else vtx_idx
                off = base_off + fetch_idx * stride + elem['byte_offset']
                raw = data[off:off + read_bytes]
                if len(raw) < read_bytes:
                    continue
                value = self._fit_value_to_width(
                    self._decode_vertex_element(raw, dec_fmt, elem['comp_count'], cbw),
                    target,
                )
                # For a degenerate element the CSV column is unreliable (zeros),
                # so the binary decode is the only source of truth — always use
                # it. For a normal element the CSV already holds the value; only
                # replace it with the binary decode when the two AGREE (the
                # binary is then simply higher-precision than the 5-decimal CSV).
                # This guards against formats _decode_vertex_element does not
                # model (e.g. R8G8B8A8_UINT BLENDINDICES) silently corrupting a
                # column that the CSV got right.
                if not degenerate and not per_instance and csv_vertex_data is not None:
                    if row_i >= len(csv_vertex_data):
                        continue
                    csv_val = csv_vertex_data[row_i].get(pname)
                    # Accept the binary decode when it AGREES with the CSV (a
                    # precision refinement) OR when the CSV column is all-zero
                    # while the binary is not — the CSV failed to capture this
                    # element (e.g. a B8G8R8A8_UNORM colour the dump left at 0),
                    # so the binary is the only truth. A non-zero CSV that
                    # disagrees is still kept (protects R8G8B8A8_UINT
                    # BLENDINDICES this decoder cannot model).
                    agree = csv_val is not None and self._values_agree(value, csv_val)
                    rescue = self._is_all_zero(csv_val) and not self._is_all_zero(value)
                    if not (agree or rescue):
                        continue
                overrides[row_i][pname] = value
        return overrides

    _BITWISE_OPS = frozenset({'>>', '<<', '&', '|', '^'})
    _INT_CAST_TYPES = frozenset({'int', 'int2', 'int3', 'int4',
                                 'uint', 'uint2', 'uint3', 'uint4'})

    def _cast_operand_is_vertex_input(self, node) -> bool:
        """True if `node` is a direct reference to a VS input attribute (vN or
        vN.swizzle) — the signal that an int cast is a bit reinterpret."""
        if node is None or getattr(node, 'node_type', None) != 'value':
            return False
        base = str(node.value).strip().split('.')[0]
        return base in self._vertex_input_names

    def _eval_bitwise_operand(self, node, local_vars):
        """Evaluate a bit-operation operand. In a bit-operation context the
        DXBC instruction consumes the register's RAW BITS, so an (int/uint)
        cast of a FLOAT value reinterprets its float32 bit pattern (e.g. the
        packed-attribute `(uint2)v1.zw >> 16`, or witcher's noise hash where
        `(int)floor(x)` feeds iadd/xor as bits). An int value passes through
        (already raw bits); anything not wrapped in an int cast evaluates
        normally."""
        if (node is not None and getattr(node, 'node_type', None) == 'cast'
                and node.value in self._INT_CAST_TYPES
                and not self._is_numeric_literal_node(node.left)):
            inner = self.evaluate_syntax_tree(node.left, local_vars)
            if inner is not None:
                signed = node.value.startswith('int')
                if isinstance(inner, list):
                    return [self._bitcast_to_int(v, signed) for v in inner]
                return self._bitcast_to_int(inner, signed)
        return self.evaluate_syntax_tree(node, local_vars)

    @staticmethod
    def _is_numeric_literal_node(node) -> bool:
        """True for a literal number node (e.g. the `1` in `(uint)1 & mask`).
        Literals are genuine VALUES — bit-reinterpreting them (our numeric
        literals evaluate as floats) would turn 1 into 0x3F800000."""
        if node is None or getattr(node, 'node_type', None) != 'value':
            return False
        s = str(node.value).strip().lstrip('+-')
        if not s:
            return False
        try:
            float(s)
            return True
        except ValueError:
            return bool(re.match(r'^(0[xX][0-9a-fA-F]+|\d+)[uUlL]*$', s))

    @staticmethod
    def _wrap_i32(v: int) -> int:
        """Wrap an int to signed 32-bit two's complement (DXBC iadd/imul)."""
        v &= 0xFFFFFFFF
        return v - 0x100000000 if v >= 0x80000000 else v

    def fix_shift_signedness(self, code: str, disasm_text: str) -> str:
        """Repair `>>` cast signedness from the disasm.

        DXBC has two right shifts: ishr (arithmetic) and ushr (logical), but
        3Dmigoto can render BOTH as `(uint)x >> n` — the witcher noise hash's
        ishr came out as (uint), losing the sign extension. The disasm is
        unambiguous: pair the HLSL `>>` occurrences with the disasm's
        ishr/ushr in program order (1:1 or don't touch anything) and rewrite
        the left-operand cast of each ishr instance to (int), which selects
        the arithmetic path in the evaluator. HLSL `>>` lines that come from
        non-shift instructions (ubfe expansions) break the 1:1 pairing and
        leave the source unchanged — those need the logical shift."""
        if not disasm_text or '>>' not in code:
            return code
        shrs = re.findall(r'\b([iu])shr\b', disasm_text)
        occ = []
        for m in re.finditer(r'>>', code):
            # Skip `>>` inside 3Dmigoto's ubfe/ibfe expansion lines
            # (`... << (32-(A + B)); ... >> (32-C); } else ... >> D;`) —
            # those come from a bitfield-extract instruction, not a shr,
            # and would break the 1:1 pairing below.
            ls = code.rfind('\n', 0, m.start()) + 1
            le = code.find('\n', m.start())
            if '<< (32-' in code[ls:le if le >= 0 else len(code)]:
                continue
            occ.append(m.start())
        if not shrs or len(occ) != len(shrs):
            return code
        out = code
        # rewrite from the end so positions stay valid
        for pos, kind in reversed(list(zip(occ, shrs))):
            if kind != 'i':
                continue
            head = out[:pos]
            m = re.search(r'\(uint[1-4]?\)(\s*[\w.\[\]]+\s*)$', head)
            if m:
                out = (head[:m.start()] + '(int)' + m.group(1) + out[pos:])
        if out is not code:
            self.log_output("Repaired ishr cast signedness from disasm "
                            f"({shrs.count('i')} arithmetic shift(s)).")
        return out

    @staticmethod
    def _bitcast_to_int(v, signed: bool):
        """Reinterpret a value's 32-bit pattern as int/uint. A float is bitcast
        via its float32 encoding; an int passes through (already raw bits)."""
        if isinstance(v, bool) or v is None:
            return int(bool(v))
        if isinstance(v, int):
            return v
        try:
            bits = struct.unpack('<I', struct.pack('<f', float(v)))[0]
        except (struct.error, ValueError, OverflowError):
            return int(v)
        if signed and bits >= 0x80000000:
            return bits - 0x100000000
        return bits

    @staticmethod
    def _f32_to_f16_rtz(f: float) -> int:
        """Convert float32 -> half bit pattern with ROUND-TOWARD-ZERO, the
        rounding mode D3D specifies for the f32tof16 instruction (plain
        struct.pack('<e') rounds to nearest-even and can land one ULP high).
        Overflow clamps to the largest finite half (IEEE RTZ); only a real
        inf input yields inf. NaN keeps a quiet-NaN payload."""
        bits = struct.unpack('<I', struct.pack('<f', f))[0]
        sign = (bits >> 16) & 0x8000
        exp = (bits >> 23) & 0xFF
        man = bits & 0x7FFFFF
        if exp == 0xFF:                      # inf / NaN
            return sign | 0x7C00 | (0x200 if man else 0)
        e = exp - 127 + 15                   # rebias to half
        if e >= 0x1F:                        # too large: RTZ -> max finite half
            return sign | 0x7BFF
        if e <= 0:                           # subnormal half (or underflow to 0)
            if e < -10:
                return sign
            man = (man | 0x800000) >> (1 - e)
            return sign | (man >> 13)
        return sign | (e << 10) | (man >> 13)

    @staticmethod
    def _to_f32(x):
        """Round a Python float (double) to the nearest float32, the precision
        the GPU actually uses. Lists are rounded element-wise; ints/bools and
        non-finite values pass through unchanged."""
        if isinstance(x, list):
            return [HLSLInterpreter._to_f32(v) for v in x]
        if isinstance(x, bool) or not isinstance(x, float):
            return x
        if x != x or x in (float('inf'), float('-inf')):
            return x
        try:
            return struct.unpack('<f', struct.pack('<f', x))[0]
        except (OverflowError, struct.error):
            return x

    def _f32(self, x):
        """Apply float32 rounding when GPU-precision emulation is enabled."""
        return self._to_f32(x) if self.f32_emulation else x

    # Smallest positive *normal* float32; anything strictly smaller (a subnormal)
    # is flushed to zero, matching the flush-denormals-to-zero (FTZ) mode GPUs
    # run shader arithmetic in.
    _MIN_NORMAL_F32 = 1.1754943508222875e-38

    @staticmethod
    def _flush_denormal(v):
        """Flush a subnormal float32 to 0.0 (GPU FTZ). A cbuffer slot holding an
        integer's bit pattern (e.g. the int 1 -> 0x00000001 -> the float denormal
        1.4e-45) is read by the shader as a *float* condition; on the GPU FTZ
        turns it into 0.0 (falsy), so `cb[i].z ? a : b` takes the false branch.
        Without this the interpreter saw a tiny-but-nonzero value and branched the
        wrong way. asint/asuint are unaffected: they read exact bits from _cb_raw."""
        if isinstance(v, float) and v == v and 0.0 < abs(v) < HLSLInterpreter._MIN_NORMAL_F32:
            return 0.0
        return v

    @staticmethod
    def _is_all_zero(v) -> bool:
        """True if v is None/empty or every component is (near) zero."""
        if v is None:
            return True
        seq = v if isinstance(v, list) else [v]
        if not seq:
            return True
        for x in seq:
            try:
                if abs(float(x)) > 1e-9:
                    return False
            except (TypeError, ValueError):
                return False
        return True

    @staticmethod
    def _values_agree(a, b, rel: float = 1e-2, abs_tol: float = 1e-3) -> bool:
        """True when two decoded vertex values match component-wise within a
        loose tolerance — used to accept a binary decode as a precision refine-
        ment of the CSV value while rejecting an outright mis-decode."""
        la = a if isinstance(a, list) else [a]
        lb = b if isinstance(b, list) else [b]
        n = min(len(la), len(lb))
        if n == 0:
            return False
        for i in range(n):
            try:
                av, bv = float(la[i]), float(lb[i])
            except (TypeError, ValueError):
                return False
            if abs(av - bv) > max(abs_tol, rel * abs(bv)):
                return False
        return True

    def _fit_value_to_width(self, value, target: int):
        """Trim/pad a decoded vertex value to the declared register width using
        D3D's (0,0,0,1) vertex-input defaults (matches load_ia_vertex_data)."""
        if not isinstance(value, list):
            value = [value]
        d3d_defaults = (0.0, 0.0, 0.0, 1.0)
        vals = list(value[:target])
        while len(vals) < target:
            vals.append(d3d_defaults[len(vals)] if len(vals) < 4 else 0.0)
        return vals[0] if target <= 1 else vals

    @staticmethod
    def _extract_resid(resid_str: str):
        """Extract the numeric id from a 'ResourceId::20417' style string."""
        m = re.search(r'(\d+)', str(resid_str))
        return m.group(1) if m else ''

    def load_vs_golden_from_mesh_csv(self, csv_path: str, vs_output_params: list) -> list:
        """
        Load *_vs_mesh.csv / MeshOut_vs_mesh.csv golden VS output.

        RenderDoc's VS-output mesh export lays the data columns out with the
        SV_Position attribute FIRST, then the remaining outputs in declared
        order — even when the column *header* names follow declaration order.
        So for a shader whose SV_Position is not output register 0 (e.g.
        `out float2 TEXCOORD; out float4 SV_Position`), the header labels are
        shifted relative to the actual data: the columns labelled TEXCOORD.x/y
        actually hold SV_Position.x/y.

        We therefore assign the physical component columns positionally in
        [SV_Position, then the rest] order rather than trusting header labels.
        When SV_Position is already the first output (the common case, e.g. the
        Collision suite), this is identical to header-order mapping.

        Returns list of dicts with canonical keys.
        """
        if not os.path.exists(csv_path):
            return []
        rows = self.load_csv(csv_path)
        if not rows or len(rows) < 2:
            return []

        header = [col.strip() for col in rows[0]]
        sem_to_key = self._get_output_semantic_to_key_map()

        # Physical component-column indices, in file order (skips VTX/IDX/...).
        comp_col_indices = [
            i for i, name in enumerate(header)
            if name.rsplit('.', 1)[-1].lower() in ('x', 'y', 'z', 'w')
        ]

        # Per-output *dumped* component count, derived from the header groups
        # (consecutive .xyzw columns sharing a base name). The dump can carry
        # FEWER components than the declared type — e.g. `out float4 o0 :
        # TEXCOORD0` whose shader only writes o0.xyz appears as TEXCOORD0.x/y/z
        # (3 columns). Trusting the declared type would mis-slice every later
        # output, so the header is the source of truth for widths.
        header_count = {}
        for name in header:
            base, _, comp = name.rpartition('.')
            if comp.lower() in ('x', 'y', 'z', 'w') and base:
                header_count[base.upper()] = header_count.get(base.upper(), 0) + 1

        def _dumped_count(param):
            base = param['semantic_base'].upper()
            idx = param['semantic_index']
            for cand in (f'{base}{idx}', base):
                if cand in header_count:
                    return header_count[cand]
            return self._type_component_count(param['type'])

        # Reorder outputs to match the data layout: SV_Position first, then the
        # remaining outputs in declared order (== header order minus position).
        def _is_sv_position(p):
            return p['semantic_base'].upper().startswith('SV_POSITION')
        ordered_params = (
            [p for p in vs_output_params if _is_sv_position(p)]
            + [p for p in vs_output_params if not _is_sv_position(p)]
        )

        # Assign a contiguous slice of physical columns to each output, in
        # SV_Position-first order, each output taking exactly its DUMPED width
        # (`_dumped_count`, from the header group). This is what remaps an
        # arbitrary run of reduced-width outputs — e.g. N consecutive float3s
        # like Octopath event3502's TEXCOORD12(.xyz)+TEXCOORD13(.xyz), or the
        # Collision suite's trailing float3 NORMAL+WORLDPOS — onto the right
        # physical columns: each float3 consumes 3 columns, so the cursor stays
        # aligned and no later output is mis-sliced by assuming float4 widths.
        param_cols = []  # (canonical_key, [col_idx, ...])
        cursor = 0
        for param in ordered_params:
            n = _dumped_count(param)
            cols = comp_col_indices[cursor:cursor + n]
            cursor += n
            sem_base = param['semantic_base']
            sem_idx = param['semantic_index']
            sem_full = f'{sem_base.upper()}{sem_idx}' if sem_idx > 0 else sem_base.upper()
            # Use the full indexed semantic as the key. Falling back to the
            # *base* semantic (e.g. TEXCOORD4 -> TEXCOORD -> 'TexCoord') would
            # collide every unmapped TEXCOORDn onto TEXCOORD0's key, so shaders
            # with >4 TEXCOORD outputs would silently overwrite each other in
            # the golden dict. sem_full is unique per output.
            key = sem_to_key.get(sem_full, sem_full)
            # A float-typed output whose data physically lands in a uint-typed
            # column (RenderDoc lays out columns positionally — see above — so a
            # float output can fall under a `uint PRIMITIVE_ID`/`uint4 PSIZE`
            # header) is printed as its uint32 bit-pattern (bare integer text).
            # Such outputs must be bit-reinterpreted, not read as the integer.
            # Genuinely integer outputs (uint/int) keep their value.
            is_float = str(param.get('type', 'float')).startswith('float')
            param_cols.append((key, cols, is_float))

        # Reconciliation guard: the SV-Position-first, dumped-width assignment
        # must consume EXACTLY the physical component columns. A leftover or
        # overrun means the dumped widths (header groups) disagree with the
        # data layout — the signature of an unhandled mesh-export misalignment.
        # Surface it loudly instead of silently mis-slicing later outputs.
        if cursor != len(comp_col_indices):
            self.log_output(
                f"Warning: golden column reconciliation off by "
                f"{len(comp_col_indices) - cursor} (assigned {cursor} of "
                f"{len(comp_col_indices)} component columns) in "
                f"{os.path.basename(csv_path)} — output widths may be misaligned."
            )

        golden = []
        for row in rows[1:]:
            entry = {}
            for key, cols, is_float in param_cols:
                try:
                    raws = [row[c].strip() for c in cols]
                    # RenderDoc always prints real floats with a decimal point,
                    # so a bare-integer token in a float column is a float
                    # dumped through a uint-typed column — reinterpret the bits
                    # (_golden_float is a no-op on normal decimal/exponent text).
                    if is_float:
                        vals = [self._golden_float(s) for s in raws]
                    else:
                        vals = [float(s) for s in raws]
                except (ValueError, IndexError):
                    continue
                if not vals:
                    continue
                entry[key] = vals[0] if len(vals) == 1 else vals
            golden.append(entry)

        return golden

    @staticmethod
    def _golden_float(s: str) -> float:
        """Parse a golden numeric token, reinterpreting integer-formatted text
        (no '.', no exponent) as a float32 bit-pattern. RenderDoc emits this for
        a float output dumped through a uint-typed column."""
        if '.' not in s and 'e' not in s and 'E' not in s:
            try:
                return struct.unpack('<f', struct.pack('<I', int(s) & 0xFFFFFFFF))[0]
            except ValueError:
                pass
        return float(s)

    def _get_output_semantic_to_key_map(self) -> dict:
        """Map VS/PS output semantic names to canonical dict keys for rasterizer/pixel."""
        return {
            'SV_POSITION': 'sv_position',
            'COLOR': 'Color',
            'COLOR0': 'Color',
            'TEXCOORD': 'TexCoord',
            'TEXCOORD0': 'TexCoord',
            'TEXCOORD1': 'TexCoord2',
            'TEXCOORD2': 'TexCoord3',
            'TEXCOORD3': 'TexCoord4',
            'NORMAL': 'Normal',
            'NORMAL0': 'Normal',
            'WORLDPOS': 'WorldPos',
            'WORLDPOS0': 'WorldPos',
            'SV_TARGET': 'Color',
            'SV_TARGET0': 'Color',
        }

    def load_mesh_output_golden(self, bin_path: str, layout_path: str) -> list:
        """Load a RenderDoc stage mesh-output dump — `<name>_<stage>_mesh.bin`
        plus `<name>_<stage>_mesh_layout.csv` — into golden rows keyed by the
        canonical output key (the same keys the VS/DS/GS results use), for any
        of VS / DS / GS.

        layout.csv:
            Stage,<vs|ds|gs>
            Stride,<bytes-per-vertex>
            NumVerts,<n>
            SemanticName,SemanticIndex,ComponentCount,VarType   (header)
            <name>,<idx>,<comp>,<Float|UInt|SInt>               (one row per attr)

        The .bin is `NumVerts * Stride` bytes; attributes are packed in declared
        order, each occupying `ComponentCount * 4` bytes. This is exact and
        unambiguous — no SV-Position-first reorder, no uint-column bit
        reinterpret, no trailing-float3 gotcha — so it is preferred over the
        `_vs_mesh.csv` golden. Returns [] if the files are absent/malformed."""
        if not (os.path.exists(bin_path) and os.path.exists(layout_path)):
            return []
        sem_to_key = self._get_output_semantic_to_key_map()
        stride = num_verts = 0
        attrs = []           # (key, comp, fmt_char, byte_off)
        off = 0
        in_attrs = False
        _FMT = {'FLOAT': 'f', 'UINT': 'I', 'SINT': 'i', 'INT': 'i', 'UNORM': 'f'}
        for row in self.load_csv(layout_path):
            if not row or not row[0].strip():
                continue
            tag = row[0].strip()
            if tag == 'Stride' and len(row) > 1:
                stride = int(row[1])
            elif tag == 'NumVerts' and len(row) > 1:
                num_verts = int(row[1])
            elif tag == 'SemanticName':
                in_attrs = True           # column header; attribute rows follow
            elif in_attrs and len(row) >= 4:
                name = row[0].strip()
                idx = int(row[1]) if row[1].strip().lstrip('-').isdigit() else 0
                comp = int(row[2]) if row[2].strip().isdigit() else 1
                vtype = row[3].strip()
                sem_full = f'{name.upper()}{idx}' if idx > 0 else name.upper()
                key = sem_to_key.get(sem_full, sem_full)
                attrs.append((key, comp, _FMT.get(vtype.upper(), 'f'), off))
                off += comp * 4
        if stride <= 0 or num_verts <= 0 or not attrs:
            return []
        with open(bin_path, 'rb') as f:
            data = f.read()
        golden = []
        for v in range(num_verts):
            base = v * stride
            if base + stride > len(data):
                break
            entry = {}
            for key, comp, fmt, aoff in attrs:
                try:
                    vals = list(struct.unpack_from('<%d%s' % (comp, fmt),
                                                   data, base + aoff))
                except struct.error:
                    continue
                entry[key] = vals[0] if len(vals) == 1 else vals
            golden.append(entry)
        return golden

    @staticmethod
    def find_stage_mesh_dump(data_folder: str, stage: str):
        """Return (bin_path, layout_path) for a stage's mesh-output dump
        (`*_<stage>_mesh.bin` + `*_<stage>_mesh_layout.csv`), or (None, None).
        `stage` is 'vs' / 'ds' / 'gs'."""
        binf = layf = None
        suffix_bin = f'_{stage}_mesh.bin'
        suffix_lay = f'_{stage}_mesh_layout.csv'
        try:
            names = os.listdir(data_folder)
        except OSError:
            return None, None
        for n in sorted(names):
            low = n.lower()
            if low.endswith(suffix_bin) and binf is None:
                binf = os.path.join(data_folder, n)
            elif low.endswith(suffix_lay) and layf is None:
                layf = os.path.join(data_folder, n)
        return (binf, layf) if (binf and layf) else (None, None)

    def load_all_cbuffers_from_combined_csv(self, csv_path: str):
        """Load all cbuffer data from a combined VS/PS cbuffer CSV file."""
        for cb_name in list(self.cbuffers.keys()):
            self.load_cbuffer_data_from_csv(cb_name, csv_path)

    def _execute_void_main(self, code: str, main_func: str, input_params: list,
                            output_params: list, input_data: dict, row_index: int,
                            capture_locals: dict = None) -> dict:
        """
        Execute a void main(...) style HLSL function.
        Returns dict {output_param_name: value}.
        If capture_locals is provided, the final local-variable environment is
        copied into it (used by quad neighbor-lane re-execution for derivatives).
        """
        local_vars = {}
        # Set input params directly by name. Only FLOAT-typed inputs feed the
        # cast-reinterpret heuristic: a (uint) cast of a float attribute unpacks
        # its bits, whereas a uint/int attribute (e.g. R32_UINT index) cast to
        # int is a genuine value, so it must stay a plain conversion.
        self._vertex_input_names = {
            param['name'] for param in input_params
            if str(param.get('type', '')).startswith('float')
        }
        for param in input_params:
            pname = param['name']
            local_vars[pname] = input_data.get(pname, self._default_value_for_type(param['type']))

        # Initialize output params. RenderDoc's mesh-output dump initialises each
        # 4-component output register to (0,0,0,1) and lets the shader overwrite
        # only the components it actually writes — so an unwritten float4 (or an
        # unwritten .w of a partially-written float4) reads back as w=1, not 0.
        # Match that so the exact bin/layout golden agrees on unwritten lanes.
        for param in output_params:
            pname = param['name']
            if param.get('type') == 'float4':
                local_vars[pname] = [0.0, 0.0, 0.0, 1.0]
            else:
                local_vars[pname] = self._default_value_for_type(param['type'])

        self._eval_counter += 1
        self._should_print = ((self._eval_counter - 1) % self.print_sequence == 0)
        self._dbg = self.debug and self._should_print and not self._in_derivative_eval
        self._sb_index_burst = None

        self.debug_print(f"\n=== ROW {row_index} (void main) ===")

        # Get cached statements for this function
        cache_key = f'void_{main_func}_{id(code)}'
        if cache_key in self._parsed_func_cache:
            statements = list(self._parsed_func_cache[cache_key]['statements'])
        else:
            statements = self._collect_function_statements(main_func)
            self._parsed_func_cache[cache_key] = {'statements': statements, 'body': ''}

        # Execute statements
        i = 0
        while i < len(statements):
            stmt = statements[i]
            if stmt is None:
                i += 1
                continue

            stmt_stripped = stmt.strip()
            if stmt_stripped == 'return' or stmt_stripped.startswith('return;'):
                break

            # End any active structured-buffer index burst (see get_value) when
            # this statement no longer references that indexed load — so the
            # next load re-reads the (now legitimately updated) index.
            if self._sb_index_burst is not None and self._sb_index_burst['token'] not in stmt_stripped:
                self._sb_index_burst = None

            try:
                if stmt_stripped.startswith('if'):
                    next_i = i + 1
                    while next_i < len(statements) and statements[next_i] is None:
                        next_i += 1
                    if (next_i < len(statements) and statements[next_i] and
                            statements[next_i].strip().startswith('else')):
                        full_stmt = stmt + '\n' + statements[next_i]
                        self.execute_if_statement(full_stmt, local_vars)
                        statements[next_i] = None
                    else:
                        self.execute_if_statement(stmt_stripped, local_vars)
                else:
                    self.execute_statement(stmt_stripped, local_vars)
            except (_LoopBreak, _LoopContinue):
                # A stray break/continue outside any loop (decompiler artifact)
                # must not abort the whole shader invocation.
                self.debug_print("[WARN] break/continue outside a loop — ignored")
            i += 1

        # Collect output param values
        result = {}
        for param in output_params:
            result[param['name']] = local_vars.get(param['name'])
        if capture_locals is not None:
            capture_locals.update(local_vars)
        return result

    def _resolve_slot_shared_params(self, output_params: list, result_params: dict):
        """
        Handle output params sharing the same output slot (e.g., TEXCOORD0 and TEXCOORD1).
        If a secondary param was never assigned, derive its value from the primary param's extra components.
        """
        slot_groups: Dict[int, list] = {}
        for param in output_params:
            slot = param.get('slot', -1)
            if slot < 0:
                continue
            if slot not in slot_groups:
                slot_groups[slot] = []
            slot_groups[slot].append(param)

        for slot, params in slot_groups.items():
            if len(params) <= 1:
                continue
            params_sorted = sorted(params, key=lambda p: p.get('semantic_index', 0))
            first = params_sorted[0]
            first_val = result_params.get(first['name'], [])
            first_comp = self._type_component_count(first['type'])

            if isinstance(first_val, list) and len(first_val) > first_comp:
                offset = first_comp
                for param in params_sorted[1:]:
                    cur = result_params.get(param['name'], [])
                    comp = self._type_component_count(param['type'])
                    is_default = not cur or all(
                        v in (0, 0.0, False) for v in (cur if isinstance(cur, list) else [cur])
                    )
                    if is_default:
                        result_params[param['name']] = first_val[offset:offset + comp]
                    offset += comp

    def executeVS_with_params(self, main_func: str, input_params: list, output_params: list,
                                vertex_data: list, execute_count: int = None) -> list:
        """
        Execute vertex shader using parameter-based I/O (void main(...) style).
        Returns list of dicts keyed by canonical names (sv_position, Color, TexCoord, etc.)
        """
        if execute_count is None or execute_count < 0:
            execute_count = len(vertex_data)
        execute_count = min(execute_count, len(vertex_data))

        self._eval_counter = 0
        self.vertex_pool.clear()
        sem_to_key = self._get_output_semantic_to_key_map()

        results = []
        # Live web mesh view: share the growing results list so the browser can
        # animate VS progress (deliverable 2). No-op on tk/html/none viewers.
        _live = self._mesh_view if (self._mesh_view_enabled and
                                    hasattr(self._mesh_view, 'bind_vs_results')) else None
        if _live is not None:
            _live.set_primitive_topology(self.primitive_topology)
            _live.bind_vs_results(results, execute_count)
            _live.show(blocking=False)
        _step = max(1, execute_count // 500)
        for row_index in range(execute_count):
            row_data = vertex_data[row_index]
            result_params = self._execute_void_main(
                self.hlsl_code, main_func, input_params, output_params, row_data, row_index
            )

            # Handle slot-shared params (e.g., TEXCOORD0/TEXCOORD1 sharing slot 2)
            self._resolve_slot_shared_params(output_params, result_params)

            # Map to canonical keys
            canonical = {}
            for param in output_params:
                sem_base = param['semantic_base'].upper()
                sem_idx = param['semantic_index']
                sem_full = f'{sem_base}{sem_idx}' if sem_idx > 0 else sem_base
                # Keep result keys collision-free for >4 TEXCOORD outputs:
                # fall back to the full indexed semantic, never the base (see
                # load_vs_golden_from_mesh_csv for the matching golden side).
                key = sem_to_key.get(sem_full, sem_full)

                value = result_params.get(param['name'])
                comp_count = self._type_component_count(param['type'])
                if isinstance(value, list) and len(value) > comp_count:
                    value = value[:comp_count]
                canonical[key] = value

            results.append(canonical)
            if _live is not None:
                # Per-vertex animation delay (read live so the web slider takes
                # effect at once). When pacing, update progress every vertex for a
                # smooth animation; otherwise throttle to keep overhead low.
                d = _live.get_delay('vertex')
                if d > 0:
                    _live.set_vs_progress(row_index + 1, execute_count)
                    time.sleep(d)
                elif row_index % _step == 0 or row_index + 1 == execute_count:
                    _live.set_vs_progress(row_index + 1, execute_count)

        if _live is not None:
            _live.set_vs_progress(execute_count, execute_count)

        return results

    @staticmethod
    def _assemble_primitives(idx_list: list, topology: int) -> list:
        """Group the drawn vertex indices into input primitives for the GS,
        per D3D primitive topology. Returns a list of tuples of vertex indices.
        topology: 1=pointlist, 2=linelist, 3=linestrip, 4=trianglelist,
        5=trianglestrip (D3D11_PRIMITIVE_TOPOLOGY values)."""
        n = len(idx_list)
        seq = list(range(n))          # positions into the VS-result list
        if topology == 1:             # point list → 1 vertex per primitive
            return [(i,) for i in seq]
        if topology == 2:             # line list
            return [(seq[i], seq[i + 1]) for i in range(0, n - 1, 2)]
        if topology == 3:             # line strip
            return [(seq[i], seq[i + 1]) for i in range(n - 1)]
        if topology == 4:             # triangle list
            return [(seq[i], seq[i + 1], seq[i + 2]) for i in range(0, n - 2, 3)]
        if topology == 5:             # triangle strip
            # D3D strip winding: even triangle i → (i, i+1, i+2); odd triangle →
            # (i, i+2, i+1) (last two swapped to keep consistent facing). The GS
            # sees the vertices in this order — confirmed by the gs_mesh golden.
            out = []
            for i in range(n - 2):
                out.append((seq[i], seq[i + 1], seq[i + 2]) if i % 2 == 0
                           else (seq[i], seq[i + 2], seq[i + 1]))
            return out
        # Unknown/patch topology: treat as a triangle list fallback.
        return [(seq[i], seq[i + 1], seq[i + 2]) for i in range(0, n - 2, 3)]

    def executeGS_with_params(self, main_func: str, gs_input_params: list,
                              gs_output_params: list, gs_input_sig: list,
                              vs_results: list, idx_list: list, topology: int,
                              num_instances: int = 1,
                              expand_strips: bool = False) -> list:
        """Execute the geometry shader over the assembled input primitives and
        return the emitted output vertices (list of dicts keyed by canonical
        output semantic), in Append order — i.e. the GS mesh-output stream.

        `gs_input_sig` is the GS input signature (slot/index/semantic); each GS
        input slot j maps by semantic to a VS-result key, so `v[i][j]` (the
        decompiled per-primitive-vertex accessor) reads primitive-vertex i's
        attribute slot j. `vs_results` are the VS outputs (canonical keys)."""
        sem_to_key = self._get_output_semantic_to_key_map()

        def _canon(sem, idx):
            sem_full = f'{sem.upper()}{idx}' if idx > 0 else sem.upper()
            return sem_to_key.get(sem_full, sem_full)

        # GS input slot -> (canonical VS-result key, semantic upper), by slot.
        slot_meta = [(_canon(s['semantic'], s['index']), s['semantic'].upper())
                     for s in sorted(gs_input_sig, key=lambda r: r['slot'])]
        # GS output param -> (canonical key, component count).
        out_keys = [(_canon(p['semantic_base'], p['semantic_index']),
                     p['name'], min(self._type_component_count(p['type']), 4))
                    for p in gs_output_params]

        prims = self._assemble_primitives(idx_list, topology)
        strips = []      # completed output strips (lists of rows)
        cur = []         # rows of the strip being built

        def _emit(local_vars):
            row = {}
            for key, pname, comp in out_keys:
                val = local_vars.get(pname)
                if isinstance(val, list) and len(val) > comp:
                    val = val[:comp]
                row[key] = val
            cur.append(row)

        def _restart():
            if cur:
                strips.append(list(cur))
                cur.clear()

        self._gs_emit = _emit
        self._gs_restart = _restart
        self._eval_counter = 0
        try:
            for inst in range(max(1, num_instances)):
                for prim in prims:
                    v2d = []
                    for vpos in prim:
                        vs = vs_results[vpos] if 0 <= vpos < len(vs_results) else {}
                        # SV_VertexID for this input vertex = the drawn index
                        # (falls back to the position); SV_InstanceID = inst.
                        vid = (idx_list[vpos] if idx_list and vpos < len(idx_list)
                               else vpos)
                        attrs = []
                        for key, sem in slot_meta:
                            if sem.startswith('SV_VERTEXID'):
                                attrs.append(vs.get(key, vid))
                            elif sem.startswith('SV_INSTANCEID'):
                                attrs.append(inst)
                            else:
                                attrs.append(vs.get(key, [0.0, 0.0, 0.0, 0.0]))
                        v2d.append(attrs)
                    self._execute_gs_main(self.hlsl_code, main_func,
                                          gs_input_params, gs_output_params, v2d, inst)
                    _restart()   # a strip never spans GS invocations
        finally:
            self._gs_emit = None
            self._gs_restart = None

        if not expand_strips:
            return [row for strip in strips for row in strip]
        # RenderDoc's post-GS mesh output stores triangle-STRIP output expanded
        # into a triangle LIST (a 4-vertex quad strip becomes 2 triangles = 6
        # rows), so expand each strip the D3D way: even triangle i = (i, i+1,
        # i+2), odd = (i, i+2, i+1) (last two swapped to keep the winding —
        # verified against the sekiro4 gs_mesh golden). Strips too short to
        # form a triangle emit nothing on the GPU either.
        emitted = []
        for strip in strips:
            for i in range(len(strip) - 2):
                a, b, c = strip[i], strip[i + 1], strip[i + 2]
                emitted.extend((a, b, c) if i % 2 == 0 else (a, c, b))
        return emitted

    def _execute_gs_main(self, code: str, main_func: str, input_params: list,
                         output_params: list, v2d: list, instance_id: int) -> None:
        """Run one GS invocation for a single input primitive. `v2d[i][j]` is
        primitive-vertex i's attribute slot j. Output vertices are produced via
        the Append interception in execute_statement (self._gs_emit)."""
        local_vars = {'v': v2d, 'SV_GSInstanceID': instance_id}
        for param in output_params:
            local_vars[param['name']] = (
                [0.0, 0.0, 0.0, 1.0] if param.get('type') == 'float4'
                else self._default_value_for_type(param['type']))
        self._eval_counter += 1
        self._should_print = ((self._eval_counter - 1) % self.print_sequence == 0)
        self._dbg = self.debug and self._should_print and not self._in_derivative_eval
        self._sb_index_burst = None
        cache_key = f'void_{main_func}_{id(code)}'
        if cache_key in self._parsed_func_cache:
            statements = list(self._parsed_func_cache[cache_key]['statements'])
        else:
            statements = self._collect_function_statements(main_func)
            self._parsed_func_cache[cache_key] = {'statements': statements, 'body': ''}
        for stmt in statements:
            if stmt is None:
                continue
            s = stmt.strip()
            if s == 'return' or s.startswith('return;') or s == 'return':
                break
            self.execute_statement(stmt, local_vars)

    # ---------------------------------------------------------------- HS / DS
    # Hull Shader (control-point phase) + Domain Shader execution. The
    # tessellator fixed-function that sits between them lives in tessellator.py;
    # the HS patch-constant (fork/join) phase — which computes SV_TessFactor —
    # is dropped by the 3Dmigoto decompiler (only the control-point phase is
    # emitted as HLSL), so tess factors default to the minimal patch and the DS
    # is invoked once per patch at a single SV_DomainLocation, matching the
    # RenderDoc `_ds_mesh` golden convention (one DS-out vertex per patch).

    @staticmethod
    def _body_is_trivial(func_statements) -> bool:
        """True if a parsed function body has no executable statements (an empty
        decompiled HS control-point phase → identity passthrough)."""
        for s in (func_statements or []):
            if s is None:
                continue
            t = s.strip()
            if not t or t.startswith('//') or t == 'return' or t.startswith('return;'):
                continue
            return False
        return True

    def executeHS_with_params(self, main_func: str, hs_input_params: list,
                              hs_output_params: list, hs_input_sig: list,
                              hs_output_sig: list, vs_results: list,
                              idx_list: list, input_cp_count: int,
                              output_cp_count: int) -> list:
        """Run the Hull Shader control-point phase. Returns a list of patches;
        each patch is a list of `output_cp_count` output control points, keyed
        by canonical output semantic (the same keys the DS reads via `vicp`).

        For the tessellation captures this project sees, the decompiled HS body
        is empty (an identity passthrough of the VS output control points), so
        the passthrough path copies each input control point straight through by
        semantic. A non-empty body is executed per output control point with the
        input control points available as the 2D arrays `v`/`vicp` and the
        current index as `SV_OutputControlPointID`."""
        sem_to_key = self._get_output_semantic_to_key_map()

        def _canon(sem, idx):
            sem_full = f'{sem.upper()}{idx}' if idx > 0 else sem.upper()
            return sem_to_key.get(sem_full, sem_full)

        # VS-result key for each HS *input* register slot (by semantic).
        in_slot_key = [_canon(s['semantic'], s['index'])
                       for s in sorted(hs_input_sig, key=lambda r: r['slot'])]

        n = len(vs_results)
        icp = max(1, input_cp_count)
        ocp = max(1, output_cp_count)
        num_patches = n // icp

        statements = self._collect_function_statements(main_func)
        trivial = self._body_is_trivial(statements)

        patches = []
        for p in range(num_patches):
            cps_in = [vs_results[p * icp + i] for i in range(icp)]
            out_cps = []
            for cpid in range(ocp):
                if trivial:
                    # Identity passthrough: output control point cpid = input
                    # control point cpid (clamped to available inputs).
                    src = cps_in[min(cpid, icp - 1)]
                    out_cps.append(dict(src))
                else:
                    out_cps.append(self._execute_hs_main(
                        main_func, statements, hs_input_params, hs_output_params,
                        cps_in, in_slot_key, cpid))
            patches.append(out_cps)
        return patches

    def _execute_hs_main(self, main_func, statements, input_params, output_params,
                         cps_in, in_slot_key, cpid) -> dict:
        """Execute one HS control-point-phase invocation for output control
        point `cpid`. Returns canonical-keyed output values."""
        sem_to_key = self._get_output_semantic_to_key_map()
        # 2D control-point arrays the decompiled HS body may index (v[i][j] /
        # vicp[i][j]): i = input control point, j = register slot.
        v2d = []
        for cp in cps_in:
            v2d.append([cp.get(k, [0.0, 0.0, 0.0, 0.0]) for k in in_slot_key])
        local_vars = {'v': v2d, 'vicp': v2d, 'SV_OutputControlPointID': cpid}
        # Seed the DS-style per-control-point named inputs by semantic too.
        for param in input_params:
            key = sem_to_key.get(
                (f"{param['semantic_base'].upper()}{param['semantic_index']}"
                 if param['semantic_index'] > 0 else param['semantic_base'].upper()),
                None)
            src = cps_in[min(cpid, len(cps_in) - 1)]
            if key is not None and key in src:
                local_vars[param['name']] = src[key]
        for param in output_params:
            local_vars[param['name']] = (
                [0.0, 0.0, 0.0, 1.0] if param.get('type') == 'float4'
                else self._default_value_for_type(param['type']))
        for stmt in statements:
            if stmt is None:
                continue
            s = stmt.strip()
            if s == 'return' or s.startswith('return;'):
                break
            self.execute_statement(stmt, local_vars)
        result = {}
        for param in output_params:
            sem_base = param['semantic_base'].upper()
            sem_idx = param['semantic_index']
            sem_full = f'{sem_base}{sem_idx}' if sem_idx > 0 else sem_base
            key = sem_to_key.get(sem_full, sem_full)
            result[key] = local_vars.get(param['name'])
        return result

    def executeDS_with_params(self, main_func: str, ds_input_params: list,
                              ds_output_params: list, ds_input_sig: list,
                              hs_patches: list, domain_points, num_instances: int = 1,
                              patch_constants: list = None) -> list:
        """Execute the Domain Shader over the tessellated patches and return the
        emitted vertices (canonical-keyed) in patch-major, domain-point order —
        i.e. the DS mesh-output stream.

        The DS is invoked once per tessellator-generated domain point of each
        patch (`domain_points` is the list of (u, v[, w]) coordinates the
        tessellator produced — see tessellator.py). For a quad domain at the
        default factor 1 that is the 4 corners per patch.

        `hs_patches` is the HS output (list of patches, each a list of
        control-point dicts keyed by canonical semantic — see
        `executeHS_with_params`). `ds_input_sig` maps DS input register slots to
        semantics, so `vicp[i][j]` = control point i's register slot j. The
        decompiled DS body reads the domain coordinate as the undeclared local
        `vDomain`. `patch_constants` (optional) supplies the HS-fork-phase
        `vpc*` registers per patch; absent captures default to 0 (the fork phase
        is not decompiled)."""
        sem_to_key = self._get_output_semantic_to_key_map()

        def _canon(sem, idx):
            sem_full = f'{sem.upper()}{idx}' if idx > 0 else sem.upper()
            return sem_to_key.get(sem_full, sem_full)

        # DS input register slot -> canonical control-point key (by semantic).
        slot_key = [_canon(s['semantic'], s['index'])
                    for s in sorted(ds_input_sig, key=lambda r: r['slot'])]
        # DS output param -> (canonical key, name, component count).
        out_keys = [(_canon(p['semantic_base'], p['semantic_index']),
                     p['name'], min(self._type_component_count(p['type']), 4))
                    for p in ds_output_params]

        pts = [list(p) + [0.0] * (3 - len(p)) for p in (domain_points or [[0.0, 0.0]])]
        emitted = []
        self._eval_counter = 0
        for p_idx, out_cps in enumerate(hs_patches):
            # vicp[i][j] = control point i's register slot j (float4).
            vicp = []
            for cp in out_cps:
                vicp.append([(cp.get(k) if isinstance(cp.get(k), list)
                              else [cp.get(k, 0.0), 0.0, 0.0, 0.0])
                             for k in slot_key])
            pc = (patch_constants[p_idx]
                  if patch_constants and p_idx < len(patch_constants) else None)
            for dom in pts:
                row = self._execute_ds_main(
                    main_func, ds_input_params, ds_output_params,
                    vicp, slot_key, out_cps, dom, out_keys, pc)
                emitted.append(row)
        return emitted

    def _execute_ds_main(self, main_func, input_params, output_params, vicp,
                         slot_key, out_cps, dom, out_keys, patch_constants) -> dict:
        """Run one DS invocation for a single patch at SV_DomainLocation `dom`."""
        sem_to_key = self._get_output_semantic_to_key_map()
        local_vars = {
            'vicp': vicp, 'v': vicp,          # 2D control-point access
            'vDomain': list(dom),             # SV_DomainLocation (decompiler name)
            'SV_DomainLocation': list(dom),
        }
        # Patch-constant registers vpc0..vpcN (HS fork-phase output). Default 0
        # when the fork phase was not decompiled.
        for k in range(8):
            local_vars[f'vpc{k}'] = (patch_constants[k]
                                     if patch_constants and k < len(patch_constants)
                                     else [0.0, 0.0, 0.0, 0.0])
        # Named per-control-point inputs by semantic (control point 0).
        for param in input_params:
            key = sem_to_key.get(
                (f"{param['semantic_base'].upper()}{param['semantic_index']}"
                 if param['semantic_index'] > 0 else param['semantic_base'].upper()),
                None)
            if key is not None and vicp and slot_key:
                if key in slot_key:
                    j = slot_key.index(key)
                    local_vars[param['name']] = vicp[0][j]
        for param in output_params:
            local_vars[param['name']] = (
                [0.0, 0.0, 0.0, 1.0] if param.get('type') == 'float4'
                else self._default_value_for_type(param['type']))
        self._eval_counter += 1
        self._should_print = ((self._eval_counter - 1) % self.print_sequence == 0)
        self._dbg = self.debug and self._should_print and not self._in_derivative_eval
        self._sb_index_burst = None
        statements = self._collect_function_statements(main_func)
        for stmt in statements:
            if stmt is None:
                continue
            s = stmt.strip()
            if s == 'return' or s.startswith('return;'):
                break
            self.execute_statement(stmt, local_vars)
        row = {}
        for key, pname, comp in out_keys:
            val = local_vars.get(pname)
            if isinstance(val, list) and len(val) > comp:
                val = val[:comp]
            row[key] = val
        return row

    # Maps PS input semantic → the canonical attribute key produced by the
    # rasterizer's barycentric interpolation (see rasterizer._interpolate_*).
    _SEM_TO_CANONICAL = {
        'SV_POSITION': 'sv_position',
        'COLOR': 'Color', 'COLOR0': 'Color',
        'TEXCOORD': 'TexCoord', 'TEXCOORD0': 'TexCoord',
        'TEXCOORD1': 'TexCoord2',
        'TEXCOORD2': 'TexCoord3', 'TEXCOORD3': 'TexCoord4',
        'NORMAL': 'Normal', 'NORMAL0': 'Normal',
        'WORLDPOS': 'WorldPos', 'WORLDPOS0': 'WorldPos',
    }

    def _lane_local_vars(self, lane_attrs: Dict[str, Any]) -> dict:
        """Build a PS main() input_data dict (param name → value) from one quad
        lane's interpolated attributes (canonical keys)."""
        input_data = {}
        for param in (self._ps_input_params or []):
            sem_base = param['semantic_base'].upper()
            sem_idx = param['semantic_index']
            sem_full = f'{sem_base}{sem_idx}' if sem_idx > 0 else sem_base
            key = self._SEM_TO_CANONICAL.get(sem_full, self._SEM_TO_CANONICAL.get(sem_base, ''))
            val = lane_attrs.get(key) if key else None
            if val is None:
                val = self._default_value_for_type(param['type'])
            comp = self._type_component_count(param['type'])
            if isinstance(val, list):
                val = (val + [0.0] * comp)[:comp]
            input_data[param['name']] = val
        return input_data

    def _get_lane_locals(self, lane_idx: int) -> Optional[dict]:
        """Re-execute PS main() for one quad lane and return its final local-var
        environment (cached per current pixel). Used so a Sample coordinate that
        depends on shader locals (e.g. `float2 uv = input.TexCoord;`) resolves for
        neighbor lanes. Derivative eval is disabled during this run (reentrancy +
        log suppression)."""
        cache = self._quad_lane_locals_cache
        if lane_idx in cache:
            return cache[lane_idx]
        if (self._quad_inputs is None or lane_idx >= len(self._quad_inputs)
                or self._ps_code is None):
            cache[lane_idx] = None
            return None
        input_data = self._lane_local_vars(self._quad_inputs[lane_idx])
        captured: dict = {}
        saved_counter = self._eval_counter
        saved_print = self._should_print
        self._in_derivative_eval = True
        TRACE.set_phase('deriv')
        try:
            self._execute_void_main(
                self._ps_code, self._ps_main_func, self._ps_input_params,
                self._ps_output_params, input_data, 0, capture_locals=captured
            )
        finally:
            self._in_derivative_eval = False
            TRACE.set_phase('main')
            self._eval_counter = saved_counter
            self._should_print = saved_print
        cache[lane_idx] = captured
        return captured

    def _compute_uv_derivatives(self, coords_node, local_vars):
        """Compute the per-quad screen-space UV gradient (ddx, ddy) of the *actual*
        sample-coordinate expression via 2x2 quad lockstep re-evaluation.
        Returns (ddx_uv, ddy_uv) 2-comp lists, or (None, None) when no quad context
        (non-triangle primitives, VS, or re-entrant neighbor execution)."""
        if self._quad_inputs is None or coords_node is None or self._in_derivative_eval:
            return None, None

        def lane_uv(lane_idx):
            env = local_vars if lane_idx == self._quad_lane else self._get_lane_locals(lane_idx)
            if env is None:
                return None
            val = self.evaluate_syntax_tree(coords_node, env)
            if isinstance(val, list) and len(val) >= 2:
                return [float(val[0]), float(val[1])]
            return None

        # GPU gradient is constant across the quad: ddx = TR-TL, ddy = BL-TL.
        uv_tl, uv_tr, uv_bl = lane_uv(0), lane_uv(1), lane_uv(2)
        if uv_tl is None or uv_tr is None or uv_bl is None:
            return None, None
        ddx_uv = [uv_tr[0] - uv_tl[0], uv_tr[1] - uv_tl[1]]
        ddy_uv = [uv_bl[0] - uv_tl[0], uv_bl[1] - uv_tl[1]]
        if TRACE.derivatives:
            TRACE.deriv(f"tl=({uv_tl[0]:.5f},{uv_tl[1]:.5f}) "
                        f"tr=({uv_tr[0]:.5f},{uv_tr[1]:.5f}) "
                        f"bl=({uv_bl[0]:.5f},{uv_bl[1]:.5f})", ddx_uv, ddy_uv)
        return ddx_uv, ddy_uv

    def executePS_with_params(self, main_func: str, ps_input_params: list,
                                ps_output_params: list, pixels: list, ps_code: str = None) -> list:
        """Execute pixel shader using parameter-based I/O."""
        self._eval_counter = 0
        code = ps_code or self.hlsl_code

        # Stash PS execution context so quad neighbor lanes can be re-executed
        # for screen-space derivatives (ddx/ddy → texture LOD).
        self._ps_input_params = ps_input_params
        self._ps_output_params = ps_output_params
        self._ps_code = code
        self._ps_main_func = main_func

        # Mapping from PS input semantic to pixel attribute name
        sem_to_pixel = {
            'SV_POSITION': 'sv_pos',
            'COLOR': 'color', 'COLOR0': 'color',
            'TEXCOORD': 'texcoord', 'TEXCOORD0': 'texcoord',
            'TEXCOORD1': 'texcoord2',
            'NORMAL': 'normal', 'NORMAL0': 'normal',
            'WORLDPOS': 'worldPos', 'WORLDPOS0': 'worldPos',
        }

        # Live web mesh view: share the pixel list so the browser can animate
        # PS progress as fragments are shaded (deliverable 3). No-op elsewhere.
        _live = self._mesh_view if (self._mesh_view_enabled and
                                    hasattr(self._mesh_view, 'bind_ps_pixels')) else None
        _ps_total = len(pixels)
        if _live is not None:
            _live.bind_ps_pixels(pixels, _ps_total)
            _live.show(blocking=False)
        _ps_step = max(1, _ps_total // 500)

        for _pix_i, pixel in enumerate(pixels):
            pixel.ps_output_color = None
            self._quad_inputs = pixel.quad_inputs
            self._quad_lane = pixel.quad_lane
            TRACE.set_pixel(pixel.x, pixel.y)
            TRACE.set_phase('main')
            # Neighbor-lane locals are shared by all covered pixels of one quad
            # (same quad_inputs object emitted consecutively by the rasterizer),
            # so only reset the cache when we cross into a new quad.
            if id(pixel.quad_inputs) != self._quad_inputs_id:
                self._quad_lane_locals_cache = {}
                self._quad_inputs_id = id(pixel.quad_inputs)
            input_data = {}

            for param in ps_input_params:
                sem_base = param['semantic_base'].upper()
                sem_idx = param['semantic_index']
                sem_full = f'{sem_base}{sem_idx}' if sem_idx > 0 else sem_base
                attr_name = sem_to_pixel.get(sem_full, sem_to_pixel.get(sem_base, ''))

                if attr_name == 'sv_pos':
                    pixel_val = [float(pixel.x), float(pixel.y), float(pixel.depth), 1.0]
                elif attr_name:
                    pixel_val = getattr(pixel, attr_name, None)
                    if pixel_val is None:
                        pixel_val = self._default_value_for_type(param['type'])
                else:
                    pixel_val = self._default_value_for_type(param['type'])

                # Trim/pad to declared type component count
                comp = self._type_component_count(param['type'])
                if isinstance(pixel_val, list):
                    pixel_val = (pixel_val + [0.0] * comp)[:comp]
                input_data[param['name']] = pixel_val

            result_params = self._execute_void_main(
                code, main_func, ps_input_params, ps_output_params, input_data, 0
            )

            # SV_TARGET0 is the output color
            for param in ps_output_params:
                if 'SV_TARGET' in param['semantic_base'].upper():
                    pixel.ps_output_color = result_params.get(param['name'])
                    break
            if pixel.ps_output_color is None and result_params:
                pixel.ps_output_color = next(iter(result_params.values()))

            if TRACE.ps_pixels:
                TRACE.ps_pixel(pixel.quad_lane, input_data, pixel.ps_output_color)

            if _live is not None:
                # Per-pixel animation delay (read live from the web slider).
                d = _live.get_delay('pixel')
                if d > 0:
                    _live.set_ps_progress(_pix_i + 1, _ps_total)
                    time.sleep(d)
                elif _pix_i % _ps_step == 0 or _pix_i + 1 == _ps_total:
                    _live.set_ps_progress(_pix_i + 1, _ps_total)

        if _live is not None:
            _live.set_ps_progress(_ps_total, _ps_total)

        # Clear quad context so VS / non-quad paths don't see stale state.
        self._quad_inputs = None
        self._quad_lane = 0
        self._quad_lane_locals_cache = {}
        self._quad_inputs_id = None
        self._ps_input_params = None
        self._ps_output_params = None
        self._ps_code = None
        self._ps_main_func = None

        return pixels

    # ------------------------------------------------- single-item instruction trace
    def _trace_single_execution(self, code, main_func, input_params, output_params,
                                input_data, row_index):
        """Run ONE vertex/pixel with per-statement debug capture and return
        (lines, result). Used by the web viewer's Selected Vertex/Pixel Info
        panels — it forces debug on for just this execution, routes the [STMT]
        lines to a buffer, and suppresses the normal run log. Restores all state
        afterwards, so it is safe to call after the pipeline has finished."""
        saved = (self.debug, self._should_print, self._eval_counter,
                 self._in_derivative_eval, self._trace_sink, self._trace_only)
        sink = []
        self._trace_sink = sink
        self._trace_only = True
        self.debug = True
        self._in_derivative_eval = False
        self._eval_counter = 0  # so _execute_void_main makes _should_print True
        try:
            result = self._execute_void_main(
                code, main_func, input_params, output_params, input_data, row_index)
        except Exception as e:
            sink.append(f"[TRACE ERROR] {type(e).__name__}: {e}")
            result = None
        finally:
            (self.debug, self._should_print, self._eval_counter,
             self._in_derivative_eval, self._trace_sink, self._trace_only) = saved
        return sink, result

    def trace_vs_vertex(self, index, main_func, input_params, output_params,
                        vertex_data):
        """Instruction trace for one VS vertex (index into vertex_data)."""
        if not (0 <= index < len(vertex_data)):
            return {'ok': False, 'error': f'vertex index {index} out of range'}
        lines, _ = self._trace_single_execution(
            self.hlsl_code, main_func, input_params, output_params,
            vertex_data[index], index)
        return {'ok': True, 'kind': 'vertex', 'index': index, 'lines': lines}

    _PS_SEM_TO_PIXEL = {
        'SV_POSITION': 'sv_pos',
        'COLOR': 'color', 'COLOR0': 'color',
        'TEXCOORD': 'texcoord', 'TEXCOORD0': 'texcoord',
        'TEXCOORD1': 'texcoord2',
        'NORMAL': 'normal', 'NORMAL0': 'normal',
        'WORLDPOS': 'worldPos', 'WORLDPOS0': 'worldPos',
    }

    def trace_ps_pixel(self, pixel, main_func, ps_input_params, ps_output_params,
                       code=None):
        """Instruction trace for one PS pixel. Rebuilds the pixel's PS inputs the
        same way executePS_with_params does (incl. quad context for derivatives)."""
        code = code or self.hlsl_code
        # Quad context so ddx/ddy neighbour-lane re-execution works.
        self._quad_inputs = getattr(pixel, 'quad_inputs', None)
        self._quad_lane = getattr(pixel, 'quad_lane', 0)
        self._quad_lane_locals_cache = {}
        self._quad_inputs_id = id(pixel.quad_inputs) if getattr(pixel, 'quad_inputs', None) else None
        self._ps_input_params = ps_input_params
        self._ps_output_params = ps_output_params
        self._ps_code = code
        self._ps_main_func = main_func

        input_data = {}
        for param in ps_input_params:
            sem_base = param['semantic_base'].upper()
            sem_idx = param['semantic_index']
            sem_full = f'{sem_base}{sem_idx}' if sem_idx > 0 else sem_base
            attr_name = self._PS_SEM_TO_PIXEL.get(sem_full,
                                                  self._PS_SEM_TO_PIXEL.get(sem_base, ''))
            if attr_name == 'sv_pos':
                pixel_val = [float(pixel.x), float(pixel.y), float(pixel.depth), 1.0]
            elif attr_name:
                pixel_val = getattr(pixel, attr_name, None)
                if pixel_val is None:
                    pixel_val = self._default_value_for_type(param['type'])
            else:
                pixel_val = self._default_value_for_type(param['type'])
            comp = self._type_component_count(param['type'])
            if isinstance(pixel_val, list):
                pixel_val = (pixel_val + [0.0] * comp)[:comp]
            input_data[param['name']] = pixel_val

        lines, _ = self._trace_single_execution(
            code, main_func, ps_input_params, ps_output_params, input_data, 0)

        self._quad_inputs = None
        self._quad_lane = 0
        self._quad_lane_locals_cache = {}
        self._quad_inputs_id = None
        self._ps_input_params = None
        self._ps_output_params = None
        self._ps_code = None
        self._ps_main_func = None
        return {'ok': True, 'kind': 'pixel', 'x': int(pixel.x), 'y': int(pixel.y),
                'lines': lines}

    # 相对容差: golden 由 GPU 以 float32 计算, 解释器用 Python float64。对幅值很大的
    # 输出(如 o2.xy = clip_xy * screen_scale + screen_scale, screen_scale≈1024 时 o2≈2000),
    # clip 坐标里远小于绝对容差的 float32 舍入误差会被放大上千倍, 绝对差超过 0.005,
    # 但相对误差仅 ~1e-5。用 max(绝对容差, 相对容差*|golden|) 组合判定: 仅放宽、不会让
    # 原本通过的分量变成失败, 且真实逻辑错误(相对差量级 >1) 仍会被捕获。
    _REL_TOLERANCE = 2e-5

    @staticmethod
    def _num_agree(ov: float, gv: float, tol: float) -> bool:
        """Whether an output component agrees with golden within tolerance,
        treating special values correctly. If BOTH are NaN (the GPU produced NaN
        for this vertex and we reproduced it — e.g. TombRaider event2867's Color
        for degenerate skinning), that is agreement; likewise matching ±inf. A
        NaN/inf on only ONE side stays a mismatch, so our NaN vs a real golden
        value is still caught. `not (|diff| <= tol)` alone would (wrongly) flag
        NaN==NaN because every comparison with NaN is False."""
        if math.isnan(ov) or math.isnan(gv):
            return math.isnan(ov) and math.isnan(gv)
        if math.isinf(ov) or math.isinf(gv):
            return ov == gv
        return abs(ov - gv) <= tol

    def compare_vs_output_with_golden_params(self, results: list, output_params: list,
                                            golden_rows: list, float_tolerance: float = 0.0001,
                                            execute_count: int = None) -> bool:
        """Compare VS output results against golden data (both using canonical key format)."""
        count = execute_count if execute_count and execute_count > 0 else len(results)
        count = min(count, len(results), len(golden_rows))

        def _tol(golden_value):
            return max(float_tolerance, self._REL_TOLERANCE * abs(golden_value))

        passed = 0
        all_match = True

        for row_idx in range(count):
            result_row = results[row_idx]
            golden_row = golden_rows[row_idx]
            row_match = True

            for key, golden_val in golden_row.items():
                output_val = result_row.get(key)
                if output_val is None or golden_val is None:
                    continue

                if isinstance(output_val, list) and isinstance(golden_val, list):
                    min_len = min(len(output_val), len(golden_val))
                    for comp_idx in range(min_len):
                        ov = output_val[comp_idx]
                        gv = golden_val[comp_idx]
                        if gv is None:
                            continue
                        if isinstance(ov, float) and isinstance(gv, float):
                            if not self._num_agree(ov, gv, _tol(gv)):
                                self.log_output(
                                    f"Error: Row {row_idx} {key}[{comp_idx}]: "
                                    f"output={ov:.6f} golden={gv:.6f} diff={abs(ov-gv):.6f}"
                                )
                                row_match = False
                        elif ov != gv:
                            self.log_output(f"Error: Row {row_idx} {key}[{comp_idx}]: output={ov} golden={gv}")
                            row_match = False
                elif isinstance(output_val, (int, float)) and isinstance(golden_val, (int, float)):
                    if not self._num_agree(float(output_val), float(golden_val),
                                           _tol(float(golden_val))):
                        self.log_output(f"Error: Row {row_idx} {key}: output={output_val:.6f} golden={golden_val:.6f}")
                        row_match = False

            if row_match:
                passed += 1
            else:
                all_match = False

        self.log_output(f"Total PASSED rows: {passed}/{count}")
        if all_match:
            self.log_output("Comparison PASSED: All output data matches golden data within tolerance")
        else:
            self.log_output("Comparison FAILED: Some output data does not match golden data")
        return all_match

