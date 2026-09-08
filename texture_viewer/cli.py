# SPDX-FileCopyrightText: 2026 PmNz8
# SPDX-License-Identifier: AGPL-3.0-only
"""Command line entry points for the Texture Viewer."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import sys

from .version import APP_NAME, VERSION


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m texture_viewer",
        description="Divinity II read-only texture viewer",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser(
        "check", help="check Tkinter and backend imports without opening a window"
    )
    check.add_argument(
        "--report",
        type=Path,
        help="write the headless import report to a new JSON file",
    )
    subparsers.add_parser("gui", help="launch the Tkinter developer viewer")
    return parser


def run_gui() -> int:
    """Import and launch the GUI only when the GUI command is selected."""

    from .desktop import run_gui as launch_gui

    return launch_gui()


def _emit_json(value: object) -> None:
    """Print JSON when a console exists; frozen windowed apps may not have one."""

    stream = getattr(sys, "stdout", None)
    if stream is None:
        return
    try:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True), file=stream)
    except (AttributeError, OSError, ValueError):
        return


def _module_list() -> list[str]:
    return sorted(
        name
        for name in sys.modules
        if name == "texture_viewer" or name.startswith("texture_viewer.")
    )


def _check_payload() -> dict[str, object]:
    """Import the runtime surface without constructing a Tk root window."""

    import tkinter

    from .desktop import run_gui as desktop_run_gui

    modules = _module_list()
    return {
        "ok": callable(desktop_run_gui),
        "frontend": "tkinter",
        "tk_version": tkinter.TkVersion,
        "gui_tested": False,
        "version": VERSION,
        "python": platform.python_version(),
        "python_version": platform.python_version(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "modules": modules,
        "module_list": modules,
    }


def _write_report(path: Path, payload: dict[str, object]) -> tuple[bool, str | None]:
    """Create one report with O_EXCL semantics and never overwrite an artifact."""

    if not isinstance(path, Path):
        path = Path(path)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
    except FileExistsError:
        return False, f"report already exists: {path}"
    except OSError as error:
        return False, f"cannot write report {path}: {error}"
    return True, None


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if not raw_args:
        return run_gui()
    if raw_args == ["--version"]:
        stream = getattr(sys, "stdout", None)
        if stream is not None:
            try:
                print(f"{APP_NAME} {VERSION}", file=stream)
            except (AttributeError, OSError, ValueError):
                pass
        return 0

    args = _build_parser().parse_args(raw_args)
    if args.command == "check":
        try:
            result = _check_payload()
        except Exception as error:
            result = {"ok": False, "reason": str(error) or type(error).__name__}
            if args.report is not None:
                written, reason = _write_report(args.report, result)
                if not written:
                    result["report_error"] = reason
            _emit_json(result)
            return 2
        if args.report is not None:
            written, reason = _write_report(args.report, result)
            if not written:
                _emit_json({"ok": False, "reason": reason})
                return 2
        _emit_json(result)
        return 0
    if args.command == "gui":
        return run_gui()
    _emit_json({"ok": False, "reason": "unknown command"})
    return 2


__all__ = ["main", "run_gui"]
