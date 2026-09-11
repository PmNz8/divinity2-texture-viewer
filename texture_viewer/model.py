# SPDX-FileCopyrightText: 2026 PmNz8
# SPDX-License-Identifier: AGPL-3.0-only
"""Headless, read-only model for the Divinity II texture viewer.

The model deliberately owns no archive-writing functionality.  It opens one
user-selected DV2 through :class:`DV2Session`, keeps the entry table in memory,
and reads bounded NIF payloads for an optional compatibility scan. Pixel decoding
is deferred until selection; the scan caches paths, not images or payloads.
All presentation-facing conversion is done by :mod:`texture_viewer.controller`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
import hashlib
import json
import re
from pathlib import Path
import shutil
import tempfile
import time
from typing import Final

from texture_viewer.package_export import (
    PackageExportError,
    build_texture_asset_package_files,
)
from texture_viewer import dv2lib
from texture_viewer.codec import bc
from texture_viewer.codec import png as png_codec
from texture_viewer.codec.nif_texture import (
    NIFTextureError,
    TextureResource,
    parse_texture_resource,
)
from texture_viewer.codec.roundtrip import (
    TextureMipViews,
    TextureRoundTripError,
    decode_mip_views,
    export_texture_set,
)


MAX_TEXTURE_PAYLOAD_SIZE: Final[int] = 512 * 1024 * 1024
VALID_VIEW_NAMES: Final[tuple[str, ...]] = ("rgb", "alpha", "composite")
_REPARSE_POINT = 0x0400


class TextureViewerError(RuntimeError):
    """A user-visible viewer operation failed safely."""


@dataclass(frozen=True, slots=True)
class EntrySnapshot:
    """A payload-free projection of one DV2 entry."""

    path: str
    logical_size: int
    stored_size: int
    storage_mode: str
    nif_hint: bool


@dataclass(frozen=True, slots=True)
class TextureSelection:
    """The currently selected, strictly parsed texture and mip."""

    entry: dv2lib.DV2Entry
    resource: TextureResource
    mip_index: int


def _as_path(value: str | os.PathLike[str] | Path, label: str) -> Path:
    try:
        candidate = Path(value).absolute()
    except (TypeError, ValueError, OSError) as error:
        raise TextureViewerError(f"{label} must be a filesystem path") from error
    if not str(candidate):
        raise TextureViewerError(f"{label} must not be empty")
    return candidate


def _is_reparse_point(path: Path) -> bool:
    """Return whether *path* is a symlink or a Windows reparse point.

    ``Path.is_symlink`` is enough on POSIX.  Windows junctions and other
    reparse points are also rejected because resolving them would make the
    source/destination boundary ambiguous.
    """

    try:
        if path.is_symlink():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attributes & _REPARSE_POINT)
    except (OSError, ValueError):
        # A broken link is still an unsafe path.  ``lexists`` is checked by
        # the caller, so this branch can safely reject inaccessible entries.
        return True


def _path_components(path: Path) -> tuple[Path, ...]:
    """Build existing path components without resolving links."""

    absolute = path.absolute()
    parts = absolute.parts
    if not parts:
        return (absolute,)
    current = Path(parts[0])
    result = [current]
    for part in parts[1:]:
        current = current / part
        result.append(current)
    return tuple(result)


def _reject_reparse_components(path: Path, *, include_leaf: bool, label: str) -> None:
    components = _path_components(path)
    if not include_leaf:
        components = components[:-1]
    for component in components:
        try:
            exists = os.path.lexists(component)
        except (OSError, ValueError) as error:
            raise TextureViewerError(f"cannot inspect {label}: {component}") from error
        if exists and _is_reparse_point(component):
            raise TextureViewerError(
                f"{label} contains a symlink or reparse point: {component}"
            )


def _existing_regular_file(value: str | os.PathLike[str] | Path, label: str) -> Path:
    candidate = _as_path(value, label)
    try:
        if not os.path.lexists(candidate):
            raise TextureViewerError(f"{label} does not exist: {candidate}")
    except (OSError, ValueError) as error:
        raise TextureViewerError(f"cannot inspect {label}: {candidate}") from error
    _reject_reparse_components(candidate, include_leaf=True, label=label)
    if not candidate.is_file():
        raise TextureViewerError(f"{label} must be a regular file: {candidate}")
    try:
        return candidate.resolve(strict=True)
    except OSError as error:
        raise TextureViewerError(f"cannot resolve {label}: {candidate}") from error


def _existing_directory(value: str | os.PathLike[str] | Path, label: str) -> Path:
    candidate = _as_path(value, label)
    try:
        if not os.path.lexists(candidate):
            raise TextureViewerError(f"{label} does not exist: {candidate}")
    except (OSError, ValueError) as error:
        raise TextureViewerError(f"cannot inspect {label}: {candidate}") from error
    _reject_reparse_components(candidate, include_leaf=True, label=label)
    if not candidate.is_dir():
        raise TextureViewerError(f"{label} must be a directory: {candidate}")
    try:
        return candidate.resolve(strict=True)
    except OSError as error:
        raise TextureViewerError(f"cannot resolve {label}: {candidate}") from error


def _new_destination(
    value: str | os.PathLike[str] | Path,
    *,
    label: str,
    extension: str | None,
    archive_path: Path,
    directory: bool = False,
) -> Path:
    """Validate an explicitly selected, not-yet-existing destination."""

    candidate = _as_path(value, label)
    if extension is not None and candidate.suffix.casefold() != extension.casefold():
        raise TextureViewerError(f"{label} must use the {extension} extension")
    try:
        if os.path.lexists(candidate):
            raise TextureViewerError(f"{label} already exists: {candidate}")
    except (OSError, ValueError) as error:
        raise TextureViewerError(f"cannot inspect {label}: {candidate}") from error

    parent = candidate.parent
    _reject_reparse_components(candidate, include_leaf=False, label=label)
    if not parent.is_dir():
        raise TextureViewerError(f"{label} parent must be an existing directory: {parent}")
    if _is_reparse_point(parent):
        raise TextureViewerError(f"{label} parent must not be a symlink or reparse point")
    try:
        resolved = candidate.resolve(strict=False)
        archive_resolved = archive_path.resolve(strict=True)
    except OSError as error:
        raise TextureViewerError(f"cannot resolve {label}") from error
    if resolved == archive_resolved:
        raise TextureViewerError(f"{label} must not be the open archive")

    packed_root = _find_packed_root(archive_resolved)
    if packed_root is not None:
        try:
            resolved.relative_to(packed_root)
        except ValueError:
            pass
        else:
            raise TextureViewerError(
                f"{label} must be outside the open archive's Packed tree: {packed_root}"
            )
    return resolved


def _find_packed_root(path: Path) -> Path | None:
    for ancestor in (path, *path.parents):
        if ancestor.name.casefold() == "packed":
            return ancestor
    return None


def _validate_limit(limit: int | None) -> int | None:
    if limit is None:
        return None
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise TextureViewerError("limit must be a positive integer")
    return limit


def _entry_snapshot(entry: dv2lib.DV2Entry) -> EntrySnapshot:
    return EntrySnapshot(
        path=entry.path,
        logical_size=entry.logical_size,
        stored_size=entry.stored_size,
        storage_mode=entry.storage_mode,
        nif_hint=entry.path.casefold().endswith(".nif"),
    )


def _validate_compatible_resource(resource: TextureResource) -> None:
    """Validate supported codec and display-size bounds without decoding pixels."""

    if resource.pixel_format not in (bc.FORMAT_BC1, bc.FORMAT_BC2, bc.FORMAT_BC3):
        raise ValueError(f"unsupported texture pixel format: {resource.pixel_format}")
    if resource.mip_count != len(resource.mips) or resource.mip_count <= 0:
        raise ValueError("texture resource has no complete mip table")
    for mip in resource.mips:
        # The strict parser already performs this same structural check; keep
        # it explicit here so the scan's compatibility predicate is bounded
        # independently of any future parser relaxation.
        if bc.mip_size(mip.width, mip.height, resource.pixel_format) != mip.size:
            raise ValueError(f"mip {mip.index} compressed size is inconsistent")
        if mip.width > bc.MAX_DIMENSION or mip.height > bc.MAX_DIMENSION:
            raise ValueError(f"mip {mip.index} exceeds the codec dimension limit")
        rgba_bytes = mip.width * mip.height * 4
        if rgba_bytes > bc.MAX_DECODED_BYTES:
            raise ValueError(f"mip {mip.index} exceeds the RGBA decode limit")
        for channels in (1, 3, 4):
            scanline_bytes = (mip.width * channels + 1) * mip.height
            if scanline_bytes > png_codec.MAX_DECODED_BYTES:
                raise ValueError(f"mip {mip.index} exceeds the PNG scanline limit")


class TextureViewerModel:
    """Stateful headless model with no GUI or archive writes."""

    def __init__(
        self,
        *,
        max_payload_size: int = MAX_TEXTURE_PAYLOAD_SIZE,
        session_factory: Callable[[Path], dv2lib.DV2Session] = dv2lib.DV2Session,
    ) -> None:
        if (
            isinstance(max_payload_size, bool)
            or not isinstance(max_payload_size, int)
            or max_payload_size <= 0
        ):
            raise ValueError("max_payload_size must be a positive integer")
        self.max_payload_size = max_payload_size
        self._session_factory = session_factory
        self._archive_path: Path | None = None
        self._session: dv2lib.DV2Session | None = None
        self._selection: TextureSelection | None = None
        self._compatible_paths: frozenset[str] | None = None
        self._compatible_scan_stats: dict[str, object] | None = None

    @property
    def archive_path(self) -> Path | None:
        return self._archive_path

    @property
    def session(self) -> dv2lib.DV2Session | None:
        return self._session

    @property
    def selection(self) -> TextureSelection | None:
        return self._selection

    def require_session(self) -> dv2lib.DV2Session:
        if self._session is None:
            raise TextureViewerError("no DV2 archive is open")
        return self._session

    def require_selection(self) -> TextureSelection:
        if self._selection is None:
            raise TextureViewerError("no texture is selected")
        return self._selection

    def open_archive(self, archive_path: str | os.PathLike[str] | Path) -> dv2lib.DV2Session:
        """Open a new archive only after complete path/parser validation."""

        candidate = _existing_regular_file(archive_path, "DV2 archive")
        if candidate.suffix.casefold() != ".dv2":
            raise TextureViewerError("DV2 archive must use the .dv2 extension")
        try:
            session = self._session_factory(candidate)
            # Parsing the table is already part of DV2Session construction;
            # access the table once so an injected factory cannot defer errors.
            session.list_entries()
        except dv2lib.DV2Error as error:
            raise TextureViewerError(f"cannot open DV2 archive: {error}") from error
        # Commit state only after the candidate succeeded, preserving a good
        # currently-open archive on every failed open.
        self._archive_path = candidate
        self._session = session
        self._selection = None
        self._compatible_paths = None
        self._compatible_scan_stats = None
        return session

    def close_archive(self) -> None:
        self._archive_path = None
        self._session = None
        self._selection = None
        self._compatible_paths = None
        self._compatible_scan_stats = None

    def list_entries(
        self,
        *,
        substring: str | None = None,
        glob_pattern: str | None = None,
        limit: int | None = None,
        compatible_only: bool = False,
    ) -> tuple[EntrySnapshot, ...]:
        """Filter only the entry table; no entry payload is read."""

        session = self.require_session()
        if not isinstance(compatible_only, bool):
            raise TextureViewerError("compatible_only must be a boolean")
        if compatible_only and self._compatible_paths is None:
            raise TextureViewerError("compatible texture scan has not completed")
        if substring is not None and not isinstance(substring, str):
            raise TextureViewerError("substring must be a string or null")
        if glob_pattern is not None and not isinstance(glob_pattern, str):
            raise TextureViewerError("glob_pattern must be a string or null")
        validated_limit = _validate_limit(limit)
        needle = None if substring is None else substring.casefold()
        pattern = None if glob_pattern is None else glob_pattern.casefold()
        rows: list[EntrySnapshot] = []
        for entry in session.list_entries():
            if compatible_only and entry.path not in self._compatible_paths:
                continue
            folded = entry.path.casefold()
            if needle is not None and needle not in folded:
                continue
            if pattern is not None and not _glob_match(folded, pattern):
                continue
            rows.append(_entry_snapshot(entry))
            if validated_limit is not None and len(rows) >= validated_limit:
                break
        return tuple(rows)

    def scan_compatible_textures(
        self,
        progress: Callable[[dict[str, object]], None] | None = None,
    ) -> dict[str, object]:
        """Scan bounded standalone NIF candidates and cache only their paths.

        Compatibility here means that the strict standalone parser accepts the
        wrapper and every mip fits the existing BC/RGBA/PNG size contracts. It
        is a structural codec predicate, not a runtime acceptance guarantee.
        """

        session = self.require_session()
        if progress is not None and not callable(progress):
            raise TextureViewerError("progress must be callable or null")
        if self._compatible_paths is not None and self._compatible_scan_stats is not None:
            cached = dict(self._compatible_scan_stats)
            if progress is not None:
                progress(dict(cached))
            return cached

        entries = tuple(
            entry for entry in session.list_entries() if entry.path.casefold().endswith(".nif")
        )
        started = time.perf_counter()
        stats: dict[str, object] = {
            "candidates": len(entries),
            "processed": 0,
            "compatible": 0,
            "rejected": 0,
            "elapsed_seconds": 0.0,
        }

        def emit(*, current_path: str | None = None) -> None:
            stats["elapsed_seconds"] = max(0.0, time.perf_counter() - started)
            payload = dict(stats)
            if current_path is not None:
                payload["current_path"] = current_path
            if progress is not None:
                progress(payload)

        compatible_paths: set[str] = set()
        emit()
        for entry in entries:
            try:
                if (
                    entry.logical_size > self.max_payload_size
                    or entry.stored_size > self.max_payload_size
                ):
                    raise ValueError(
                        f"payload exceeds limit {self.max_payload_size} bytes"
                    )
                payload = session.read_entry_bytes(
                    entry.path, maximum_size=self.max_payload_size
                )
                resource = parse_texture_resource(payload, entry.path)
                _validate_compatible_resource(resource)
            except (dv2lib.DV2Error, NIFTextureError, ValueError, OSError):
                stats["rejected"] = int(stats["rejected"]) + 1
            else:
                compatible_paths.add(entry.path)
                stats["compatible"] = int(stats["compatible"]) + 1
            finally:
                # Release the previous resource before reading the next payload.
                payload = None
                resource = None
            stats["processed"] = int(stats["processed"]) + 1
            emit(current_path=entry.path)

        emit()
        committed_stats = dict(stats)
        self._compatible_paths = frozenset(compatible_paths)
        self._compatible_scan_stats = committed_stats
        return dict(committed_stats)

    def open_texture(self, logical_path: str, mip_index: int = 0) -> TextureSelection:
        """Read, strictly parse and decode one texture before replacing state."""

        session = self.require_session()
        if not isinstance(logical_path, str) or not logical_path.strip():
            raise TextureViewerError("texture path must be a non-empty string")
        _validate_mip_index_value(mip_index)
        try:
            entry = session.find_entry(logical_path)
            if (
                entry.logical_size > self.max_payload_size
                or entry.stored_size > self.max_payload_size
            ):
                raise TextureViewerError(
                    f"refusing to load {entry.path!r}: payload exceeds limit "
                    f"{self.max_payload_size} bytes"
                )
            payload = session.read_entry_bytes(
                entry.path, maximum_size=self.max_payload_size
            )
        except dv2lib.DV2Error as error:
            raise TextureViewerError(f"cannot read texture entry: {error}") from error
        try:
            resource = parse_texture_resource(payload, entry.path)
            if mip_index >= resource.mip_count:
                raise TextureViewerError(
                    f"mip index {mip_index} out of range (0..{resource.mip_count - 1})"
                )
            # Decode the requested mip before committing the selection.  This
            # makes malformed/unsupported BC payloads fail without destroying
            # the previous valid texture.
            decode_mip_views(resource, mip_index)
        except TextureViewerError:
            raise
        except (NIFTextureError, TextureRoundTripError, ValueError) as error:
            raise TextureViewerError(
                f"entry is not a supported texture resource: {error}"
            ) from error
        selection = TextureSelection(entry=entry, resource=resource, mip_index=mip_index)
        self._selection = selection
        return selection

    def close_texture(self) -> None:
        self._selection = None

    def select_mip(self, mip_index: int) -> TextureSelection:
        selection = self.require_selection()
        _validate_mip_index_value(mip_index)
        if mip_index >= selection.resource.mip_count:
            raise TextureViewerError(
                f"mip index {mip_index} out of range (0..{selection.resource.mip_count - 1})"
            )
        decode_mip_views(selection.resource, mip_index)
        updated = TextureSelection(
            entry=selection.entry,
            resource=selection.resource,
            mip_index=mip_index,
        )
        self._selection = updated
        return updated

    def mip_views(self) -> TextureMipViews:
        selection = self.require_selection()
        return decode_mip_views(selection.resource, selection.mip_index)

    def export_png(
        self,
        output_path: str | os.PathLike[str] | Path,
        png_bytes: bytes,
    ) -> Path:
        """Publish one selected-view PNG without replacing an existing file."""

        archive = self._archive_path
        if archive is None:
            raise TextureViewerError("no DV2 archive is open")
        if not isinstance(png_bytes, bytes):
            raise TextureViewerError("PNG output must be immutable bytes")
        destination = _new_destination(
            output_path,
            label="PNG output",
            extension=".png",
            archive_path=archive,
        )
        descriptor: int | None = None
        created = False
        completed = False
        identity: tuple[int, int] | None = None
        try:
            # O_EXCL makes the no-overwrite promise race-safe for the file
            # export.  The selected view is already encoded before this call.
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            binary = getattr(os, "O_BINARY", 0)
            descriptor = os.open(destination, flags | binary, 0o600)
            created = True
            file_stat = os.fstat(descriptor)
            identity = (int(file_stat.st_dev), int(file_stat.st_ino))
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(png_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            completed = True
        except FileExistsError as error:
            raise TextureViewerError(f"PNG output already exists: {destination}") from error
        except OSError as error:
            raise TextureViewerError(f"cannot write PNG output: {destination}") from error
        except Exception as error:
            raise TextureViewerError(f"cannot write PNG output: {destination}") from error
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if created and not completed:
                try:
                    current = destination.stat()
                    current_identity = (int(current.st_dev), int(current.st_ino))
                    if identity is None or current_identity == identity:
                        destination.unlink()
                except OSError:
                    pass
        return destination

    def export_set(self, output_directory: str | os.PathLike[str] | Path) -> Path:
        """Publish a complete existing-mip export set through shared roundtrip."""

        archive = self._archive_path
        selection = self.require_selection()
        if archive is None:  # pragma: no cover - require_selection normally precedes this
            raise TextureViewerError("no DV2 archive is open")
        destination = _new_destination(
            output_directory,
            label="export-set directory",
            extension=None,
            archive_path=archive,
            directory=True,
        )
        try:
            return export_texture_set(selection.resource, destination)
        except (TextureRoundTripError, NIFTextureError, OSError, ValueError) as error:
            raise TextureViewerError(f"cannot export texture set: {error}") from error

    def export_asset_package(
        self,
        output_directory: str | os.PathLike[str] | Path,
        *,
        mip_mode: str = "all",
        mip_profile: str = "raw",
    ) -> Path:
        """Publish one complete Builder package without changing the archive."""

        archive = self._archive_path
        selection = self.require_selection()
        if archive is None:  # pragma: no cover - selection implies an open archive
            raise TextureViewerError("no DV2 archive is open")
        destination = _new_destination(
            output_directory,
            label="asset-package directory",
            extension=None,
            archive_path=archive,
            directory=True,
        )
        try:
            package = build_texture_asset_package_files(
                selection.resource,
                selection.entry.path,
                mip_mode=mip_mode,
                mip_profile=mip_profile,
            )
        except PackageExportError as error:
            raise TextureViewerError(f"cannot build asset package: {error}") from error

        staging: Path | None = None
        try:
            staging = Path(
                tempfile.mkdtemp(prefix=f".{destination.name}.", dir=str(destination.parent))
            )
            for relative_name, payload in package.files.items():
                relative = Path(*relative_name.split("/"))
                output = staging / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                with output.open("xb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            # On the supported Windows host os.rename refuses an existing
            # destination.  _new_destination already rejected any pre-existing
            # path and the staging directory is a same-parent private sibling.
            os.rename(staging, destination)
            staging = None
        except FileExistsError as error:
            raise TextureViewerError(
                f"asset-package directory already exists: {destination}"
            ) from error
        except OSError as error:
            raise TextureViewerError(
                f"cannot publish asset-package directory: {destination}"
            ) from error
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
        return destination

    def export_asset_packages(
        self, output_directory, *, mip_mode="all", mip_profile="raw",
        substring=None, glob_pattern=None, limit=None, progress=None, cancelled=None,
    ):
        """Export independently committed packages; cancellation retains successes."""
        self.require_session()
        if mip_mode not in ("all", "base") or mip_profile not in ("raw", "srgb"):
            raise TextureViewerError("unsupported mip mode/profile")
        rows = tuple(row for row in self.list_entries(
            substring=substring, glob_pattern=glob_pattern, limit=limit,
            compatible_only=self._compatible_paths is not None,
        ) if row.nif_hint)
        destination = _new_destination(
            output_directory, label="batch directory", extension=None,
            archive_path=self._archive_path, directory=True,
        )
        destination.mkdir()
        report = {"schema": "divinity2.texture_package_batch", "schema_version": 1,
                  "archive": str(self._archive_path), "mip_mode": mip_mode,
                  "mip_profile": mip_profile, "total": len(rows), "cancelled": False,
                  "items": []}
        original_selection = self._selection
        try:
            for index, row in enumerate(rows):
                if cancelled is not None and cancelled():
                    report["cancelled"] = True
                    break
                digest = hashlib.sha256(row.path.casefold().encode("utf-8")).hexdigest()[:20]
                stem = row.path.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
                stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem)[:64] or "texture"
                package_name = f"{index + 1:05d}-{stem}-{digest}"
                item = {"logical_path": row.path, "package": package_name}
                try:
                    selection = self.open_texture(row.path)
                    if mip_mode == "base" and selection.resource.pixel_format == bc.FORMAT_BC2:
                        item.update(status="skipped", reason="BC2 is excluded from MIP0 generation")
                    else:
                        self.export_asset_package(destination / package_name,
                            mip_mode=mip_mode, mip_profile=mip_profile)
                        item["status"] = "exported"
                except TextureViewerError as error:
                    item.update(status="error", reason=str(error))
                report["items"].append(item)
                if progress is not None:
                    progress({"processed": index + 1, "total": len(rows), "path": row.path})
        finally:
            self._selection = original_selection
            # This report lives outside individual strict Builder packages.
            with (destination / "batch-report.json").open("x", encoding="utf-8") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        return {**report, "output_directory": str(destination)}


def _validate_mip_index_value(mip_index: int) -> None:
    if isinstance(mip_index, bool) or not isinstance(mip_index, int) or mip_index < 0:
        raise TextureViewerError("mip index must be a non-negative integer")


def _glob_match(value: str, pattern: str) -> bool:
    # fnmatch is intentionally imported lazily: the model's hot list path is
    # otherwise only a casefolded substring operation.
    import fnmatch

    return fnmatch.fnmatchcase(value, pattern)


__all__ = [
    "EntrySnapshot",
    "MAX_TEXTURE_PAYLOAD_SIZE",
    "TextureSelection",
    "TextureViewerError",
    "TextureViewerModel",
    "VALID_VIEW_NAMES",
]
