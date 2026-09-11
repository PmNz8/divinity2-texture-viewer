# Texture Viewer usage

## Launch

On Windows 10/11 x64, extract the **entire** portable ZIP into a writable local
folder and launch `TextureViewer.exe`. Keep `_internal`, `LICENSES`,
`docs/images` and the accompanying files together. Do not run directly inside
the ZIP. The release is unsigned; Windows may show a reputation or security
warning. Do not disable system protection globally.

The packaged runtime includes Python 3.12.6 and Tcl/Tk 8.6.13. Python, UV,
Node.js, .NET and WebView2 do not need to be installed separately. The tool has
no telemetry, updater, runtime downloader or game-installation action.

## Basic workflow

1. **Open DV2** selects a structurally supported archive. A background scan
   examines `.nif` contents and reports processed/total progress and the
   compatible count. Only supported standalone BC1/BC2/BC3 textures are listed;
   models and items are not texture entries.
2. **Substring** matches part of a path, **Glob pattern** accepts `*` and `?`,
   and **Limit** caps the number of results. Empty fields impose no user filter.
   Filtering is live; **Reset** clears all three user filters, not the
   compatibility rules.
3. Double-click a row to decode its preview. Select an existing mip and RGB,
   alpha or composite. Preview is always **1:1**, with scrollbars and no zoom.
4. **Info** reads archive information and computes its hash; large files may
   take time. Closing waits for the current operation to finish.

## Exports

The three export actions are:

- **Export PNG** writes the selected view/mip to a new `.png` file.
- **Export Set** writes the complete selected texture set.
- **Export Builder Package** writes a handoff package for the separate writable
  [DKS Patch Builder](https://github.com/PmNz8/divinity2-dks-patch-builder),
  which compiles texture changes into a `DKS_Patch.dv2` archive.

For the set and Builder Package actions, choose an existing parent folder. The
viewer creates a new `<texture>-export` or `<texture>-package` child. Existing
destinations are refused; choose another parent or move the earlier export
yourself. Source archives are never modified by GUI actions.

Keep exports outside game directories. Reparse/symlink destinations, existing
paths and the opened archive's `Packed` tree are rejected. This is not a
sandbox against hostile concurrent filesystem changes. Use trusted local
files/folders. The Builder Package is a read-only handoff; assembling or
writing a modified DV2 is outside this viewer and belongs to the separate DKS
Patch Builder.

## Batch and MIP0 packages (0.1.2)

Set **Builder package mip policy** before a single or batch package export:

- **All mips (original)** keeps the v1 contract: edit each desired mip separately.
- **MIP0 / raw channels** exports only base PNGs and marks the package for area
  filtering of independent 8-bit channels. No gamma or vector interpretation.
- **MIP0 / sRGB + opacity** is for colour with opacity: lower mips are area
  filtered in linear light with premultiplied alpha, then stored as sRGB.

Use **Batch: all textures** for all recognized textures in the opened archive,
or **Batch: filtered textures** for the current substring/glob/limit.
No preview selection is necessary. Choose a parent outside the game installation.
A new timestamped folder contains independent packages named with an ordinal,
readable texture stem and logical-path hash. Same basenames do not collide.
The root **batch-report.json** maps every attempted texture to its package and
exported/skipped/error result. Packages are committed individually; **Stop batch
after current texture** retains completed packages and writes a partial report.
Close requests the same stop and waits for the current texture. No overwrite.

MIP0 packages require Builder 0.1.2 or newer. Only BC1/BC3 conventional halving
chains are supported; BC2 is skipped in this mode. The original format,
dimensions, mip count and template stay fixed. Only the base RGB and required
alpha PNGs need editing. Unchanged base pixels preserve ALL original NIF bytes;
changed pixels regenerate every existing lower mip in the Builder.
Plain **Export Set** remains full-mip v1, regardless of this package policy.

Choose profiles explicitly; filenames are not used to guess texture semantics.
Do not use sRGB for normals or packed material data. Raw filtering does not
renormalize normals; neither profile preserves alpha-test coverage, performs
edge wrapping, resizes the base or promises game-equivalent mip quality.
Use the full-mip workflow for custom normals, cutouts or specialized filtering.
Process textures in manageable batches: work is sequential, but one large
texture can take time and the destination needs space for templates and PNGs.

## Scope and limits

- Supported standalone NIF wrapper layouts only; embedded textures, models and
  arbitrary NIF layouts are excluded. A structural scan is not a game-runtime
  guarantee; decoding can still fail and is reported without inventing a
  preview.
- Archive-internal paths use the canonical DV2 backslash separator.
  Noncanonical forward-slash paths are outside the tested support and may be
  rejected by the compatibility scan.
- Preview uses existing mip levels. MIP0 packages request lower-mip generation
  in the Builder; there is no import, editing or save-to-DV2 UI in the Viewer.
- Archive payload and codec size limits remain. Huge valid images/exports can
  consume significant memory/CPU; native 1:1 display can fail if resources are
  exhausted. No automatic shrinking is performed.
- Scan results live only for the current open archive. Reopen after external
  changes; do not modify an archive while it is open in the viewer.
- This is a hobby tool, not security-hardened for hostile archives. It embeds
  the tested development runtime, not a claim of latest security maintenance.
  A runtime upgrade requires another tested build.

## Headless diagnostics

From PowerShell, a non-GUI import check can write a **new** report file:

```powershell
.\TextureViewer.exe check --report .\check-result.json
```

The report path must not already exist. This checks imports and the packaged
runtime; it does not verify a working Tk window. A failed check returns a
non-zero exit code and reports the reason without opening the GUI.

## Tested manual scope

Manual GUI, filtering and three-export checks passed for the candidate r2
functional build on two Windows computers. This documentation, branding and
packaging refresh was validated by automated checks; it is not a new manual
test of the refreshed ZIP. The tested scope does not cover every archive layout,
hostile input or every DKS Patch Builder workflow.
