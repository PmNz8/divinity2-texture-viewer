# Divinity II Texture Viewer 0.1.0

Experimental release candidate by [PmNz8](https://github.com/PmNz8).
Read-only texture viewer/exporter for Divinity II: Developer's Cut DV2 archives,
including compatible mod archives. Not affiliated with or endorsed by the game
developer/publisher. No game files are included.

## Uruchomienie / launch

Windows 10/11, x64. Extract the **entire** portable ZIP into a writable local
folder, then double-click `TextureViewer.exe`. Keep `_internal`, `LICENSES` and
the accompanying files together. Do not run directly inside the ZIP.
Python, UV, Node.js, .NET and WebView2 do not need to be installed by the user.
The candidate is unsigned; Windows may show a reputation/security warning.
Do not disable system protection globally. The final second-PC test is pending.

## Use

1. **Open DV2** selects any structurally supported archive. A background scan
   examines `.nif` contents; progress shows processed/total and compatible count.
   Only supported standalone BC1/BC2/BC3 textures are listed, not models or items.
2. **Substring** matches part of a path; **Glob pattern** accepts `*` and `?`;
   **Limit** caps the number of results. Empty fields impose no user filter.
   Filtering is live; **Reset** clears all three fields, not compatibility checks.
3. Double-click a row to decode its preview. Select an existing mip and RGB,
   alpha or composite. Preview is always **1:1**, with scrollbars and no zoom.
4. **Export PNG** writes the selected view/mip to a new `.png` file.
   **Export Set** and **Export Builder Package** ask for an existing parent
   folder, then create a new `<texture>-export` or `<texture>-package` child.
   Existing destinations are refused; choose a different parent or move your
   earlier export yourself. Source archives are never modified by GUI actions.
5. **Info** reads archive information and computes its hash; large files may
   take time. Closing waits for the current operation to finish.

Keep exports outside game directories. Reparse/symlink destinations, existing
paths and the opened archive's `Packed` tree are rejected. This is not a sandbox
against hostile concurrent filesystem changes. Use trusted local files/folders.
The profile link is the only network-facing action, opened by an explicit click
in the system browser. There is no telemetry, updater, runtime downloader or
game installation action.

## Scope and limitations

- Supported standalone NIF wrapper layouts only; embedded textures, models and
  arbitrary NIF layouts are excluded. A structural scan is not a game-runtime
  guarantee; decoding can still fail and is reported without inventing a preview.
- Archive-internal paths use the canonical DV2 backslash separator. Noncanonical
  forward-slash paths are outside this candidate's tested support and may be
  rejected by the compatibility scan.
- Existing mip levels only; no mip generation, import, editing or save-to-DV2 UI.
- Archive payload and codec size limits remain. Huge valid images/exports can
  consume significant memory/CPU; native 1:1 display may fail if resources are
  exhausted. No automatic shrinking is performed.
- Scan results live only for the current open archive. Reopen after external
  changes; do not modify an archive while it is open in the viewer.
- This is a hobby tool, not security-hardened for hostile archives. It embeds
  Python 3.12.6 / Tcl-Tk 8.6.13, the tested development runtime, not a claim of
  latest security maintenance. A runtime upgrade requires another tested build.
- Automated synthetic tests are not final GUI/second-computer acceptance.

## Diagnostics and manual candidate check

From PowerShell, a non-GUI import check can write a **new** report file:

```powershell
.\TextureViewer.exe check --report .\check-result.json
```

This does not verify a working Tk window. For the manual acceptance, test
opening mixed DV2s, scan progress, live filters/Reset, double-click, all views
and representative mips, 1:1 scrolling, each export, cancellation, failed
operations and archive switching. Check About/license and the profile link.
Repeat from the extracted ZIP on a second Windows computer without development
tools. Report the ZIP SHA-256 and any failure; do not send game files, saves or
private paths by default. Candidate changes require new hashes and retesting.

## License and source

Copyright (C) 2026 PmNz8. Own application code: **AGPL-3.0-only**.
This program comes with **ABSOLUTELY NO WARRANTY**. You may redistribute and
modify it under GNU AGPL version 3; see `LICENSE`.
Third-party components retain their own terms: see `THIRD_PARTY_NOTICES.md`
and `LICENSES`. The license does not grant rights to game assets you export.

The matching source archive is `TextureViewer-0.1.0-source.zip`, supplied
alongside the runtime candidate. It includes tests and `BUILD.md`; tests are
not included in the runtime ZIP. `BUILD_INFO.json` identifies the source commit
used for the executable; compare it with source `SOURCE_REVISION`.

### Licensing of exported content

This tool's own code is licensed under GNU AGPLv3 (`AGPL-3.0-only`). Merely using
the tool to create, modify, or export content does not automatically place that
content under the AGPLv3.

This clarification does not override licenses already applicable to the content
or to any tool code incorporated into it.

Content derived from Divinity II game assets remains subject to the rights of
the respective copyright holders. This tool does not grant permission to
redistribute those assets.

Mod authors may license their own original contributions only to the extent
that they hold the necessary rights, without overriding rights in underlying
game assets.
