# SPDX-FileCopyrightText: 2026 PmNz8
# SPDX-License-Identifier: AGPL-3.0-only
"""Strict parser for Divinity II texture NIF wrappers.

This module parses the complete Gamebryo NIF envelope before looking at the
single ``NiPersistentSrcTextureRendererData`` block used by the texture
corpus.  It keeps every byte range explicit and fails closed on malformed
tables, truncation, gaps, overlaps, or trailing data.  Its serializer only
allows fixed-size replacement of already parsed compressed mip ranges.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
import hashlib
import struct

from . import bc


EXPECTED_NIF_VERSION = 0x14030009
EXPECTED_NIF_USER_VERSION = 0x00030000
# Standalone texture wrappers occur with both the legacy zero user version
# and the canonical 0x00030000 variant.  Other container versions may be
# accepted by a structural corpus probe, but not by this strict parser.
SUPPORTED_TEXTURE_USER_VERSIONS: frozenset[int] = frozenset(
    {0x00000000, EXPECTED_NIF_USER_VERSION}
)
EXPECTED_NIF_HEADER_LINE = "Gamebryo File Format, Version 20.3.0.9"
EXPECTED_NIF_ENDIAN = 1
TEXTURE_BLOCK_TYPE = "NiPersistentSrcTextureRendererData"

MAX_NIF_COUNT = 1_000_000
MAX_NIF_STRING_BYTES = 1 << 20
MAX_MIP_COUNT = 32
TEXTURE_DESCRIPTOR_BYTES = 59
TEXTURE_RESERVED_BYTES = 7
TEXTURE_HEADER_BYTES = 16


class NIFTextureError(ValueError):
    """Raised when a texture NIF violates the supported byte contract."""


class _Reader:
    __slots__ = ("data", "label", "position")

    def __init__(self, data: bytes, label: str) -> None:
        self.data = data
        self.label = label
        self.position = 0

    @property
    def remaining(self) -> int:
        return len(self.data) - self.position

    def read(self, size: int, what: str) -> bytes:
        if size < 0 or size > self.remaining:
            raise NIFTextureError(
                f"{self.label}: truncated {what}; need {size} bytes, "
                f"have {self.remaining} at 0x{self.position:X}"
            )
        start = self.position
        self.position += size
        return self.data[start : start + size]

    def u8(self, what: str) -> int:
        return self.read(1, what)[0]

    def u16(self, what: str) -> int:
        return struct.unpack("<H", self.read(2, what))[0]

    def u32(self, what: str) -> int:
        return struct.unpack("<I", self.read(4, what))[0]

    def i32(self, what: str) -> int:
        return struct.unpack("<i", self.read(4, what))[0]

    def count(self, value: int, what: str) -> int:
        if value > MAX_NIF_COUNT:
            raise NIFTextureError(f"{self.label}: implausible {what} {value}")
        return value

    def sized_string(self, what: str) -> str:
        length = self.u32(f"{what} length")
        if length > MAX_NIF_STRING_BYTES:
            raise NIFTextureError(f"{self.label}: {what} is too large ({length} bytes)")
        raw = self.read(length, what)
        try:
            return raw.decode("utf-8", "strict")
        except UnicodeDecodeError as error:
            raise NIFTextureError(f"{self.label}: {what} is not valid UTF-8") from error


@dataclass(frozen=True, slots=True)
class TextureMip:
    """One validated BC mip entry.

    ``offset`` is relative to the 16-byte in-band texture header.  The
    absolute fields are offsets in the original NIF payload and are useful to
    callers that need to inspect exact byte ranges without re-deriving them.
    """

    index: int
    width: int
    height: int
    offset: int
    size: int
    absolute_offset: int

    @property
    def end_offset(self) -> int:
        return self.absolute_offset + self.size


@dataclass(frozen=True, slots=True)
class TextureResource:
    """Immutable, fully parsed texture wrapper and its source payload."""

    label: str
    payload_size: int
    payload_sha256: str
    header_line: str
    nif_version: int
    endian: int
    user_version: int
    block_count: int
    block_types: tuple[str, ...]
    type_indices: tuple[int, ...]
    block_sizes: tuple[int, ...]
    global_strings: tuple[str, ...]
    maximum_string_length: int
    groups: tuple[int, ...]
    roots: tuple[int, ...]
    block_offset: int
    block_size: int
    pixel_format: int
    descriptor: bytes
    mip_count: int
    mips: tuple[TextureMip, ...]
    texture_header_offset: int
    texture_header: bytes
    total_bytes: int
    _payload: bytes = field(repr=False, compare=False)

    @property
    def block_end(self) -> int:
        """Absolute end of the renderer block, i.e. the NIF footer offset."""

        return self.block_offset + self.block_size

    @property
    def block_footer_offset(self) -> int:
        """Absolute offset where the NIF root-count footer begins."""

        return self.block_end

    @property
    def texture_body_offset(self) -> int:
        """Absolute offset of the renderer block body."""

        return self.block_offset

    @property
    def bc_data_offset(self) -> int:
        """Absolute offset of the first BC byte, after the texture header."""

        return self.texture_header_offset + TEXTURE_HEADER_BYTES

    @property
    def pixel_format_name(self) -> str:
        return {
            bc.FORMAT_BC1: "BC1",
            bc.FORMAT_BC2: "BC2",
            bc.FORMAT_BC3: "BC3",
        }[self.pixel_format]

    def mip_bytes(self, mip_index: int) -> bytes:
        """Return exactly one compressed mip's bytes, without padding."""

        if isinstance(mip_index, bool) or not isinstance(mip_index, int):
            raise NIFTextureError("mip index must be an integer")
        if not 0 <= mip_index < self.mip_count:
            raise NIFTextureError(
                f"{self.label}: mip index {mip_index} out of range "
                f"(0..{self.mip_count - 1})"
            )
        mip = self.mips[mip_index]
        result = self._payload[mip.absolute_offset : mip.end_offset]
        if len(result) != mip.size:
            # This cannot occur after a successful parse unless an internal
            # invariant is broken; retain the fail-closed API boundary.
            raise NIFTextureError(f"{self.label}: mip {mip_index} range is truncated")
        return result

    def decode_mip(self, mip_index: int = 0) -> bytes:
        """Decode one validated mip to tightly packed RGBA8 bytes."""

        if isinstance(mip_index, bool) or not isinstance(mip_index, int):
            raise NIFTextureError("mip index must be an integer")
        if not 0 <= mip_index < self.mip_count:
            raise NIFTextureError(
                f"{self.label}: mip index {mip_index} out of range "
                f"(0..{self.mip_count - 1})"
            )
        mip = self.mips[mip_index]
        try:
            return bc.decode_mip(self.mip_bytes(mip_index), mip.width, mip.height, self.pixel_format)
        except bc.BCCodecError as error:
            raise NIFTextureError(f"{self.label}: cannot decode mip {mip_index}: {error}") from error


def _as_payload(payload: bytes | bytearray | memoryview, label: str) -> bytes:
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise NIFTextureError(f"{label}: payload must be bytes-like")
    try:
        return bytes(payload)
    except (TypeError, ValueError) as error:
        raise NIFTextureError(f"{label}: payload must be a contiguous bytes-like buffer") from error


def _parse_header(reader: _Reader, payload: bytes) -> str:
    newline = payload.find(b"\n", 0, 1025)
    if newline < 0:
        raise NIFTextureError(f"{reader.label}: missing NIF header line")
    raw = reader.read(newline + 1, "NIF header line")[:-1]
    if raw.endswith(b"\r"):
        raw = raw[:-1]
    try:
        header_line = raw.decode("ascii", "strict")
    except UnicodeDecodeError as error:
        raise NIFTextureError(f"{reader.label}: NIF header is not ASCII") from error
    if header_line != EXPECTED_NIF_HEADER_LINE:
        raise NIFTextureError(
            f"{reader.label}: unsupported NIF header {header_line!r}; "
            f"expected {EXPECTED_NIF_HEADER_LINE!r}"
        )
    return header_line


def _parse_nif_envelope(payload: bytes, label: str) -> tuple[
    str,
    int,
    int,
    int,
    tuple[str, ...],
    tuple[int, ...],
    tuple[int, ...],
    tuple[str, ...],
    int,
    tuple[int, ...],
    tuple[int, ...],
    int,
    int,
]:
    reader = _Reader(payload, label)
    header_line = _parse_header(reader, payload)
    version = reader.u32("NIF version")
    if version != EXPECTED_NIF_VERSION:
        raise NIFTextureError(
            f"{label}: unsupported NIF version 0x{version:08X}; "
            f"expected 0x{EXPECTED_NIF_VERSION:08X}"
        )
    endian = reader.u8("NIF endian marker")
    if endian != EXPECTED_NIF_ENDIAN:
        raise NIFTextureError(f"{label}: unsupported endian marker {endian}")
    user_version = reader.u32("NIF user version")
    if user_version not in SUPPORTED_TEXTURE_USER_VERSIONS:
        raise NIFTextureError(
            f"{label}: unsupported NIF user version 0x{user_version:08X}; "
            "expected one of 0x00000000, 0x00030000"
        )

    block_count = reader.count(reader.u32("NIF block count"), "NIF block count")
    if block_count != 1:
        raise NIFTextureError(f"{label}: expected exactly one NIF block, found {block_count}")
    type_count = reader.count(reader.u16("NIF block type count"), "NIF block type count")
    if type_count == 0:
        raise NIFTextureError(f"{label}: NIF has no block types")
    block_types = tuple(
        reader.sized_string(f"NIF block type {index}") for index in range(type_count)
    )
    type_indices = tuple(
        reader.u16(f"NIF block type index {index}") for index in range(block_count)
    )
    for index, type_index in enumerate(type_indices):
        if type_index >= type_count:
            raise NIFTextureError(
                f"{label}: block {index} type index {type_index} is outside "
                f"the {type_count}-entry type table"
            )
    block_sizes = tuple(reader.u32(f"NIF block size {index}") for index in range(block_count))

    string_count = reader.count(reader.u32("NIF global string count"), "NIF global string count")
    maximum_string_length = reader.u32("NIF maximum string length")
    if maximum_string_length > MAX_NIF_STRING_BYTES:
        raise NIFTextureError(
            f"{label}: NIF maximum string length is implausible ({maximum_string_length})"
        )
    global_strings = tuple(
        reader.sized_string(f"NIF global string {index}") for index in range(string_count)
    )
    for index, value in enumerate(global_strings):
        encoded_length = len(value.encode("utf-8"))
        if encoded_length > maximum_string_length:
            raise NIFTextureError(
                f"{label}: global string {index} exceeds declared maximum string length"
            )

    group_count = reader.count(reader.u32("NIF group count"), "NIF group count")
    groups = tuple(reader.u32(f"NIF group {index}") for index in range(group_count))

    block_offset = reader.position
    block_size = block_sizes[0]
    reader.read(block_size, "NIF renderer block")
    block_end = reader.position

    root_count = reader.count(reader.u32("NIF root count"), "NIF root count")
    roots = tuple(reader.i32(f"NIF root {index}") for index in range(root_count))
    if reader.remaining:
        raise NIFTextureError(
            f"{label}: {reader.remaining} trailing bytes after NIF root footer"
        )
    for root in roots:
        if root < -1 or root >= block_count:
            raise NIFTextureError(f"{label}: NIF root index {root} is out of range")

    return (
        header_line,
        version,
        endian,
        user_version,
        block_types,
        type_indices,
        block_sizes,
        global_strings,
        maximum_string_length,
        groups,
        roots,
        block_offset,
        block_end,
    )


def parse_texture_resource(
    payload: bytes | bytearray | memoryview, label: str = "texture"
) -> TextureResource:
    """Parse and validate one complete texture NIF payload.

    The source is copied to immutable ``bytes``.  The only accepted renderer
    block is a single ``NiPersistentSrcTextureRendererData`` block.  Texture
    mip offsets are relative to the 16-byte in-band header and must be exactly
    contiguous.
    """

    raw = _as_payload(payload, label)
    (
        header_line,
        version,
        endian,
        user_version,
        block_types,
        type_indices,
        block_sizes,
        global_strings,
        maximum_string_length,
        groups,
        roots,
        block_offset,
        block_end,
    ) = _parse_nif_envelope(raw, label)
    type_name = block_types[type_indices[0]]
    if type_name != TEXTURE_BLOCK_TYPE:
        raise NIFTextureError(
            f"{label}: NIF block type is {type_name!r}; expected {TEXTURE_BLOCK_TYPE!r}"
        )

    block_size = block_end - block_offset
    body = raw[block_offset:block_end]
    minimum = 4 + TEXTURE_DESCRIPTOR_BYTES + 1 + TEXTURE_RESERVED_BYTES + 12 + TEXTURE_HEADER_BYTES
    if len(body) < minimum:
        raise NIFTextureError(
            f"{label}: renderer block is too small ({len(body)} bytes; need at least {minimum})"
        )

    pixel_format = struct.unpack_from("<I", body, 0)[0]
    try:
        # Also checks the format and the compressed-size limit for every mip.
        bc.block_size(pixel_format)
    except bc.BCCodecError as error:
        raise NIFTextureError(f"{label}: unsupported texture pixel format {pixel_format}") from error

    descriptor = body[4 : 4 + TEXTURE_DESCRIPTOR_BYTES]
    mip_count = body[4 + TEXTURE_DESCRIPTOR_BYTES]
    if mip_count < 1 or mip_count > MAX_MIP_COUNT:
        raise NIFTextureError(f"{label}: invalid mip count {mip_count}")

    reserved_start = 4 + TEXTURE_DESCRIPTOR_BYTES + 1
    reserved_end = reserved_start + TEXTURE_RESERVED_BYTES
    if any(body[reserved_start:reserved_end]):
        raise NIFTextureError(f"{label}: reserved texture bytes are not all zero")

    table_start = reserved_end
    table_end = table_start + mip_count * 12
    if table_end + TEXTURE_HEADER_BYTES > len(body):
        raise NIFTextureError(f"{label}: mip table or texture header overruns renderer block")

    parsed: list[tuple[int, int, int, int]] = []
    expected_offset = 0
    previous_width: int | None = None
    previous_height: int | None = None
    for index in range(mip_count):
        width, height, offset = struct.unpack_from("<III", body, table_start + index * 12)
        try:
            size = bc.mip_size(width, height, pixel_format)
        except bc.BCCodecError as error:
            raise NIFTextureError(
                f"{label}: invalid dimensions or compressed size for mip {index} "
                f"({width}x{height})"
            ) from error
        if index == 0:
            if offset != 0:
                raise NIFTextureError(f"{label}: first mip offset is {offset}, expected zero")
        else:
            assert previous_width is not None and previous_height is not None
            expected_width = max(1, previous_width // 2)
            expected_height = max(1, previous_height // 2)
            if (width, height) != (expected_width, expected_height):
                raise NIFTextureError(
                    f"{label}: mip {index} dimensions are {width}x{height}; "
                    f"expected {expected_width}x{expected_height}"
                )
        if offset != expected_offset:
            relation = "gap or overlap" if offset > expected_offset else "overlap or backward offset"
            raise NIFTextureError(
                f"{label}: mip {index} offset {offset} is not contiguous; "
                f"expected {expected_offset} ({relation})"
            )
        parsed.append((width, height, offset, size))
        expected_offset += size
        previous_width, previous_height = width, height

    texture_header_offset = block_offset + table_end
    total_bytes, header_total, header_word_2, header_word_3 = struct.unpack_from(
        "<4I", body, table_end
    )
    if (total_bytes, header_total, header_word_2, header_word_3) != (
        expected_offset,
        expected_offset,
        1,
        3,
    ):
        raise NIFTextureError(
            f"{label}: texture header is {total_bytes},{header_total},{header_word_2},{header_word_3}; "
            f"expected {expected_offset},{expected_offset},1,3"
        )
    expected_block_size = table_end + TEXTURE_HEADER_BYTES + expected_offset
    if block_size != expected_block_size:
        relation = "under-consumed" if block_size < expected_block_size else "trailing bytes"
        raise NIFTextureError(
            f"{label}: renderer block size {block_size} does not end after texture data "
            f"({expected_block_size}); {relation}"
        )

    bc_start = texture_header_offset + TEXTURE_HEADER_BYTES
    mips = tuple(
        TextureMip(
            index=index,
            width=width,
            height=height,
            offset=offset,
            size=size,
            absolute_offset=bc_start + offset,
        )
        for index, (width, height, offset, size) in enumerate(parsed)
    )
    texture_header = bytes(body[table_end : table_end + TEXTURE_HEADER_BYTES])
    return TextureResource(
        label=label,
        payload_size=len(raw),
        payload_sha256=hashlib.sha256(raw).hexdigest(),
        header_line=header_line,
        nif_version=version,
        endian=endian,
        user_version=user_version,
        block_count=1,
        block_types=block_types,
        type_indices=type_indices,
        block_sizes=block_sizes,
        global_strings=global_strings,
        maximum_string_length=maximum_string_length,
        groups=groups,
        roots=roots,
        block_offset=block_offset,
        block_size=block_size,
        pixel_format=pixel_format,
        descriptor=bytes(descriptor),
        mip_count=mip_count,
        mips=mips,
        texture_header_offset=texture_header_offset,
        texture_header=texture_header,
        total_bytes=total_bytes,
        _payload=raw,
    )


def _texture_resource_signature(resource: TextureResource) -> tuple[tuple[str, object], ...]:
    """Return every parsed field except the source hash and private payload."""

    return tuple(
        (item.name, getattr(resource, item.name))
        for item in fields(TextureResource)
        if item.name not in {"payload_sha256", "_payload"}
    )


def _validate_expected_source_sha256(
    expected_source_sha256: str | None, actual_source_sha256: str, label: str
) -> None:
    if expected_source_sha256 is None:
        return
    if (
        not isinstance(expected_source_sha256, str)
        or len(expected_source_sha256) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in expected_source_sha256)
    ):
        raise NIFTextureError(
            f"{label}: expected_source_sha256 must be a 64-character hexadecimal SHA-256"
        )
    if expected_source_sha256.casefold() != actual_source_sha256:
        raise NIFTextureError(f"{label}: expected_source_sha256 does not match source payload")


def _normalize_mip_replacements(
    resource: TextureResource,
    mip_replacements: Mapping[int, bytes | bytearray | memoryview] | None,
) -> dict[int, bytes]:
    if mip_replacements is None:
        return {}
    if not isinstance(mip_replacements, Mapping):
        raise NIFTextureError(f"{resource.label}: mip_replacements must be a mapping")

    replacements: dict[int, bytes] = {}
    try:
        items = mip_replacements.items()
        for index, replacement in items:
            if isinstance(index, bool) or not isinstance(index, int):
                raise NIFTextureError(f"{resource.label}: mip replacement index must be an integer")
            if not 0 <= index < resource.mip_count:
                raise NIFTextureError(
                    f"{resource.label}: mip replacement index {index} out of range "
                    f"(0..{resource.mip_count - 1})"
                )
            if index in replacements:
                raise NIFTextureError(
                    f"{resource.label}: duplicate mip replacement index {index}"
                )
            if not isinstance(replacement, (bytes, bytearray, memoryview)):
                raise NIFTextureError(
                    f"{resource.label}: mip replacement {index} must be bytes-like"
                )
            try:
                replacement_bytes = bytes(replacement)
            except (TypeError, ValueError) as error:
                raise NIFTextureError(
                    f"{resource.label}: mip replacement {index} must be a contiguous bytes-like buffer"
                ) from error
            mip = resource.mips[index]
            if len(replacement_bytes) != mip.size:
                raise NIFTextureError(
                    f"{resource.label}: mip replacement {index} has {len(replacement_bytes)} bytes; "
                    f"expected exactly {mip.size}"
                )
            replacements[index] = replacement_bytes
    except NIFTextureError:
        raise
    except (TypeError, ValueError, RuntimeError) as error:
        raise NIFTextureError(
            f"{resource.label}: cannot iterate mip replacements mapping"
        ) from error
    return replacements


def _assert_unchanged_outside_ranges(
    source: bytes, output: bytes, ranges: tuple[tuple[int, int], ...], label: str
) -> None:
    if len(output) != len(source):
        raise NIFTextureError(f"{label}: serialized payload size changed internally")
    cursor = 0
    for start, end in ranges:
        if start < cursor or end < start or end > len(source):
            raise NIFTextureError(f"{label}: serialized mip range is internally invalid")
        if source[cursor:start] != output[cursor:start]:
            raise NIFTextureError(
                f"{label}: bytes outside replacement mip ranges changed internally"
            )
        cursor = end
    if source[cursor:] != output[cursor:]:
        raise NIFTextureError(
            f"{label}: bytes outside replacement mip ranges changed internally"
        )


def serialize_texture_resource(
    resource: TextureResource,
    mip_replacements: Mapping[int, bytes | bytearray | memoryview] | None = None,
    *,
    expected_source_sha256: str | None = None,
) -> bytes:
    """Serialize a parsed texture, replacing only fixed-size compressed mip ranges.

    The source object is verified against a fresh parse before any splice is
    attempted.  Replacements cannot change the wrapper layout, dimensions,
    format, or mip count; every generated payload is parsed again and checked
    byte-for-byte outside the explicitly replaced ranges.
    """

    if not isinstance(resource, TextureResource):
        raise NIFTextureError("resource must be a parsed TextureResource")
    source = resource._payload
    if not isinstance(source, bytes):
        raise NIFTextureError(f"{resource.label}: private source payload must be immutable bytes")

    actual_source_sha256 = hashlib.sha256(source).hexdigest()
    _validate_expected_source_sha256(
        expected_source_sha256, actual_source_sha256, resource.label
    )
    try:
        reparsed_source = parse_texture_resource(source, resource.label)
    except NIFTextureError as error:
        raise NIFTextureError(
            f"{resource.label}: private source payload failed revalidation: {error}"
        ) from error
    if reparsed_source != resource:
        raise NIFTextureError(
            f"{resource.label}: resource metadata does not match private source payload"
        )
    if resource.payload_size != len(source) or resource.payload_sha256 != actual_source_sha256:
        raise NIFTextureError(
            f"{resource.label}: resource payload size or SHA-256 metadata is inconsistent"
        )

    replacements = _normalize_mip_replacements(resource, mip_replacements)
    if not replacements:
        return source

    output = bytearray(source)
    ranges: list[tuple[int, int]] = []
    for index in sorted(replacements):
        mip = resource.mips[index]
        start = mip.absolute_offset
        end = start + mip.size
        if start < 0 or end > len(output) or end < start:
            raise NIFTextureError(
                f"{resource.label}: mip {index} range is internally invalid"
            )
        output[start:end] = replacements[index]
        if output[start:end] != replacements[index]:
            raise NIFTextureError(
                f"{resource.label}: mip {index} replacement was not applied exactly"
            )
        ranges.append((start, end))

    serialized = bytes(output)
    try:
        reparsed_output = parse_texture_resource(serialized, resource.label)
    except NIFTextureError as error:
        raise NIFTextureError(
            f"{resource.label}: serialized output failed validation: {error}"
        ) from error
    if len(serialized) != len(source):
        raise NIFTextureError(f"{resource.label}: serialized payload size changed internally")
    if _texture_resource_signature(reparsed_output) != _texture_resource_signature(resource):
        raise NIFTextureError(
            f"{resource.label}: serialized output changed texture layout metadata"
        )
    _assert_unchanged_outside_ranges(
        source, serialized, tuple(sorted(ranges)), resource.label
    )
    return serialized


def mip_bytes(resource: TextureResource, mip_index: int = 0) -> bytes:
    """Functional counterpart to :meth:`TextureResource.mip_bytes`."""

    return resource.mip_bytes(mip_index)


def decode_texture_mip(resource: TextureResource, mip_index: int = 0) -> bytes:
    """Functional counterpart to :meth:`TextureResource.decode_mip`."""

    return resource.decode_mip(mip_index)


__all__ = [
    "EXPECTED_NIF_ENDIAN",
    "EXPECTED_NIF_HEADER_LINE",
    "EXPECTED_NIF_USER_VERSION",
    "EXPECTED_NIF_VERSION",
    "NIFTextureError",
    "SUPPORTED_TEXTURE_USER_VERSIONS",
    "TEXTURE_BLOCK_TYPE",
    "TEXTURE_HEADER_BYTES",
    "TextureMip",
    "TextureResource",
    "decode_texture_mip",
    "mip_bytes",
    "parse_texture_resource",
    "serialize_texture_resource",
]
