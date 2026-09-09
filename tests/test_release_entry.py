# SPDX-FileCopyrightText: 2026 PmNz8
# SPDX-License-Identifier: AGPL-3.0-only
"""Headless release-entry and packaging-validation tests."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import build as build_script
from texture_viewer import cli
from texture_viewer.desktop import ABOUT_TEXT, FOOTER_TEXT, WINDOW_TITLE, TextureViewerTkApp
from texture_viewer.version import APP_NAME, COPYRIGHT, LICENSE_NAME, PROFILE_URL, VERSION


class ReleaseEntryTests(unittest.TestCase):
    def test_build_environment_excludes_ambient_runtime_paths(self) -> None:
        contamination = {name: "unrelated-runtime" for name in (
            "PATH", "PYTHONPATH", "PYTHONHOME", "TCL_LIBRARY", "TK_LIBRARY", "TCLLIBPATH"
        )}
        with mock.patch.dict(build_script.os.environ, contamination):
            environment = build_script._build_environment()
        self.assertNotIn("unrelated-runtime", environment["PATH"])
        for name in contamination:
            if name != "PATH":
                self.assertNotIn(name, environment)

    def test_version_literals_and_frontend_branding(self) -> None:
        self.assertEqual(VERSION, "0.1.0")
        self.assertEqual(APP_NAME, "Texture Viewer")
        self.assertEqual(COPYRIGHT, "© 2026 PmNz8")
        self.assertEqual(LICENSE_NAME, "AGPL-3.0-only")
        self.assertEqual(PROFILE_URL, "https://github.com/PmNz8")
        self.assertIn("0.1.0", WINDOW_TITLE)
        self.assertNotIn("candidate", WINDOW_TITLE)
        self.assertIn(COPYRIGHT, FOOTER_TEXT)
        self.assertIn("AGPL", FOOTER_TEXT)
        self.assertIn("No warranty", FOOTER_TEXT)

    def test_default_and_gui_commands_delegate_without_constructing_tk(self) -> None:
        with mock.patch.object(cli, "run_gui", return_value=7) as launch:
            self.assertEqual(cli.main([]), 7)
            self.assertEqual(cli.main(["gui"]), 7)
        self.assertEqual(launch.call_count, 2)

    def test_version_command_is_headless(self) -> None:
        stream = io.StringIO()
        with mock.patch.object(sys, "stdout", stream):
            self.assertEqual(cli.main(["--version"]), 0)
        self.assertIn("0.1.0", stream.getvalue())

    def test_check_imports_runtime_without_calling_gui(self) -> None:
        fake_tk = type("FakeTk", (), {"TkVersion": "test-tk"})
        stream = io.StringIO()
        with mock.patch.dict(sys.modules, {"tkinter": fake_tk}), mock.patch.object(
            cli, "run_gui", side_effect=AssertionError("GUI must not launch")
        ), mock.patch.object(sys, "stdout", stream):
            self.assertEqual(cli.main(["check"]), 0)
        payload = json.loads(stream.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["version"], VERSION)
        self.assertIn("python", payload)
        self.assertIn("frozen", payload)
        self.assertIn("modules", payload)
        self.assertFalse(payload["gui_tested"])

    def test_check_report_is_new_file_only_and_refuses_overwrite(self) -> None:
        payload = {
            "ok": True,
            "version": VERSION,
            "python": "3.12.6",
            "frozen": False,
            "modules": ["texture_viewer"],
        }
        with tempfile.TemporaryDirectory(prefix="texture-viewer-release-") as raw:
            report = Path(raw) / "report.json"
            with mock.patch.object(cli, "_check_payload", return_value=payload):
                self.assertEqual(cli.main(["check", "--report", str(report)]), 0)
                original = report.read_bytes()
                self.assertEqual(cli.main(["check", "--report", str(report)]), 2)
            self.assertEqual(report.read_bytes(), original)
            self.assertEqual(json.loads(original), payload)

    def test_check_failure_writes_failure_report_without_gui(self) -> None:
        with tempfile.TemporaryDirectory(prefix="texture-viewer-release-") as raw:
            report = Path(raw) / "failed.json"
            with mock.patch.object(cli, "_check_payload", side_effect=RuntimeError("blocked")):
                self.assertEqual(cli.main(["check", "--report", str(report)]), 2)
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertFalse(payload["ok"])
            self.assertIn("blocked", payload["reason"])

    def test_check_does_not_write_to_windowed_none_stdout(self) -> None:
        with mock.patch.object(cli, "_check_payload", return_value={"ok": True}), mock.patch.object(
            sys, "stdout", None
        ):
            self.assertEqual(cli.main(["check"]), 0)

    def test_about_dialog_explains_license_redistribution_and_warranty(self) -> None:
        app = object.__new__(TextureViewerTkApp)
        app.root = object()
        app.messagebox = mock.Mock()
        app._set_status = mock.Mock()
        app.show_about()
        title, message = app.messagebox.showinfo.call_args.args[:2]
        self.assertEqual(title, "About Texture Viewer")
        self.assertIn("AGPL-3.0-only", message)
        self.assertIn("Redistribution", message)
        self.assertIn("No warranty", message)

    def test_build_validates_platform_and_new_absolute_directories(self) -> None:
        with mock.patch.object(build_script.sys, "version_info", (3, 12, 6)), mock.patch.object(
            build_script.platform, "system", return_value="Linux"
        ):
            with self.assertRaises(build_script.BuildError):
                build_script._validate_runtime()
        with tempfile.TemporaryDirectory(prefix="texture-viewer-build-") as raw:
            parent = Path(raw)
            existing = parent / "existing"
            existing.mkdir()
            with self.assertRaises(build_script.BuildError):
                build_script._require_new_absolute_directory(existing, "output")
            created = build_script._require_new_absolute_directory(
                "new", "output", base_dir=parent
            )
            self.assertTrue(created.is_absolute())
            self.assertFalse(created.exists())

    def test_build_reads_exact_pinned_requirements_and_source_marker(self) -> None:
        lines = []
        for name, version in build_script.PINNED_BUILD_PACKAGES.items():
            lines.extend((f"{name}=={version} \\", "    --hash=sha256:" + "a" * 64))
        with tempfile.TemporaryDirectory(prefix="texture-viewer-build-") as raw:
            root = Path(raw)
            requirements = root / "requirements-build.txt"
            requirements.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.assertEqual(build_script._parse_pinned_requirements(requirements), build_script.PINNED_BUILD_PACKAGES)
            (root / "SOURCE_REVISION").write_text("a" * 40 + "\n", encoding="ascii")
            with mock.patch.object(build_script, "_git_output", return_value=(127, "", "missing")):
                self.assertEqual(build_script._resolve_source_revision(root), "a" * 40)

    def test_build_checks_installed_metadata_for_every_pinned_package(self) -> None:
        expected = dict(build_script.PINNED_BUILD_PACKAGES)
        with mock.patch.object(
            build_script.importlib_metadata,
            "version",
            side_effect=lambda name: expected[name],
        ):
            self.assertEqual(build_script._validate_installed_build_packages(expected), expected)
        with mock.patch.object(
            build_script.importlib_metadata,
            "version",
            side_effect=lambda name: "0.0.0" if name == "pyinstaller" else expected[name],
        ):
            with self.assertRaises(build_script.BuildError):
                build_script._validate_installed_build_packages(expected)

    def test_pyinstaller_command_has_required_flags_and_exclusions(self) -> None:
        context = build_script.BuildContext(
            Path("C:/candidate"),
            Path("C:/output"),
            Path("C:/work"),
            "b" * 40,
            dict(build_script.PINNED_BUILD_PACKAGES),
        )
        command = build_script._pyinstaller_command(context, Path("C:/work/version-file.txt"))
        self.assertEqual(command[:9], [
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--windowed", "--noupx", "--name", "TextureViewer"
        ])
        self.assertIn("--icon", command)
        self.assertIn("NONE", command)
        for module in build_script.EXCLUDED_MODULES:
            self.assertIn(module, command)
        self.assertEqual(command[-1].replace("\\", "/"), "C:/candidate/launcher.py")

    def test_build_metadata_uses_relative_hash_manifest_without_private_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="texture-viewer-build-") as raw:
            bundle = Path(raw) / "TextureViewer"
            bundle.mkdir()
            (bundle / "TextureViewer.exe").write_bytes(b"synthetic")
            context = build_script.BuildContext(
                Path(raw), Path(raw) / "dist", Path(raw) / "work", "c" * 40,
                dict(build_script.PINNED_BUILD_PACKAGES),
            )
            build_script._write_bundle_metadata(context, bundle)
            build_info = json.loads((bundle / "BUILD_INFO.json").read_text(encoding="utf-8"))
            manifest = json.loads((bundle / "MANIFEST.json").read_text(encoding="utf-8"))
            self.assertEqual(build_info["source_revision"], "c" * 40)
            self.assertTrue(any(item["path"] == "TextureViewer.exe" for item in build_info["manifest"]))
            self.assertTrue(any(item["path"] == "BUILD_INFO.json" for item in manifest["entries"]))
            self.assertFalse(any(Path(item["path"]).is_absolute() for item in manifest["entries"]))
            self.assertNotIn("MANIFEST.json", {item["path"] for item in manifest["entries"]})


if __name__ == "__main__":
    unittest.main(verbosity=2)
