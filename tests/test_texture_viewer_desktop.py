# SPDX-FileCopyrightText: 2026 PmNz8
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import base64
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from texture_viewer.desktop import (
    TextureViewerTkApp,
    _metadata_without_views,
    _parse_limit,
    _png_base64_from_result,
    _validate_single_basename,
    run_gui,
)
from texture_viewer.codec.png import encode_png_rgb


class TextureViewerDesktopTests(unittest.TestCase):
    def test_suggested_name_rejects_path_traversal_and_reserved_names(self) -> None:
        for value in (
            "../escape",
            r"..\escape",
            "C:\\escape",
            ".",
            "..",
            "CON",
            "name.",
            "name ",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _validate_single_basename(value)
        self.assertEqual(_validate_single_basename("texture-export"), "texture-export")

    def test_limit_parser_is_headless_and_strict(self) -> None:
        self.assertIsNone(_parse_limit("  "))
        self.assertEqual(_parse_limit("12"), 12)
        for value in ("0", "-1", "one", "1.5"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _parse_limit(value)

    def test_metadata_removes_views_and_base64_recursively(self) -> None:
        value = {
            "logical_path": "Textures\\A.nif",
            "views": {"rgb": {"png_base64": "secret"}},
            "nested": [{"png_base64": "secret"}, {"width": 2}],
        }
        self.assertEqual(
            _metadata_without_views(value),
            {"logical_path": "Textures\\A.nif", "nested": [{}, {"width": 2}]},
        )

    def test_entries_clear_stale_selection_and_preserve_current_entry(self) -> None:
        class FakeList:
            def __init__(self):
                self.items = []
                self.selected = None

            def delete(self, *_args):
                self.items.clear()

            def insert(self, _where, value):
                self.items.append(value)

            def selection_set(self, index):
                self.selected = index

        class FakeVar:
            def __init__(self, value=""):
                self.value = value

            def set(self, value):
                self.value = value

        app = object.__new__(TextureViewerTkApp)
        app.entries = []
        app.selected_entry = "Textures\\Keep.nif"
        app.selected_entry_var = FakeVar(app.selected_entry)
        app.entry_list = FakeList()
        app._set_status = lambda *_args, **_kwargs: None
        app._on_entries(
            {
                "ok": True,
                "count": 2,
                "entries": [
                    {"path": "Textures\\Drop.nif", "storage_mode": "raw", "logical_size": 1},
                    {"path": "Textures\\Keep.nif", "storage_mode": "raw", "logical_size": 2},
                ],
            }
        )
        self.assertEqual(app.selected_entry, "Textures\\Keep.nif")
        self.assertEqual(app.selected_entry_var.value, "Textures\\Keep.nif")
        self.assertEqual(app.entry_list.selected, 1)

        app.selected_entry = "Textures\\Missing.nif"
        app.selected_entry_var.set(app.selected_entry)
        app._on_entries({"ok": True, "count": 1, "entries": [{"path": "Other.nif"}]})
        self.assertIsNone(app.selected_entry)
        self.assertEqual(app.selected_entry_var.value, "")

    def test_failed_backend_operation_restores_last_accepted_controls(self) -> None:
        class FakeVar:
            def __init__(self):
                self.value = None

            def set(self, value):
                self.value = value

        app = object.__new__(TextureViewerTkApp)
        app.active_texture = {"selected_mip": {"index": 2}, "view": "alpha"}
        app.mip_var = FakeVar()
        app.view_var = FakeVar()
        app._updating_controls = False
        app._set_status = lambda *_args, **_kwargs: None
        self.assertFalse(app._handle_failure({"ok": False, "reason": "rejected"}))
        self.assertEqual(app.mip_var.value, "2")
        self.assertEqual(app.view_var.value, "alpha")

    def test_live_filter_debounces_and_rearms_after_busy_worker(self):
        class FakeRoot:
            def __init__(self):
                self.next_id = 0
                self.callbacks = {}
                self.cancelled = []

            def after(self, _delay, callback):
                self.next_id += 1
                self.callbacks[self.next_id] = callback
                return self.next_id

            def after_cancel(self, identifier):
                self.cancelled.append(identifier)
                self.callbacks.pop(identifier, None)

        root = FakeRoot()
        app = object.__new__(TextureViewerTkApp)
        app.root = root
        app.closing = app.closed = False
        app.archive_document = {"path": "sample.dv2"}
        app.future = None
        app.request_entries = mock.Mock()
        app._filter_after_id = None
        app._on_filter_variable_changed()
        first = app._filter_after_id
        app._on_filter_variable_changed()
        second = app._filter_after_id
        self.assertIn(first, root.cancelled)
        self.assertNotEqual(first, second)
        root.callbacks[second]()
        app.request_entries.assert_called_once_with()

        app.request_entries.reset_mock()
        app.future = object()
        app._on_filter_variable_changed()
        busy_id = app._filter_after_id
        root.callbacks[busy_id]()
        self.assertIsNotNone(app._filter_after_id)
        app.request_entries.assert_not_called()

    def test_live_filter_no_archive_or_closing_is_noop(self):
        class FakeRoot:
            def after(self, _delay, callback):
                self.callback = callback
                return 1

            def after_cancel(self, _identifier):
                return None

        app = object.__new__(TextureViewerTkApp)
        app.root = FakeRoot()
        app.closing = False
        app.closed = False
        app.archive_document = None
        app.future = None
        app._filter_after_id = None
        app.request_entries = mock.Mock()
        app._on_filter_variable_changed()
        app.root.callback()
        app.request_entries.assert_not_called()

        app.closing = True
        app._on_filter_variable_changed()
        app.request_entries.assert_not_called()

    def test_reset_clears_all_filter_fields_and_coalesces_refresh(self):
        class FakeVar:
            def __init__(self, value):
                self.value = value

            def set(self, value):
                self.value = value

        app = object.__new__(TextureViewerTkApp)
        app.closing = app.closed = False
        app.filter_substring = FakeVar("foo")
        app.filter_glob = FakeVar("*.nif")
        app.filter_limit = FakeVar("4")
        app._schedule_filter_refresh = mock.Mock()
        app.reset_filters()
        self.assertEqual((app.filter_substring.value, app.filter_glob.value, app.filter_limit.value), ("", "", ""))
        app._schedule_filter_refresh.assert_called_once_with(delay=0)

    def test_double_click_opens_only_the_actual_row_and_guards_empty_busy_area(self):
        class FakeList:
            def __init__(self, index, box, selection):
                self.index = index
                self.box = box
                self.selection = selection

            def nearest(self, _y):
                return self.index

            def bbox(self, _index):
                return self.box

            def selection_clear(self, *_args):
                return None

            def selection_set(self, index):
                self.selection = (index,)

            def curselection(self):
                return self.selection

        app = object.__new__(TextureViewerTkApp)
        app.closing = app.closed = False
        app.future = None
        app.entries = [{"path": "A.nif"}, {"path": "B.nif"}]
        app.selected_entry = None
        app.selected_entry_var = mock.Mock()
        app.entry_list = FakeList(1, (0, 10, 100, 20), (1,))
        app.request_open_texture = mock.Mock()
        self.assertEqual(app._on_entry_double_click(SimpleNamespace(y=15)), "break")
        self.assertEqual(app.selected_entry, "B.nif")
        app.request_open_texture.assert_called_once_with()

        app.request_open_texture.reset_mock()
        app.entry_list = FakeList(1, (0, 10, 100, 20), (1,))
        self.assertEqual(app._on_entry_double_click(SimpleNamespace(y=40)), "break")
        app.request_open_texture.assert_not_called()

        app.future = object()
        self.assertEqual(app._on_entry_double_click(SimpleNamespace(y=15)), "break")
        app.request_open_texture.assert_not_called()

    def test_run_gui_can_be_mocked_without_tk_or_mainloop(self) -> None:
        class FakeApp:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.ran = False

            def run(self):
                self.ran = True

        fake_root = object()
        fake_tk = object()
        fake_ttk = object()
        fake_dialog = object()
        with mock.patch(
            "texture_viewer.desktop.TextureViewerTkApp", side_effect=FakeApp
        ) as app_type:
            result = run_gui(
                root=fake_root,
                tk_module=fake_tk,
                ttk_module=fake_ttk,
                filedialog_module=fake_dialog,
            )
        self.assertEqual(result, 0)
        self.assertEqual(app_type.call_count, 1)
        self.assertIs(app_type.call_args.kwargs["root"], fake_root)

    def test_close_waits_for_pending_job_without_running_callback(self):
        app = object.__new__(TextureViewerTkApp)
        app.root = mock.Mock()
        app.executor = mock.Mock()
        app.future = Future()
        app.closed = app.closing = False
        app._set_busy = mock.Mock()
        app._set_status = mock.Mock()
        callback = mock.Mock()
        pending = app.future
        app.request_close()
        app.root.destroy.assert_not_called()
        app._poll_future(callback)
        app.root.after.assert_called_once()
        pending.set_result({"ok": True})
        app._poll_future(callback)
        app.root.destroy.assert_called_once()
        app.executor.shutdown.assert_called_once_with(wait=True)
        callback.assert_not_called()

    def test_export_dialogs_dispatch_loaded_texture_and_allow_cancellation(self):
        app = object.__new__(TextureViewerTkApp)
        app.root = object()
        app.active_texture = {"logical_path": "Textures/Loaded.nif", "selected_mip": {"index": 2}}
        app.selected_entry = "Textures/Other.nif"
        app.view_var = mock.Mock()
        app.view_var.get.return_value = "alpha"
        app.filedialog = mock.Mock()
        app.controller = mock.Mock()
        app._submit = mock.Mock()
        app._set_status = mock.Mock()
        app.filedialog.asksaveasfilename.return_value = ""
        app.choose_png_export()
        app._submit.assert_not_called()
        app.filedialog.asksaveasfilename.return_value = "new.png"
        app.choose_png_export()
        app._submit.call_args.args[0]()
        app.controller.export_png.assert_called_once_with("new.png", "alpha", 2)
        self.assertEqual(app.filedialog.asksaveasfilename.call_args.kwargs["initialfile"], "Loaded-mip02.png")
        app.filedialog.askdirectory.return_value = "exports"
        app.choose_set_export()
        app._submit.call_args.args[0]()
        app.controller.export_set.assert_called_once_with(str(Path("exports") / "Loaded-export"))
        app.choose_package_export()
        app._submit.call_args.args[0]()
        app.controller.export_asset_package.assert_called_once_with(str(Path("exports") / "Loaded-package"))
        app.active_texture = None
        app.filedialog.reset_mock()
        app.choose_package_export()
        app.filedialog.askdirectory.assert_not_called()

    def test_profile_link_is_fixed_and_only_opened_on_click(self):
        app = object.__new__(TextureViewerTkApp)
        app._set_status = mock.Mock()
        with mock.patch("texture_viewer.desktop.webbrowser.open_new_tab", return_value=True) as browser:
            app.open_profile()
        browser.assert_called_once_with("https://github.com/PmNz8")

    def test_native_preview_forwards_original_png_and_sets_actual_scrollregion(self):
        class FakeCanvas:
            def __init__(self):
                self.deleted = []
                self.created = []
                self.configurations = []

            def delete(self, value):
                self.deleted.append(value)

            def create_image(self, *args, **kwargs):
                self.created.append((args, kwargs))
                return "image-id"

            def configure(self, **kwargs):
                self.configurations.append(kwargs)

        class FakePhoto:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def width(self):
                return 8192

            def height(self):
                return 4096

        source = encode_png_rgb(2, 1, bytes((255, 0, 0, 0, 255, 0)))
        encoded = base64.b64encode(source).decode("ascii")
        photos = []

        def photo_factory(**kwargs):
            photo = FakePhoto(**kwargs)
            photos.append(photo)
            return photo

        app = object.__new__(TextureViewerTkApp)
        app.photo_factory = photo_factory
        app.preview_canvas = FakeCanvas()
        app.photo = None
        app.preview_item = None
        app._set_status = mock.Mock()
        result = {"ok": True, "view": "rgb", "views": {"rgb": {"png_base64": encoded}}}
        self.assertTrue(app._render_result(result))
        self.assertEqual(len(photos), 1)
        self.assertEqual(photos[0].kwargs, {"data": encoded, "format": "png"})
        self.assertEqual(app.preview_canvas.configurations[-1]["scrollregion"], (0, 0, 8192, 4096))
        self.assertEqual(_png_base64_from_result(result), encoded)

    def test_native_preview_does_not_cap_large_photo_dimensions(self):
        class FakeCanvas:
            def __init__(self):
                self.scrollregion = None

            def delete(self, *_args):
                return None

            def create_image(self, *_args, **_kwargs):
                return "image-id"

            def configure(self, **kwargs):
                self.scrollregion = kwargs.get("scrollregion")

        class FakePhoto:
            def width(self):
                return 10000

            def height(self):
                return 9000

        source = encode_png_rgb(1, 1, bytes((1, 2, 3)))
        app = object.__new__(TextureViewerTkApp)
        app.photo_factory = lambda **_kwargs: FakePhoto()
        app.preview_canvas = FakeCanvas()
        app.photo = None
        app.preview_item = None
        app._set_status = mock.Mock()
        self.assertTrue(
            app._render_result(
                {"ok": True, "png_base64": base64.b64encode(source).decode("ascii")}
            )
        )
        self.assertEqual(app.preview_canvas.scrollregion, (0, 0, 10000, 9000))

    def test_native_preview_reports_photo_dimension_failure(self):
        class FakeCanvas:
            def delete(self, *_args):
                return None

            def create_image(self, *_args, **_kwargs):
                return "image-id"

            def configure(self, **_kwargs):
                return None

        class BadPhoto:
            def width(self):
                return 0

            def height(self):
                return 10

        source = encode_png_rgb(1, 1, bytes((1, 2, 3)))
        app = object.__new__(TextureViewerTkApp)
        app.photo_factory = lambda **_kwargs: BadPhoto()
        app.preview_canvas = FakeCanvas()
        app.photo = None
        app.preview_item = None
        app._set_status = mock.Mock()
        self.assertFalse(
            app._render_result(
                {"ok": True, "png_base64": base64.b64encode(source).decode("ascii")}
            )
        )
        self.assertIn("preview failed", app._set_status.call_args.args[0])

    def test_zoom_frontend_controls_and_callbacks_are_removed(self):
        self.assertFalse(hasattr(TextureViewerTkApp, "_on_zoom_changed"))
        self.assertFalse(hasattr(TextureViewerTkApp, "_set_zoom_backend"))
        self.assertFalse(hasattr(TextureViewerTkApp, "_run_pending_zoom"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
