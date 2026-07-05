"""HTML mesh viewer — a browser-based twin of mesh_view.MeshView.

Exposes the SAME public API the pipeline calls on the tk MeshView
(set_input_data / set_output_data / set_rasterizer_pixels /
set_pixel_shader_output / set_output_merger_pixels / set_pipeline_stats /
set_primitive_topology / clear / show / close, plus the _draw_* no-ops), but
instead of drawing to a tkinter Canvas it accumulates the same data windows
and, on show(), writes ONE self-contained HTML file (data + a canvas renderer
inlined, no external deps, opens over file://) and launches the browser.

The HTML reproduces MeshView's windows: the Input-Vertices canvas (3D
projected topology wireframe), the Output notebook (VS Result wireframe +
Rasterizer / Pixel Shader / Output Merger pixel images), the Selected-Vertex
info panel, and the pipeline-stats bar — with drag-to-rotate, wheel-zoom, and
click-to-select, matching the tk viewer's interactions.
"""
import json
import math
import os
import webbrowser
from typing import Any, Dict, List

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


class HtmlMeshView:
    """Browser twin of MeshView. Same setter API; renders to a standalone HTML."""

    def __init__(self, vertices: List = None,
                 primitive_topology: int = D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST,
                 title: str = "HLSL Interpreter - Mesh View (HTML)",
                 out_path: str = None):
        self.title = title
        self.primitive_topology = primitive_topology
        self._out_path = out_path
        self._input = []          # list of vertex dicts
        self._output = []
        self._rasterizer_pixels = []
        self._output_merger_pixels = []
        self._pipeline_stats = {}
        self._log_output = print

    # --------------------------------------------------------------- setters
    def set_primitive_topology(self, primitive_topology: int):
        self.primitive_topology = primitive_topology

    def clear(self):
        self._input = []
        self._output = []
        self._rasterizer_pixels = []
        self._output_merger_pixels = []

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
        self._input = self._pack_vertices(positions, normals, colors,
                                          tex_coords, tex_coords2)

    def set_output_data(self, positions, normals=None, colors=None,
                        tex_coords=None, tex_coords2=None):
        self._output = self._pack_vertices(positions, normals, colors,
                                           tex_coords, tex_coords2)

    def set_input_vertices(self, vertices):
        self._input = [self._vd(v) for v in (vertices or [])]

    def set_output_vertices(self, vertices):
        self._output = [self._vd(v) for v in (vertices or [])]

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
        self._rasterizer_pixels = self._pack_pixels(pixels)

    def set_pixel_shader_output(self, pixels):
        self._rasterizer_pixels = self._pack_pixels(pixels)

    def set_output_merger_pixels(self, pixels):
        self._output_merger_pixels = self._pack_pixels(pixels)

    def set_pipeline_stats(self, stats: dict):
        self._pipeline_stats = dict(stats or {})

    # ------------------------------------------------- tk-parity no-op hooks
    def _draw_rasterizer_pixels(self):
        pass

    def _draw_pixel_shader_pixels(self):
        pass

    def _draw_output_merger_pixels(self):
        pass

    def set_hlsl_interpreter(self, *a, **k):
        pass  # re-execution is a tk-only interaction

    def set_hlsl_interpreter_params(self, *a, **k):
        pass

    def close(self):
        pass

    # ---------------------------------------------------------------- render
    def _payload(self) -> dict:
        return {
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
            'output': self._output,
            'rasterizer': self._rasterizer_pixels,
            'output_merger': self._output_merger_pixels,
            'stats': self._pipeline_stats,
        }

    def _resolve_out_path(self) -> str:
        if self._out_path:
            return self._out_path
        return os.path.join(os.getcwd(), 'mesh_view.html')

    def show(self, blocking: bool = False):
        path = self._resolve_out_path()
        try:
            safe_title = (self.title or 'Mesh View').replace('<', '').replace('>', '')
            html = _HTML_TEMPLATE.replace(
                '/*__PAYLOAD__*/{}',
                json.dumps(self._payload(), separators=(',', ':'))
            ).replace('__TITLE__', safe_title)
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(html)
            self._log_output(f"HTML mesh view written: {path}")
            try:
                webbrowser.open('file://' + os.path.abspath(path))
            except Exception:
                pass
        except Exception as e:
            self._log_output(f"HTML mesh view failed: {e}")


# The self-contained page: minimal CSS/JS, all data injected as JSON at the
# __PAYLOAD__ marker. Reproduces MeshView's projection (rotation + zoom + pan),
# topology wireframe, pixel-image tabs, vertex-info panel, and stats bar.
_HTML_TEMPLATE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>__TITLE__</title>
<style>
  :root{color-scheme:dark;}
  body{margin:0;background:#12121c;color:#e6e6ef;font:13px/1.4 Consolas,monospace;}
  header{padding:6px 10px;background:#1a1a2e;border-bottom:1px solid #2a2a44;}
  #stats{white-space:pre-wrap;color:#9fb;font-size:12px;margin-top:4px;}
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
  #info{width:360px;min-height:640px;padding:8px;white-space:pre-wrap;
    font-size:12px;overflow:auto;}
  .k{color:#8ad;}
</style></head><body>
<header>
  <b id="ttl">__TITLE__</b>
  <div id="stats"></div>
</header>
<div class="wrap">
  <div class="panel">
    <h3>Input Vertices</h3>
    <div class="ctl">
      <label>Zoom <input type="range" id="izoom" min="0.1" max="5" step="0.05" value="1"></label>
      drag = rotate &nbsp; right-click = select
    </div>
    <canvas id="incanvas" width="640" height="360"></canvas>
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
    <canvas id="outcanvas" width="640" height="360"></canvas>
  </div>
  <div class="panel">
    <h3>Selected Vertex Info</h3>
    <div id="info">Right-click a vertex to inspect.</div>
  </div>
</div>
<script>
var DATA = /*__PAYLOAD__*/{};
(function(){
  var T = DATA.topo_names[DATA.topology] || "Unknown";
  document.getElementById("ttl").textContent = DATA.title;
  var s = DATA.stats || {};
  var line = "Input: "+DATA.input.length+" vertices | Output: "+DATA.output.length
    +" vertices | Topology: "+T;
  if (Object.keys(s).length){
    line += "\nVerts: "+(s.vertices||0)+" | Prims: "+(s.primitives||0)
      +" | Clipped: "+(s.clipped||0)+"/"+(s.not_clipped||0)
      +" | Culled: "+(s.culled||0)+"/"+(s.not_culled||0)
      +" | Rast px: "+(s.rast_pixels||0)
      +" | Depth fail: "+(s.depth_failed||0)+" (pass "+(s.depth_passed||0)+")"
      +" | PS px: "+(s.ps_pixels||0);
  }
  document.getElementById("stats").textContent = line;

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
  // rotation state per view; project 3D -> 2D with same math as MeshView
  function makeView(canvas, verts, zoomEl, getState){
    var rx=0, ry=0, ox=0, oy=0, zoom=1, sel=-1, dragging=false, last=null;
    function transform(p){
      var ax=rx*Math.PI/180, ay=ry*Math.PI/180;
      var cx=Math.cos(ax), sx=Math.sin(ax), cy=Math.cos(ay), sy=Math.sin(ay);
      var y1=p[1]*cx - p[2]*sx, z1=p[1]*sx + p[2]*cx;
      var x2=p[0]*cy + z1*sy;
      return [x2, y1];
    }
    function bounds(){
      var mn=[1e30,1e30], mx=[-1e30,-1e30];
      for(var i=0;i<verts.length;i++){var t=transform(verts[i].p);
        mn[0]=Math.min(mn[0],t[0]);mn[1]=Math.min(mn[1],t[1]);
        mx[0]=Math.max(mx[0],t[0]);mx[1]=Math.max(mx[1],t[1]);}
      return [mn,mx];
    }
    function project(p,W,H,b){
      var t=transform(p), span=Math.max(b[1][0]-b[0][0], b[1][1]-b[0][1], 1e-6);
      var margin=40, usable=Math.min(W,H)-2*margin;
      var sc=zoom*usable/span;
      var cx=(b[0][0]+b[1][0])/2, cy=(b[0][1]+b[1][1])/2;
      return [(t[0]-cx)*sc + W/2 + ox, -(t[1]-cy)*sc + H/2 + oy];
    }
    function draw(){
      var ctx=canvas.getContext("2d"), W=canvas.width, H=canvas.height;
      ctx.fillStyle="#1a1a2e"; ctx.fillRect(0,0,W,H);
      if(!verts.length){ctx.fillStyle="#667";ctx.fillText("(no vertices)",20,20);return;}
      var b=bounds();
      var topo=DATA.topology, E=DATA.topo_enum;
      ctx.lineWidth=1;
      function seg(a,c,col){var pa=project(verts[a].p,W,H,b),pc=project(verts[c].p,W,H,b);
        ctx.strokeStyle=col;ctx.beginPath();ctx.moveTo(pa[0],pa[1]);ctx.lineTo(pc[0],pc[1]);ctx.stroke();}
      if(topo===E.TRIANGLELIST){for(var i=0;i+2<verts.length;i+=3){var col=vcolor(verts[i]);seg(i,i+1,col);seg(i+1,i+2,col);seg(i+2,i,col);}}
      else if(topo===E.TRIANGLESTRIP){for(var i=0;i+2<verts.length;i++){var col=vcolor(verts[i]);seg(i,i+1,col);seg(i+1,i+2,col);seg(i+2,i,col);}}
      else if(topo===E.LINELIST){for(var i=0;i+1<verts.length;i+=2)seg(i,i+1,vcolor(verts[i]));}
      else if(topo===E.LINESTRIP){for(var i=0;i+1<verts.length;i++)seg(i,i+1,vcolor(verts[i]));}
      // vertex dots
      for(var i=0;i<verts.length;i++){var p=project(verts[i].p,W,H,b);
        ctx.fillStyle=(i===sel)?"#ffdd33":vcolor(verts[i]);
        ctx.beginPath();ctx.arc(p[0],p[1],(i===sel)?5:2.5,0,7);ctx.fill();
        if(i===sel){ctx.strokeStyle="#fff";ctx.stroke();}}
    }
    function pick(mx,my){
      var W=canvas.width,H=canvas.height,b=bounds(),best=-1,bd=400;
      for(var i=0;i<verts.length;i++){var p=project(verts[i].p,W,H,b);
        var d=(p[0]-mx)*(p[0]-mx)+(p[1]-my)*(p[1]-my);if(d<bd){bd=d;best=i;}}
      return best;
    }
    canvas.addEventListener("mousedown",function(e){dragging=true;last=[e.offsetX,e.offsetY];});
    canvas.addEventListener("mousemove",function(e){if(dragging&&last){ry+=(e.offsetX-last[0])*0.5;rx+=(e.offsetY-last[1])*0.5;last=[e.offsetX,e.offsetY];draw();}});
    window.addEventListener("mouseup",function(){dragging=false;last=null;});
    canvas.addEventListener("contextmenu",function(e){e.preventDefault();
      sel=pick(e.offsetX,e.offsetY);draw();if(getState)getState(sel);});
    canvas.addEventListener("wheel",function(e){e.preventDefault();
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

  var inView, outVs;
  function syncSel(i){ if(outVs)outVs.setSel(i); vinfo(i); }
  function syncSelO(i){ if(inView)inView.setSel(i); vinfo(i); }
  inView = makeView(document.getElementById("incanvas"), DATA.input,
    document.getElementById("izoom"), syncSel);
  outVs = makeView(document.getElementById("outcanvas"), DATA.output,
    document.getElementById("ozoom"), syncSelO);
  inView.draw();

  // Output tabs: VS wireframe (canvas view) vs pixel-image stages.
  var octx = document.getElementById("outcanvas").getContext("2d");
  function drawPixels(pixels, shaded){
    var cv=document.getElementById("outcanvas"), W=cv.width, H=cv.height;
    octx.fillStyle="#1a1a2e"; octx.fillRect(0,0,W,H);
    if(!pixels||!pixels.length){octx.fillStyle="#667";octx.fillText("(no pixels)",20,20);return;}
    var mnx=1e9,mny=1e9,mxx=-1e9,mxy=-1e9;
    for(var i=0;i<pixels.length;i++){var p=pixels[i];
      mnx=Math.min(mnx,p[0]);mny=Math.min(mny,p[1]);mxx=Math.max(mxx,p[0]);mxy=Math.max(mxy,p[1]);}
    var pw=mxx-mnx+1, ph=mxy-mny+1;
    var sc=Math.min((W-8)/pw,(H-8)/ph); if(sc<=0)sc=1;
    var ox=(W-pw*sc)/2, oy=(H-ph*sc)/2, cell=Math.max(1,sc);
    for(var i=0;i<pixels.length;i++){var p=pixels[i];
      var col;
      if(shaded && p[3]>=0) col="rgb("+p[3]+","+p[4]+","+p[5]+")";
      else col=primColor(p[2]);
      octx.fillStyle=col;
      octx.fillRect(ox+(p[0]-mnx)*sc, oy+(p[1]-mny)*sc, cell, cell);
    }
  }
  var tabs=document.querySelectorAll("#otabs button");
  function setTab(name){
    tabs.forEach(function(b){b.classList.toggle("active",b.dataset.tab===name);});
    document.getElementById("octl").style.display=(name==="vs")?"block":"none";
    if(name==="vs")outVs.draw();
    else if(name==="rast")drawPixels(DATA.rasterizer,false);
    else if(name==="ps")drawPixels(DATA.rasterizer,true);
    else if(name==="om")drawPixels(DATA.output_merger.length?DATA.output_merger:DATA.rasterizer,true);
  }
  tabs.forEach(function(b){b.addEventListener("click",function(){setTab(b.dataset.tab);});});
  setTab("vs");
})();
</script>
</body></html>
"""
