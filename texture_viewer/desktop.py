# SPDX-FileCopyrightText: 2026 PmNz8
# SPDX-License-Identifier: AGPL-3.0-only
"""Small Tkinter desktop frontend for the developer Texture Viewer."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import json
from pathlib import Path
from pathlib import PureWindowsPath
from queue import Empty, Full, Queue
import webbrowser
from typing import Any, Callable

from .controller import TextureViewerController
from .version import APP_NAME, COPYRIGHT, LICENSE_NAME, PROFILE_URL, VERSION

WINDOW_TITLE = f"{APP_NAME} {VERSION} candidate"
FOOTER_TEXT = f"{COPYRIGHT} · AGPLv3 ({LICENSE_NAME}) · No warranty"
ABOUT_TEXT = (
    f"{APP_NAME} {VERSION} candidate\n\n"
    f"{COPYRIGHT}\n"
    f"Licensed under GNU AGPL-3.0-only. Redistribution is permitted only under "
    "the license terms. The corresponding source, LICENSE, and third-party "
    "notices are distributed beside the executable.\n\n"
    "No warranty is provided; use the tool and exported files at your own risk."
)


def _validate_single_basename(value: str) -> str:
    """Accept one conservative directory basename for an export destination."""

    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("suggested export-set name must be a non-empty basename")
    if value in {".", ".."} or any(
        character in value for character in '<>:"/\\|?*'
    ) or any(ord(character) < 32 for character in value):
        raise ValueError("suggested export-set name contains unsafe filename characters")
    windows = PureWindowsPath(value)
    if windows.is_absolute() or windows.drive or windows.root or len(windows.parts) != 1:
        raise ValueError("suggested export-set name must be one relative basename")
    if value[-1] in {" ", "."}:
        raise ValueError("suggested export-set name must not end with space or dot")
    stem = value.split(".", 1)[0].casefold()
    if stem in {"con", "prn", "aux", "nul"} or (
        len(stem) == 4 and stem[:3] in {"com", "lpt"} and stem[3].isdigit()
    ):
        raise ValueError("suggested export-set name uses a reserved Windows device name")
    return value


def _metadata_without_views(value: object) -> object:
    """Remove image payloads before showing a controller document as JSON."""

    if isinstance(value, dict):
        return {
            key: _metadata_without_views(item)
            for key, item in value.items()
            if key not in {"views", "png_base64"}
        }
    if isinstance(value, list):
        return [_metadata_without_views(item) for item in value]
    return value


def _png_base64_from_result(result: dict[str, object]) -> str | None:
    """Extract the currently selected PNG payload without PNG re-encoding."""

    encoded: object | None = result.get("png_base64")
    if encoded is None:
        views = result.get("views")
        if isinstance(views, dict):
            view_name = result.get("view", "composite")
            view = views.get(view_name)
            if isinstance(view, dict):
                encoded = view.get("png_base64")
    if not isinstance(encoded, str) or not encoded:
        return None
    return encoded


def _parse_limit(value: str) -> int | None:
    """Parse the optional list limit without touching Tk or the backend."""

    stripped = value.strip()
    if not stripped:
        return None
    if not stripped.isdecimal():
        raise ValueError("entry limit must be a positive integer")
    result = int(stripped)
    if result <= 0:
        raise ValueError("entry limit must be a positive integer")
    return result


class TextureViewerTkApp:
    """Tkinter view/controller shell; all backend work is serialized off-thread."""

    def __init__(
        self,
        *,
        root: Any,
        tk_module: Any,
        ttk_module: Any,
        filedialog_module: Any,
        messagebox_module: Any | None = None,
        controller: TextureViewerController | None = None,
        photo_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.root = root
        self.tk = tk_module
        self.ttk = ttk_module
        self.filedialog = filedialog_module
        self.messagebox = messagebox_module
        self.controller = controller or TextureViewerController()
        self.photo_factory = photo_factory or tk_module.PhotoImage
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="texture-viewer-backend"
        )
        self.future: Future[dict[str, object]] | None = None
        self.closing = False
        self.closed = False
        self.busy_widgets: list[tuple[Any, str]] = []
        self.entries: list[dict[str, object]] = []
        self.selected_entry: str | None = None
        self.active_texture: dict[str, object] | None = None
        self.archive_document: dict[str, object] | None = None
        self.photo: Any | None = None
        self.preview_item: Any | None = None
        self._updating_controls = False
        self._filter_after_id: Any | None = None
        self._scan_progress: Queue[dict[str, object]] = Queue(maxsize=1)
        self._scan_running = False
        self.scan_summary: dict[str, object] | None = None

        self.filter_substring = tk_module.StringVar(value="")
        self.filter_glob = tk_module.StringVar(value="")
        self.filter_limit = tk_module.StringVar(value="")
        self.selected_entry_var = tk_module.StringVar(value="")
        self.active_texture_var = tk_module.StringVar(value="")
        self.mip_var = tk_module.StringVar(value="")
        self.view_var = tk_module.StringVar(value="composite")
        self.status_var = tk_module.StringVar(value="No archive open")
        self._filter_trace_tokens = [
            self.filter_substring.trace_add("write", self._on_filter_variable_changed),
            self.filter_glob.trace_add("write", self._on_filter_variable_changed),
            self.filter_limit.trace_add("write", self._on_filter_variable_changed),
        ]
        self._build_ui()

    def _build_ui(self) -> None:
        self.root.title(WINDOW_TITLE)
        self.root.geometry("1280x860")
        self.root.minsize(960, 600)
        self.root.configure(bg="#111820")
        self.root.protocol("WM_DELETE_WINDOW", self.request_close)
        try:
            style = self.ttk.Style(self.root)
            style.configure("Viewer.TFrame", background="#111820")
            style.configure("Viewer.TLabel", background="#111820", foreground="#e8eef2")
            style.configure("Viewer.TLabelframe", background="#111820", foreground="#e8eef2")
            style.configure("Viewer.TLabelframe.Label", background="#111820", foreground="#e8eef2")
        except Exception:
            style = None

        toolbar = self.ttk.Frame(self.root, style="Viewer.TFrame", padding=8)
        toolbar.grid(row=0, column=0, sticky="ew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        self._button(toolbar, "Open DV2", self.choose_archive, "open_archive").pack(
            side="left", padx=(0, 4)
        )
        self._button(toolbar, "Info", self.request_archive_info, "archive_info").pack(
            side="left", padx=4
        )
        self._button(toolbar, "Close", self.request_close_archive, "close_archive").pack(
            side="left", padx=4
        )
        self._button(toolbar, "About / License", self.show_about, "about").pack(
            side="left", padx=4
        )
        self.status_label = self.ttk.Label(
            toolbar, textvariable=self.status_var, style="Viewer.TLabel"
        )
        self.status_label.pack(side="right", padx=(16, 0))
        self.progress_bar = self.ttk.Progressbar(toolbar, length=170, mode="indeterminate")

        body = self.ttk.Frame(self.root, style="Viewer.TFrame", padding=(8, 0, 8, 4))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        left = self.ttk.Frame(body, style="Viewer.TFrame", padding=(0, 0, 8, 0))
        left.grid(row=0, column=0, sticky="ns")
        right = self.ttk.Frame(body, style="Viewer.TFrame")
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)
        right.rowconfigure(1, weight=0)

        entries_frame = self.ttk.LabelFrame(
            left, text="Entries", style="Viewer.TLabelframe", padding=6
        )
        left.rowconfigure(0, weight=1)
        left.rowconfigure(1, weight=0)
        entries_frame.grid(row=0, column=0, sticky="nsew")
        self.ttk.Label(entries_frame, text="Substring", style="Viewer.TLabel").pack(anchor="w")
        substring = self.ttk.Entry(entries_frame, textvariable=self.filter_substring, width=34)
        substring.pack(fill="x")
        self.ttk.Label(entries_frame, text="Glob pattern", style="Viewer.TLabel").pack(anchor="w")
        glob = self.ttk.Entry(entries_frame, textvariable=self.filter_glob, width=34)
        glob.pack(fill="x")
        self.ttk.Label(entries_frame, text="Limit", style="Viewer.TLabel").pack(anchor="w")
        limit = self.ttk.Entry(entries_frame, textvariable=self.filter_limit, width=34)
        limit.pack(fill="x")
        self._button(entries_frame, "Reset", self.reset_filters, "reset_filters").pack(
            fill="x", pady=(5, 4)
        )
        list_frame = self.ttk.Frame(entries_frame)
        list_frame.pack(fill="both", expand=True)
        self.entry_list = self.tk.Listbox(
            list_frame,
            height=6,
            width=46,
            exportselection=False,
            background="#1a2630",
            foreground="#e8eef2",
            selectbackground="#345063",
            relief="flat",
        )
        self.entry_list.pack(side="left", fill="both", expand=True)
        entry_scroll = self.ttk.Scrollbar(list_frame, orient="vertical", command=self.entry_list.yview)
        entry_scroll.pack(side="right", fill="y")
        self.entry_list.configure(yscrollcommand=entry_scroll.set)
        self.entry_list.bind("<<ListboxSelect>>", self._on_entry_selected)
        self.entry_list.bind("<Double-Button-1>", self._on_entry_double_click)
        self._register_busy(self.entry_list, "normal")

        texture_frame = self.ttk.LabelFrame(
            left, text="Texture", style="Viewer.TLabelframe", padding=6
        )
        texture_frame.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.ttk.Label(texture_frame, text="Selected entry", style="Viewer.TLabel").pack(anchor="w")
        selected_entry = self.ttk.Entry(
            texture_frame, textvariable=self.selected_entry_var, state="readonly", width=42
        )
        selected_entry.pack(fill="x")
        self.ttk.Label(texture_frame, text="Loaded texture", style="Viewer.TLabel").pack(anchor="w")
        active_entry = self.ttk.Entry(
            texture_frame, textvariable=self.active_texture_var, state="readonly", width=42
        )
        active_entry.pack(fill="x")
        open_close = self.ttk.Frame(texture_frame)
        open_close.pack(fill="x", pady=(5, 0))
        self._button(open_close, "Close texture", self.request_close_texture, "close_texture").pack(
            side="left", fill="x", expand=True
        )

        self.ttk.Label(texture_frame, text="MIP level", style="Viewer.TLabel").pack(anchor="w", pady=(6, 0))
        self.mip_combo = self.ttk.Combobox(
            texture_frame, textvariable=self.mip_var, state="readonly", values=()
        )
        self.mip_combo.pack(fill="x")
        self.mip_combo.bind("<<ComboboxSelected>>", self._on_mip_selected)
        self._register_busy(self.mip_combo, "readonly")
        self.ttk.Label(texture_frame, text="View", style="Viewer.TLabel").pack(anchor="w", pady=(6, 0))
        self.view_combo = self.ttk.Combobox(
            texture_frame,
            textvariable=self.view_var,
            state="readonly",
            values=("rgb", "alpha", "composite"),
        )
        self.view_combo.pack(fill="x")
        self.view_combo.bind("<<ComboboxSelected>>", self._on_view_selected)
        self._register_busy(self.view_combo, "readonly")
        self._button(texture_frame, "Export PNG", self.choose_png_export, "export_png").pack(
            fill="x", pady=(6, 2)
        )
        self._button(texture_frame, "Export Set", self.choose_set_export, "export_set").pack(
            fill="x", pady=2
        )
        self._button(
            texture_frame,
            "Export Builder Package",
            self.choose_package_export,
            "export_package",
        ).pack(fill="x", pady=2)

        preview_frame = self.ttk.LabelFrame(
            right, text="Preview", style="Viewer.TLabelframe", padding=4
        )
        preview_frame.grid(row=0, column=0, sticky="nsew")
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)
        self.preview_canvas = self.tk.Canvas(
            preview_frame, background="#273039", highlightthickness=0
        )
        self.preview_canvas.grid(row=0, column=0, sticky="nsew")
        preview_x = self.ttk.Scrollbar(
            preview_frame, orient="horizontal", command=self.preview_canvas.xview
        )
        preview_x.grid(row=1, column=0, sticky="ew")
        preview_y = self.ttk.Scrollbar(
            preview_frame, orient="vertical", command=self.preview_canvas.yview
        )
        preview_y.grid(row=0, column=1, sticky="ns")
        self.preview_canvas.configure(xscrollcommand=preview_x.set, yscrollcommand=preview_y.set)

        metadata_frame = self.ttk.LabelFrame(
            right, text="Metadata", style="Viewer.TLabelframe", padding=4
        )
        metadata_frame.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        metadata_frame.columnconfigure(0, weight=1)
        self.metadata_text = self.tk.Text(
            metadata_frame,
            height=9,
            wrap="word",
            background="#1a2630",
            foreground="#cfe0e8",
            insertbackground="#cfe0e8",
            relief="flat",
        )
        self.metadata_text.grid(row=0, column=0, sticky="ew")
        metadata_scroll = self.ttk.Scrollbar(
            metadata_frame, orient="vertical", command=self.metadata_text.yview
        )
        metadata_scroll.grid(row=0, column=1, sticky="ns")
        self.metadata_text.configure(yscrollcommand=metadata_scroll.set, state="disabled")

        footer = self.ttk.Frame(self.root, style="Viewer.TFrame", padding=(8, 0, 8, 6))
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        footer.columnconfigure(1, weight=1)
        self.ttk.Label(
            footer,
            text=FOOTER_TEXT,
            style="Viewer.TLabel",
        ).grid(row=0, column=0, sticky="w")
        profile = self.tk.Label(
            footer,
            text="PmNz8 profile",
            foreground="#9ed7ff",
            background="#111820",
            cursor="hand2",
        )
        profile.grid(row=0, column=1, sticky="e")
        profile.bind("<Button-1>", self.open_profile)

    def _button(self, parent: Any, text: str, command: Callable[[], None], name: str) -> Any:
        button = self.ttk.Button(parent, text=text, command=command)
        self._register_busy(button, "normal")
        setattr(self, f"{name}_button", button)
        return button

    def _register_busy(self, widget: Any, restore_state: str) -> None:
        self.busy_widgets.append((widget, restore_state))

    def _set_busy(self, busy: bool) -> None:
        for widget, restore_state in self.busy_widgets:
            try:
                widget.configure(state="disabled" if busy else restore_state)
            except (AttributeError, TypeError):
                try:
                    widget.state(["disabled"] if busy else [f"!disabled"])
                except (AttributeError, TypeError):
                    pass

    def _set_status(self, text: str, error: bool = False) -> None:
        self.status_var.set(text)
        try:
            self.status_label.configure(foreground="#ff9d9d" if error else "#a8bbc7")
        except AttributeError:
            pass

    def _set_metadata(self, value: object) -> None:
        text = json.dumps(_metadata_without_views(value), ensure_ascii=False, indent=2, sort_keys=True)
        self.metadata_text.configure(state="normal")
        self.metadata_text.delete("1.0", "end")
        self.metadata_text.insert("1.0", text)
        self.metadata_text.configure(state="disabled")

    def _clear_preview(self) -> None:
        self.photo = None
        self.preview_item = None
        self.preview_canvas.delete("all")
        self.preview_canvas.configure(scrollregion=(0, 0, 0, 0))

    def _clear_texture_state(self) -> None:
        self.active_texture = None
        self.active_texture_var.set("")
        self.mip_var.set("")
        self.mip_combo.configure(values=())
        self.view_var.set("composite")
        self._clear_preview()

    def _render_result(self, result: dict[str, object]) -> bool:
        encoded = _png_base64_from_result(result)
        if encoded is None:
            return True
        try:
            self.photo = self.photo_factory(data=encoded, format="png")
            width_value = getattr(self.photo, "width", None)
            height_value = getattr(self.photo, "height", None)
            width = width_value() if callable(width_value) else width_value
            height = height_value() if callable(height_value) else height_value
            if (
                isinstance(width, bool)
                or not isinstance(width, int)
                or width <= 0
                or isinstance(height, bool)
                or not isinstance(height, int)
                or height <= 0
            ):
                raise ValueError("preview image reported invalid dimensions")
            self.preview_canvas.delete("all")
            self.preview_item = self.preview_canvas.create_image(0, 0, anchor="nw", image=self.photo)
            self.preview_canvas.configure(scrollregion=(0, 0, width, height))
            return True
        except Exception as error:
            self._clear_preview()
            self._set_status(f"preview failed: {error}", error=True)
            return False

    def _handle_failure(self, result: dict[str, object]) -> bool:
        if result.get("ok") is True:
            return True
        if self.active_texture is not None:
            self._restore_texture_controls()
        self._set_status(str(result.get("reason", "operation failed")), error=True)
        return False

    def _restore_texture_controls(self) -> None:
        if self.active_texture is None:
            return
        self._updating_controls = True
        try:
            selected = self.active_texture.get("selected_mip")
            if isinstance(selected, dict):
                self.mip_var.set(str(selected.get("index", 0)))
            self.view_var.set(str(self.active_texture.get("view", "composite")))
        finally:
            self._updating_controls = False

    def _submit(
        self,
        operation: Callable[[], dict[str, object]],
        callback: Callable[[dict[str, object]], None],
    ) -> None:
        if self.future is not None or self.closing:
            return
        self._set_busy(True)
        self.future = self.executor.submit(operation)
        self.root.after(30, lambda: self._poll_future(callback))

    def _poll_future(self, callback: Callable[[dict[str, object]], None]) -> None:
        self._show_scan_progress()
        future = self.future
        if future is None:
            return
        if not future.done():
            self.root.after(30, lambda: self._poll_future(callback))
            return
        self.future = None
        try:
            result = future.result()
        except Exception as error:
            result = {"ok": False, "reason": str(error) or type(error).__name__}
        self._set_busy(False)
        if self.closing:
            self._finish_close()
            return
        callback(result)

    def request_close(self) -> None:
        if self.closed:
            return
        self.closing = True
        filter_after_id = getattr(self, "_filter_after_id", None)
        if filter_after_id is not None:
            try:
                self.root.after_cancel(filter_after_id)
            except Exception:
                pass
            self._filter_after_id = None
        if self.future is None:
            self._finish_close()
        else:
            self._set_busy(True)
            self._set_status("Waiting for the current operation to finish…")

    def _finish_close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.executor.shutdown(wait=True)
        self.root.destroy()

    def choose_archive(self) -> None:
        try:
            path = self.filedialog.askopenfilename(
                title="Open DV2 archive",
                filetypes=(("DV2 archives", "*.dv2"), ("All files", "*.*")),
            )
        except Exception as error:
            self._set_status(str(error), error=True)
            return
        if not path:
            return
        self._scan_running = True
        self.progress_bar.configure(mode="indeterminate", value=0)
        self.progress_bar.pack(side="left", padx=10)
        self.progress_bar.start(15)
        self._set_status("Opening archive…")
        self._submit(
            lambda: self.controller.open_archive(str(path)),
            self._on_archive_open,
        )

    def request_archive_info(self) -> None:
        self._submit(self.controller.archive_info, self._on_archive_info)

    def request_close_archive(self) -> None:
        self._submit(self.controller.close_archive, self._on_archive_closed)

    def request_entries(self) -> None:
        try:
            limit = _parse_limit(self.filter_limit.get())
        except ValueError as error:
            self._set_status(str(error), error=True)
            return
        substring = self.filter_substring.get()
        glob_pattern = self.filter_glob.get() or None
        self._submit(
            lambda: self.controller.list_entries(
                substring, glob_pattern, limit, compatible_only=True
            ),
            self._on_entries,
        )

    def _on_filter_variable_changed(self, *_args: object) -> None:
        self._schedule_filter_refresh()

    def _schedule_filter_refresh(self, *, delay: int = 200) -> None:
        if self.closing or self.closed:
            return
        if self._filter_after_id is not None:
            try:
                self.root.after_cancel(self._filter_after_id)
            except Exception:
                pass
            self._filter_after_id = None
        self._filter_after_id = self.root.after(delay, self._run_scheduled_filter_refresh)

    def _run_scheduled_filter_refresh(self) -> None:
        self._filter_after_id = None
        if self.closing or self.closed or self.archive_document is None:
            return
        if self.future is not None or getattr(self, "_scan_running", False):
            self._schedule_filter_refresh()
            return
        self.request_entries()

    def reset_filters(self) -> None:
        if self.closing or self.closed:
            return
        self.filter_substring.set("")
        self.filter_glob.set("")
        self.filter_limit.set("")
        self._schedule_filter_refresh(delay=0)

    def _on_archive_open(self, result: dict[str, object]) -> None:
        if not self._handle_failure(result):
            self._finish_scan_progress()
            return
        self.scan_summary = None
        self.archive_document = result.get("archive") if isinstance(result.get("archive"), dict) else None
        self.entries = []
        self.selected_entry = None
        self.selected_entry_var.set("")
        self.entry_list.delete(0, "end")
        self._clear_texture_state()
        self._set_metadata(self.archive_document or {})
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate", maximum=1, value=0)
        self._set_status("Scanning textures…")
        self._submit(
            lambda: self.controller.scan_compatible_textures(progress=self._queue_scan_progress),
            self._on_scan_finished,
        )

    def _queue_scan_progress(self, progress: dict[str, object]) -> None:
        """Worker callback: bounded mailbox, never call Tk from here."""
        try:
            self._scan_progress.put_nowait(dict(progress))
        except Full:
            try:
                self._scan_progress.get_nowait()
            except Empty:
                pass
            self._scan_progress.put_nowait(dict(progress))

    def _show_scan_progress(self) -> None:
        mailbox = getattr(self, "_scan_progress", None)
        if mailbox is None:
            return
        try:
            progress = mailbox.get_nowait()
        except Empty:
            return
        total = int(progress["candidates"])
        processed = int(progress["processed"])
        self.progress_bar.configure(maximum=max(1, total), value=processed)
        if not self.closing:
            self._set_status(
                f"Scanning {processed}/{total} · compatible {progress['compatible']}"
            )

    def _finish_scan_progress(self) -> None:
        self._scan_running = False
        self.progress_bar.stop()
        self.progress_bar.pack_forget()

    def _on_scan_finished(self, result: dict[str, object]) -> None:
        self._finish_scan_progress()
        if not self._handle_failure(result):
            return
        self.scan_summary = dict(result["scan"])
        if self.archive_document is not None:
            self.archive_document["texture_scan"] = self.scan_summary
        self._set_metadata(self.archive_document or {})
        self.request_entries()

    def _on_archive_info(self, result: dict[str, object]) -> None:
        if self._handle_failure(result):
            self._set_metadata(result.get("archive", result))
            self._set_status("Ready")

    def _on_archive_closed(self, result: dict[str, object]) -> None:
        if not self._handle_failure(result):
            return
        self.archive_document = None
        self.scan_summary = None
        self.entries = []
        self.selected_entry = None
        self.selected_entry_var.set("")
        self.entry_list.delete(0, "end")
        self._clear_texture_state()
        self._set_metadata({})
        self._set_status("No archive open")

    def _on_entries(self, result: dict[str, object]) -> None:
        if not self._handle_failure(result):
            return
        entries = result.get("entries", [])
        self.entries = [entry for entry in entries if isinstance(entry, dict)]
        self.entry_list.delete(0, "end")
        entry_paths = [str(entry.get("path", "")) for entry in self.entries]
        for entry in self.entries:
            self.entry_list.insert(
                "end",
                f"{entry.get('path', '')}  [{entry.get('storage_mode', '')}, {entry.get('logical_size', 0)} B]",
            )
        if self.selected_entry in entry_paths:
            self.entry_list.selection_set(entry_paths.index(self.selected_entry))
        else:
            self.selected_entry = None
            self.selected_entry_var.set("")
        status = f"{result.get('count', len(self.entries))} textures"
        summary = getattr(self, "scan_summary", None)
        if summary is not None:
            status += f" · scan {summary['elapsed_seconds']:.2f}s · rejected {summary['rejected']}"
        self._set_status(status)

    def _on_entry_selected(self, _event: Any = None) -> None:
        selected = self.entry_list.curselection()
        if not selected:
            return
        index = int(selected[0])
        if 0 <= index < len(self.entries):
            self.selected_entry = str(self.entries[index].get("path", ""))
            self.selected_entry_var.set(self.selected_entry)

    def _on_entry_double_click(self, event: Any) -> str:
        if self.closing or self.closed or self.future is not None:
            return "break"
        try:
            index = int(self.entry_list.nearest(event.y))
            row_box = self.entry_list.bbox(index)
        except (AttributeError, TypeError, ValueError):
            return "break"
        if not row_box:
            return "break"
        row_y, row_height = int(row_box[1]), int(row_box[3])
        if event.y < row_y or event.y >= row_y + row_height:
            return "break"
        if not 0 <= index < len(self.entries):
            return "break"
        self.entry_list.selection_clear(0, "end")
        self.entry_list.selection_set(index)
        self._on_entry_selected()
        if self.selected_entry:
            self.request_open_texture()
        return "break"

    def request_open_texture(self) -> None:
        if not self.selected_entry:
            self._set_status("select an entry first", error=True)
            return
        logical_path = self.selected_entry
        self._submit(
            lambda: self.controller.open_texture(logical_path, 0),
            self._on_texture_open,
        )

    def request_close_texture(self) -> None:
        self._submit(self.controller.close_texture, self._on_texture_closed)

    def _on_texture_open(self, result: dict[str, object]) -> None:
        if not self._handle_failure(result):
            return
        self.active_texture = dict(result)
        self.active_texture_var.set(str(result.get("logical_path", "")))
        mips = result.get("mips", [])
        values = [str(item.get("index")) for item in mips if isinstance(item, dict)]
        self.mip_combo.configure(values=values)
        self.mip_var.set(str(result.get("selected_mip", {}).get("index", 0)))
        self.view_var.set(str(result.get("view", "composite")))
        self._set_metadata(self.active_texture)
        if self._render_result(result):
            self._set_status("Texture loaded")

    def _on_texture_closed(self, result: dict[str, object]) -> None:
        if not self._handle_failure(result):
            return
        self._clear_texture_state()
        self._set_metadata(self.archive_document or {})
        self._set_status("Texture closed")

    def _on_mip_selected(self, _event: Any = None) -> None:
        if self._updating_controls or self.active_texture is None or not self.mip_var.get():
            return
        mip_index = int(self.mip_var.get())
        self._submit(
            lambda: self.controller.select_mip(mip_index),
            self._on_texture_update,
        )

    def _on_view_selected(self, _event: Any = None) -> None:
        if self._updating_controls or self.active_texture is None:
            return
        view_name = self.view_var.get()
        self._submit(
            lambda: self.controller.set_view(view_name),
            self._on_view_update,
        )

    def _on_texture_update(self, result: dict[str, object]) -> None:
        if not self._handle_failure(result):
            return
        self.active_texture = dict(result)
        selected = result.get("selected_mip")
        if isinstance(selected, dict):
            self.mip_var.set(str(selected.get("index", 0)))
        self._set_metadata(self.active_texture)
        if self._render_result(result):
            self._set_status("Texture updated")

    def _on_view_update(self, result: dict[str, object]) -> None:
        if not self._handle_failure(result):
            return
        if self.active_texture is not None:
            self.active_texture.update(result)
        if self.active_texture is not None:
            self._set_metadata(self.active_texture)
        if self._render_result(result):
            self._set_status("View updated")

    def _export_stem(self) -> str:
        logical_path = "texture"
        if self.active_texture is not None:
            logical_path = str(self.active_texture.get("logical_path", logical_path))
        return Path(logical_path).stem or "texture"

    def choose_png_export(self) -> None:
        if self.active_texture is None:
            self._set_status("open a texture first", error=True)
            return
        mip = self.active_texture.get("selected_mip", {})
        mip_index = int(mip.get("index", 0)) if isinstance(mip, dict) else 0
        suggested = f"{self._export_stem()}-mip{mip_index:02d}.png"
        path = self.filedialog.asksaveasfilename(
            title="Export PNG", initialfile=suggested, defaultextension=".png", filetypes=(("PNG images", "*.png"),)
        )
        if path:
            view_name = self.view_var.get()
            self._submit(
                lambda: self.controller.export_png(
                    str(path), view_name, mip_index
                ),
                self._on_export_result,
            )

    def _choose_export_directory(self, suffix: str) -> str | None:
        if self.active_texture is None:
            self._set_status("open a texture first", error=True)
            return None
        parent = self.filedialog.askdirectory(parent=self.root, title="Choose export parent")
        if not parent:
            return None
        try:
            name = _validate_single_basename(f"{self._export_stem()}-{suffix}")
        except ValueError as error:
            self._set_status(str(error), error=True)
            return None
        return str(Path(parent) / name)

    def choose_set_export(self) -> None:
        destination = self._choose_export_directory("export")
        if destination:
            self._submit(lambda: self.controller.export_set(destination), self._on_export_result)

    def choose_package_export(self) -> None:
        destination = self._choose_export_directory("package")
        if destination:
            self._submit(
                lambda: self.controller.export_asset_package(destination),
                self._on_export_result,
            )

    def _on_export_result(self, result: dict[str, object]) -> None:
        if self._handle_failure(result):
            self._set_status("Export complete")

    def show_about(self, _event: Any = None) -> None:
        if self.messagebox is None:
            self._set_status(ABOUT_TEXT)
            return
        try:
            self.messagebox.showinfo("About Texture Viewer", ABOUT_TEXT, parent=self.root)
        except Exception as error:
            self._set_status(f"about dialog failed: {error}", error=True)

    def open_profile(self, _event: Any = None) -> None:
        try:
            if not webbrowser.open_new_tab(PROFILE_URL):
                raise RuntimeError("browser did not accept the profile URL")
        except Exception as error:
            self._set_status(f"profile link failed: {error}", error=True)

    def run(self) -> None:
        try:
            self.root.mainloop()
        finally:
            try:
                if self.future is not None:
                    self.future.result()
            finally:
                self.future = None
                if not self.closed:
                    self.closing = True
                    self._finish_close()


def run_gui(
    *,
    controller: TextureViewerController | None = None,
    root: Any | None = None,
    tk_module: Any | None = None,
    ttk_module: Any | None = None,
    filedialog_module: Any | None = None,
    messagebox_module: Any | None = None,
    photo_factory: Callable[..., Any] | None = None,
) -> int:
    """Launch the Tkinter frontend; imports Tk lazily for headless commands."""

    if tk_module is None or ttk_module is None or filedialog_module is None:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        tk_module = tk_module or tk
        ttk_module = ttk_module or ttk
        filedialog_module = filedialog_module or filedialog
        messagebox_module = messagebox_module or messagebox
    if root is None:
        root = tk_module.Tk()
    app = TextureViewerTkApp(
        root=root,
        tk_module=tk_module,
        ttk_module=ttk_module,
        filedialog_module=filedialog_module,
        messagebox_module=messagebox_module,
        controller=controller,
        photo_factory=photo_factory,
    )
    app.run()
    return 0


__all__ = [
    "TextureViewerTkApp",
    "_metadata_without_views",
    "_parse_limit",
    "_png_base64_from_result",
    "_validate_single_basename",
    "run_gui",
]
