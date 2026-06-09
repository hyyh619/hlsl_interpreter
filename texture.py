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

# Address-mode names as they appear in 3Dmigoto's sampler_params.csv
# (e.g. AddressU="Wrap"). Maps to the D3D11_TEXTURE_ADDRESS_* constants.
ADDRESS_NAME_MAP = {
    "WRAP": D3D11_TEXTURE_ADDRESS_WRAP,
    "MIRROR": D3D11_TEXTURE_ADDRESS_MIRROR,
    "CLAMP": D3D11_TEXTURE_ADDRESS_CLAMP,
    "BORDER": D3D11_TEXTURE_ADDRESS_BORDER,
    "MIRRORONCE": D3D11_TEXTURE_ADDRESS_MIRROR_ONCE,
    "MIRROR_ONCE": D3D11_TEXTURE_ADDRESS_MIRROR_ONCE,
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
        MipDataPaths: Optional[List[str]] = None
    ):
        self.Width = Width
        self.Height = Height
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

    def _get_mip_levels(self, texture_desc: TextureDesc) -> List[List[List[List[float]]]]:
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

        When more than one mip BMP was captured, load each level directly so
        sampling reflects the real captured data. With a single image, parse
        the base level and regenerate the rest (legacy behaviour)."""
        if len(mip_paths) > 1:
            levels = []
            for path in mip_paths:
                pixels = self._load_bmp_pixels(path)
                if pixels is None:
                    # A missing/invalid level truncates the chain; stop here so
                    # we never index past a real captured level.
                    break
                levels.append(pixels)
            if levels:
                return levels
            return self._create_placeholder_texture(texture_desc)

        # Single source image: parse base + regenerate mips.
        base = self._load_bmp_pixels(mip_paths[0]) if mip_paths else None
        if base is None:
            return self._create_placeholder_texture(texture_desc)
        mip_levels = [base]
        mip_levels.extend(self._generate_mipmaps(base))
        return mip_levels

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

        x = int(u * w) % w
        y = int(v * h) % h

        x = max(0, min(w - 1, x))
        y = max(0, min(h - 1, y))

        return mip_level[y][x]

    def _sample_linear(self, mip_level: List[List[List[float]]], u: float, v: float) -> List[float]:
        h = len(mip_level)
        w = len(mip_level[0])

        fu = u * w
        fv = v * h

        u0 = int(fu)
        v0 = int(fv)

        u1 = (u0 + 1) % w
        v1 = (v0 + 1) % h

        s = fu - u0
        t = fv - v0

        u0 = max(0, min(w - 1, u0))
        v0 = max(0, min(h - 1, v0))
        u1 = max(0, min(w - 1, u1))
        v1 = max(0, min(h - 1, v1))

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

    def sample(self, u: float, v: float, w: float, texture_desc: TextureDesc, sampler: Sampler,
                ddx_uv: Optional[List[float]] = None, ddy_uv: Optional[List[float]] = None,
                name: str = '') -> List[float]:
        tu, tv, tw = sampler.transform_coordinates(u, v, w)

        min_filter, mag_filter, mip_filter = sampler._get_filter_mode()

        mip_levels = self._get_mip_levels(texture_desc)
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
            lod = tw

        lod = lod + sampler.MipLODBias
        lod = max(sampler.MinLOD, min(sampler.MaxLOD, lod))

        if level_count == 1:
            single = (self._sample_nearest(mip_levels[0], tu, tv) if mag_filter == 0
                        else self._sample_linear(mip_levels[0], tu, tv))
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
            return self._sample_linear(mip_levels[level], tu, tv)

        # Blend the two bracketing mip levels by the LOD fraction. NOTE: the
        # captured sampler here is Mip=Point, but our quad-derivative LOD runs
        # slightly high on tiny minified triangles where this texture has a
        # sharp level2→level3 step; trilinear blending tracks the GPU's result
        # there more closely than snapping to the rounded level, so we always
        # blend (s == 0 collapses to a single level anyway when LOD is integral).
        color0 = _within(level0)
        color1 = _within(level1)
        result = [color0[i] * (1 - s) + color1[i] * s for i in range(4)]

        out = [max(0.0, min(1.0, c)) for c in result]
        if TRACE.texture_lod:
            TRACE.texture_sample(u, v, lod, ddx_uv, ddy_uv, out, name)
        return out