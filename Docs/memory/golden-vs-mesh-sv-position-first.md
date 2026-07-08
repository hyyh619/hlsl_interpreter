---
name: golden-vs-mesh-sv-position-first
description: RenderDoc *_vs_mesh.csv golden data dumps SV_Position columns first regardless of header order
metadata:
  type: project
---

In RenderDoc/3Dmigoto `*_vs_mesh.csv` golden VS-output dumps, the **data columns are laid out with SV_Position FIRST**, then the remaining outputs in declared order — even though the column *header* names follow declaration order. So when a shader declares e.g. `out float2 o0 : TEXCOORD0; out float4 o1 : SV_Position0`, the columns labelled `TEXCOORD.x/y` actually hold SV_Position data (header is shifted relative to data).

`load_vs_golden_from_mesh_csv` (step105) handles this by assigning physical component columns **positionally** in `[SV_Position, then remaining outputs in declared order]`, NOT by header label. When SV_Position is already output register 0 (e.g. the Collision suite), this is identical to header-order mapping, so no regression.

Diagnostic tell: VS comparison fails on ALL rows but the depth comparison still matches ~99% → our SV_Position is actually correct and the golden column mapping is the problem, not the interpreter. Related: [[witcher3-array-cbuffers-instanced-inputs]]. This is distinct from CLAUDE.md gotcha #1 (trailing-float3 MeshOut shift).
