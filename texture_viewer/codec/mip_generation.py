# SPDX-FileCopyrightText: 2026 PmNz8
# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit v2 MIP0 authoring; regenerate only the existing BC1/BC3 chain.

Profiles are author choices, never inferred from filenames. Raw averages four
independent data channels; sRGB treats RGB as colour and alpha as opacity.
Neither mode renormalizes normal vectors or preserves alpha-test coverage.
"""
from __future__ import annotations

from . import bc, roundtrip as rt
from .nif_texture import serialize_texture_resource
from .png import encode_png_rgb, encode_png_gray

PROFILES = ("raw", "srgb")


def _manifest(resource, source, views, profile):
    if profile not in PROFILES:
        raise rt.TextureRoundTripError("mip profile must be raw or srgb")
    if resource.pixel_format not in (bc.FORMAT_BC1, bc.FORMAT_BC3):
        raise rt.TextureRoundTripError("MIP0 generation supports BC1/BC3 only")
    for previous, mip in zip(resource.mips, resource.mips[1:]):
        if (mip.width, mip.height) != (max(1, previous.width // 2), max(1, previous.height // 2)):
            raise rt.TextureRoundTripError("MIP0 generation requires a conventional halving chain")
    manifest = rt._manifest_for_resource(resource, source, views)
    manifest["schema_version"] = 2
    manifest["policy"] = {
        "existing_mips": "preserve_all_when_base_unchanged",
        "mip_generation": "source_count_area_v1",
        "sample_encoding": "raw_8bit",
        "color_space": "srgb" if profile == "srgb" else "uninterpreted",
        "semantic_kind": "color_opacity" if profile == "srgb" else "independent_channels",
        "alpha_coverage": "not_preserved",
        "bc2_edits": "unsupported",
        "profile": profile,
    }
    for mip in manifest["mips"][1:]:
        mip["rgb_file"] = None
        mip["alpha_file"] = None
    return manifest


def build_base_export_set(resource, *, profile="raw"):
    source, views, _ = rt._prepare_resource(resource)
    manifest = _manifest(resource, source, views, profile)
    base = views[0]
    files = {
        rt.SIDECAR_NAME: rt._canonical_json(manifest),
        "mip-00.rgb.png": encode_png_rgb(base.width, base.height, base.rgb),
    }
    if base.alpha_required:
        files["mip-00.alpha.png"] = encode_png_gray(base.width, base.height, base.alpha)
    return dict(sorted(files.items()))


def _linear(value):
    value /= 255
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


_LINEAR = tuple(_linear(value) for value in range(256))


def _byte(value):
    return max(0, min(255, int(value + 0.5)))


def _srgb(value):
    return _byte(255 * (12.92 * value if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055))


def _weights(source, target):
    # Integer-coordinate area overlap includes the final row/column of odd sizes.
    result = []
    for out in range(target):
        left, right = out * source, (out + 1) * source
        result.append(tuple(
            (index, min(right, (index + 1) * target) - max(left, index * target))
            for index in range(left // target, (right + target - 1) // target)
        ))
    return result


def downsample(rgba, width, height, target_width, target_height, *, profile):
    """Deterministic area filter, linear-light premultiplied opacity for sRGB."""
    if profile not in PROFILES:
        raise rt.TextureRoundTripError("unsupported mip profile")
    if any(type(v) is not int or v <= 0 for v in (width, height, target_width, target_height)):
        raise rt.TextureRoundTripError("invalid mip dimensions")
    if target_width > width or target_height > height or len(rgba) != width * height * 4:
        raise rt.TextureRoundTripError("invalid downsample geometry or RGBA size")
    xs, ys = _weights(width, target_width), _weights(height, target_height)
    output = bytearray(target_width * target_height * 4)
    for y, rows in enumerate(ys):
        for x, columns in enumerate(xs):
            sums = [0.0, 0.0, 0.0, 0.0]
            total = 0
            for iy, wy in rows:
                for ix, wx in columns:
                    weight = wx * wy
                    offset = (iy * width + ix) * 4
                    alpha = rgba[offset + 3]
                    total += weight
                    sums[3] += alpha * weight
                    for channel in range(3):
                        value = rgba[offset + channel]
                        sums[channel] += (_LINEAR[value] * alpha if profile == "srgb" else value) * weight
            destination = (y * target_width + x) * 4
            for channel in range(3):
                output[destination + channel] = (
                    _srgb(sums[channel] / sums[3]) if sums[3] else 0
                ) if profile == "srgb" else _byte(sums[channel] / total)
            output[destination + 3] = _byte(sums[3] / total)
    return bytes(output)


def import_base_export_set(resource, files):
    source, views, _ = rt._prepare_resource(resource)
    normalized = rt._normalize_files(files)
    raw = normalized.get(rt.SIDECAR_NAME, b"")
    # Compare against the finite allowed canonical manifests, not input-selected
    # offsets or filenames. No JSON field can widen the serializer boundary.
    for profile in PROFILES:
        expected = rt._canonical_json(_manifest(resource, source, views, profile))
        if raw == expected:
            break
    else:
        raise rt.TextureRoundTripError("texture.json does not match an exact v2 source manifest")
    rt._parse_sidecar(raw, expected, resource.label)
    base = views[0]
    expected_files = {rt.SIDECAR_NAME, "mip-00.rgb.png"}
    if base.alpha_required:
        expected_files.add("mip-00.alpha.png")
    if set(normalized) != expected_files:
        raise rt.TextureRoundTripError("MIP0 export-set file set mismatch")
    rgb = rt._decode_png_view(normalized["mip-00.rgb.png"], width=base.width,
        height=base.height, color_type=2, label="mip-00.rgb.png")
    alpha = base.alpha
    if base.alpha_required:
        alpha = rt._decode_png_view(normalized["mip-00.alpha.png"], width=base.width,
            height=base.height, color_type=0, label="mip-00.alpha.png")
    if rgb == base.rgb and alpha == base.alpha:
        return source
    rgba = rt._interleave_rgb_alpha(rgb, alpha, base.width * base.height)
    replacements = {}
    width, height = base.width, base.height
    for index, mip in enumerate(resource.mips):
        if index:
            rgba = downsample(rgba, width, height, mip.width, mip.height, profile=profile)
            width, height = mip.width, mip.height
        try:
            if resource.pixel_format == bc.FORMAT_BC3:
                encoded = bc.encode_bc3(rgba, width, height)
            elif base.alpha_required:
                encoded = bc.encode_bc1_binary_alpha(rgba, width, height, threshold=128)
            else:
                encoded = bc.encode_bc1_opaque(rgba, width, height)
        except bc.BCCodecError as error:
            raise rt.TextureRoundTripError(f"cannot encode mip {index}: {error}") from error
        if len(encoded) != mip.size:
            raise rt.TextureRoundTripError(f"generated mip {index} size mismatch")
        replacements[index] = encoded
    return serialize_texture_resource(resource, replacements, expected_source_sha256=resource.payload_sha256)

