# SPDX-FileCopyrightText: 2026 PmNz8
# SPDX-License-Identifier: AGPL-3.0-only
"""Strict, read-only texture mip views and PNG export/import sets.

The module deliberately sits above the standalone BC/NIF/PNG primitives.  It
does not know about DV2 archives or game paths: a caller supplies one already
parsed :class:`~texture_viewer.codec.nif_texture.TextureResource`, receives an
in-memory export set, and can optionally import edited PNGs back into the
fixed-size mip ranges of that same resource.

No mip generation, resampling, semantic colour interpretation, or BC2
encoding is performed here.  Every existing mip is handled independently and
the source wrapper is revalidated before it is used.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
from typing import Final

from . import bc
from .nif_texture import (
    NIFTextureError,
    TextureResource,
    serialize_texture_resource,
)
from .png import (
    MAX_PNG_BYTES,
    PNGError,
    PNGImage,
    decode_png,
    encode_png_gray,
    encode_png_rgb,
)


SCHEMA: Final = "divinity2.texture_export_set"
SCHEMA_VERSION: Final = 1
SIDECAR_NAME: Final = "texture.json"
MAX_SIDECAR_BYTES: Final = 1 << 20
MAX_EXPORT_FILE_BYTES: Final = MAX_PNG_BYTES

_POLICY: Final[dict[str, str]] = {
    "existing_mips": "preserve_individually",
    "mip_generation": "forbidden",
    "sample_encoding": "raw_8bit",
    "color_space": "uninterpreted",
    "semantic_kind": "unknown",
    "alpha_coverage": "not_regenerated",
    "bc2_edits": "unsupported",
}


class TextureRoundTripError(ValueError):
    """Raised when a texture export set violates its strict contract."""


@dataclass(frozen=True, slots=True)
class ChannelStatistics:
    """Deterministic statistics for one byte channel of one decoded mip."""

    minimum: int
    maximum: int
    total: int
    mean: float
    nonzero_count: int
    sample_count: int


@dataclass(frozen=True, slots=True)
class TextureMipViews:
    """Decoded views of one existing mip.

    ``statistics`` is a four-item tuple in RGBA channel order.  ``rgb`` and
    ``alpha`` are tightly packed, independently useful views of ``rgba``.
    """

    index: int
    width: int
    height: int
    rgba: bytes
    rgb: bytes
    alpha: bytes
    statistics: tuple[ChannelStatistics, ...]
    alpha_required: bool
    alpha_mode: str

    def __post_init__(self) -> None:
        # The decoder already returns immutable bytes.  Copying here also
        # keeps this public immutable value safe if it is manually built from
        # a bytearray or memoryview.
        try:
            rgba = bytes(self.rgba)
            rgb = bytes(self.rgb)
            alpha = bytes(self.alpha)
            statistics = tuple(self.statistics)
        except (TypeError, ValueError) as error:
            raise TextureRoundTripError("mip views contain invalid byte data") from error
        object.__setattr__(self, "rgba", rgba)
        object.__setattr__(self, "rgb", rgb)
        object.__setattr__(self, "alpha", alpha)
        object.__setattr__(self, "statistics", statistics)


def _copy_bytes(value: bytes | bytearray | memoryview, label: str, limit: int) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise TextureRoundTripError(f"{label} must be bytes-like")
    view: memoryview | None = None
    size: int | None = None
    try:
        view = memoryview(value)
        if not view.contiguous:
            raise TextureRoundTripError(
                f"{label} must be a contiguous bytes-like buffer"
            )
        size = view.nbytes
        if size > limit:
            raise TextureRoundTripError(f"{label} exceeds the supported size limit")
        result = bytes(view)
    except TextureRoundTripError:
        raise
    except (TypeError, ValueError) as error:
        raise TextureRoundTripError(
            f"{label} must be a contiguous bytes-like buffer"
        ) from error
    finally:
        if view is not None:
            view.release()
    if size is not None and len(result) != size:
        # ``bytes(view)`` is exact for a contiguous view.  Keep this check
        # explicit because this function is also the filesystem size guard.
        raise TextureRoundTripError(f"{label} could not be copied exactly")
    return result


def _validated_source(resource: TextureResource) -> bytes:
    if not isinstance(resource, TextureResource):
        raise TextureRoundTripError("resource must be a parsed TextureResource")
    try:
        # The serializer performs the accepted full metadata/source reparse
        # guard and returns the original bytes for a no-op.
        return serialize_texture_resource(resource)
    except NIFTextureError as error:
        raise TextureRoundTripError(f"{resource.label}: source validation failed: {error}") from error


def _validate_mip_index(resource: TextureResource, mip_index: int) -> int:
    if isinstance(mip_index, bool) or not isinstance(mip_index, int):
        raise TextureRoundTripError("mip index must be an integer")
    if not 0 <= mip_index < resource.mip_count:
        raise TextureRoundTripError(
            f"{resource.label}: mip index {mip_index} out of range "
            f"(0..{resource.mip_count - 1})"
        )
    return mip_index


def _bc1_has_in_bounds_transparent_index(
    compressed: bytes, width: int, height: int
) -> bool:
    """Return whether an in-bounds BC1 texel uses three-colour index 3."""

    blocks_x = (width + 3) // 4
    blocks_y = (height + 3) // 4
    expected = blocks_x * blocks_y * 8
    if len(compressed) != expected:
        raise TextureRoundTripError(
            f"BC1 mip has {len(compressed)} bytes; expected exactly {expected}"
        )
    for block_y in range(blocks_y):
        for block_x in range(blocks_x):
            block_offset = (block_y * blocks_x + block_x) * 8
            color0, color1 = struct.unpack_from("<HH", compressed, block_offset)
            if color0 > color1:
                continue
            indices = struct.unpack_from("<I", compressed, block_offset + 4)[0]
            for local_y in range(4):
                y = block_y * 4 + local_y
                if y >= height:
                    break
                for local_x in range(4):
                    x = block_x * 4 + local_x
                    if x >= width:
                        break
                    pixel_index = local_y * 4 + local_x
                    if ((indices >> (2 * pixel_index)) & 0x3) == 3:
                        return True
    return False


def _alpha_metadata(
    resource: TextureResource, mip_index: int, compressed: bytes
) -> tuple[bool, str]:
    mip = resource.mips[mip_index]
    if resource.pixel_format == bc.FORMAT_BC1:
        required = _bc1_has_in_bounds_transparent_index(compressed, mip.width, mip.height)
        return required, "bc1_binary" if required else "bc1_opaque"
    if resource.pixel_format == bc.FORMAT_BC2:
        return True, "bc2_explicit"
    if resource.pixel_format == bc.FORMAT_BC3:
        return True, "bc3_interpolated"
    # A parsed TextureResource cannot currently reach this branch, but keep
    # the roundtrip API fail-closed if that invariant changes later.
    raise TextureRoundTripError(
        f"{resource.label}: unsupported texture pixel format {resource.pixel_format}"
    )


def _channel_statistics(rgba: bytes, sample_count: int) -> tuple[ChannelStatistics, ...]:
    if sample_count <= 0 or len(rgba) != sample_count * 4:
        raise TextureRoundTripError("decoded RGBA mip has an inconsistent size")
    result: list[ChannelStatistics] = []
    for channel in range(4):
        values = rgba[channel::4]
        total = sum(values)
        result.append(
            ChannelStatistics(
                minimum=min(values),
                maximum=max(values),
                total=total,
                mean=total / sample_count,
                nonzero_count=sum(1 for value in values if value != 0),
                sample_count=sample_count,
            )
        )
    return tuple(result)


def _decode_mip_views_unchecked(
    resource: TextureResource, mip_index: int
) -> TextureMipViews:
    mip = resource.mips[mip_index]
    compressed = resource.mip_bytes(mip_index)
    try:
        rgba = resource.decode_mip(mip_index)
    except NIFTextureError as error:
        raise TextureRoundTripError(
            f"{resource.label}: cannot decode mip {mip_index}: {error}"
        ) from error
    expected_rgba_size = mip.width * mip.height * 4
    if len(rgba) != expected_rgba_size:
        raise TextureRoundTripError(
            f"{resource.label}: decoded mip {mip_index} has {len(rgba)} bytes; "
            f"expected exactly {expected_rgba_size}"
        )
    rgb = bytearray(mip.width * mip.height * 3)
    alpha = bytearray(mip.width * mip.height)
    for pixel in range(mip.width * mip.height):
        source = pixel * 4
        rgb[pixel * 3 : pixel * 3 + 3] = rgba[source : source + 3]
        alpha[pixel] = rgba[source + 3]
    alpha_required, alpha_mode = _alpha_metadata(resource, mip_index, compressed)
    return TextureMipViews(
        index=mip_index,
        width=mip.width,
        height=mip.height,
        rgba=rgba,
        rgb=bytes(rgb),
        alpha=bytes(alpha),
        statistics=_channel_statistics(rgba, mip.width * mip.height),
        alpha_required=alpha_required,
        alpha_mode=alpha_mode,
    )


def decode_mip_views(resource: TextureResource, mip_index: int = 0) -> TextureMipViews:
    """Decode one existing mip and expose RGBA/RGB/alpha views and statistics."""

    _validated_source(resource)
    index = _validate_mip_index(resource, mip_index)
    return _decode_mip_views_unchecked(resource, index)


def _canonical_json(value: object) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise TextureRoundTripError("cannot serialize canonical texture manifest") from error
    return (text + "\n").encode("utf-8")


def _manifest_for_resource(
    resource: TextureResource,
    source: bytes,
    views: tuple[TextureMipViews, ...],
) -> dict[str, object]:
    if len(views) != resource.mip_count:
        raise TextureRoundTripError(f"{resource.label}: not all existing mips were decoded")
    mips: list[dict[str, object]] = []
    for index, view in enumerate(views):
        mip = resource.mips[index]
        compressed = resource.mip_bytes(index)
        mips.append(
            {
                "index": index,
                "width": mip.width,
                "height": mip.height,
                "relative_offset": mip.offset,
                "absolute_offset": mip.absolute_offset,
                "compressed_size": mip.size,
                "compressed_sha256": hashlib.sha256(compressed).hexdigest(),
                "decoded_rgba_sha256": hashlib.sha256(view.rgba).hexdigest(),
                "alpha_mode": view.alpha_mode,
                "alpha_required": view.alpha_required,
                "rgb_file": f"mip-{index:02d}.rgb.png",
                "alpha_file": (
                    f"mip-{index:02d}.alpha.png" if view.alpha_required else None
                ),
            }
        )
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "source": {
            "payload_sha256": hashlib.sha256(source).hexdigest(),
            "payload_size": len(source),
            "user_version": resource.user_version,
            "pixel_format": resource.pixel_format,
            "pixel_format_name": resource.pixel_format_name,
            "mip_count": resource.mip_count,
        },
        "policy": dict(_POLICY),
        "mips": mips,
    }


def _prepare_resource(
    resource: TextureResource,
) -> tuple[bytes, tuple[TextureMipViews, ...], dict[str, object]]:
    source = _validated_source(resource)
    views = tuple(
        _decode_mip_views_unchecked(resource, index)
        for index in range(resource.mip_count)
    )
    return source, views, _manifest_for_resource(resource, source, views)


def build_export_set(resource: TextureResource) -> dict[str, bytes]:
    """Return the complete deterministic PNG/JSON export set in memory."""

    source, views, manifest = _prepare_resource(resource)
    del source
    files: dict[str, bytes] = {}
    for view in views:
        files[f"mip-{view.index:02d}.rgb.png"] = encode_png_rgb(
            view.width, view.height, view.rgb
        )
        if view.alpha_required:
            files[f"mip-{view.index:02d}.alpha.png"] = encode_png_gray(
                view.width, view.height, view.alpha
            )
    files[SIDECAR_NAME] = _canonical_json(manifest)
    return {name: files[name] for name in sorted(files)}


def _validate_file_name(name: object) -> str:
    if not isinstance(name, str):
        raise TextureRoundTripError("export-set file names must be strings")
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or Path(name).is_absolute()
    ):
        raise TextureRoundTripError(f"export-set file name is not a basename: {name!r}")
    return name


def _normalize_files(files: Mapping[str, bytes | bytearray | memoryview]) -> dict[str, bytes]:
    if not isinstance(files, Mapping):
        raise TextureRoundTripError("export-set files must be a mapping")
    normalized: dict[str, bytes] = {}
    try:
        items = files.items()
        for name, value in items:
            safe_name = _validate_file_name(name)
            if safe_name in normalized:
                raise TextureRoundTripError(f"duplicate export-set file {safe_name!r}")
            normalized[safe_name] = _copy_bytes(
                value, f"export-set file {safe_name!r}", MAX_EXPORT_FILE_BYTES
            )
    except TextureRoundTripError:
        raise
    except (TypeError, ValueError, RuntimeError) as error:
        raise TextureRoundTripError("cannot iterate export-set files mapping") from error
    return normalized


def _parse_sidecar(raw: bytes, expected: bytes, label: str) -> None:
    if len(raw) > MAX_SIDECAR_BYTES:
        raise TextureRoundTripError(f"{label}: sidecar exceeds the supported size limit")

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise ValueError(f"non-finite JSON constant {value}")

    try:
        text = raw.decode("utf-8", "strict")
        parsed = json.loads(
            text,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as error:
        raise TextureRoundTripError(f"{label}: invalid texture.json") from error
    try:
        expected_parsed = json.loads(expected.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:  # pragma: no cover - internal
        raise TextureRoundTripError("internal canonical texture manifest is invalid") from error

    def exact_json_equal(left: object, right: object) -> bool:
        # Python considers True == 1 and 1.0 == 1; JSON manifests must not
        # permit those aliases to bypass source/layout validation.
        if type(left) is not type(right):
            return False
        if isinstance(left, dict):
            if set(left) != set(right):  # type: ignore[arg-type]
                return False
            return all(
                exact_json_equal(left[key], right[key])  # type: ignore[index]
                for key in left
            )
        if isinstance(left, list):
            return len(left) == len(right) and all(  # type: ignore[arg-type]
                exact_json_equal(item_left, item_right)
                for item_left, item_right in zip(left, right, strict=True)  # type: ignore[arg-type]
            )
        return left == right

    # Require the canonical bytes as well as exact typed semantic equality.
    # This catches numeric type aliases (e.g. true/1), alternate whitespace,
    # and any schema field that might otherwise compare equal in Python.
    if raw != expected or not exact_json_equal(parsed, expected_parsed):
        raise TextureRoundTripError(
            f"{label}: texture.json does not match the exact source manifest"
        )


def _decode_png_view(
    raw: bytes,
    *,
    width: int,
    height: int,
    color_type: int,
    label: str,
) -> bytes:
    try:
        image: PNGImage = decode_png(raw, label=label)
    except PNGError as error:
        raise TextureRoundTripError(f"{label}: invalid PNG: {error}") from error
    if image.width != width or image.height != height:
        raise TextureRoundTripError(
            f"{label}: dimensions are {image.width}x{image.height}; "
            f"expected exactly {width}x{height}"
        )
    if image.color_type != color_type:
        raise TextureRoundTripError(
            f"{label}: color type {image.color_type} is unsupported here; "
            f"expected exactly {color_type}"
        )
    expected_channels = 3 if color_type == 2 else 1
    if image.channels != expected_channels:
        raise TextureRoundTripError(
            f"{label}: channel count {image.channels} is inconsistent with color type"
        )
    return image.pixels


def _interleave_rgb_alpha(rgb: bytes, alpha: bytes, pixel_count: int) -> bytes:
    if len(rgb) != pixel_count * 3 or len(alpha) != pixel_count:
        raise TextureRoundTripError("edited PNG views have inconsistent sizes")
    rgba = bytearray(pixel_count * 4)
    for pixel in range(pixel_count):
        source_rgb = pixel * 3
        destination = pixel * 4
        rgba[destination : destination + 3] = rgb[source_rgb : source_rgb + 3]
        rgba[destination + 3] = alpha[pixel]
    return bytes(rgba)


def import_export_set(
    resource: TextureResource,
    files: Mapping[str, bytes | bytearray | memoryview],
) -> bytes:
    """Validate a complete export set and serialize only changed mip ranges."""

    source, views, manifest = _prepare_resource(resource)
    normalized = _normalize_files(files)
    expected_files = {
        SIDECAR_NAME,
        *(f"mip-{view.index:02d}.rgb.png" for view in views),
        *(f"mip-{view.index:02d}.alpha.png" for view in views if view.alpha_required),
    }
    actual_files = set(normalized)
    if actual_files != expected_files:
        missing = sorted(expected_files - actual_files)
        extra = sorted(actual_files - expected_files)
        details: list[str] = []
        if missing:
            details.append(f"missing={missing!r}")
        if extra:
            details.append(f"extra={extra!r}")
        raise TextureRoundTripError(
            f"{resource.label}: export-set file set mismatch ({', '.join(details)})"
        )
    _parse_sidecar(normalized[SIDECAR_NAME], _canonical_json(manifest), resource.label)

    replacements: dict[int, bytes] = {}
    for view in views:
        mip = resource.mips[view.index]
        rgb_name = f"mip-{view.index:02d}.rgb.png"
        edited_rgb = _decode_png_view(
            normalized[rgb_name],
            width=mip.width,
            height=mip.height,
            color_type=2,
            label=rgb_name,
        )
        edited_alpha = view.alpha
        if view.alpha_required:
            alpha_name = f"mip-{view.index:02d}.alpha.png"
            edited_alpha = _decode_png_view(
                normalized[alpha_name],
                width=mip.width,
                height=mip.height,
                color_type=0,
                label=alpha_name,
            )

        if edited_rgb == view.rgb and edited_alpha == view.alpha:
            # Keeping the original compressed bytes is important for BC2 and
            # also preserves exact source bytes when a PNG was rewritten but
            # decoded to the same samples.
            continue
        if resource.pixel_format == bc.FORMAT_BC2:
            raise TextureRoundTripError(
                f"{resource.label}: BC2 edits are unsupported"
            )
        edited_rgba = _interleave_rgb_alpha(
            edited_rgb, edited_alpha, mip.width * mip.height
        )
        try:
            if resource.pixel_format == bc.FORMAT_BC1:
                if view.alpha_required:
                    replacement = bc.encode_bc1_binary_alpha(
                        edited_rgba, mip.width, mip.height, threshold=128
                    )
                else:
                    replacement = bc.encode_bc1_opaque(
                        edited_rgba, mip.width, mip.height
                    )
            elif resource.pixel_format == bc.FORMAT_BC3:
                replacement = bc.encode_bc3(edited_rgba, mip.width, mip.height)
            else:
                raise TextureRoundTripError(
                    f"{resource.label}: unsupported texture pixel format "
                    f"{resource.pixel_format}"
                )
        except TextureRoundTripError:
            raise
        except bc.BCCodecError as error:
            raise TextureRoundTripError(
                f"{resource.label}: cannot encode edited mip {view.index}: {error}"
            ) from error
        if len(replacement) != mip.size:
            raise TextureRoundTripError(
                f"{resource.label}: edited mip {view.index} encoder returned "
                f"{len(replacement)} bytes; expected exactly {mip.size}"
            )
        replacements[view.index] = replacement

    try:
        return serialize_texture_resource(
            resource,
            replacements,
            expected_source_sha256=hashlib.sha256(source).hexdigest(),
        )
    except NIFTextureError as error:
        raise TextureRoundTripError(
            f"{resource.label}: serialized export set failed validation: {error}"
        ) from error


def _as_path(value: str | os.PathLike[str], label: str) -> Path:
    try:
        return Path(value)
    except (TypeError, ValueError) as error:
        raise TextureRoundTripError(f"{label} must be a filesystem path") from error


def _path_exists(path: Path, label: str) -> bool:
    try:
        return os.path.lexists(path)
    except (OSError, ValueError) as error:
        raise TextureRoundTripError(f"{label} is not a valid filesystem path") from error


def _write_synced(path: Path, data: bytes) -> None:
    try:
        with path.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as error:
        raise TextureRoundTripError(f"cannot write {path.name!r}") from error


def _read_bounded(path: Path, limit: int) -> bytes:
    """Read at most ``limit`` bytes without trusting a racy size preflight."""

    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError as error:
        raise TextureRoundTripError(f"cannot read input file: {path.name}") from error
    if len(data) > limit:
        raise TextureRoundTripError(
            f"input file exceeds the supported size limit: {path.name}"
        )
    return data


def export_texture_set(
    resource: TextureResource, output_directory: str | os.PathLike[str]
) -> Path:
    """Atomically publish a new export-set directory and refuse overwrites."""

    destination = _as_path(output_directory, "output_directory")
    if _path_exists(destination, "output_directory"):
        raise TextureRoundTripError(
            f"output directory already exists: {destination}"
        )
    parent = destination.parent
    if not parent.is_dir():
        raise TextureRoundTripError(f"output parent is not a directory: {parent}")

    files = build_export_set(resource)
    staging: Path | None = None
    try:
        staging = Path(
            tempfile.mkdtemp(prefix=f".{destination.name}.", dir=str(parent))
        )
        for name in sorted(files):
            _write_synced(staging / name, files[name])
        # The destination was checked before the build and the staging path is
        # a sibling, so the rename publishes the complete set in one operation.
        os.replace(staging, destination)
        staging = None
    except TextureRoundTripError:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        raise
    except OSError as error:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        raise TextureRoundTripError(
            f"cannot atomically publish texture export set: {destination}"
        ) from error
    except Exception as error:
        # Keep the staging directory private even if an unexpected I/O or
        # platform-specific exception escapes one of the bounded operations.
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        raise TextureRoundTripError(
            f"cannot atomically publish texture export set: {destination}"
        ) from error
    return destination


def import_texture_set(
    resource: TextureResource, input_directory: str | os.PathLike[str]
) -> bytes:
    """Read one exact export-set directory without writing to the filesystem."""

    directory = _as_path(input_directory, "input_directory")
    if _path_exists(directory, "input_directory") and directory.is_symlink():
        raise TextureRoundTripError("input directory must not be a symlink")
    if not directory.is_dir():
        raise TextureRoundTripError(f"input directory is not a directory: {directory}")

    # Preparing the resource determines the exact expected file names before
    # any input file is read.  import_export_set repeats the source guard and
    # all semantic validation before it returns.
    _, views, _ = _prepare_resource(resource)
    expected_files = {
        SIDECAR_NAME,
        *(f"mip-{view.index:02d}.rgb.png" for view in views),
        *(f"mip-{view.index:02d}.alpha.png" for view in views if view.alpha_required),
    }
    files: dict[str, bytes] = {}
    try:
        entries = list(directory.iterdir())
    except OSError as error:
        raise TextureRoundTripError(f"cannot enumerate input directory: {directory}") from error
    for entry in entries:
        if entry.is_symlink():
            raise TextureRoundTripError(f"input entry must not be a symlink: {entry.name}")
        if not entry.is_file():
            raise TextureRoundTripError(
                f"input directory contains a non-file entry: {entry.name}"
            )
        if entry.name not in expected_files:
            raise TextureRoundTripError(f"unexpected input file: {entry.name}")
        try:
            limit = (
                MAX_SIDECAR_BYTES
                if entry.name == SIDECAR_NAME
                else MAX_EXPORT_FILE_BYTES
            )
            size = entry.stat().st_size
            if size > limit:
                raise TextureRoundTripError(
                    f"input file exceeds the supported size limit: {entry.name}"
                )
            files[entry.name] = _copy_bytes(
                _read_bounded(entry, limit), f"input file {entry.name!r}", limit
            )
        except TextureRoundTripError:
            raise
        except OSError as error:
            raise TextureRoundTripError(f"cannot read input file: {entry.name}") from error
    if set(files) != expected_files:
        missing = sorted(expected_files - set(files))
        raise TextureRoundTripError(f"input directory is missing files: {missing!r}")
    return import_export_set(resource, files)


__all__ = [
    "ChannelStatistics",
    "SIDECAR_NAME",
    "SCHEMA",
    "SCHEMA_VERSION",
    "TextureMipViews",
    "TextureRoundTripError",
    "build_export_set",
    "decode_mip_views",
    "export_texture_set",
    "import_export_set",
    "import_texture_set",
]
