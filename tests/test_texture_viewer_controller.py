# SPDX-FileCopyrightText: 2026 PmNz8
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from unittest import mock
import zlib

from texture_viewer.codec import bc
from texture_viewer.codec.png import decode_png

from texture_viewer.controller import TextureViewerController
from texture_viewer.model import TextureViewerModel

from tests.test_texture_viewer_model import _make_archive, _make_texture


class TextureViewerControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="texture-viewer-controller-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.archive = _make_archive(
            self.tmp,
            [
                ("Textures\\Valid.nif", _make_texture(bc.FORMAT_BC3), "raw"),
                ("Data\\Readme.txt", b"not texture", "raw"),
            ],
        )
        self.original_sha = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.controller = TextureViewerController()

    def test_controller_results_are_json_safe_and_views_decode(self) -> None:
        result = self.controller.open_archive(str(self.archive))
        encoded = json.dumps(result, allow_nan=False)
        self.assertIsInstance(json.loads(encoded), dict)
        listed = self.controller.list_entries(substring="valid")
        self.assertEqual(listed["count"], 1)
        opened = self.controller.open_texture("Textures\\Valid.nif", 1)
        self.assertTrue(opened["ok"])
        self.assertEqual(opened["selected_mip"], {"index": 1, "width": 4, "height": 2})
        self.assertEqual(set(opened["views"]), {"rgb", "alpha", "composite"})
        for name, expected_color_type in (("rgb", 2), ("alpha", 0), ("composite", 6)):
            image = decode_png(base64.b64decode(opened["views"][name]["png_base64"]))
            self.assertEqual(image.color_type, expected_color_type)
            self.assertEqual((image.width, image.height), (4, 2))
        self.assertEqual(opened["pixel_format_name"], "BC3")
        self.assertEqual(opened["alpha_mode"], "bc3_interpolated")
        self.assertTrue(opened["alpha_required"])

    def test_bridge_failures_return_false_and_preserve_state(self) -> None:
        self.assertEqual(self.controller.open_archive(None)["ok"], False)
        self.assertEqual(self.controller.open_archive(str(self.archive))["ok"], True)
        self.assertEqual(self.controller.open_texture("Textures\\Valid.nif")["ok"], True)
        before = self.controller.current_view()
        bad_texture = self.controller.open_texture("Data\\Readme.txt")
        self.assertFalse(bad_texture["ok"])
        after = self.controller.current_view()
        self.assertEqual(after["mip_index"], before["mip_index"])
        bad_mip = self.controller.select_mip(99)
        self.assertFalse(bad_mip["ok"])
        self.assertEqual(self.controller.current_view()["mip_index"], before["mip_index"])
        self.assertFalse(self.controller.set_view("bogus")["ok"])
        self.assertFalse(self.controller.set_zoom("bad")["ok"])
        self.assertTrue(self.controller.archive_info()["ok"])

    def test_close_and_failed_reopen_follow_lifecycle(self) -> None:
        self.controller.open_archive(str(self.archive))
        self.controller.open_texture("Textures\\Valid.nif")
        failed = self.controller.open_archive(str(self.tmp / "missing.dv2"))
        self.assertFalse(failed["ok"])
        self.assertEqual(self.controller.current_view()["ok"], True)
        self.assertEqual(self.controller.close_archive(), {"ok": True, "archive": None, "texture": None})
        self.assertFalse(self.controller.archive_info()["ok"])

    def test_png_export_refuses_overwrite_and_preserves_source(self) -> None:
        self.controller.open_archive(str(self.archive))
        self.controller.open_texture("Textures\\Valid.nif", 0)
        destination = self.tmp / "out.png"
        result = self.controller.export_png(str(destination))
        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], str(destination.resolve()))
        self.assertEqual(hashlib.sha256(destination.read_bytes()).hexdigest(), result["sha256"])
        refused = self.controller.export_png(str(destination))
        self.assertFalse(refused["ok"])
        self.assertEqual(hashlib.sha256(self.archive.read_bytes()).hexdigest(), self.original_sha)

    def test_png_export_cleans_owned_partial_file_after_fsync_failure(self) -> None:
        self.controller.open_archive(str(self.archive))
        self.controller.open_texture("Textures\\Valid.nif", 0)
        destination = self.tmp / "failed.png"
        with mock.patch("texture_viewer.model.os.fsync", side_effect=OSError("injected fsync failure")):
            result = self.controller.export_png(str(destination))
        self.assertFalse(result["ok"])
        self.assertFalse(destination.exists())
        self.assertEqual(hashlib.sha256(self.archive.read_bytes()).hexdigest(), self.original_sha)

    def test_export_set_and_destination_safety(self) -> None:
        self.controller.open_archive(str(self.archive))
        self.controller.open_texture("Textures\\Valid.nif")
        destination = self.tmp / "export-set"
        result = self.controller.export_set(str(destination))
        self.assertTrue(result["ok"])
        self.assertEqual(result["file_count"], 7)  # manifest + 3 RGB + 3 alpha
        self.assertTrue((destination / "texture.json").is_file())
        self.assertFalse(self.controller.export_set(str(destination))["ok"])
        # A directory named Packed is treated as the game-tree boundary.
        packed = self.tmp / "Packed"
        packed.mkdir()
        packed_archive = _make_archive(
            packed,
            [("Textures\\Valid.nif", _make_texture(bc.FORMAT_BC1), "raw")],
            "inside.dv2",
        )
        packed_controller = TextureViewerController()
        self.assertTrue(packed_controller.open_archive(str(packed_archive))["ok"])
        self.assertTrue(packed_controller.open_texture("Textures\\Valid.nif")["ok"])
        self.assertFalse(packed_controller.export_png(str(packed / "outside.png"))["ok"])
        self.assertFalse(packed_controller.export_set(str(packed / "new-set"))["ok"])
        self.assertFalse(
            packed_controller.export_asset_package(str(packed / "new-package"))["ok"]
        )

    def test_asset_package_export_schema_template_and_read_only(self) -> None:
        self.controller.open_archive(str(self.archive))
        self.controller.open_texture("Textures\\Valid.nif")
        destination = self.tmp / "asset-package"
        result = self.controller.export_asset_package(str(destination))
        self.assertTrue(result["ok"])
        self.assertEqual(
            set(result["files"]),
            {
                "asset.json",
                "template.nif",
                "texture/texture.json",
                "texture/mip-00.rgb.png",
                "texture/mip-00.alpha.png",
                "texture/mip-01.rgb.png",
                "texture/mip-01.alpha.png",
                "texture/mip-02.rgb.png",
                "texture/mip-02.alpha.png",
            },
        )
        self.assertEqual((destination / "template.nif").read_bytes(), _make_texture(bc.FORMAT_BC3))
        asset = json.loads((destination / "asset.json").read_text(encoding="utf-8"))
        self.assertEqual(asset["schema"], "divinity2.dks_asset_package")
        self.assertEqual(asset["schema_version"], 1)
        self.assertEqual(asset["template"]["payload_sha256"], hashlib.sha256(_make_texture(bc.FORMAT_BC3)).hexdigest())
        self.assertFalse(self.controller.export_asset_package(str(destination))["ok"])
        self.assertEqual(hashlib.sha256(self.archive.read_bytes()).hexdigest(), self.original_sha)

    def test_asset_package_export_cleans_private_staging_on_write_failure(self) -> None:
        self.controller.open_archive(str(self.archive))
        self.controller.open_texture("Textures\\Valid.nif")
        destination = self.tmp / "failed-package"
        with mock.patch(
            "texture_viewer.model.os.fsync",
            side_effect=OSError("injected package fsync failure"),
        ):
            result = self.controller.export_asset_package(str(destination))
        self.assertFalse(result["ok"])
        self.assertFalse(destination.exists())
        self.assertFalse(
            any(path.name.startswith(".failed-package.") for path in self.tmp.iterdir())
        )
        self.assertEqual(hashlib.sha256(self.archive.read_bytes()).hexdigest(), self.original_sha)

    def test_bad_archive_and_non_texture_are_honest_failures(self) -> None:
        self.assertFalse(self.controller.open_archive(str(self.tmp / "bad.dv2"))["ok"])
        bad = self.tmp / "bad.dv2"
        bad.write_bytes(b"bad")
        self.assertFalse(self.controller.open_archive(str(bad))["ok"])
        self.assertTrue(self.controller.open_archive(str(self.archive))["ok"])
        result = self.controller.open_texture("Data\\Readme.txt")
        self.assertFalse(result["ok"])
        self.assertIn("texture", result["reason"].casefold())

        malformed_archive = _make_archive(
            self.tmp,
            [("Textures\\Malformed.nif", _make_texture(bc.FORMAT_BC1)[:-1], "raw")],
            "malformed.dv2",
        )
        malformed_controller = TextureViewerController()
        self.assertTrue(malformed_controller.open_archive(str(malformed_archive))["ok"])
        malformed = malformed_controller.open_texture("Textures\\Malformed.nif")
        self.assertFalse(malformed["ok"])
        self.assertIn("texture", malformed["reason"].casefold())

    def test_invalid_extension_and_optional_symlink_destination_are_rejected(self) -> None:
        self.controller.open_archive(str(self.archive))
        self.controller.open_texture("Textures\\Valid.nif")
        self.assertFalse(self.controller.export_png(str(self.tmp / "wrong.jpg"))["ok"])
        target = self.tmp / "existing.png"
        target.write_bytes(b"do not replace")
        self.assertFalse(self.controller.export_png(str(target))["ok"])
        link = self.tmp / "link.png"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlink creation unavailable: {error}")
        self.assertFalse(self.controller.export_png(str(link))["ok"])

    def test_reparse_component_guard_is_deterministic_without_symlink_privilege(self) -> None:
        self.controller.open_archive(str(self.archive))
        self.controller.open_texture("Textures\\Valid.nif")
        guarded_parent = self.tmp / "guarded-parent"
        guarded_parent.mkdir()
        destination = guarded_parent / "out.png"
        with mock.patch(
            "texture_viewer.model._is_reparse_point",
            side_effect=lambda path: path == guarded_parent,
        ):
            result = self.controller.export_png(str(destination))
        self.assertFalse(result["ok"])
        self.assertFalse(destination.exists())

    def test_dialog_cancellation_is_success_and_preserves_selection(self) -> None:
        self.controller.open_archive(str(self.archive))
        self.controller.open_texture("Textures\\Valid.nif", 1)
        before = self.controller.current_view()
        cancelled_archive = TextureViewerController(choose_archive=lambda: None)
        self.assertEqual(cancelled_archive.open_archive_dialog(), {"ok": True, "cancelled": True})
        self.assertIsNone(cancelled_archive.model.archive_path)
        cancelled_png = TextureViewerController(
            model=self.controller.model,
            save_png=lambda _suggested: None,
        )
        self.assertEqual(cancelled_png.export_png_dialog(), {"ok": True, "cancelled": True})
        cancelled_set = TextureViewerController(
            model=self.controller.model,
            choose_export_directory=lambda _suggested: None,
        )
        self.assertEqual(cancelled_set.export_set_dialog(), {"ok": True, "cancelled": True})
        cancelled_package = TextureViewerController(
            model=self.controller.model,
            choose_package_directory=lambda _suggested: None,
        )
        self.assertEqual(
            cancelled_package.export_asset_package_dialog(),
            {"ok": True, "cancelled": True},
        )
        self.assertEqual(cancelled_png.current_view()["mip_index"], before["mip_index"])

    def test_dialog_callback_exception_is_a_bridge_failure(self) -> None:
        controller = TextureViewerController(
            choose_archive=lambda: (_ for _ in ()).throw(RuntimeError("dialog failure")),
        )
        result = controller.open_archive_dialog()
        self.assertFalse(result["ok"])
        self.assertIn("dialog failure", result["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
