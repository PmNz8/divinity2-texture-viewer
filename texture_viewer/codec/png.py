# SPDX-FileCopyrightText: 2026 PmNz8
# SPDX-License-Identifier: AGPL-3.0-only
"""Strict, dependency-free PNG reader and deterministic writer.

The supported image contract is deliberately small: 8-bit, non-interlaced
greyscale, RGB, greyscale+alpha, and RGBA images.  The writer emits one
unfiltered IDAT chunk; the reader accepts PNG filter methods 0 through 4 and
rejects malformed structure, CRCs, and bounded decompression failures.
"""

from __future__ import annotations

from dataclasses import dataclass
import struct
import zlib
from typing import Final


PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
MAX_DIMENSION: Final = 16_384
MAX_DECODED_BYTES: Final = 256 * 1024 * 1024
MAX_PNG_BYTES: Final = 256 * 1024 * 1024
MAX_CHUNK_BYTES: Final = MAX_PNG_BYTES

_CHANNELS_BY_COLOR_TYPE: Final = {0: 1, 2: 3, 4: 2, 6: 4}
_IHDR: Final = b"IHDR"
_IDAT: Final = b"IDAT"
_IEND: Final = b"IEND"
_PLTE: Final = b"PLTE"


class PNGError(ValueError):
    """Raised when PNG input or image dimensions violate the PNG contract."""


def _validate_dimensions(width: int, height: int, channels: int) -> tuple[int, int, int, int]:
    if isinstance(width, bool) or not isinstance(width, int):
        raise PNGError("width must be an integer")
    if isinstance(height, bool) or not isinstance(height, int):
        raise PNGError("height must be an integer")
    if isinstance(channels, bool) or not isinstance(channels, int):
        raise PNGError("channels must be an integer")
    if width <= 0 or height <= 0:
        raise PNGError("width and height must be positive")
    if width > MAX_DIMENSION or height > MAX_DIMENSION:
        raise PNGError(
            f"dimensions {width}x{height} exceed the supported limit "
            f"{MAX_DIMENSION}x{MAX_DIMENSION}"
        )
    if channels not in (1, 2, 3, 4):
        raise PNGError(f"unsupported channel count {channels}")
    pixel_bytes = width * height * channels
    row_bytes = width * channels
    scanline_bytes = (row_bytes + 1) * height
    if pixel_bytes > MAX_DECODED_BYTES or scanline_bytes > MAX_DECODED_BYTES:
        raise PNGError("image exceeds the supported decoded size limit")
    return width, height, channels, scanline_bytes


def _copy_bytes(data: bytes | bytearray | memoryview, label: str, limit: int) -> bytes:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise PNGError(f"{label} must be bytes-like")
    try:
        view = memoryview(data)
        if not view.contiguous:
            raise PNGError(f"{label} must be a contiguous bytes-like buffer")
        size = view.nbytes
        if size > limit:
            raise PNGError(f"{label} exceeds the supported size limit")
        result = bytes(view)
    except PNGError:
        raise
    except (TypeError, ValueError) as error:
        raise PNGError(f"{label} must be a contiguous bytes-like buffer") from error
    finally:
        try:
            view.release()
        except (NameError, AttributeError):
            pass
    if len(result) != size:
        raise PNGError(f"{label} could not be copied exactly")
    return result


@dataclass(frozen=True, slots=True)
class PNGImage:
    """Immutable decoded 8-bit PNG pixels in row-major interleaved order."""

    width: int
    height: int
    color_type: int
    channels: int
    pixels: bytes

    def __post_init__(self) -> None:
        if isinstance(self.color_type, bool) or not isinstance(self.color_type, int):
            raise PNGError("color_type must be an integer")
        expected_channels = _CHANNELS_BY_COLOR_TYPE.get(self.color_type)
        if expected_channels is None:
            raise PNGError(f"unsupported PNG color type {self.color_type}")
        if isinstance(self.channels, bool) or not isinstance(self.channels, int):
            raise PNGError("channels must be an integer")
        if self.channels != expected_channels:
            raise PNGError(
                f"color type {self.color_type} requires {expected_channels} channels"
            )
        width, height, channels, _ = _validate_dimensions(
            self.width, self.height, self.channels
        )
        expected_size = width * height * channels
        pixels = _copy_bytes(self.pixels, "pixels", expected_size)
        if len(pixels) != expected_size:
            raise PNGError(
                f"pixels has {len(pixels)} bytes; expected exactly {expected_size} "
                f"for {width}x{height}"
            )
        object.__setattr__(self, "pixels", pixels)


def _validate_pixels(
    width: int,
    height: int,
    color_type: int,
    pixels: bytes | bytearray | memoryview,
) -> tuple[bytes, int, int, int]:
    if isinstance(color_type, bool) or not isinstance(color_type, int):
        raise PNGError("color_type must be an integer")
    channels = _CHANNELS_BY_COLOR_TYPE.get(color_type)
    if channels is None:
        raise PNGError(f"unsupported PNG color type {color_type}")
    width, height, channels, scanline_bytes = _validate_dimensions(width, height, channels)
    expected_size = width * height * channels
    raw_pixels = _copy_bytes(pixels, "pixels", expected_size)
    if len(raw_pixels) != expected_size:
        raise PNGError(
            f"pixels has {len(raw_pixels)} bytes; expected exactly {expected_size} "
            f"for {width}x{height}"
        )
    return raw_pixels, width, height, scanline_bytes


def _chunk(kind: bytes, payload: bytes) -> bytes:
    if len(kind) != 4:
        raise PNGError("internal PNG chunk type is invalid")
    if len(payload) > MAX_CHUNK_BYTES:
        raise PNGError("PNG chunk exceeds the supported size limit")
    checksum = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)


def _encode_png(
    width: int,
    height: int,
    color_type: int,
    pixels: bytes | bytearray | memoryview,
) -> bytes:
    raw_pixels, width, height, scanline_bytes = _validate_pixels(
        width, height, color_type, pixels
    )
    channels = _CHANNELS_BY_COLOR_TYPE[color_type]
    row_bytes = width * channels
    scanlines = bytearray(scanline_bytes)
    for row in range(height):
        source_start = row * row_bytes
        destination_start = row * (row_bytes + 1)
        scanlines[destination_start] = 0
        scanlines[destination_start + 1 : destination_start + 1 + row_bytes] = raw_pixels[
            source_start : source_start + row_bytes
        ]
    try:
        compressed = zlib.compress(bytes(scanlines), level=9)
    except zlib.error as error:
        raise PNGError(f"cannot compress PNG scanlines: {error}") from error
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return PNG_SIGNATURE + _chunk(_IHDR, ihdr) + _chunk(_IDAT, compressed) + _chunk(_IEND, b"")


def encode_png_gray(
    width: int, height: int, pixels: bytes | bytearray | memoryview
) -> bytes:
    """Encode tightly packed 8-bit greyscale pixels as deterministic PNG."""

    return _encode_png(width, height, 0, pixels)


def encode_png_rgb(
    width: int, height: int, pixels: bytes | bytearray | memoryview
) -> bytes:
    """Encode tightly packed 8-bit RGB pixels as deterministic PNG."""

    return _encode_png(width, height, 2, pixels)


def encode_png_rgba(
    width: int, height: int, pixels: bytes | bytearray | memoryview
) -> bytes:
    """Encode tightly packed 8-bit RGBA pixels as deterministic PNG."""

    return _encode_png(width, height, 6, pixels)


def _validate_chunk_type(kind: bytes, label: str) -> None:
    if len(kind) != 4 or any(
        not (65 <= value <= 90 or 97 <= value <= 122) for value in kind
    ):
        raise PNGError(f"{label}: invalid PNG chunk type")
    # PNG's reserved bit is the third type-letter bit and must be uppercase.
    # The fourth letter is the safe-to-copy bit and may be lowercase (as in
    # the standard tEXt chunk).
    if 97 <= kind[2] <= 122:
        raise PNGError(f"{label}: PNG chunk type has a lowercase reserved bit")


def _is_critical(kind: bytes) -> bool:
    return 65 <= kind[0] <= 90


def _parse_ihdr(payload: bytes, label: str) -> tuple[int, int, int, int]:
    if len(payload) != 13:
        raise PNGError(f"{label}: IHDR must contain exactly 13 bytes")
    width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(
        ">IIBBBBB", payload
    )
    if bit_depth != 8:
        raise PNGError(f"{label}: unsupported PNG bit depth {bit_depth}; expected 8")
    if color_type == 3:
        raise PNGError(f"{label}: palette PNGs are unsupported")
    if color_type not in _CHANNELS_BY_COLOR_TYPE:
        raise PNGError(f"{label}: unsupported PNG color type {color_type}")
    if compression != 0:
        raise PNGError(f"{label}: unsupported PNG compression method {compression}")
    if filter_method != 0:
        raise PNGError(f"{label}: unsupported PNG filter method {filter_method}")
    if interlace != 0:
        raise PNGError(f"{label}: interlaced PNGs are unsupported")
    channels = _CHANNELS_BY_COLOR_TYPE[color_type]
    width, height, channels, scanline_bytes = _validate_dimensions(width, height, channels)
    return width, height, color_type, scanline_bytes


def _decompress_scanlines(compressed: bytes, expected_size: int, label: str) -> bytes:
    try:
        decoder = zlib.decompressobj()
        output = bytearray(decoder.decompress(compressed, expected_size + 1))
        if len(output) > expected_size:
            raise PNGError(f"{label}: decompressed IDAT exceeds expected scanline size")
        if decoder.unconsumed_tail:
            raise PNGError(f"{label}: decompressed IDAT exceeds expected scanline size")
        remaining = expected_size + 1 - len(output)
        if remaining:
            output.extend(decoder.flush(remaining))
        if len(output) > expected_size:
            raise PNGError(f"{label}: decompressed IDAT exceeds expected scanline size")
        if decoder.unconsumed_tail:
            raise PNGError(f"{label}: decompressed IDAT exceeds expected scanline size")
    except PNGError:
        raise
    except zlib.error as error:
        raise PNGError(f"{label}: invalid or truncated zlib stream: {error}") from error
    if not decoder.eof:
        raise PNGError(f"{label}: truncated zlib stream")
    if decoder.unused_data:
        raise PNGError(f"{label}: trailing compressed data after zlib stream")
    if len(output) != expected_size:
        raise PNGError(
            f"{label}: decompressed IDAT has {len(output)} bytes; expected exactly {expected_size}"
        )
    return bytes(output)


def _paeth(left: int, up: int, upper_left: int) -> int:
    estimate = left + up - upper_left
    distance_left = abs(estimate - left)
    distance_up = abs(estimate - up)
    distance_upper_left = abs(estimate - upper_left)
    if distance_left <= distance_up and distance_left <= distance_upper_left:
        return left
    if distance_up <= distance_upper_left:
        return up
    return upper_left


def _unfilter(
    scanlines: bytes, width: int, height: int, channels: int, label: str
) -> bytes:
    row_bytes = width * channels
    expected_size = (row_bytes + 1) * height
    if len(scanlines) != expected_size:
        raise PNGError(f"{label}: scanline buffer has an unexpected size")
    pixels = bytearray(width * height * channels)
    previous = bytes(row_bytes)
    source_position = 0
    destination_position = 0
    for row in range(height):
        filter_type = scanlines[source_position]
        source_position += 1
        encoded = scanlines[source_position : source_position + row_bytes]
        source_position += row_bytes
        if filter_type not in (0, 1, 2, 3, 4):
            raise PNGError(f"{label}: unsupported PNG row filter {filter_type}")
        reconstructed = bytearray(row_bytes)
        for index, value in enumerate(encoded):
            left = reconstructed[index - channels] if index >= channels else 0
            up = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = up
            elif filter_type == 3:
                predictor = (left + up) // 2
            else:
                predictor = _paeth(left, up, upper_left)
            reconstructed[index] = (value + predictor) & 0xFF
        pixels[destination_position : destination_position + row_bytes] = reconstructed
        destination_position += row_bytes
        previous = bytes(reconstructed)
    if source_position != len(scanlines):
        raise PNGError(f"{label}: scanline parser did not consume the exact buffer")
    return bytes(pixels)


def decode_png(data: bytes | bytearray | memoryview, *, label: str = "png") -> PNGImage:
    """Strictly decode one supported PNG image to immutable interleaved pixels."""

    raw = _copy_bytes(data, label, MAX_PNG_BYTES)
    if len(raw) < len(PNG_SIGNATURE) or raw[: len(PNG_SIGNATURE)] != PNG_SIGNATURE:
        raise PNGError(f"{label}: invalid PNG signature")

    position = len(PNG_SIGNATURE)
    ihdr: tuple[int, int, int, int] | None = None
    idat_parts: list[bytes] = []
    idat_size = 0
    idat_closed = False
    saw_iend = False
    while position < len(raw):
        if len(raw) - position < 8:
            raise PNGError(f"{label}: truncated PNG chunk header")
        length = struct.unpack_from(">I", raw, position)[0]
        if length > MAX_CHUNK_BYTES:
            raise PNGError(f"{label}: PNG chunk exceeds the supported size limit")
        chunk_total = 12 + length
        if chunk_total > len(raw) - position:
            raise PNGError(f"{label}: truncated PNG chunk")
        kind = raw[position + 4 : position + 8]
        _validate_chunk_type(kind, label)
        data_start = position + 8
        data_end = data_start + length
        chunk_data = raw[data_start:data_end]
        stored_crc = struct.unpack_from(">I", raw, data_end)[0]
        computed_crc = zlib.crc32(kind + chunk_data) & 0xFFFFFFFF
        if stored_crc != computed_crc:
            raise PNGError(f"{label}: CRC mismatch in {kind.decode('ascii')} chunk")
        position += chunk_total

        if ihdr is None:
            if kind != _IHDR:
                raise PNGError(f"{label}: IHDR must be the first PNG chunk")
            ihdr = _parse_ihdr(chunk_data, label)
            continue
        if kind == _IHDR:
            raise PNGError(f"{label}: duplicate IHDR chunk")
        if kind == _IDAT:
            if idat_closed:
                raise PNGError(f"{label}: IDAT chunks must be contiguous")
            idat_parts.append(chunk_data)
            idat_size += length
            if idat_size > MAX_PNG_BYTES:
                raise PNGError(f"{label}: IDAT data exceeds the supported size limit")
            continue
        if kind == _IEND:
            if length != 0:
                raise PNGError(f"{label}: IEND must be empty")
            if not idat_parts:
                raise PNGError(f"{label}: PNG has no IDAT chunk")
            saw_iend = True
            if position != len(raw):
                raise PNGError(f"{label}: trailing bytes after IEND")
            break
        if idat_parts:
            idat_closed = True
        if kind == _PLTE:
            raise PNGError(f"{label}: palette PNGs are unsupported")
        if _is_critical(kind):
            raise PNGError(f"{label}: unknown critical PNG chunk {kind.decode('ascii')}")
        # Structurally valid ancillary chunks are intentionally ignored.

    if ihdr is None:
        raise PNGError(f"{label}: missing IHDR chunk")
    if not saw_iend:
        raise PNGError(f"{label}: missing IEND chunk")
    width, height, color_type, scanline_bytes = ihdr
    channels = _CHANNELS_BY_COLOR_TYPE[color_type]
    compressed = b"".join(idat_parts)
    scanlines = _decompress_scanlines(compressed, scanline_bytes, label)
    pixels = _unfilter(scanlines, width, height, channels, label)
    return PNGImage(width, height, color_type, channels, pixels)


__all__ = [
    "PNGError",
    "PNGImage",
    "decode_png",
    "encode_png_gray",
    "encode_png_rgb",
    "encode_png_rgba",
]
