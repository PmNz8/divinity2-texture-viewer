# SPDX-FileCopyrightText: 2026 PmNz8
# SPDX-License-Identifier: AGPL-3.0-only
import json
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from tests.test_texture_viewer_model import _make_texture, _make_archive
from texture_viewer.model import TextureViewerModel, TextureViewerError
from texture_viewer.codec import bc


class BatchExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive = _make_archive(self.root, [
            (r"a\same.nif", _make_texture(bc.FORMAT_BC1), "raw"),
            (r"b\same.nif", _make_texture(bc.FORMAT_BC3), "zlib"),
            ("test.nif", _make_texture(bc.FORMAT_BC2), "raw"),
            ("bad.nif", b"unsupported", "raw"),
        ])
        self.model = TextureViewerModel()
        self.model.open_archive(self.archive)

    def test_partial_errors_bc2_skip_identity_and_source_preserved(self):
        before = self.archive.read_bytes()
        original = self.model.open_texture("a/same.nif")
        report = self.model.export_asset_packages(self.root / "batch", mip_mode="base")
        self.assertEqual([item["status"] for item in report["items"]],
                         ["exported", "exported", "skipped", "error"])
        self.assertEqual(len({item["package"] for item in report["items"]}), 4)
        self.assertIs(self.model.selection, original)
        self.assertEqual(self.archive.read_bytes(), before)
        saved = json.loads((self.root / "batch/batch-report.json").read_text())
        self.assertEqual(len(saved["items"]), 4)
        for item in saved["items"][:2]:
            texture = self.root / "batch" / item["package"] / "texture"
            self.assertTrue((texture / "mip-00.rgb.png").is_file())
            self.assertFalse((texture / "mip-01.rgb.png").exists())
        with self.assertRaises(TextureViewerError):
            self.model.export_asset_packages(self.root / "batch")

    def test_cancellation_and_filter(self):
        progress = []
        report = self.model.export_asset_packages(self.root / "stopped",
            cancelled=lambda: bool(progress), progress=progress.append)
        self.assertTrue(report["cancelled"])
        self.assertEqual(len(report["items"]), 1)
        self.model.scan_compatible_textures()
        report = self.model.export_asset_packages(self.root / "filtered", substring=r"b\same")
        self.assertEqual(report["total"], 1)
        self.assertEqual(report["items"][0]["status"], "exported")

    def test_failed_package_publish_leaves_no_partial_package(self):
        with mock.patch("texture_viewer.model.os.rename", side_effect=OSError("simulated")):
            report = self.model.export_asset_packages(self.root / "failed", limit=1)
        self.assertEqual(report["items"][0]["status"], "error")
        self.assertEqual([p.name for p in (self.root / "failed").iterdir()], ["batch-report.json"])


if __name__ == "__main__":
    unittest.main()
