# SPDX-FileCopyrightText: 2026 PmNz8
# SPDX-License-Identifier: AGPL-3.0-only
"""Small, dependency-free BC1/BC2/BC3 decoder.

Only one complete mip is decoded at a time.  The functions intentionally have
strict byte contracts: a short or overlong input is an error, and no data is
silently padded or discarded.
"""

from __future__ import annotations

import struct
from math import sqrt
from typing import Final, Sequence


FORMAT_BC1: Final = 4
FORMAT_BC2: Final = 5
FORMAT_BC3: Final = 6

MAX_DIMENSION: Final = 16_384
MAX_DECODED_BYTES: Final = 256 * 1024 * 1024


class BCCodecError(ValueError):
    """Raised when BC input or dimensions violate the codec contract."""


def _validate_dimensions(width: int, height: int) -> tuple[int, int]:
    if isinstance(width, bool) or not isinstance(width, int):
        raise BCCodecError("width must be an integer")
    if isinstance(height, bool) or not isinstance(height, int):
        raise BCCodecError("height must be an integer")
    if width <= 0 or height <= 0:
        raise BCCodecError("width and height must be positive")
    if width > MAX_DIMENSION or height > MAX_DIMENSION:
        raise BCCodecError(
            f"dimensions {width}x{height} exceed the supported limit "
            f"{MAX_DIMENSION}x{MAX_DIMENSION}"
        )
    return width, height


def _format_block_size(pixel_format: int) -> int:
    if pixel_format == FORMAT_BC1:
        return 8
    if pixel_format in (FORMAT_BC2, FORMAT_BC3):
        return 16
    raise BCCodecError(f"unsupported BC pixel format {pixel_format}; expected 4, 5, or 6")


def block_size(pixel_format: int) -> int:
    """Return the compressed bytes per 4x4 block for format 4, 5, or 6."""

    if isinstance(pixel_format, bool) or not isinstance(pixel_format, int):
        raise BCCodecError("pixel_format must be an integer")
    return _format_block_size(pixel_format)


def mip_size(width: int, height: int, pixel_format: int) -> int:
    """Return the exact compressed size of one mip."""

    width, height = _validate_dimensions(width, height)
    compressed = ((width + 3) // 4) * ((height + 3) // 4) * block_size(pixel_format)
    if compressed > MAX_DECODED_BYTES:
        raise BCCodecError("compressed mip exceeds the supported size limit")
    return compressed


def _as_bytes(data: bytes | bytearray | memoryview) -> bytes:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise BCCodecError("compressed data must be bytes-like")
    try:
        return bytes(data)
    except (TypeError, ValueError) as error:
        raise BCCodecError("compressed data must be a contiguous bytes-like buffer") from error


def _validate_rgba(
    rgba: bytes | bytearray | memoryview, width: int, height: int
) -> tuple[bytes, int, int]:
    """Validate and copy an RGBA8 image at an encoder API boundary."""

    width, height = _validate_dimensions(width, height)
    expected = width * height * 4
    if expected > MAX_DECODED_BYTES:
        raise BCCodecError("RGBA image exceeds the supported size limit")
    if not isinstance(rgba, (bytes, bytearray, memoryview)):
        raise BCCodecError("RGBA data must be bytes-like")
    try:
        raw = bytes(rgba)
    except (TypeError, ValueError) as error:
        raise BCCodecError("RGBA data must be a contiguous bytes-like buffer") from error
    if len(raw) != expected:
        raise BCCodecError(
            f"RGBA image has {len(raw)} bytes; expected exactly {expected} "
            f"for {width}x{height}"
        )
    return raw, width, height


def _validate_alpha_threshold(threshold: int) -> int:
    if isinstance(threshold, bool) or not isinstance(threshold, int):
        raise BCCodecError("alpha_threshold must be an integer")
    if not 0 <= threshold <= 255:
        raise BCCodecError("alpha_threshold must be between 0 and 255")
    return threshold


def _rgb565(value: int) -> tuple[int, int, int]:
    """Expand RGB565 using the integer expansion used by the game tooling."""

    red = (value >> 11) & 0x1F
    green = (value >> 5) & 0x3F
    blue = value & 0x1F
    return (red * 255 // 31, green * 255 // 63, blue * 255 // 31)


def _opaque_palette(color0: int, color1: int) -> tuple[tuple[int, int, int, int], ...]:
    first = _rgb565(color0)
    second = _rgb565(color1)
    third = tuple((2 * first[index] + second[index]) // 3 for index in range(3))
    fourth = tuple((first[index] + 2 * second[index]) // 3 for index in range(3))
    return (
        (*first, 255),
        (*second, 255),
        (*third, 255),
        (*fourth, 255),
    )


def _bc1_palette(color0: int, color1: int) -> tuple[tuple[int, int, int, int], ...]:
    first = _rgb565(color0)
    second = _rgb565(color1)
    if color0 > color1:
        return _opaque_palette(color0, color1)
    midpoint = tuple((first[index] + second[index]) // 2 for index in range(3))
    return ((*first, 255), (*second, 255), (*midpoint, 255), (0, 0, 0, 0))


def _decode_color_block(
    block: bytes, *, bc1_mode: bool
) -> tuple[tuple[int, int, int, int], ...]:
    color0, color1 = struct.unpack_from("<HH", block, 0)
    palette = _bc1_palette(color0, color1) if bc1_mode else _opaque_palette(color0, color1)
    indices = struct.unpack_from("<I", block, 4)[0]
    return tuple(palette[(indices >> (2 * pixel)) & 0x3] for pixel in range(16))


def _decode_bc2_alpha(block: bytes) -> tuple[int, ...]:
    values: list[int] = []
    for pixel in range(16):
        nibble = (block[pixel // 2] >> (4 * (pixel & 1))) & 0xF
        values.append(nibble * 17)
    return tuple(values)


def _bc3_alpha_palette(alpha0: int, alpha1: int) -> tuple[int, ...]:
    if alpha0 > alpha1:
        values = [alpha0, alpha1]
        values.extend(((8 - index) * alpha0 + (index - 1) * alpha1) // 7 for index in range(2, 8))
        return tuple(values)
    values = [alpha0, alpha1]
    values.extend(((6 - index) * alpha0 + (index - 1) * alpha1) // 5 for index in range(2, 6))
    values.extend((0, 255))
    return tuple(values)


def _decode_bc3_alpha(block: bytes) -> tuple[int, ...]:
    palette = _bc3_alpha_palette(block[0], block[1])
    indices = int.from_bytes(block[2:8], "little")
    return tuple(palette[(indices >> (3 * pixel)) & 0x7] for pixel in range(16))


def _decode_block(block: bytes, pixel_format: int) -> tuple[tuple[int, int, int, int], ...]:
    if pixel_format == FORMAT_BC1:
        return _decode_color_block(block, bc1_mode=True)
    # BC2 and BC3 always use the four-color opaque color interpolation.  Their
    # separate alpha blocks carry transparency, so BC1's color0<=color1 mode
    # must not be applied to their color half.
    if pixel_format == FORMAT_BC2:
        colors = _decode_color_block(block[8:16], bc1_mode=False)
        alpha = _decode_bc2_alpha(block[:8])
    elif pixel_format == FORMAT_BC3:
        colors = _decode_color_block(block[8:16], bc1_mode=False)
        alpha = _decode_bc3_alpha(block[:8])
    else:  # pragma: no cover - guarded by block_size and public validation
        raise BCCodecError(f"unsupported BC pixel format {pixel_format}")
    return tuple((r, g, b, alpha[index]) for index, (r, g, b, _) in enumerate(colors))


def decode_mip(
    data: bytes | bytearray | memoryview,
    width: int,
    height: int,
    pixel_format: int,
) -> bytes:
    """Decode exactly one complete mip to tightly packed RGBA8 bytes.

    The compressed data must have exactly the size returned by
    :func:`mip_size`.  Blocks at image edges are decoded normally but texels
    outside the requested dimensions are discarded.
    """

    width, height = _validate_dimensions(width, height)
    expected = mip_size(width, height, pixel_format)
    raw = _as_bytes(data)
    if len(raw) != expected:
        raise BCCodecError(
            f"compressed mip has {len(raw)} bytes; expected exactly {expected} "
            f"for {width}x{height} format {pixel_format}"
        )
    # Protect the output allocation independently from the compressed-size
    # limit.  This is mostly relevant for small-block, large images.
    output_size = width * height * 4
    if output_size > MAX_DECODED_BYTES:
        raise BCCodecError("decoded mip exceeds the supported size limit")

    blocks_x = (width + 3) // 4
    blocks_y = (height + 3) // 4
    output = bytearray(output_size)
    cursor = 0
    for block_y in range(blocks_y):
        for block_x in range(blocks_x):
            size = block_size(pixel_format)
            block_pixels = _decode_block(raw[cursor : cursor + size], pixel_format)
            cursor += size
            for local_y in range(4):
                y = block_y * 4 + local_y
                if y >= height:
                    break
                for local_x in range(4):
                    x = block_x * 4 + local_x
                    if x >= width:
                        break
                    source = block_pixels[local_y * 4 + local_x]
                    destination = (y * width + x) * 4
                    output[destination : destination + 4] = bytes(source)
    # The loop above consumes one exact block for every block coordinate.
    assert cursor == expected
    return bytes(output)


# ---------------------------------------------------------------------------
# Encoders
# ---------------------------------------------------------------------------

RGB = tuple[int, int, int]
RGBA = tuple[int, int, int, int]


def _to_rgb565(red: int, green: int, blue: int) -> int:
    """Quantize an RGB8 value to the wire-format RGB565 representation."""

    return ((red >> 3) << 11) | ((green >> 2) << 5) | (blue >> 3)


def _clamp_byte(value: float) -> int:
    # ``int(value + .5)`` is intentional: unlike round(), it has no
    # platform-dependent ties-to-even behavior for the candidate search.
    return max(0, min(255, int(value + 0.5)))


def _principal_axis_candidates(pixels: Sequence[RGB]) -> list[tuple[RGB, RGB]]:
    """Return deterministic endpoint candidates for one 4x4 RGB block.

    The first nine candidates preserve the fast principal-axis search used by
    the exploratory encoder.  The final candidate is the complete component
    min/max span, which is deliberately not guaranteed to be present in the
    principal-axis samples after RGB565 quantization.
    """

    if not pixels:
        raise BCCodecError("cannot encode an empty color block")
    red_min = min(pixel[0] for pixel in pixels)
    green_min = min(pixel[1] for pixel in pixels)
    blue_min = min(pixel[2] for pixel in pixels)
    red_max = max(pixel[0] for pixel in pixels)
    green_max = max(pixel[1] for pixel in pixels)
    blue_max = max(pixel[2] for pixel in pixels)

    count = float(len(pixels))
    mean_red = sum(pixel[0] for pixel in pixels) / count
    mean_green = sum(pixel[1] for pixel in pixels) / count
    mean_blue = sum(pixel[2] for pixel in pixels) / count
    covariance_xx = sum((pixel[0] - mean_red) ** 2 for pixel in pixels) / count
    covariance_yy = sum((pixel[1] - mean_green) ** 2 for pixel in pixels) / count
    covariance_zz = sum((pixel[2] - mean_blue) ** 2 for pixel in pixels) / count
    covariance_xy = (
        sum((pixel[0] - mean_red) * (pixel[1] - mean_green) for pixel in pixels)
        / count
    )
    covariance_yz = (
        sum((pixel[1] - mean_green) * (pixel[2] - mean_blue) for pixel in pixels)
        / count
    )
    covariance_zx = (
        sum((pixel[2] - mean_blue) * (pixel[0] - mean_red) for pixel in pixels)
        / count
    )

    # A fixed initial vector and fixed number of iterations make this search
    # deterministic without introducing a dependency on a numeric package.
    axis_x, axis_y, axis_z = 1.0, 1.0, 1.0
    for _ in range(4):
        next_x = covariance_xx * axis_x + covariance_xy * axis_y + covariance_zx * axis_z
        next_y = covariance_xy * axis_x + covariance_yy * axis_y + covariance_yz * axis_z
        next_z = covariance_zx * axis_x + covariance_yz * axis_y + covariance_zz * axis_z
        norm = sqrt(next_x * next_x + next_y * next_y + next_z * next_z)
        if norm == 0:
            axis_x = axis_y = axis_z = 0.0
        else:
            axis_x, axis_y, axis_z = next_x / norm, next_y / norm, next_z / norm

    center = (
        (red_min + red_max) / 2.0,
        (green_min + green_max) / 2.0,
        (blue_min + blue_max) / 2.0,
    )
    half_span = (
        (red_max - red_min) / 2.0,
        (green_max - green_min) / 2.0,
        (blue_max - blue_min) / 2.0,
    )
    span = (
        abs(half_span[0] * axis_x)
        + abs(half_span[1] * axis_y)
        + abs(half_span[2] * axis_z)
    )

    candidates: list[tuple[RGB, RGB]] = []
    for index in range(9):
        fraction = index / 8.0
        first = (
            _clamp_byte(center[0] - axis_x * span * (1.0 - fraction)),
            _clamp_byte(center[1] - axis_y * span * (1.0 - fraction)),
            _clamp_byte(center[2] - axis_z * span * (1.0 - fraction)),
        )
        second = (
            _clamp_byte(center[0] + axis_x * span * fraction),
            _clamp_byte(center[1] + axis_y * span * fraction),
            _clamp_byte(center[2] + axis_z * span * fraction),
        )
        candidates.append((first, second))
    candidates.append(
        (
            (red_min, green_min, blue_min),
            (red_max, green_max, blue_max),
        )
    )
    return candidates


def _ordered_endpoints(
    first: RGB, second: RGB, *, binary_alpha: bool
) -> tuple[int, int]:
    """Quantize and enforce the BC1 ordering mode for a candidate pair."""

    color0 = _to_rgb565(*first)
    color1 = _to_rgb565(*second)
    if binary_alpha:
        if color0 > color1:
            color0, color1 = color1, color0
        if color0 == color1:
            if color1 < 0xFFFF:
                color1 += 1
            else:
                color0 -= 1
    else:
        if color0 < color1:
            color0, color1 = color1, color0
        if color0 == color1:
            if color0 > 0:
                color1 -= 1
            else:
                color0 += 1
    return color0, color1


def _nearest_palette_indices(
    pixels: Sequence[RGB], palette: Sequence[tuple[int, int, int]]
) -> tuple[int, int]:
    """Return squared RGB error and packed 2-bit indices.

    Iteration order is also the tie-break: equal-distance texels use the
    lowest palette index, and equal candidate errors retain the first pair.
    """

    error = 0
    indices = 0
    for pixel_index, (red, green, blue) in enumerate(pixels):
        best_index = 0
        best_distance = 1 << 60
        for palette_index, (palette_red, palette_green, palette_blue) in enumerate(palette):
            delta_red = red - palette_red
            delta_green = green - palette_green
            delta_blue = blue - palette_blue
            distance = delta_red * delta_red + delta_green * delta_green + delta_blue * delta_blue
            if distance < best_distance:
                best_distance = distance
                best_index = palette_index
        error += best_distance
        indices |= best_index << (2 * pixel_index)
    return error, indices


def _best_color_encoding(
    pixels: Sequence[RGB], *, binary_alpha: bool
) -> tuple[int, int, int]:
    """Find the lowest-error quantized endpoint pair for one color block."""

    seen: set[tuple[int, int]] = set()
    best: tuple[int, int, int, int] | None = None
    for first, second in _principal_axis_candidates(pixels):
        color0, color1 = _ordered_endpoints(first, second, binary_alpha=binary_alpha)
        pair = (color0, color1)
        if pair in seen:
            continue
        seen.add(pair)
        if binary_alpha:
            palette_rgba = _bc1_palette(color0, color1)
            palette = tuple(color[:3] for color in palette_rgba[:3])
        else:
            palette = tuple(color[:3] for color in _opaque_palette(color0, color1))
        error, indices = _nearest_palette_indices(pixels, palette)
        candidate = (error, color0, color1, indices)
        if best is None or error < best[0]:
            best = candidate
    if best is None:  # pragma: no cover - candidates always contains one item
        raise BCCodecError("cannot encode a color block")
    _, color0, color1, indices = best
    return color0, color1, indices


def _encode_opaque_block(pixels: Sequence[RGB]) -> bytes:
    color0, color1, indices = _best_color_encoding(pixels, binary_alpha=False)
    return struct.pack("<HHI", color0, color1, indices)


def _encode_binary_alpha_block(pixels: Sequence[RGBA], threshold: int) -> bytes:
    opaque_positions = [index for index, pixel in enumerate(pixels) if pixel[3] >= threshold]
    if not opaque_positions:
        # RGB values in transparent BC1 texels are deliberately not encoded.
        return struct.pack("<HHI", 0, 0xFFFF, 0xFFFFFFFF)
    if len(opaque_positions) == len(pixels):
        return _encode_opaque_block(tuple(pixel[:3] for pixel in pixels))

    opaque_pixels = tuple(pixels[index][:3] for index in opaque_positions)
    color0, color1, opaque_indices = _best_color_encoding(opaque_pixels, binary_alpha=True)
    indices = 0
    opaque_cursor = 0
    for pixel_index in range(16):
        if pixel_index in opaque_positions:
            index = (opaque_indices >> (2 * opaque_cursor)) & 0x3
            opaque_cursor += 1
        else:
            index = 3
        indices |= index << (2 * pixel_index)
    return struct.pack("<HHI", color0, color1, indices)


def _alpha_palette_for_encoding(alpha0: int, alpha1: int) -> tuple[int, ...]:
    return _bc3_alpha_palette(alpha0, alpha1)


def _encode_alpha_block(alphas: Sequence[int]) -> bytes:
    alpha_min = min(alphas)
    alpha_max = max(alphas)
    pairs = ((alpha_min, alpha_max), (alpha_max, alpha_min))
    seen: set[tuple[int, int]] = set()
    best: tuple[int, int, int, int] | None = None
    for alpha0, alpha1 in pairs:
        if (alpha0, alpha1) in seen:
            continue
        seen.add((alpha0, alpha1))
        palette = _alpha_palette_for_encoding(alpha0, alpha1)
        error = 0
        indices = 0
        for pixel_index, alpha in enumerate(alphas):
            best_index = 0
            best_distance = 1 << 60
            for palette_index, palette_alpha in enumerate(palette):
                distance = (alpha - palette_alpha) * (alpha - palette_alpha)
                if distance < best_distance:
                    best_distance = distance
                    best_index = palette_index
            error += best_distance
            indices |= best_index << (3 * pixel_index)
        candidate = (error, alpha0, alpha1, indices)
        if best is None or error < best[0]:
            best = candidate
    if best is None:  # pragma: no cover - pairs always contains one item
        raise BCCodecError("cannot encode an alpha block")
    _, alpha0, alpha1, indices = best
    return struct.pack("<BB", alpha0, alpha1) + indices.to_bytes(6, "little")


def _block_pixels(raw: bytes, width: int, height: int, block_x: int, block_y: int) -> tuple[RGBA, ...]:
    """Read a 4x4 block, replicating the final valid edge texels."""

    pixels: list[RGBA] = []
    for local_y in range(4):
        source_y = min(block_y * 4 + local_y, height - 1)
        for local_x in range(4):
            source_x = min(block_x * 4 + local_x, width - 1)
            offset = (source_y * width + source_x) * 4
            pixels.append((raw[offset], raw[offset + 1], raw[offset + 2], raw[offset + 3]))
    return tuple(pixels)


def _encode_image(raw: bytes, width: int, height: int, pixel_format: int, *, binary_alpha: bool = False, alpha_threshold: int = 128) -> bytes:
    blocks_x = (width + 3) // 4
    blocks_y = (height + 3) // 4
    block_bytes = 8 if pixel_format == FORMAT_BC1 else 16
    result = bytearray(blocks_x * blocks_y * block_bytes)
    cursor = 0
    for block_y in range(blocks_y):
        for block_x in range(blocks_x):
            pixels = _block_pixels(raw, width, height, block_x, block_y)
            if pixel_format == FORMAT_BC1:
                if binary_alpha:
                    encoded = _encode_binary_alpha_block(pixels, alpha_threshold)
                else:
                    encoded = _encode_opaque_block(tuple(pixel[:3] for pixel in pixels))
            elif pixel_format == FORMAT_BC3:
                encoded = _encode_alpha_block(tuple(pixel[3] for pixel in pixels))
                encoded += _encode_opaque_block(tuple(pixel[:3] for pixel in pixels))
            else:  # pragma: no cover - public dispatch rejects BC2
                raise BCCodecError(f"unsupported encoder pixel format {pixel_format}")
            result[cursor : cursor + block_bytes] = encoded
            cursor += block_bytes
    expected = mip_size(width, height, pixel_format)
    if cursor != expected or len(result) != expected:  # pragma: no cover - internal invariant
        raise BCCodecError(f"encoder produced {len(result)} bytes; expected {expected}")
    return bytes(result)


def encode_bc1_opaque(
    rgba: bytes | bytearray | memoryview, width: int, height: int
) -> bytes:
    """Encode an RGBA8 mip to opaque BC1, rejecting non-opaque alpha."""

    raw, width, height = _validate_rgba(rgba, width, height)
    if any(alpha != 255 for alpha in raw[3::4]):
        raise BCCodecError("BC1 opaque encoding requires alpha=255 for every texel")
    return _encode_image(raw, width, height, FORMAT_BC1)


def encode_bc1_binary_alpha(
    rgba: bytes | bytearray | memoryview,
    width: int,
    height: int,
    threshold: int = 128,
) -> bytes:
    """Encode an RGBA8 mip to BC1 with thresholded 1-bit transparency."""

    raw, width, height = _validate_rgba(rgba, width, height)
    threshold = _validate_alpha_threshold(threshold)
    return _encode_image(
        raw,
        width,
        height,
        FORMAT_BC1,
        binary_alpha=True,
        alpha_threshold=threshold,
    )


def encode_bc3(
    rgba: bytes | bytearray | memoryview, width: int, height: int
) -> bytes:
    """Encode an RGBA8 mip to BC3/DXT5."""

    raw, width, height = _validate_rgba(rgba, width, height)
    return _encode_image(raw, width, height, FORMAT_BC3)


def encode_mip(
    rgba: bytes | bytearray | memoryview,
    width: int,
    height: int,
    pixel_format: int,
    *,
    bc1_binary_alpha: bool = False,
    alpha_threshold: int = 128,
) -> bytes:
    """Encode one RGBA8 mip for a supported game pixel format.

    BC2/DXT3 is intentionally decode-only; callers receive a stable explicit
    error rather than an accidental BC1/BC3 substitution.
    """

    if isinstance(pixel_format, bool) or not isinstance(pixel_format, int):
        raise BCCodecError("pixel_format must be an integer")
    if not isinstance(bc1_binary_alpha, bool):
        raise BCCodecError("bc1_binary_alpha must be a boolean")
    alpha_threshold = _validate_alpha_threshold(alpha_threshold)
    raw, width, height = _validate_rgba(rgba, width, height)
    if pixel_format == FORMAT_BC2:
        raise BCCodecError("BC2 encoding is unsupported")
    if pixel_format == FORMAT_BC1:
        if not bc1_binary_alpha and any(alpha != 255 for alpha in raw[3::4]):
            raise BCCodecError("BC1 opaque encoding requires alpha=255 for every texel")
        return _encode_image(
            raw,
            width,
            height,
            FORMAT_BC1,
            binary_alpha=bc1_binary_alpha,
            alpha_threshold=alpha_threshold,
        )
    if pixel_format == FORMAT_BC3:
        return _encode_image(raw, width, height, FORMAT_BC3)
    raise BCCodecError(f"unsupported BC pixel format {pixel_format}; expected 4, 5, or 6")


__all__ = [
    "BCCodecError",
    "FORMAT_BC1",
    "FORMAT_BC2",
    "FORMAT_BC3",
    "MAX_DECODED_BYTES",
    "MAX_DIMENSION",
    "block_size",
    "decode_mip",
    "encode_bc1_binary_alpha",
    "encode_bc1_opaque",
    "encode_bc3",
    "encode_mip",
    "mip_size",
]
