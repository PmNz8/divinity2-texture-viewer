# Third-party notices — Texture Viewer 0.1.0

The product's AGPL-3.0-only license covers its own source, not the following
independently licensed runtime components. Required notices are supplied
verbatim under `LICENSES`; retain them when redistributing. The Windows-only
runtime conditions do not impose Windows-only terms on the application source.

| Component | Version / origin | Terms / notice |
|---|---|---|
| CPython, standard library and bundled extension modules | Official PSF Windows x64 Python 3.12.6 | PSF License v2 and incorporated notices, `Python-3.12.6.txt` |
| Tcl and Tk | 8.6.13, supplied with the above Python | Tcl/Tk permissive terms, `Tcl-8.6.13.txt`, `Tk-8.6.13.txt`; existing script headers retained |
| Microsoft C/C++ runtime code | VC runtime DLLs 14.38.33126.1 supplied with official Windows CPython; hashes in runtime manifest | Additional Windows binary-build conditions in `Python-3.12.6.txt`; apply only to Microsoft Distributable Code |
| zlib | Python extension 1.3.1; Tcl/Tk companion DLL 1.2.13 where collected | zlib license, `zlib-1.3.1.txt`, `zlib-1.2.13.txt` |
| OpenSSL | 3.0.15 via CPython's crypto extensions, where collected | Apache-2.0, `OpenSSL-3.0.15.txt` |
| libffi | 3.4.4 via CPython, where collected | MIT-style terms included in `Python-3.12.6.txt` |
| bzip2/libbzip2 | 1.0.8 via CPython `_bz2` | bzip2 terms included in `Python-3.12.6.txt` |
| XZ Utils liblzma | 5.2.5 via CPython `_lzma` | Public-domain library/permissive fallback grant, `XZ-5.2.5.txt`; XZ command-line programs/build tools are not bundled |
| Expat | 2.6.3 via CPython `pyexpat` | MIT-style terms, `Expat-2.6.3.txt` |
| libmpdec | 2.5.1 via CPython `_decimal` | BSD-2-Clause terms in `Python-incorporated-notices.txt` |
| PyInstaller bootloader/loader | 6.22.2 | GPL-2.0-or-later with Bootloader-exception; generated combinations permitted, `PyInstaller-6.22.2.txt` |
| PyInstaller runtime hooks | 6.22.2 | Apache-2.0, included in the same PyInstaller notice |

Python's license and incorporated-notices files also preserve notices for other code;
including those notices is not a claim that every optional Python module is
bundled. Exact runtime files and hashes are in `MANIFEST.json`; Python module
closure is audited separately during candidate preparation. Tcl/Tk data files
remain intact except PyInstaller's normal demo/development exclusions.

Build-only dependencies (not application runtime dependencies) are pinned in
`requirements-build.txt`: PyInstaller, pyinstaller-hooks-contrib, altgraph,
packaging, pefile, pywin32-ctypes and setuptools. Their wheel license metadata
is audited in the build environment. No vendored build-tool distribution is
shipped with the source ZIP; install the pinned packages to rebuild.

Upstream sources:

- [Python 3.12.6](https://www.python.org/downloads/release/python-3126/)
- [CPython external dependency versions](https://github.com/python/cpython/blob/v3.12.6/PCbuild/get_externals.bat)
- [Tcl](https://github.com/tcltk/tcl/tree/core-8-6-13), [Tk](https://github.com/tcltk/tk/tree/core-8-6-13)
- [PyInstaller license and exception](https://pyinstaller.org/en/v6.22.2/license.html)

`LICENSES/SOURCES.json` records the origin and digest of included license texts.
No third-party copyright notice is replaced by PmNz8's own copyright statement.
