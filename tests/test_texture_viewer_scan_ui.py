# SPDX-FileCopyrightText: 2026 PmNz8
# SPDX-License-Identifier: AGPL-3.0-only
"""Scan/controller wiring tests with no Tk window or real game archive."""
from pathlib import Path
from queue import Queue
import tempfile
import unittest
from unittest import mock

from texture_viewer.controller import TextureViewerController
from texture_viewer.desktop import TextureViewerTkApp
from tests.test_texture_viewer_model import _make_archive, _make_texture
from texture_viewer.codec import bc


class ScanIntegrationTests(unittest.TestCase):
    def test_controller_scan_and_filtered_list(self):
        with tempfile.TemporaryDirectory() as raw:
            archive = _make_archive(Path(raw), [
                ("bad.nif", b"not a texture", "raw"),
                ("other.item", b"not a texture", "raw"),
                ("valid.NIF", _make_texture(bc.FORMAT_BC1), "zlib"),
            ])
            controller = TextureViewerController()
            self.assertTrue(controller.open_archive(str(archive))["ok"])
            self.assertFalse(controller.list_entries(compatible_only=True)["ok"])
            progress = []
            result = controller.scan_compatible_textures(progress.append)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["scan"]["compatible"], 1)
            self.assertEqual(progress[-1]["processed"], 2)
            rows = controller.list_entries(limit=1, compatible_only=True)
            self.assertEqual([row["path"] for row in rows["entries"]], ["valid.NIF"])

    def test_progress_mailbox_is_bounded_and_tk_updated_only_by_poll(self):
        app = object.__new__(TextureViewerTkApp)
        app._scan_progress = Queue(maxsize=1)
        app.progress_bar = mock.Mock()
        app._set_status = mock.Mock()
        app.closing = False
        for index in range(101):
            app._queue_scan_progress({"candidates": 100, "processed": index, "compatible": index})
        self.assertEqual(app._scan_progress.qsize(), 1)
        app.progress_bar.configure.assert_not_called()
        app._show_scan_progress()
        app.progress_bar.configure.assert_called_once_with(maximum=100, value=100)
        self.assertIn("100/100", app._set_status.call_args.args[0])

    def test_scan_completion_hides_progress_and_applies_latest_filters(self):
        app = object.__new__(TextureViewerTkApp)
        app._scan_running = True
        app.progress_bar = mock.Mock()
        app.archive_document = {"path": "sample.dv2"}
        app.active_texture = None
        app._set_metadata = mock.Mock()
        app._set_status = mock.Mock()
        app.request_entries = mock.Mock()
        scan = {"candidates": 0, "processed": 0, "compatible": 0, "rejected": 0, "elapsed_seconds": 0.0}
        app._on_scan_finished({"ok": True, "scan": scan})
        self.assertFalse(app._scan_running)
        app.progress_bar.pack_forget.assert_called_once()
        app.request_entries.assert_called_once()
        self.assertEqual(app.archive_document["texture_scan"], scan)
        app.request_entries.reset_mock()
        app._on_scan_finished({"ok": False, "reason": "scan failed"})
        app.request_entries.assert_not_called()

    def test_gui_listing_always_requests_compatible_only(self):
        app = object.__new__(TextureViewerTkApp)
        app.filter_limit = mock.Mock(get=lambda: "5")
        app.filter_substring = mock.Mock(get=lambda: "rock")
        app.filter_glob = mock.Mock(get=lambda: "*.nif")
        app.controller = mock.Mock()
        app._submit = mock.Mock()
        app.request_entries()
        app._submit.call_args.args[0]()
        app.controller.list_entries.assert_called_once_with("rock", "*.nif", 5, compatible_only=True)


if __name__ == "__main__":
    unittest.main()
