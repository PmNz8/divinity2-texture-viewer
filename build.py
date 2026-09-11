# SPDX-FileCopyrightText: 2026 PmNz8
# SPDX-License-Identifier: AGPL-3.0-only
"""Reproducible Windows onedir packaging for Texture Viewer.

This script validates the release inputs and invokes PyInstaller only after the
candidate is frozen. It deliberately does not install dependencies or remove
DLLs/assets from the PyInstaller result.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import importlib.metadata as importlib_metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
from typing import Iterable

from texture_viewer.version import APP_NAME, VERSION


REQUIRED_PYTHON = (3, 12, 6)
REQUIRED_ARCHITECTURE = {"amd64", "x86_64"}
PINNED_BUILD_PACKAGES = {
    "pyinstaller": "6.22.2",
    "altgraph": "0.17.5",
    "packaging": "26.3",
    "pefile": "2024.8.26",
    "pyinstaller-hooks-contrib": "2026.7",
    "pywin32-ctypes": "0.2.3",
    "setuptools": "84.0.0",
}
EXCLUDED_MODULES = (
    "tests",
    "test",
    "unittest",
    "pytest",
    "doctest",
    "webview",
    "clr",
    "terrain_viewer",
    "dks_patch_builder",
)
PUBLIC_FILES = (
    "README.md",
    "USAGE.md",
    "BUILD.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
)
PUBLIC_DOCS_DIR = "docs/images"
PUBLIC_DOC_FILES = ("README.md", "diffuse-preview.png", "composite-preview.png")
BUILD_INFO_NAME = "BUILD_INFO.json"
MANIFEST_NAME = "MANIFEST.json"
_REVISION_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class BuildError(RuntimeError):
    """Raised when a release input is absent or violates the build contract."""


@dataclass(frozen=True, slots=True)
class BuildContext:
    root: Path
    output: Path
    workdir: Path
    source_revision: str
    build_packages: dict[str, str]


def _require_new_absolute_directory(
    value: str | Path, label: str, *, base_dir: Path | None = None
) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = ((base_dir or Path.cwd()) / candidate).resolve()
    if candidate.exists():
        raise BuildError(f"{label} must not already exist: {candidate}")
    return candidate


def _validate_runtime() -> None:
    actual_python = tuple(sys.version_info[:3])
    if actual_python != REQUIRED_PYTHON:
        raise BuildError(
            f"Python {'.'.join(map(str, REQUIRED_PYTHON))} is required; "
            f"got {'.'.join(map(str, actual_python))}"
        )
    if platform.system() != "Windows":
        raise BuildError(f"Windows x64 is required; got {platform.system()}")
    if platform.machine().casefold() not in REQUIRED_ARCHITECTURE:
        raise BuildError(f"Windows x64 is required; got {platform.machine()}")


def _parse_pinned_requirements(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise BuildError(f"missing pinned build requirements: {path}")
    parsed: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("--hash=") or line == "\\":
            continue
        line = line.rstrip("\\").strip()
        match = re.match(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)", line)
        if match is None:
            raise BuildError(f"requirements-build.txt line {line_number} is not pinned: {raw_line}")
        name, version = match.groups()
        key = name.casefold()
        if not name or not version or key in parsed:
            raise BuildError(f"requirements-build.txt line {line_number} is invalid")
        parsed[key] = version
    expected = {name.casefold(): version for name, version in PINNED_BUILD_PACKAGES.items()}
    if parsed != expected:
        raise BuildError(
            "requirements-build.txt must contain exactly the pinned build packages: "
            + ", ".join(f"{name}=={version}" for name, version in PINNED_BUILD_PACKAGES.items())
        )
    return {name: parsed[name.casefold()] for name in PINNED_BUILD_PACKAGES}


def _validate_installed_build_packages(expected: dict[str, str]) -> dict[str, str]:
    actual: dict[str, str] = {}
    for name, required in expected.items():
        try:
            installed = importlib_metadata.version(name)
        except importlib_metadata.PackageNotFoundError as error:
            raise BuildError(f"required build package is not installed: {name}=={required}") from error
        if installed != required:
            raise BuildError(
                f"build package {name} must be {required}; installed metadata reports {installed}"
            )
        actual[name] = installed
    return actual


def _git_output(root: Path, *arguments: str) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            ["git", *arguments], cwd=root, capture_output=True, text=True, check=False
        )
    except OSError:
        return 127, "", "git is unavailable"
    return completed.returncode, completed.stdout.strip(), completed.stderr.strip()


def _resolve_source_revision(root: Path) -> str:
    top_code, top_level, _ = _git_output(root, "rev-parse", "--show-toplevel")
    if top_code == 0:
        try:
            is_own_checkout = Path(top_level).resolve() == root.resolve()
        except OSError:
            is_own_checkout = False
        if is_own_checkout:
            status_code, status, _ = _git_output(root, "status", "--porcelain")
            if status_code != 0 or status:
                raise BuildError("source checkout must be clean before packaging")
            commit_code, commit, error = _git_output(root, "rev-parse", "HEAD")
            if commit_code != 0 or not _REVISION_RE.fullmatch(commit):
                raise BuildError(f"cannot resolve an exact 40-hex Git commit: {error or commit}")
            return commit.lower()
    marker = root / "SOURCE_REVISION"
    try:
        value = marker.read_text(encoding="ascii").strip()
    except OSError as error:
        raise BuildError("source is neither a clean Git checkout nor a SOURCE_REVISION archive") from error
    if not _REVISION_RE.fullmatch(value):
        raise BuildError("SOURCE_REVISION must contain exactly one 40-hex commit")
    return value.lower()


def _version_file_text() -> str:
    return f'''# UTF-8\nVSVersionInfo(\n  ffi=FixedFileInfo(\n    filevers=(0, 1, 1, 0),\n    prodvers=(0, 1, 1, 0),\n    mask=0x3f,\n    flags=0x0,\n    OS=0x40004,\n    fileType=0x1,\n    subtype=0x0,\n    date=(0, 0)\n  ),\n  kids=[\n    StringFileInfo([\n      StringTable(\'040904B0\', [\n        StringStruct(\'CompanyName\', \'PmNz8\'),\n        StringStruct(\'FileDescription\', \'{APP_NAME}\'),\n        StringStruct(\'FileVersion\', \'{VERSION}\'),\n        StringStruct(\'ProductName\', \'{APP_NAME}\'),\n        StringStruct(\'ProductVersion\', \'{VERSION}\'),\n        StringStruct(\'LegalCopyright\', \'© 2026 PmNz8\'),\n      ])\n    ]),\n    VarFileInfo([VarStruct(\'Translation\', [1033, 1200])])\n  ]\n)\n'''


def _pyinstaller_command(context: BuildContext, version_file: Path) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onedir",
        "--windowed",
        "--noupx",
        "--name",
        "TextureViewer",
        "--icon",
        "NONE",
        "--version-file",
        str(version_file),
        "--distpath",
        str(context.output),
        "--workpath",
        str(context.workdir),
        "--specpath",
        str(context.workdir),
    ]
    for module in EXCLUDED_MODULES:
        command.extend(("--exclude-module", module))
    command.append(str(context.root / "launcher.py"))
    return command


def _copy_public_release_files(root: Path, bundle: Path) -> None:
    _validate_public_release_files(root)
    for name in PUBLIC_FILES:
        source = root / name
        shutil.copy2(source, bundle / name)
    destination_docs = bundle / PUBLIC_DOCS_DIR
    destination_docs.mkdir(parents=True, exist_ok=False)
    for name in PUBLIC_DOC_FILES:
        shutil.copy2(root / PUBLIC_DOCS_DIR / name, destination_docs / name)
    licenses = root / "LICENSES"
    shutil.copytree(licenses, bundle / "LICENSES")


def _validate_public_release_files(root: Path) -> None:
    for name in PUBLIC_FILES:
        source = root / name
        if not source.is_file():
            raise BuildError(f"missing public release file: {source}")
    licenses = root / "LICENSES"
    if not licenses.is_dir():
        raise BuildError(f"missing public license directory: {licenses}")
    docs = root / PUBLIC_DOCS_DIR
    if not docs.is_dir():
        raise BuildError(f"missing public documentation image directory: {docs}")
    for name in PUBLIC_DOC_FILES:
        source = docs / name
        if not source.is_file():
            raise BuildError(f"missing public documentation image file: {source}")


def _file_manifest(root: Path, *, exclude: Iterable[str] = ()) -> list[dict[str, object]]:
    excluded = set(exclude)
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        data = path.read_bytes()
        entries.append({"path": relative, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    return entries


def _write_bundle_metadata(context: BuildContext, bundle: Path) -> None:
    payload_entries = _file_manifest(bundle, exclude=(BUILD_INFO_NAME, MANIFEST_NAME))
    build_info = {
        "app": APP_NAME,
        "version": VERSION,
        "source_revision": context.source_revision,
        "python_version": ".".join(map(str, REQUIRED_PYTHON)),
        "platform": "Windows x64",
        "build_packages": context.build_packages,
        "manifest": payload_entries,
        "manifest_excludes": [BUILD_INFO_NAME, MANIFEST_NAME],
    }
    (bundle / BUILD_INFO_NAME).write_text(
        json.dumps(build_info, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    all_entries = _file_manifest(bundle, exclude=(MANIFEST_NAME,))
    (bundle / MANIFEST_NAME).write_text(
        json.dumps({"version": VERSION, "entries": all_entries}, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _build_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME", "TCL_LIBRARY", "TK_LIBRARY", "TCLLIBPATH"):
        environment.pop(name, None)
    windows_root = environment.get("SYSTEMROOT") or environment.get("SystemRoot")
    if not windows_root:
        raise BuildError("Windows SystemRoot is unavailable")
    windows = Path(windows_root)
    environment["PATH"] = os.pathsep.join(map(str, (
        Path(sys.executable).parent, Path(sys.base_prefix),
        Path(sys.base_prefix) / "DLLs", windows / "System32", windows,
    )))
    return environment


def build_release(root: Path, output: Path, workdir: Path) -> Path:
    _validate_runtime()
    output = _require_new_absolute_directory(output, "output", base_dir=root)
    workdir = _require_new_absolute_directory(workdir, "workdir", base_dir=root)
    if output == workdir:
        raise BuildError("output and workdir must be different new directories")
    source_revision = _resolve_source_revision(root)
    declared_packages = _parse_pinned_requirements(root / "requirements-build.txt")
    packages = _validate_installed_build_packages(declared_packages)
    _validate_public_release_files(root)
    if not (root / "launcher.py").is_file():
        raise BuildError("launcher.py is missing")
    workdir.mkdir(parents=True, exist_ok=False)
    version_file = workdir / "version-file.txt"
    version_file.write_text(_version_file_text(), encoding="utf-8", newline="\n")
    context = BuildContext(root, output, workdir, source_revision, packages)
    completed = subprocess.run(
        _pyinstaller_command(context, version_file), cwd=root, env=_build_environment(), check=False
    )
    if completed.returncode != 0:
        raise BuildError(f"PyInstaller failed with exit code {completed.returncode}")
    bundle = output / "TextureViewer"
    if not bundle.is_dir():
        raise BuildError(f"PyInstaller did not create the expected bundle: {bundle}")
    _copy_public_release_files(root, bundle)
    _write_bundle_metadata(context, bundle)
    return bundle


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=f"Build {APP_NAME} {VERSION} on Windows x64")
    parser.add_argument("--output", required=True, type=Path, help="new absolute dist directory")
    parser.add_argument(
        "--workdir",
        type=Path,
        default=root / ".build",
        help="new absolute PyInstaller work/spec directory (default: .build)",
    )
    args = parser.parse_args(argv)
    try:
        bundle = build_release(root, args.output, args.workdir)
    except BuildError as error:
        print(f"build failed: {error}", file=sys.stderr)
        return 2
    print(f"built {bundle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
