# SPDX-FileCopyrightText: 2026 PmNz8
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from unittest import mock
import zlib

from texture_viewer import dv2lib
from texture_viewer.codec import bc

from texture_viewer.model import (
    MAX_TEXTURE_PAYLOAD_SIZE,
    TextureViewerError,
    TextureViewerModel,
)


HEADER = struct.Struct("<IIIBBII")
ENTRY = struct.Struct("<III")
BLOCK_SIZE = 0x8000


def _make_texture(
    pixel_format: int,
    dimensions: tuple[tuple[int, int], ...] = ((8, 4), (4, 2), (2, 1)),
) -> bytes:
    compressed: list[bytes] = []
    for index, (width, height) in enumerate(dimensions):
        if pixel_format == bc.FORMAT_BC1:
            # Red, green, then blue blocks make the selected-mip tests
            # visibly independent without relying on any decoder internals.
            color = ((index + 1) * 0x20) & 0xFF
            rgba = bytes((color, 30, 200 - color, 255)) * (width * height)
            compressed.append(bc.encode_bc1_opaque(rgba, width, height))
        elif pixel_format == bc.FORMAT_BC3:
            rgba = bytes((50 + index, 120, 210, 20 + index * 50)) * (width * height)
            compressed.append(bc.encode_bc3(rgba, width, height))
        elif pixel_format == bc.FORMAT_BC2:
            # Explicit DXT3 block: alpha nibble 0xF and an opaque red color.
            blocks = ((width + 3) // 4) * ((height + 3) // 4)
            block = b"\xff" * 8 + struct.pack("<HHI", 0xF800, 0x07E0, 0)
            compressed.append(block * blocks)
        else:
            raise AssertionError(pixel_format)
    data = b"".join(compressed)
    descriptor = b"D" * 59
    body = bytearray(struct.pack("<I", pixel_format) + descriptor + bytes((len(dimensions),)) + b"\0" * 7)
    cursor = 0
    for (width, height), mip in zip(dimensions, compressed, strict=True):
        body += struct.pack("<III", width, height, cursor)
        cursor += len(mip)
    body += struct.pack("<4I", len(data), len(data), 1, 3) + data
    header_line = b"Gamebryo File Format, Version 20.3.0.9\n"
    output = bytearray(header_line)
    output += struct.pack("<IBIIH", 0x14030009, 1, 0x00030000, 1, 1)
    type_bytes = b"NiPersistentSrcTextureRendererData"
    output += struct.pack("<I", len(type_bytes)) + type_bytes
    output += struct.pack("<H", 0)
    output += struct.pack("<I", len(body))
    output += struct.pack("<II", 0, 0)
    output += struct.pack("<I", 0)
    output += body
    output += struct.pack("<Ii", 1, 0)
    return bytes(output)


def _make_archive(directory: Path, entries: list[tuple[str, bytes, str]], name: str = "sample.dv2") -> Path:
    path_table = b"".join(path.encode("ascii") + b"\0" for path, _payload, _mode in entries)
    table_end = HEADER.size + len(path_table) + 4 + len(entries) * ENTRY.size
    data_offset = (table_end + BLOCK_SIZE - 1) // BLOCK_SIZE * BLOCK_SIZE
    result = bytearray(HEADER.pack(5, 1, 4, 0, 1, data_offset, len(path_table)))
    result += path_table + struct.pack("<I", len(entries))
    result += bytes(len(entries) * ENTRY.size)
    result += bytes(data_offset - len(result))
    records: list[tuple[int, int, int]] = []
    cursor = 0
    for entry_path, payload, mode in entries:
        cursor = (cursor + BLOCK_SIZE - 1) // BLOCK_SIZE * BLOCK_SIZE
        if mode == "zlib":
            stored = zlib.compress(payload, 9)
            unpacked = len(payload)
        else:
            stored = payload
            unpacked = 0
        records.append((cursor, len(stored), unpacked))
        result += bytes(cursor - (len(result) - data_offset))
        result += stored
        cursor += len(stored)
    result += bytes(((len(result) + BLOCK_SIZE - 1) // BLOCK_SIZE * BLOCK_SIZE) - len(result))
    records_offset = table_end - len(entries) * ENTRY.size
    for index, record in enumerate(records):
        struct.pack_into("<III", result, records_offset + index * ENTRY.size, *record)
    output = directory / name
    output.write_bytes(result)
    return output


class TextureViewerModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="texture-viewer-model-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.archive = _make_archive(
            self.tmp,
            [
                ("Textures\\ValidBC1.nif", _make_texture(bc.FORMAT_BC1), "raw"),
                ("Textures\\ValidBC2.nif", _make_texture(bc.FORMAT_BC2), "zlib"),
                ("Textures\\ValidBC3.nif", _make_texture(bc.FORMAT_BC3), "raw"),
                ("Data\\Readme.txt", b"not a texture", "raw"),
                ("Data\\Case.XML", b"metadata", "zlib"),
            ],
        )
        self.model = TextureViewerModel()

    def test_open_and_table_filter_do_not_read_payloads(self) -> None:
        self.model.open_archive(self.archive)
        session = self.model.session
        assert session is not None

        def fail(*_args, **_kwargs):
            raise AssertionError("entry payload was read during table filtering")

        session.read_entry_bytes = fail  # type: ignore[method-assign]
        self.assertEqual(
            [row.path for row in self.model.list_entries(substring="CASE")],
            ["Data\\Case.XML"],
        )
        self.assertEqual(
            [row.path for row in self.model.list_entries(glob_pattern="textures\\*.NIF")],
            ["Textures\\ValidBC1.nif", "Textures\\ValidBC2.nif", "Textures\\ValidBC3.nif"],
        )
        with self.assertRaises(TextureViewerError):
            self.model.list_entries(limit=0)
        with self.assertRaises(TextureViewerError):
            self.model.list_entries(limit=True)

    def test_entry_snapshot_contains_sizes_mode_and_filename_hint(self) -> None:
        self.model.open_archive(self.archive)
        rows = self.model.list_entries()
        nif = next(row for row in rows if row.path.endswith("ValidBC1.nif"))
        text = next(row for row in rows if row.path.endswith("Readme.txt"))
        self.assertTrue(nif.nif_hint)
        self.assertFalse(text.nif_hint)
        self.assertGreater(nif.logical_size, 0)
        self.assertEqual(nif.storage_mode, "raw")
        compressed = next(row for row in rows if row.path.endswith("ValidBC2.nif"))
        self.assertEqual(compressed.storage_mode, "zlib")
        self.assertGreater(compressed.logical_size, compressed.stored_size)

    def test_all_bc_formats_and_mips_select_strictly(self) -> None:
        self.model.open_archive(self.archive)
        for name, format_name in (("ValidBC1.nif", "BC1"), ("ValidBC2.nif", "BC2"), ("ValidBC3.nif", "BC3")):
            selection = self.model.open_texture("Textures\\" + name, 2)
            self.assertEqual(selection.resource.pixel_format_name, format_name)
            views = self.model.mip_views()
            self.assertEqual((views.index, views.width, views.height), (2, 2, 1))
            self.assertEqual(len(views.rgb), 2 * 1 * 3)
            self.assertEqual(len(views.alpha), 2)
            self.assertEqual(len(views.rgba), 2 * 4)
            self.assertEqual(len(views.statistics), 4)
        with self.assertRaises(TextureViewerError):
            self.model.open_texture("Data\\Readme.txt")

    def test_failed_texture_open_preserves_previous_selection(self) -> None:
        self.model.open_archive(self.archive)
        first = self.model.open_texture("Textures\\ValidBC1.nif")
        with self.assertRaises(TextureViewerError):
            self.model.open_texture("Data\\Readme.txt")
        self.assertIs(self.model.selection.resource, first.resource)
        self.assertEqual(self.model.selection.entry.path, "Textures\\ValidBC1.nif")

    def test_failed_archive_open_preserves_previous_archive_and_reopen_clears(self) -> None:
        self.model.open_archive(self.archive)
        self.model.open_texture("Textures\\ValidBC1.nif")
        with self.assertRaises(TextureViewerError):
            self.model.open_archive(self.tmp / "missing.dv2")
        self.assertEqual(self.model.archive_path, self.archive.resolve())
        self.assertIsNotNone(self.model.selection)
        self.model.open_archive(self.archive)
        self.assertIsNone(self.model.selection)

    def test_oversized_payload_uses_explicit_read_limit(self) -> None:
        model = TextureViewerModel(max_payload_size=32)
        model.open_archive(self.archive)
        with self.assertRaises(TextureViewerError) as context:
            model.open_texture("Textures\\ValidBC1.nif")
        self.assertIn("exceeds limit", str(context.exception))
        self.assertGreater(MAX_TEXTURE_PAYLOAD_SIZE, 32)

    def test_archive_hash_is_unchanged_after_read_and_close(self) -> None:
        original = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.model.open_archive(self.archive)
        self.model.open_texture("Textures\\ValidBC3.nif", 1)
        self.model.close_texture()
        self.model.close_archive()
        self.assertEqual(hashlib.sha256(self.archive.read_bytes()).hexdigest(), original)

    def test_scan_compatible_textures_filters_structural_candidates_and_reports_progress(self) -> None:
        valid1 = _make_texture(bc.FORMAT_BC1)
        valid2 = _make_texture(bc.FORMAT_BC2)
        valid3 = _make_texture(bc.FORMAT_BC3)
        archive = _make_archive(
            self.tmp,
            [
                ("Textures\\Valid.NIF", valid1, "raw"),
                ("Textures\\Valid2.nif", valid2, "zlib"),
                ("nested\\Valid3.NiF", valid3, "raw"),
                ("Data\\Bad.NIF", b"bad", "raw"),
                ("Data\\Header.NIF", b"Gamebryo File Format, Version 20.3.0.9", "raw"),
                ("Data\\Ignored.item", valid1, "raw"),
            ],
            "mixed.dv2",
        )
        model = TextureViewerModel()
        model.open_archive(archive)
        selected = model.open_texture("Textures\\Valid.NIF")
        events: list[dict[str, object]] = []
        stats = model.scan_compatible_textures(progress=lambda event: events.append(dict(event)))
        self.assertEqual(stats["candidates"], 5)
        self.assertEqual(stats["processed"], 5)
        self.assertEqual(stats["compatible"], 3)
        self.assertEqual(stats["rejected"], 2)
        self.assertEqual(events[0]["processed"], 0)
        self.assertEqual(events[-1]["processed"], 5)
        self.assertTrue(all("candidates" in event and "elapsed_seconds" in event for event in events))
        self.assertIs(model.selection, selected)
        compatible = model.list_entries(compatible_only=True)
        self.assertEqual(
            {row.path for row in compatible},
            {"Textures\\Valid.NIF", "Textures\\Valid2.nif", "nested\\Valid3.NiF"},
        )
        self.assertEqual(
            [row.path for row in model.list_entries(compatible_only=True, limit=1)],
            ["Textures\\Valid.NIF"],
        )

    def test_scan_compatible_textures_rejects_oversized_payload_and_caches(self) -> None:
        valid = _make_texture(bc.FORMAT_BC1)
        archive = _make_archive(
            self.tmp,
            [
                ("Textures\\Valid.nif", valid, "raw"),
                ("Textures\\TooLarge.NIF", b"x" * (len(valid) + 1), "raw"),
            ],
            "limited.dv2",
        )
        model = TextureViewerModel(max_payload_size=len(valid))
        model.open_archive(archive)
        stats = model.scan_compatible_textures()
        self.assertEqual(stats["compatible"], 1)
        self.assertEqual(stats["rejected"], 1)
        session = model.session
        assert session is not None
        with mock.patch.object(
            session, "read_entry_bytes", side_effect=AssertionError("cache reread")
        ):
            cached_events: list[dict[str, object]] = []
            self.assertEqual(
                model.scan_compatible_textures(progress=cached_events.append), stats
            )
        self.assertEqual(len(cached_events), 1)
        self.assertEqual(
            [row.path for row in model.list_entries(compatible_only=True)],
            ["Textures\\Valid.nif"],
        )

    def test_scan_cache_resets_on_successful_reopen_and_close(self) -> None:
        self.model.open_archive(self.archive)
        self.model.scan_compatible_textures()
        self.model.open_archive(self.archive)
        with self.assertRaises(TextureViewerError):
            self.model.list_entries(compatible_only=True)
        self.model.close_archive()
        with self.assertRaises(TextureViewerError):
            self.model.scan_compatible_textures()

    def test_empty_scan_emits_initial_and_final_progress_and_no_partial_cache(self) -> None:
        empty = _make_archive(self.tmp, [("Data\\Readme.txt", b"text", "raw")], "empty.dv2")
        model = TextureViewerModel()
        model.open_archive(empty)
        events: list[dict[str, object]] = []
        stats = model.scan_compatible_textures(progress=events.append)
        self.assertEqual(stats["candidates"], 0)
        self.assertEqual(stats["processed"], 0)
        self.assertEqual(stats["compatible"], 0)
        self.assertEqual(stats["rejected"], 0)
        self.assertEqual(len(events), 2)
        self.assertEqual(model.list_entries(compatible_only=True), ())

        failing = TextureViewerModel()
        failing.open_archive(self.archive)
        session = failing.session
        assert session is not None
        with mock.patch.object(session, "read_entry_bytes", side_effect=MemoryError("stop")):
            with self.assertRaises(MemoryError):
                failing.scan_compatible_textures()
        with self.assertRaises(TextureViewerError):
            failing.list_entries(compatible_only=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
