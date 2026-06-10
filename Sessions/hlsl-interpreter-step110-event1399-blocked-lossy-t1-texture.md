# Step 110 — event1399 blocked by lossy t1 texture (capture-data limitation)

## Goal

Seventh case in the witcher3_countryside loop:
`witcher3_countryside_event1399.zip`.

## 1. Symptom

```
Total PASSED rows: 3921/5658     # 1737 rows FAIL
5211 "Error:" lines (sv_position[0],[1],[2] only; w matches)
Depth: golden 1401 | matched 1379 | extra 10357   (we draw far too many pixels)
```

All 1737 failing rows produce the **identical** wrong output
`sv_position = (-5.329466, 1.037670, 0.547560, ...)`.

## 2. Root-cause investigation

This shader is a skinned mesh (like event994) but with an extra blend path. It
adds `Texture2D<float4> t1 : register(t1)` and, after computing the normal
skinned position `r0`, does:

```hlsl
r2.x = v1.x;                       // BLENDINDICES.x  (bone 0)
r2.yzw = float3(0,0,0);
r2.xyz = t1.Load(r2.xyz).xyz;      // per-bone reference position from t1
...
r3 = M(bone0) · r2;                // alternative skinned position
r1.xyz = -r3.xyz + r0.xyz;
r1.w  = saturate(v6.w);            // v6 = COLOR0
r0.xyz = r1.www * r1.xyz + r3.xyz; // lerp(r3, r0, saturate(COLOR.w))
```

So the final position is `lerp(r3_from_t1, r0_skinned, saturate(COLOR.w))`.

**Decisive evidence** — the failing rows are *exactly* the vertices with
`COLOR.w == 0`:

```
total verts:            5658
COLOR.w == 0:           1737   ← use the t1.Load path fully  → the 1737 failures
COLOR.w >= 0.999:       3921   ← pure skinning               → the 3921 passes
```

For `COLOR.w == 0`, `r0_final = r3` entirely (the t1.Load path). For
`COLOR.w == 1`, `r0_final = r0_skinned` (which the interpreter already computes
correctly — those 3921 rows pass). **The interpreter's skinning math is
correct;** the failures are 100% the `t1.Load` path.

## 3. Why this case is blocked (not an interpreter bug)

`t1` is, per `texture_params.csv`, a `Texture2D 81×1 R32G32B32A32_FLOAT`
(ByteSize 1296 = 81 × 16 B). But the capture only ships it as
`VS_slot_1_res_113813_mip0_arr0.bmp` — a **24-bit (8-bit/channel) BMP**
(298 bytes, bpp=24). The raw float32 texel data was never dumped.

`r2.xyz` are per-bone reference positions in model space (arbitrary magnitude),
fed straight into the position. Reconstructing them from an 8-bit, [0,1]-scaled
BMP cannot reproduce the float values within the 0.005 NDC position tolerance.
The golden VS output was produced by the real GPU using the true float texture.

**Therefore event1399 cannot be made bit-exact from the provided capture data.**
Implementing `Texture2D.Load` (a legitimate missing feature) would only replace
the constant wrong value with a *different* 8-bit-approximate wrong value — it
would not make the 1737 vertices pass. A check of the remaining cases shows
neither event1433 nor event1450 uses a VS-stage `Texture2D.Load`, so this path
is unique to event1399 and implementing it would unblock no case.

## 4. Decision

- **No code change.** The interpreter is already correct for everything the
  available data supports (3921/3921 pure-skinning vertices match golden).
- **Not added to the regression suite** — it cannot pass the gate with this
  capture.
- **Action required to fully resolve:** re-capture event1399 with the t1 SRV
  dumped as raw `R32G32B32A32_FLOAT` (e.g. a `_res_113813.bin`) instead of a
  lossy BMP; then add VS `Texture2D.Load` support and the float-texture loader.

## 5. Regression

Unchanged — suite remains 12/12 (event1399 deliberately excluded).
