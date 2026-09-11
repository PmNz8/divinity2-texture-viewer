# SPDX-FileCopyrightText: 2026 PmNz8
# SPDX-License-Identifier: AGPL-3.0-only
import json
import tempfile
from pathlib import Path
import unittest

from texture_viewer.codec import bc
from texture_viewer.codec.nif_texture import parse_texture_resource
from texture_viewer.codec.roundtrip import build_export_set, import_export_set, import_texture_set, TextureRoundTripError
from texture_viewer.codec.mip_generation import build_base_export_set, downsample
from texture_viewer.codec.png import encode_png_rgb
from tests.test_texture_viewer_model import _make_texture


class MipGenerationTests(unittest.TestCase):
    def test_noop_preserves_independently_coloured_lower_mips(self):
        for fmt in (bc.FORMAT_BC1, bc.FORMAT_BC3):
            payload = _make_texture(fmt)
            resource = parse_texture_resource(payload)
            for profile in ("raw", "srgb"):
                files = build_base_export_set(resource, profile=profile)
                self.assertNotIn("mip-01.rgb.png", files)
                manifest = json.loads(files["texture.json"])
                self.assertEqual(manifest["source"]["mip_count"], 3)
                self.assertEqual(len(manifest["mips"]), 3)
                self.assertEqual(import_export_set(resource, files), payload)
                with tempfile.TemporaryDirectory() as tmp:
                    for name, data in files.items():
                        (Path(tmp) / name).write_bytes(data)
                    self.assertEqual(import_texture_set(resource, tmp), payload)

    def test_edited_base_regenerates_every_mip_and_preserves_wrapper(self):
        for fmt in (bc.FORMAT_BC1, bc.FORMAT_BC3):
            resource = parse_texture_resource(_make_texture(fmt))
            files = build_base_export_set(resource)
            base = resource.mips[0]
            files["mip-00.rgb.png"] = encode_png_rgb(base.width, base.height, bytes((240, 10, 10)) * (base.width * base.height))
            output = import_export_set(resource, files)
            edited = parse_texture_resource(output)
            self.assertEqual(output, import_export_set(resource, files))
            self.assertEqual(edited.mip_count, resource.mip_count)
            changed = set()
            for index, mip in enumerate(resource.mips):
                self.assertNotEqual(resource.mip_bytes(index), edited.mip_bytes(index))
                changed.update(range(mip.absolute_offset, mip.absolute_offset + mip.size))
            original = _make_texture(fmt)
            self.assertTrue(all(a == b for i, (a, b) in enumerate(zip(original, output)) if i not in changed))

    def test_strict_contract(self):
        resource = parse_texture_resource(_make_texture(bc.FORMAT_BC3))
        files = build_base_export_set(resource)
        for name, value in (("mip-01.rgb.png", b"x"), ("texture.json", b"{}"), ("mip-00.rgb.png", encode_png_rgb(1, 1, b"abc"))):
            with self.subTest(name=name):
                bad = dict(files)
                bad[name] = value
                with self.assertRaises(TextureRoundTripError):
                    import_export_set(resource, bad)
        bad = dict(files)
        bad.pop("mip-00.alpha.png")
        with self.assertRaises(TextureRoundTripError):
            import_export_set(resource, bad)

    def test_bc2_old_contract_unchanged(self):
        payload = _make_texture(bc.FORMAT_BC2)
        resource = parse_texture_resource(payload)
        self.assertEqual(import_export_set(resource, build_export_set(resource)), payload)
        with self.assertRaises(TextureRoundTripError):
            build_base_export_set(resource)

    def test_filter_profiles(self):
        data = bytes((0, 0, 0, 255, 255, 255, 255, 255))
        self.assertEqual(downsample(data, 2, 1, 1, 1, profile="raw"), bytes((128, 128, 128, 255)))
        self.assertEqual(downsample(data, 2, 1, 1, 1, profile="srgb"), bytes((188, 188, 188, 255)))
        data = bytes((255, 0, 0, 255, 0, 0, 255, 0))
        self.assertEqual(downsample(data, 2, 1, 1, 1, profile="srgb"), bytes((255, 0, 0, 128)))
        data = bytes((0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255, 255))
        self.assertEqual(downsample(data, 3, 1, 1, 1, profile="raw"), bytes((85, 85, 85, 255)))


if __name__ == "__main__":
    unittest.main()

