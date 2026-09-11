# SPDX-FileCopyrightText: 2026 PmNz8
# SPDX-License-Identifier: AGPL-3.0-only
"""JSON-safe controller for the headless and Tkinter Texture Viewer.

The controller is intentionally a narrow bridge.  It catches all ordinary
user/input failures at this boundary and returns a small JSON-compatible
document; callers never receive bytes, ``Path`` objects, sessions, or
dataclasses.  All texture decoding and PNG encoding are delegated to the
accepted shared codec/roundtrip modules through :class:`TextureViewerModel`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
import base64
import hashlib
import json
from pathlib import Path
from typing import Any

from texture_viewer.codec import png as png_codec

from .model import (
    EntrySnapshot,
    TextureViewerError,
    TextureViewerModel,
    VALID_VIEW_NAMES,
)


class TextureViewerController:
    """Stateful JSON bridge over a :class:`TextureViewerModel`."""

    def __init__(
        self,
        *,
        model: TextureViewerModel | None = None,
        choose_archive: Callable[[], str | None] | None = None,
        save_png: Callable[[str], str | None] | None = None,
        choose_export_directory: Callable[[str], str | None] | None = None,
        choose_package_directory: Callable[[str], str | None] | None = None,
    ) -> None:
        self.model = model or TextureViewerModel()
        self._choose_archive = choose_archive
        self._save_png = save_png
        self._choose_export_directory = choose_export_directory
        self._choose_package_directory = choose_package_directory
        self._view_name = "composite"
        self._zoom = 1.0
        self._last_error: str | None = None
        self._exit_callback: Callable[[], None] | None = None

    # ------------------------------------------------------------------
    # Public JSON bridge methods
    # ------------------------------------------------------------------

    def open_archive(self, archive_path: str | None) -> dict[str, object]:
        if archive_path in (None, ""):
            return self._failure("archive selection cancelled")
        return self._call(lambda: self._open_archive(archive_path))

    def open_archive_dialog(self) -> dict[str, object]:
        if self._choose_archive is None:
            return self._failure("archive dialog is unavailable")

        def operation() -> dict[str, object]:
            selected_path = self._choose_archive()
            if selected_path in (None, ""):
                return {"ok": True, "cancelled": True}
            return self._open_archive(selected_path)

        return self._call(operation)

    def close_archive(self) -> dict[str, object]:
        def operation() -> dict[str, object]:
            self.model.close_archive()
            return {"ok": True, "archive": None, "texture": None}

        return self._call(operation)

    def archive_info(self) -> dict[str, object]:
        return self._call(lambda: self._archive_info(include_source_hash=True))

    def list_entries(
        self,
        substring: str | None = None,
        glob_pattern: str | None = None,
        limit: int | None = None,
        compatible_only: bool = False,
    ) -> dict[str, object]:
        return self._call(
            lambda: self._list_entries(
                substring=substring, glob_pattern=glob_pattern, limit=limit,
                compatible_only=compatible_only,
            )
        )

    def scan_compatible_textures(
        self, progress: Callable[[dict[str, object]], None] | None = None
    ) -> dict[str, object]:
        return self._call(lambda: {
            "ok": True, "scan": self.model.scan_compatible_textures(progress=progress)
        })

    def open_texture(
        self, logical_path: str | None, mip_index: int = 0
    ) -> dict[str, object]:
        if logical_path in (None, ""):
            return self._failure("texture selection is empty")
        return self._call(lambda: self._open_texture(logical_path, mip_index))

    def close_texture(self) -> dict[str, object]:
        return self._call(self._close_texture)

    def select_mip(self, mip_index: int) -> dict[str, object]:
        return self._call(lambda: self._select_mip(mip_index))

    def set_view(self, view_name: str) -> dict[str, object]:
        return self._call(lambda: self._set_view(view_name))

    def set_zoom(self, zoom: float) -> dict[str, object]:
        return self._call(lambda: self._set_zoom(zoom))

    def current_view(self) -> dict[str, object]:
        return self._call(self._current_view)

    def export_png(
        self,
        output_path: str | None = None,
        view_name: str | None = None,
        mip_index: int | None = None,
    ) -> dict[str, object]:
        return self._call(
            lambda: self._export_png(
                output_path=output_path, view_name=view_name, mip_index=mip_index
            )
        )

    def export_png_dialog(self) -> dict[str, object]:
        if self._save_png is None:
            return self._failure("PNG save dialog is unavailable")

        def operation() -> dict[str, object]:
            selection = self.model.require_selection()
            suggested = Path(selection.entry.path).stem + f"-mip{selection.mip_index:02d}.png"
            selected_path = self._save_png(suggested)
            if selected_path in (None, ""):
                return {"ok": True, "cancelled": True}
            return self._export_png(output_path=str(selected_path))

        return self._call(operation)

    def export_set(self, output_directory: str | None = None) -> dict[str, object]:
        return self._call(
            lambda: self._export_set(output_directory=output_directory)
        )

    def export_set_dialog(self) -> dict[str, object]:
        if self._choose_export_directory is None:
            return self._failure("export-set directory dialog is unavailable")

        def operation() -> dict[str, object]:
            selection = self.model.require_selection()
            suggested = Path(selection.entry.path).stem + "-export"
            selected_path = self._choose_export_directory(suggested)
            if selected_path in (None, ""):
                return {"ok": True, "cancelled": True}
            return self._export_set(output_directory=str(selected_path))

        return self._call(operation)

    def export_asset_package(
        self, output_directory: str | None = None, *, mip_mode="all", mip_profile="raw"
    ) -> dict[str, object]:
        return self._call(
            lambda: self._export_asset_package(output_directory=output_directory,
                mip_mode=mip_mode, mip_profile=mip_profile)
        )

    def export_asset_packages(self, output_directory, **options):
        return self._call(lambda: {"ok": True, "batch":
            self.model.export_asset_packages(output_directory, **options)})

    def export_asset_package_dialog(self) -> dict[str, object]:
        if self._choose_package_directory is None:
            return self._failure("asset-package directory dialog is unavailable")

        def operation() -> dict[str, object]:
            selection = self.model.require_selection()
            suggested = Path(selection.entry.path).stem + "-package"
            selected_path = self._choose_package_directory(suggested)
            if selected_path in (None, ""):
                return {"ok": True, "cancelled": True}
            return self._export_asset_package(output_directory=str(selected_path))

        return self._call(operation)

    def set_exit_callback(self, callback: Callable[[], None] | None) -> None:
        self._exit_callback = callback

    def exit(self) -> dict[str, object]:
        def operation() -> dict[str, object]:
            if self._exit_callback is not None:
                self._exit_callback()
            return {"ok": True}

        return self._call(operation)

    # ------------------------------------------------------------------
    # Internal operations
    # ------------------------------------------------------------------

    def _open_archive(self, archive_path: str) -> dict[str, object]:
        self.model.open_archive(archive_path)
        self._view_name = "composite"
        self._zoom = 1.0
        self._last_error = None
        # Keep opening/listing lightweight: hashing the complete archive would
        # read all payload regions even though the entry-table contract does
        # not require it.  The explicit Info action opts into the hash.
        return self._archive_info(include_source_hash=False)

    def _archive_info(self, *, include_source_hash: bool) -> dict[str, object]:
        session = self.model.require_session()
        header = session.header
        archive: dict[str, object] = {
            "path": str(self.model.archive_path),
            "file_size": int(session.file_size),
            "entry_count": len(session.entries),
            "header": {
                "version": int(header.version),
                "unknown_04": int(header.unknown_04),
                "unknown_08": int(header.unknown_08),
                "layout_mode": int(header.layout_mode),
                "compression_mode": int(header.compression_mode),
                "data_offset": int(header.data_offset),
                "path_table_size": int(header.path_table_size),
            },
            "unaligned_mode0_entries": list(session.unaligned_mode0_entries),
        }
        if include_source_hash:
            archive["source_sha256"] = session.source_sha256
        return {
            "ok": True,
            "archive": archive,
            "texture": None if self.model.selection is None else self._texture_document(),
        }

    def _list_entries(
        self,
        *,
        substring: str | None,
        glob_pattern: str | None,
        limit: int | None,
        compatible_only: bool = False,
    ) -> dict[str, object]:
        rows = self.model.list_entries(
            substring=substring, glob_pattern=glob_pattern, limit=limit,
            compatible_only=compatible_only,
        )
        return {
            "ok": True,
            "entries": [self._entry_document(row) for row in rows],
            "count": len(rows),
        }

    def _open_texture(self, logical_path: str, mip_index: int) -> dict[str, object]:
        self.model.open_texture(logical_path, mip_index)
        self._last_error = None
        return self._texture_document(include_views=True)

    def _close_texture(self) -> dict[str, object]:
        self.model.close_texture()
        return {"ok": True, "texture": None}

    def _select_mip(self, mip_index: int) -> dict[str, object]:
        self.model.select_mip(mip_index)
        return self._texture_document(include_views=True)

    def _set_view(self, view_name: str) -> dict[str, object]:
        if not isinstance(view_name, str) or view_name not in VALID_VIEW_NAMES:
            raise TextureViewerError(
                f"view must be one of {', '.join(VALID_VIEW_NAMES)}"
            )
        self._view_name = view_name
        return self._current_view()

    def _set_zoom(self, zoom: float) -> dict[str, object]:
        if isinstance(zoom, bool) or not isinstance(zoom, (int, float)):
            raise TextureViewerError("zoom must be a number")
        if not 0.1 <= float(zoom) <= 8.0:
            raise TextureViewerError("zoom must be between 0.1 and 8.0")
        self._zoom = float(zoom)
        return {"ok": True, "zoom": self._zoom}

    def _current_view(self) -> dict[str, object]:
        selection = self.model.require_selection()
        view = self.model.mip_views()
        encoded = base64.b64encode(
            self._png_bytes_for_view(view, self._view_name)
        ).decode("ascii")
        return {
            "ok": True,
            "view": self._view_name,
            "zoom": self._zoom,
            "mip_index": selection.mip_index,
            "width": view.width,
            "height": view.height,
            "png_base64": encoded,
        }

    def _export_png(
        self,
        *,
        output_path: str | None,
        view_name: str | None = None,
        mip_index: int | None = None,
    ) -> dict[str, object]:
        if output_path in (None, ""):
            raise TextureViewerError("PNG output path is required")
        if view_name is not None:
            self._set_view(view_name)
        if mip_index is not None:
            self.model.select_mip(mip_index)
        selection = self.model.require_selection()
        views = self.model.mip_views()
        data = self._png_bytes_for_view(views, self._view_name)
        destination = self.model.export_png(output_path, data)
        return {
            "ok": True,
            "path": str(destination),
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "view": self._view_name,
            "mip_index": selection.mip_index,
            "width": views.width,
            "height": views.height,
            "available_views": list(VALID_VIEW_NAMES),
        }

    def _export_set(self, *, output_directory: str | None) -> dict[str, object]:
        if output_directory in (None, ""):
            raise TextureViewerError("export-set directory is required")
        destination = self.model.export_set(output_directory)
        names = sorted(path.name for path in destination.iterdir() if path.is_file())
        return {
            "ok": True,
            "directory": str(destination),
            "files": names,
            "file_count": len(names),
        }

    def _export_asset_package(
        self, *, output_directory: str | None, mip_mode="all", mip_profile="raw"
    ) -> dict[str, object]:
        if output_directory in (None, ""):
            raise TextureViewerError("asset-package directory is required")
        destination = self.model.export_asset_package(output_directory,
            mip_mode=mip_mode, mip_profile=mip_profile)
        names = sorted(
            str(path.relative_to(destination)).replace("\\", "/")
            for path in destination.rglob("*")
            if path.is_file()
        )
        return {
            "ok": True,
            "directory": str(destination),
            "files": names,
            "file_count": len(names),
        }

    def _texture_document(self, *, include_views: bool = False) -> dict[str, object]:
        selection = self.model.require_selection()
        resource = selection.resource
        views = self.model.mip_views()
        document: dict[str, object] = {
            "ok": True,
            "logical_path": selection.entry.path,
            "stored_size": int(selection.entry.stored_size),
            "logical_size": int(selection.entry.logical_size),
            "storage_mode": selection.entry.storage_mode,
            "nif_hint": True,
            "payload_sha256": resource.payload_sha256,
            "payload_size": int(resource.payload_size),
            "nif_user_version": int(resource.user_version),
            "nif_version": int(resource.nif_version),
            "nif_block_types": list(resource.block_types),
            "pixel_format": int(resource.pixel_format),
            "pixel_format_name": resource.pixel_format_name,
            "mip_count": int(resource.mip_count),
            "mips": [
                {
                    "index": int(mip.index),
                    "width": int(mip.width),
                    "height": int(mip.height),
                    "relative_offset": int(mip.offset),
                    "compressed_size": int(mip.size),
                    "absolute_offset": int(mip.absolute_offset),
                }
                for mip in resource.mips
            ],
            "selected_mip": {
                "index": int(views.index),
                "width": int(views.width),
                "height": int(views.height),
            },
            "alpha_mode": views.alpha_mode,
            "alpha_required": bool(views.alpha_required),
            "statistics": [asdict(statistic) for statistic in views.statistics],
        }
        if include_views:
            document["views"] = self._view_documents(views)
            document["view"] = self._view_name
            document["zoom"] = self._zoom
        return document

    def _view_documents(self, views: Any) -> dict[str, object]:
        encoded = self._encoded_views(views)
        return {
            name: {
                "name": name,
                "png_base64": encoded[name],
                "mime_type": "image/png",
                "width": int(views.width),
                "height": int(views.height),
            }
            for name in VALID_VIEW_NAMES
        }

    @staticmethod
    def _encoded_views(views: Any) -> dict[str, str]:
        raw = {
            "rgb": png_codec.encode_png_rgb(views.width, views.height, views.rgb),
            "alpha": png_codec.encode_png_gray(views.width, views.height, views.alpha),
            "composite": png_codec.encode_png_rgba(views.width, views.height, views.rgba),
        }
        return {
            name: base64.b64encode(data).decode("ascii") for name, data in raw.items()
        }

    @staticmethod
    def _png_bytes_for_view(views: Any, view_name: str) -> bytes:
        if view_name == "rgb":
            return png_codec.encode_png_rgb(views.width, views.height, views.rgb)
        if view_name == "alpha":
            return png_codec.encode_png_gray(views.width, views.height, views.alpha)
        if view_name == "composite":
            return png_codec.encode_png_rgba(views.width, views.height, views.rgba)
        raise TextureViewerError(f"unknown view {view_name!r}")

    @staticmethod
    def _entry_document(row: EntrySnapshot) -> dict[str, object]:
        return {
            "path": row.path,
            "logical_size": int(row.logical_size),
            "stored_size": int(row.stored_size),
            "storage_mode": row.storage_mode,
            "nif_hint": bool(row.nif_hint),
        }

    def _call(self, operation: Callable[[], dict[str, object]]) -> dict[str, object]:
        try:
            result = operation()
            if not isinstance(result, dict):
                raise TextureViewerError("controller operation returned a non-object")
            # JSON round-trip is an inexpensive boundary assertion that keeps
            # accidental Path/dataclass/bytes leakage from reaching the frontend.
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
            checked = json.loads(encoded)
            if not isinstance(checked, dict):  # pragma: no cover - defensive
                raise TextureViewerError("controller result is not a JSON object")
            self._last_error = None if checked.get("ok") is True else str(checked.get("reason", "operation failed"))
            return checked
        except Exception as error:
            reason = str(error).strip() or type(error).__name__
            self._last_error = reason
            return {"ok": False, "reason": reason}

    def _failure(self, reason: str) -> dict[str, object]:
        self._last_error = reason
        return {"ok": False, "reason": reason}


# A short alias is useful to callers that do not need the longer class name.
ViewerController = TextureViewerController


__all__ = ["TextureViewerController", "ViewerController"]
