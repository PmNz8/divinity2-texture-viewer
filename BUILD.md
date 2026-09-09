# Building 0.1.1 from source

Own code is AGPL-3.0-only; copyright 2026 PmNz8. Use Windows x64 and the official
Python **3.12.6** x64 installation with Tcl/Tk **8.6.13**. The source tree has no
runtime pip dependency. The installed runtime and build packages are versioned
inputs; reproducing the procedure is not a promise of byte-identical EXEs.

Use a clean checkout of the recorded source commit, or the matching source ZIP
with its `SOURCE_REVISION` file. No research repository, game install or private
helper is needed. No global installation of build packages is required:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-build.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m texture_viewer check
.\.venv\Scripts\python.exe build.py --output .build\candidate-output --workdir .build\candidate-work
```

Both output/work directories must be new. Do not reuse a previous candidate's
files. Build from the source-tree root. The builder verifies exact Python and
build-tool versions and requires a clean Git source commit, or the revision
marker in an extracted source ZIP. Source ZIP users can initialize a local Git
repository for their changes; do not claim modified code matches the original.

The resulting `TextureViewer` folder contains the EXE, `_internal` runtime,
license notices, build identity and file manifest. Archive this entire folder,
not just its executable. No tests, test data or test runner are included.
PyInstaller's standard Tcl/Tk hook supplies the matching Tcl/Tk data; do not
manually remove DLLs or compatibility files from its output.
The builder sanitizes the child process's PATH and Python/Tcl environment so
unrelated development runtimes cannot supply DLLs. UPX is disabled. Audit the
actual native-file origins as well as the Python dependency lock.

The build is windowed. Headless validation uses:

```powershell
.\.build\candidate-output\TextureViewer\TextureViewer.exe check --report .build\check-result.json
```

The report path must not already exist. This verifies imports, not GUI behavior.
Final GUI and clean second-PC acceptance are manual. Keep hashes of the exact
tested ZIP, and distribute matching source alongside binaries. Standard library,
Tcl/Tk and PyInstaller-derived components retain the notices in `LICENSES`.

Build package hashes cover the complete pinned wheel/sdist distributions, not
just one developer environment. Runtime binary identities are recorded in the
generated manifest. CPython's official Windows binaries carry PSF signatures;
the generated application executable itself is unsigned.
