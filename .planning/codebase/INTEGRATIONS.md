# External Integrations

**Analysis Date:** 2026-06-05

## Overview

This project has **no network integrations** — no HTTP clients, no APIs, no databases, no auth providers, no webhooks, no cloud SDKs, no telemetry. It is a fully offline, single-process command-line tool with zero third-party dependencies.

Its "integrations" are entirely **data-input file formats**: it consumes GPU frame-capture dumps produced by external graphics-debugging tools (RenderDoc / 3Dmigoto), plus local JSON config. This document catalogs those file-format integrations as the relevant integration surface.

## APIs & External Services

- None. No outbound or inbound network calls anywhere in the codebase.

## Data Input Integrations (capture formats)

### Capture-zip archives (RenderDoc / 3Dmigoto frame dumps)

- **Source tools:** RenderDoc and 3Dmigoto (external GPU capture/debugging tools, run separately — not invoked by this project).
- **Format:** `.zip` archive, one per Direct3D 11 draw event. Stored in `Cases/*.zip` (e.g. `Cases/Collision-fix-constant-buffer-and-RdotV-zero_event399.zip`).
- **Consumer:** `render.py` `_run_zip_workflow` → `_execute_pipeline`. Extracts to a temp dir (`zipfile` + `tempfile`), drives the pipeline from the loose files, then cleans up (`shutil`).
- **Selected via:** `data_path` key in the JSON config.
- **Manifest index:** `Cases/zip_files.csv` lists the available capture zips.

### Files inside each capture zip (the actual integration payloads)

Top-level folder per zip; paths resolved in `render.py` `_execute_pipeline`.

**HLSL shader source (text):**
- `VS_shader.hlsl`, `PS_shader.hlsl` - shader source. `main()` declares inputs/outputs as parameters with semantics (e.g. `out float4 o0 : SV_POSITION0`). Parsed by `parse_main_params_with_semantics` (`hlsl_interpreter.py`).

**CSV capture data (parsed via stdlib `csv` in `hlsl_interpreter.py` / `rasterizer.py`):**
- `VS_input_output_signature.csv`, `PS_input_output_signature.csv` - param ↔ register-slot mapping (`map_params_to_signature`). VS-output / PS-input wired by matching semantics.
- `ia_vertex_data.csv` + `ia_input_layouts.csv` - input-assembler vertex data and layout, mapped to VS inputs.
- `VS_constant_buffers.csv`, `PS_constant_buffers.csv` - combined cbuffer dumps (`load_all_cbuffers_from_combined_csv`).
- `pipeline_state.csv` - rasterizer/blend/depth-stencil state and primitive topology (`load_config_from_pipeline_state_csv` in `rasterizer.py`). CSV topology wins unless overridden by JSON `primitive_topology`.
- `MeshOut_*_mesh.csv` / `*_vs_mesh.csv` - golden VS output reference for validation (`load_vs_golden_from_mesh_csv`). **Gotcha:** 3Dmigoto shifts trailing `float3` outputs by one float; the loader remaps to `[None, x, y]` and the comparator skips `None` (see `CLAUDE.md` §gotchas).

**BMP images (parsed via stdlib `struct` in `texture.py`):**
- Texture BMP files - decoded by `Texture`/`TextureDesc`/`Sampler` (`texture.py`, lines ~289-298: reads `b'BM'` magic, pixel-data offset, header size, width/height, bits-per-pixel via little-endian `struct.unpack`). Backs HLSL `Texture2D.Sample(...)`. Mipmap generation, nearest/linear sampling, wrap/mirror/clamp/border address modes.
- `pre_draw_rt0_*.bmp`, `post_draw_rt0_*.bmp`, `pre_draw_ds_*.bmp`, `post_draw_ds_*.bmp` - render-target and depth-stencil snapshots (reference/visual artifacts).

**Reference artifacts (present but NOT consumed by the interpreter):**
- `*.dxbc` (compiled shader bytecode, e.g. `VS_shader.dxbc`), `*_disasm.txt` (shader disassembly), `*.bin` (`VS_buf_*_*.bin`, `vb_slot0_*.bin`, `ib_res_*.bin` — raw cbuffer/vertex/index buffer binaries). Kept for cross-reference/debugging only.

## Data Storage

**Databases:**
- None.

**File Storage:**
- Local filesystem only. Inputs read from `Cases/`; capture zips extracted to a temp directory during a run and deleted afterward.

**Caching:**
- In-process only: `functools.lru_cache` on hot expression-parsing functions in `hlsl_syntax_tree.py`. No persistent cache.

## Authentication & Identity

- None. No auth of any kind (no users, no tokens, no secrets).

## Monitoring & Observability

**Error Tracking:**
- None (no Sentry/etc.). "Errors" are domain-level VS-vs-golden mismatches written to the log as `Error:` lines, then grepped manually.

**Logs:**
- Plain-text log file written by the interpreter's `log_output` to `log_file_path` (default `Cases/output.log`, relative to the config file). `log_file_mode` `"w"` truncates, `"a"` appends. Contains `[STMT]` / `[SYNTAX TREE]` interpreter traces and `Error:` mismatch lines.

## CI/CD & Deployment

**Hosting:**
- None. Local CLI tool only.

**CI Pipeline:**
- None. No `.github/workflows`, no CI config.

## Environment Configuration

**Required env vars:**
- None. The tool reads no environment variables. All configuration is via the JSON config file passed on the command line.

**Secrets location:**
- Not applicable — the project handles no secrets and has no `.env` file.

## Webhooks & Callbacks

**Incoming:**
- None.

**Outgoing:**
- None.

---

*Integration audit: 2026-06-05*
