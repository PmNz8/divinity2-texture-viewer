# SPDX-FileCopyrightText: 2026 PmNz8
# SPDX-License-Identifier: AGPL-3.0-only
#!/usr/bin/env python3
"""Independently implemented DV2 backend, shared with the original tooling.

The Viewer exposes only archive reads and separate texture exports. Internal
rebuild services are retained as shared code, not exposed as GUI operations.
Passing structural verification does not establish game-runtime acceptance.
"""

from __future__ import annotations

import hashlib
import os
import stat
import struct
import tempfile
import time
import zlib
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import BinaryIO, Callable, Iterable, Sequence

HEADER = struct.Struct("<IIIBBII")
ENTRY = struct.Struct("<III")
EXPECTED_HEADER_SIZE = 22
BLOCK_SIZE = 0x8000
IO_CHUNK = 1024 * 1024
ZLIB_LEVEL = 9
STORAGE_MODES = ("raw", "zlib")
MAX_FIELD32 = 0xFFFFFFFF

# Conservative Windows reserved device names (stored lowercase; compared via
# casefold against each path segment and its extension-less stem).
_RESERVED_NAMES = frozenset(
    ["con", "prn", "aux", "nul"]
    + [f"com{i}" for i in range(1, 10)]
    + [f"lpt{i}" for i in range(1, 10)]
)


class DV2Error(Exception):
    """Raised for a malformed/unsupported DV2 archive or an invalid edit."""


# ---------------------------------------------------------------------------
# Header / entry model (layout facts inherited from the reference parser)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DV2Header:
    version: int
    unknown_04: int
    unknown_08: int
    layout_mode: int
    compression_mode: int
    data_offset: int
    path_table_size: int


@dataclass(frozen=True)
class DV2Entry:
    path: str
    relative_offset: int
    packed_size: int
    unpacked_size: int

    @property
    def key(self) -> str:
        """Casefolded identity key used for case-insensitive lookups."""
        return self.path.casefold()

    @property
    def is_compressed(self) -> bool:
        # OBSERVATION (reference parser): unpacked_size == 0 marks a raw payload.
        return self.unpacked_size != 0

    @property
    def storage_mode(self) -> str:
        return "zlib" if self.is_compressed else "raw"

    @property
    def stored_size(self) -> int:
        return self.packed_size

    @property
    def logical_size(self) -> int:
        return self.unpacked_size if self.is_compressed else self.packed_size


def read_exact(stream: BinaryIO, size: int, label: str) -> bytes:
    data = stream.read(size)
    if len(data) != size:
        raise DV2Error(f"truncated {label}: got {len(data)} of {size} bytes")
    return data


def align_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


# ---------------------------------------------------------------------------
# Path handling (Windows-style internal paths, conservative safety policy)
# ---------------------------------------------------------------------------


def normalize_archive_path(raw: str) -> str:
    """Normalize an internal archive path to Windows form and reject unsafe input.

    Accepts ``/`` or ``\\`` separators; returns the backslash-separated form.
    Rejects absolute paths, drives/UNC, ``..``, ``.``, empty segments, colons,
    non-ASCII characters and Windows reserved device names.
    """
    if not isinstance(raw, str):
        raise DV2Error(f"archive path must be a string, got {type(raw).__name__}")
    if not raw.strip():
        raise DV2Error("archive path is empty")
    candidate = raw.replace("/", "\\")
    windows_path = PureWindowsPath(candidate)
    if windows_path.is_absolute() or windows_path.drive or windows_path.root:
        raise DV2Error(f"absolute path is forbidden in archive: {raw!r}")
    if any(":" in part for part in windows_path.parts):
        raise DV2Error(f"colon is forbidden in archive path: {raw!r}")
    parts = [part for part in candidate.split("\\")]
    if any(part in ("", ".", "..") for part in parts):
        raise DV2Error(f"unsafe relative path in archive: {raw!r}")
    for part in parts:
        try:
            part.encode("ascii")
        except UnicodeEncodeError as error:
            raise DV2Error(f"DV2 paths must be ASCII: {raw!r}") from error
        if part[-1] in (" ", "."):
            raise DV2Error(
                f"segment ending with space/dot is rejected by conservative policy: {raw!r}"
            )
        segment_stem = part.split(".", 1)[0]
        if part.casefold() in _RESERVED_NAMES or segment_stem.casefold() in _RESERVED_NAMES:
            raise DV2Error(f"Windows reserved device name in path: {raw!r}")
    normalized = "\\".join(parts)
    try:
        normalized.encode("ascii")
    except UnicodeEncodeError as error:  # pragma: no cover - defensive
        raise DV2Error(f"DV2 paths must be ASCII: {raw!r}") from error
    return normalized


# ---------------------------------------------------------------------------
# Streaming helpers
# ---------------------------------------------------------------------------


def hash_and_size_file(path: Path, progress: Callable[[int], None] | None = None) -> tuple[str, int]:
    """Stream a file once; return (sha256_hex, size)."""
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as stream:
        while True:
            chunk = stream.read(IO_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
            if progress is not None:
                progress(len(chunk))
    return digest.hexdigest(), size


class PayloadSource:
    """A new payload supplied either as bytes or as a local file path."""

    def __init__(self, source: bytes | bytearray | memoryview | str | os.PathLike):
        if isinstance(source, (bytes, bytearray, memoryview)):
            self.data: bytes | None = bytes(source)
            self.path: Path | None = None
            self.size = len(self.data)
        else:
            path = Path(source)
            if not path.is_file():
                raise DV2Error(f"payload source does not exist or is not a file: {path}")
            self.data = None
            self.path = path
            self.size = path.stat().st_size

    def describe(self) -> str:
        return f"<{self.size} bytes inline>" if self.data is not None else str(self.path)

    def stream(self) -> Iterable[bytes]:
        if self.data is not None:
            view = memoryview(self.data)
            for start in range(0, len(view), IO_CHUNK):
                yield bytes(view[start : start + IO_CHUNK])
            return
        with self.path.open("rb") as stream:  # pragma: no branch - trivial loop
            while True:
                chunk = stream.read(IO_CHUNK)
                if not chunk:
                    break
                yield chunk


class HashSink:
    """Minimal sink feeding a SHA-256 digest."""

    def __init__(self) -> None:
        self.digest = hashlib.sha256()

    def write(self, data: bytes) -> int:
        self.digest.update(data)
        return len(data)

    @property
    def hexdigest(self) -> str:
        return self.digest.hexdigest()


def write_zeros(stream: BinaryIO, size: int) -> None:
    if size <= 0:
        return
    zero_chunk = bytes(min(IO_CHUNK, size))
    remaining = size
    while remaining:
        amount = min(len(zero_chunk), remaining)
        stream.write(zero_chunk[:amount])
        remaining -= amount


# ---------------------------------------------------------------------------
# Archive reading / validation (parsing core from the reference tool)
# ---------------------------------------------------------------------------


def decode_paths(raw_table: bytes) -> tuple[str, ...]:
    if raw_table and not raw_table.endswith(b"\0"):
        raise DV2Error("path table is not NUL-terminated")
    raw_paths = raw_table[:-1].split(b"\0") if raw_table else []
    paths: list[str] = []
    for index, raw_path in enumerate(raw_paths):
        if not raw_path:
            raise DV2Error(f"empty path at index {index}")
        try:
            path = raw_path.decode("ascii")
        except UnicodeDecodeError as error:
            raise DV2Error(f"non-ASCII path at index {index}") from error
        # Re-validate existing entries through the same safety policy.
        normalize_archive_path(path)
        paths.append(path)
    return tuple(paths)


def _read_raw_table_and_records(
    path: Path,
) -> tuple[DV2Header, bytes, int, tuple[tuple[int, int, int], ...]]:
    """Read the raw path table and entry records without normalising bytes.

    The public model intentionally exposes decoded, validated paths.  A
    compact no-change relayout also needs to preserve the original path-table
    bytes exactly, so this small helper keeps that representation available
    for the guarded rebuild and its post-write comparison.
    """
    with Path(path).open("rb") as stream:
        header = DV2Header(*HEADER.unpack(read_exact(stream, HEADER.size, "header")))
        raw_path_table = read_exact(stream, header.path_table_size, "path table")
        count = struct.unpack("<I", read_exact(stream, 4, "entry count"))[0]
        raw_records = read_exact(stream, count * ENTRY.size, "entry records")
    records = tuple(
        ENTRY.unpack_from(raw_records, index * ENTRY.size)
        for index in range(count)
    )
    return header, raw_path_table, count, records


def consume_entry(stream: BinaryIO, header: DV2Header, entry: DV2Entry, output: BinaryIO | None) -> int:
    """Decode one logical payload; optionally write it to ``output``."""
    if entry.logical_size == 0:
        return 0
    stream.seek(header.data_offset + entry.relative_offset)
    if not entry.is_compressed:
        remaining = entry.logical_size
        produced = 0
        while remaining:
            chunk = stream.read(min(IO_CHUNK, remaining))
            if not chunk:
                raise DV2Error(f"truncated raw payload for {entry.path!r}")
            remaining -= len(chunk)
            produced += len(chunk)
            if output is not None:
                output.write(chunk)
        return produced

    decoder = zlib.decompressobj()
    remaining = entry.packed_size
    produced = 0
    while remaining:
        chunk = stream.read(min(IO_CHUNK, remaining))
        if not chunk:
            raise DV2Error(f"truncated compressed stream for {entry.path!r}")
        remaining -= len(chunk)
        try:
            decoded = decoder.decompress(chunk)
        except zlib.error as error:
            raise DV2Error(f"invalid zlib stream for {entry.path!r}: {error}") from error
        produced += len(decoded)
        if output is not None:
            output.write(decoded)
    try:
        tail = decoder.flush()
    except zlib.error as error:
        raise DV2Error(f"cannot finish zlib stream for {entry.path!r}: {error}") from error
    produced += len(tail)
    if output is not None:
        output.write(tail)
    if not decoder.eof:
        raise DV2Error(f"incomplete zlib stream for {entry.path!r}")
    if decoder.unused_data or decoder.unconsumed_tail:
        raise DV2Error(f"unexpected trailing bytes inside stream for {entry.path!r}")
    return produced


def parse_archive(path: Path) -> tuple[int, DV2Header, tuple[DV2Entry, ...], int]:
    """Read and structurally validate an archive.

    Returns ``(file_size, header, entries, entry_table_end)``.
    """
    path = Path(path).resolve()
    try:
        file_size = path.stat().st_size
    except OSError as error:
        raise DV2Error(f"cannot stat archive: {error}") from error
    if file_size < EXPECTED_HEADER_SIZE:
        raise DV2Error(
            f"archive is only {file_size} bytes; header requires {EXPECTED_HEADER_SIZE}"
        )
    try:
        with path.open("rb") as stream:
            values = HEADER.unpack(read_exact(stream, HEADER.size, "header"))
            header = DV2Header(*values)
            if header.path_table_size > file_size - HEADER.size:
                raise DV2Error("path table extends beyond archive")
            paths = decode_paths(read_exact(stream, header.path_table_size, "path table"))
            count = struct.unpack("<I", read_exact(stream, 4, "entry count"))[0]
            if count != len(paths):
                raise DV2Error(f"entry count {count} does not match path count {len(paths)}")
            maximum_records = (file_size - stream.tell()) // ENTRY.size
            if count > maximum_records:
                raise DV2Error(f"entry count {count} cannot fit in archive")
            entries: list[DV2Entry] = []
            for index, path_string in enumerate(paths):
                relative_offset, packed_size, unpacked_size = ENTRY.unpack(
                    read_exact(stream, ENTRY.size, f"entry {index}")
                )
                entries.append(DV2Entry(path_string, relative_offset, packed_size, unpacked_size))
            entry_table_end = stream.tell()
    except OSError as error:
        raise DV2Error(f"cannot read archive: {error}") from error
    validate_structure(file_size, header, entries, entry_table_end)
    return file_size, header, tuple(entries), entry_table_end


def validate_structure(
    file_size: int, header: DV2Header, entries: Sequence[DV2Entry], entry_table_end: int
) -> None:
    if HEADER.size != EXPECTED_HEADER_SIZE:
        raise AssertionError("internal header-size mismatch")
    if header.data_offset < entry_table_end:
        raise DV2Error(
            f"declared data offset 0x{header.data_offset:X} overlaps entry table "
            f"ending at 0x{entry_table_end:X}"
        )
    if header.data_offset > file_size:
        raise DV2Error("declared data offset is beyond EOF")
    if header.layout_mode not in (0, 1):
        raise DV2Error(f"unsupported layout mode {header.layout_mode}")
    if header.compression_mode != 1:
        raise DV2Error(
            f"unsupported compression mode {header.compression_mode}; "
            "only observed zlib mode 1 is supported"
        )
    intervals: list[tuple[int, int, str]] = []
    seen_paths: set[str] = set()
    for entry in entries:
        if entry.key in seen_paths:
            raise DV2Error(f"duplicate archive path: {entry.path!r}")
        seen_paths.add(entry.key)
        normalize_archive_path(entry.path)
        start = header.data_offset + entry.relative_offset
        end = start + entry.stored_size
        if start < header.data_offset or end < start or end > file_size:
            raise DV2Error(
                f"payload outside archive for {entry.path!r}: "
                f"0x{start:X}..0x{end:X} of 0x{file_size:X}"
            )
        if not entry.packed_size and entry.unpacked_size:
            raise DV2Error(
                f"non-empty unpacked payload has zero stored length for {entry.path!r}"
            )
        # NOTE (layout-mode-0 variants): an unaligned relative_offset is NOT an
        # error. A runtime-accepted experimental artifact designated by the
        # repair task uses sequential, unaligned offsets in layout mode 0
        # (001_XMLs.dv2 variant, SHA-256 9E215E58C364E9DF288AA6CB3BC7F62E999CEE
        # 9C54A721290E43BB0688951FB7). Unalignment is reported as a variant
        # property via mode0_unaligned_paths(); it is never a rejection reason.
        if entry.stored_size:
            intervals.append((start, end, entry.path))
    intervals.sort()
    for previous, current in zip(intervals, intervals[1:]):
        if current[0] < previous[1]:
            raise DV2Error(
                f"overlapping payloads: {previous[2]!r} ends at 0x{previous[1]:X}, "
                f"{current[2]!r} begins at 0x{current[0]:X}"
            )


def canonical_data_offset(layout_mode: int, entry_table_end: int) -> int:
    """Data offset the writer produces for a given entry-table end."""
    return align_up(entry_table_end, BLOCK_SIZE) if layout_mode == 0 else entry_table_end


def mode0_unaligned_paths(header: DV2Header, entries: Sequence[DV2Entry]) -> tuple[str, ...]:
    """Paths of layout-mode-0 entries whose relative offsets are NOT 0x8000-aligned.

    Reported as a read-only variant property; never a rejection criterion.
    """
    if header.layout_mode != 0:
        return ()
    return tuple(entry.path for entry in entries if entry.relative_offset % BLOCK_SIZE)


# ---------------------------------------------------------------------------
# Pending edit operations
# ---------------------------------------------------------------------------

OP_ADD = "add"
OP_SET = "set"
OP_REMOVE = "remove"


@dataclass
class PendingOp:
    op_id: int
    kind: str  # OP_ADD | OP_SET | OP_REMOVE
    path: str  # normalized display path
    storage: str | None  # required for adds
    source: PayloadSource | None  # for adds and sets

    @property
    def key(self) -> str:
        return self.path.casefold()

    def describe(self) -> dict:
        document = {"op_id": self.op_id, "op": self.kind, "path": self.path}
        if self.storage is not None:
            document["storage"] = self.storage
        if self.source is not None:
            document["source"] = self.source.describe()
        return document


class DV2Session:
    """An open archive plus optional staged edits, saved to a NEW file only."""

    def __init__(self, archive_path: str | os.PathLike):
        self.path = Path(archive_path).resolve()
        self.file_size, self.header, self.entries, self.entry_table_end = parse_archive(self.path)
        self._by_key: dict[str, DV2Entry] = {}
        for entry in self.entries:
            if entry.key in self._by_key:  # pragma: no cover - validate_structure guards
                raise DV2Error(f"duplicate archive path: {entry.path!r}")
            self._by_key[entry.key] = entry
        self._pending: dict[str, PendingOp] = {}
        self._add_order: list[str] = []  # keys of pending adds, user order
        self._next_op_id = 1
        self._source_sha256: str | None = None

    # -- basic accessors ----------------------------------------------------

    @property
    def source_sha256(self) -> str:
        """SHA-256 of the source archive (computed once, cached)."""
        if self._source_sha256 is None:
            digest, size = hash_and_size_file(self.path)
            if size != self.file_size:
                raise DV2Error(
                    f"source changed while hashing: expected {self.file_size} bytes, read {size}"
                )
            self._source_sha256 = digest
        return self._source_sha256

    def find_entry(self, raw_path: str) -> DV2Entry:
        normalized = normalize_archive_path(raw_path)
        entry = self._by_key.get(normalized.casefold())
        if entry is None:
            raise DV2Error(f"archive has no entry {normalized!r}")
        return entry

    def list_entries(self) -> tuple[DV2Entry, ...]:
        return self.entries

    @property
    def unaligned_mode0_entries(self) -> tuple[str, ...]:
        """Layout-mode-0 entries with sequential (non-0x8000-aligned) offsets.

        Variant property only — such entries are fully accepted by the reader.
        """
        return mode0_unaligned_paths(self.header, self.entries)

    def read_entry_bytes(self, raw_path: str, maximum_size: int | None = None) -> bytes:
        entry = self.find_entry(raw_path)
        if maximum_size is not None and entry.logical_size > maximum_size:
            raise DV2Error(
                f"refusing to load {entry.path!r}: logical size {entry.logical_size} "
                f"exceeds limit {maximum_size}"
            )
        chunks: list[bytes] = []
        with self.path.open("rb") as stream:
            produced = consume_entry(stream, self.header, entry, _ListSink(chunks))
        if produced != entry.logical_size:
            raise DV2Error(
                f"logical size mismatch for {entry.path!r}: got {produced}, "
                f"expected {entry.logical_size}"
            )
        return b"".join(chunks)

    def extract_entry(self, raw_path: str, destination: str | os.PathLike) -> Path:
        """Extract one entry to a local file (atomic via temp + rename)."""
        entry = self.find_entry(raw_path)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".dsh-dv2tmp")
        try:
            with self.path.open("rb") as source, temporary.open("wb") as target:
                produced = consume_entry(source, self.header, entry, target)
                if produced != entry.logical_size:
                    raise DV2Error(
                        f"logical size mismatch for {entry.path!r}: got {produced}, "
                        f"expected {entry.logical_size}"
                    )
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, destination)
        except Exception:
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                pass
            raise
        return destination

    # -- staging API ---------------------------------------------------------

    def stage_add(self, raw_path: str, source: bytes | str | os.PathLike, storage: str) -> PendingOp:
        normalized = normalize_archive_path(raw_path)
        key = normalized.casefold()
        storage = _check_storage(storage)
        if key in self._by_key:
            raise DV2Error(
                f"cannot add {normalized!r}: an entry with this path already exists "
                "(paths are case-insensitive)"
            )
        if key in self._pending:
            raise DV2Error(f"duplicate pending operation for {normalized!r}")
        payload = PayloadSource(source)
        if storage == "zlib" and payload.size == 0:
            raise DV2Error(
                f"cannot add {normalized!r} as zlib: an empty payload cannot be represented "
                "in compressed mode (zero unpacked-size field is the raw-data sentinel); "
                "use storage='raw'"
            )
        if payload.size > MAX_FIELD32:
            raise DV2Error(f"payload for {normalized!r} exceeds the 32-bit DV2 size fields")
        operation = PendingOp(self._next_op_id, OP_ADD, normalized, storage, payload)
        self._next_op_id += 1
        self._pending[key] = operation
        self._add_order.append(key)
        return operation

    def stage_set(self, raw_path: str, source: bytes | str | os.PathLike) -> PendingOp:
        normalized = normalize_archive_path(raw_path)
        key = normalized.casefold()
        if key not in self._by_key:
            raise DV2Error(
                f"cannot replace {normalized!r}: no such entry in the opened archive"
            )
        if key in self._pending:
            raise DV2Error(f"duplicate pending operation for {normalized!r}")
        payload = PayloadSource(source)
        existing = self._by_key[key]
        if existing.is_compressed and payload.size == 0:
            raise DV2Error(
                f"cannot replace compressed entry {normalized!r} with an empty payload: "
                "the zero unpacked-size field would silently change its storage semantics"
            )
        if payload.size > MAX_FIELD32:
            raise DV2Error(f"payload for {normalized!r} exceeds the 32-bit DV2 size fields")
        # Report replacements/removals using the archive's canonical path text.
        operation = PendingOp(self._next_op_id, OP_SET, existing.path, None, payload)
        self._next_op_id += 1
        self._pending[key] = operation
        return operation

    def stage_remove(self, raw_path: str) -> PendingOp:
        normalized = normalize_archive_path(raw_path)
        key = normalized.casefold()
        if key not in self._by_key:
            raise DV2Error(
                f"cannot remove {normalized!r}: no such entry in the opened archive "
                "(missing paths are never silently ignored)"
            )
        if key in self._pending:
            raise DV2Error(f"duplicate pending operation for {normalized!r}")
        operation = PendingOp(
            self._next_op_id, OP_REMOVE, self._by_key[key].path, None, None
        )
        self._next_op_id += 1
        self._pending[key] = operation
        return operation

    def cancel_pending(self, raw_path_or_op_id: str | int) -> bool:
        """Cancel one staged operation by path or op id. Returns True if removed."""
        if isinstance(raw_path_or_op_id, int):
            matches = [op for op in self._pending.values() if op.op_id == raw_path_or_op_id]
        else:
            normalized = normalize_archive_path(str(raw_path_or_op_id))
            operation = self._pending.get(normalized.casefold())
            matches = [operation] if operation is not None else []
        if not matches:
            return False
        operation = matches[0]
        if operation.kind == OP_ADD:
            self._add_order.remove(operation.key)
        del self._pending[operation.key]
        return True

    def clear_pending(self) -> int:
        """Cancel all staged operations; returns how many were cancelled."""
        count = len(self._pending)
        self._pending.clear()
        self._add_order.clear()
        return count

    def pending_ops(self) -> tuple[PendingOp, ...]:
        """Staged operations: removes/replacements keyed by original position,
        additions in explicit user order."""
        ordered = sorted(
            (op for op in self._pending.values() if op.kind != OP_ADD),
            key=lambda op: self._by_key[op.key].relative_offset,
        )
        ordered.extend(self._pending[key] for key in self._add_order)
        return tuple(ordered)

    def has_pending_changes(self) -> bool:
        return bool(self._pending)

    def added_replaced_removed(self) -> tuple[list[str], list[str], list[str]]:
        added = [self._pending[key].path for key in self._add_order]
        replaced = [
            op.path
            for op in sorted(
                (o for o in self._pending.values() if o.kind == OP_SET),
                key=lambda o: self._by_key[o.key].relative_offset,
            )
        ]
        removed = [
            op.path
            for op in sorted(
                (o for o in self._pending.values() if o.kind == OP_REMOVE),
                key=lambda o: self._by_key[o.key].relative_offset,
            )
        ]
        return added, replaced, removed

    # -- content passes ------------------------------------------------------

    def deep_verify(self, progress: Callable[[int], None] | None = None) -> dict:
        """Re-read the source and decode every payload.

        Returns totals plus per-entry logical SHA-256 map (keyed casefolded).
        """
        hashes: dict[str, str] = {}
        packed_total = 0
        unpacked_total = 0
        sink = _HashSinkFactory()
        with self.path.open("rb") as stream:
            for entry in self.entries:
                hasher = sink.new()
                produced = consume_entry(stream, self.header, entry, hasher)
                if produced != entry.logical_size:
                    raise DV2Error(
                        f"logical size mismatch for {entry.path!r}: got {produced}, "
                        f"expected {entry.logical_size}"
                    )
                hashes[entry.key] = hasher.hexdigest
                packed_total += entry.stored_size
                unpacked_total += produced
                if progress is not None:
                    progress(entry.stored_size)
        return {
            "entries": len(self.entries),
            "packed_total": packed_total,
            "unpacked_total": unpacked_total,
            "logical_sha256": hashes,
        }

    def stored_region_hashes(self, progress: Callable[[int], None] | None = None) -> dict:
        """SHA-256 of each entry's STORED bytes, copied byte-for-byte regions.

        Uses one sequential pass over the file; gaps between payloads are skipped.
        """
        spans = sorted(
            (
                (self.header.data_offset + e.relative_offset, e.stored_size, e.key)
                for e in self.entries
                if e.stored_size
            )
        )
        hashes: dict[str, str] = {}
        with self.path.open("rb") as stream:
            position = 0
            for start, size, key in spans:
                if start < position:
                    raise DV2Error("internal error: overlapping stored spans")  # pragma: no cover
                skip = start - position
                if skip:
                    stream.seek(skip, os.SEEK_CUR)
                    position += skip
                digest = hashlib.sha256()
                remaining = size
                while remaining:
                    chunk = stream.read(min(IO_CHUNK, remaining))
                    if not chunk:
                        raise DV2Error("file shrank while hashing stored regions")
                    digest.update(chunk)
                    remaining -= len(chunk)
                position += size
                hashes[key] = digest.hexdigest()
                if progress is not None:
                    progress(size)
        return hashes

    def stored_region_hash_subset(self, wanted_keys: Iterable[str]) -> dict:
        """Stored-region SHA-256 for a subset of entries (seek per entry)."""
        keys = set(wanted_keys)
        hashes: dict[str, str] = {}
        with self.path.open("rb") as stream:
            for entry in self.entries:
                if entry.key not in keys or not entry.stored_size:
                    continue
                stream.seek(self.header.data_offset + entry.relative_offset)
                digest = hashlib.sha256()
                remaining = entry.stored_size
                while remaining:
                    chunk = stream.read(min(IO_CHUNK, remaining))
                    if not chunk:
                        raise DV2Error("file shrank while hashing stored regions")
                    digest.update(chunk)
                    remaining -= len(chunk)
                hashes[entry.key] = digest.hexdigest()
        return hashes

    # -- save operations ------------------------------------------------------

    def save_as(self, output_path: str | os.PathLike, progress: Callable[[str], None] | None = None) -> dict:
        """Save to a NEW file.

        With no staged changes this is a verbatim byte-for-byte copy
        (bit-perfect by construction, still re-read and deep-verified).
        With staged changes the container is rebuilt canonically; unchanged
        stored payloads are copied byte-for-byte from the source.
        """
        started = time.perf_counter()
        if self.has_pending_changes():
            result = self._rebuild(output_path, force=False, progress=progress)
        else:
            result = self._copy_verbatim(output_path, progress=progress)
        result["duration_s"] = round(time.perf_counter() - started, 6)
        result["operation"] = result.get("operation", "save_as")
        return result

    def force_rebuild_save_as(self, output_path: str | os.PathLike, progress: Callable[[str], None] | None = None) -> dict:
        """Diagnostic: rebuild the container without logical changes.

        Requires a clean session (no staged edits): use ``save_as`` first or
        ``clear_pending``.  Physical identity is measured and reported, never
        assumed and never 'fixed' by guessing unknown fields.
        """
        if self.has_pending_changes():
            raise DV2Error(
                "force-rebuild requires a clean session; cancel staged changes or save them first"
            )
        started = time.perf_counter()
        result = self._rebuild(output_path, force=True, progress=progress)
        result["duration_s"] = round(time.perf_counter() - started, 6)
        result["operation"] = "force_rebuild"
        return result

    def compact_rebuild_save_as(
        self,
        output_path: str | os.PathLike,
        *,
        layout: str,
        progress: Callable[[str], None] | None = None,
    ) -> dict:
        """Write a guarded, no-change compact relayout to a new file.

        ``layout='mode1'`` places data immediately after the entry table.
        ``layout='mode0-sequential'`` keeps an aligned data start but places
        stored payloads sequentially without per-entry alignment.  In both
        cases the source entry order, raw path table, stored blobs and all
        unpacked-size fields are preserved exactly.  This is an offline
        experimental operation; it does not claim runtime compatibility.
        """
        if layout not in ("mode1", "mode0-sequential"):
            raise DV2Error(
                "compact layout must be 'mode1' or 'mode0-sequential', "
                f"got {layout!r}"
            )
        if self.has_pending_changes():
            raise DV2Error(
                "compact rebuild requires a clean session; cancel staged changes first"
            )

        output_path = _prepare_output(self.path, output_path)
        started = time.perf_counter()
        temp_path: Path | None = None

        # Capture the source identity and fully verify its payloads before any
        # output is created.  The same size/hash check is repeated immediately
        # before publication to make a source mutation fail closed.
        source_sha256, source_size = hash_and_size_file(self.path)
        if source_size != self.file_size:
            raise DV2Error(
                f"source changed before compact rebuild: expected {self.file_size} bytes, "
                f"read {source_size}"
            )
        self._source_sha256 = source_sha256
        source_header, raw_path_table, source_count, source_records = (
            _read_raw_table_and_records(self.path)
        )
        if source_header != self.header:
            raise DV2Error("source header changed before compact rebuild")
        expected_records = tuple(
            (entry.relative_offset, entry.packed_size, entry.unpacked_size)
            for entry in self.entries
        )
        if source_count != len(self.entries) or source_records != expected_records:
            raise DV2Error("source entry table changed before compact rebuild")

        if progress is not None:
            progress("deep-verifying source payloads")
        source_logical = self.deep_verify()
        source_stored = _all_stored_entry_hashes(self)

        entry_table_offset = HEADER.size + len(raw_path_table)
        entry_table_end = entry_table_offset + 4 + len(self.entries) * ENTRY.size
        if layout == "mode1":
            output_layout_mode = 1
            output_data_offset = entry_table_end
        else:
            output_layout_mode = 0
            output_data_offset = align_up(entry_table_end, BLOCK_SIZE)
        if output_data_offset > MAX_FIELD32:
            raise DV2Error("compact data offset exceeds the 32-bit DV2 field")

        records: list[tuple[int, int, int]] = []
        try:
            handle = tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{output_path.name}.",
                suffix=".dsh-dv2tmp",
                dir=output_path.parent,
                delete=False,
            )
            temp_path = Path(handle.name)
            if progress is not None:
                progress(f"writing {layout} compact relayout")
            with handle, self.path.open("rb") as source_stream:
                handle.write(
                    HEADER.pack(
                        self.header.version,
                        self.header.unknown_04,
                        self.header.unknown_08,
                        output_layout_mode,
                        self.header.compression_mode,
                        output_data_offset,
                        len(raw_path_table),
                    )
                )
                handle.write(raw_path_table)
                handle.write(struct.pack("<I", len(self.entries)))
                record_offset = handle.tell()
                write_zeros(handle, len(self.entries) * ENTRY.size)
                write_zeros(handle, output_data_offset - handle.tell())

                for entry in self.entries:
                    relative_offset = handle.tell() - output_data_offset
                    if relative_offset > MAX_FIELD32:
                        raise DV2Error(
                            f"relative payload offset exceeds the 32-bit field for {entry.path!r}"
                        )
                    _copy_stored_region(source_stream, self.header, entry, handle)
                    records.append(
                        (relative_offset, entry.packed_size, entry.unpacked_size)
                    )
                    if progress is not None:
                        progress(f"relayout {entry.path}")

                write_zeros(handle, align_up(handle.tell(), BLOCK_SIZE) - handle.tell())
                handle.seek(record_offset)
                for record in records:
                    handle.write(ENTRY.pack(*record))
                handle.flush()
                os.fsync(handle.fileno())

            if progress is not None:
                progress("deep-verifying compact output")
            output_session = DV2Session(temp_path)
            output_header, output_path_table, output_count, output_records = (
                _read_raw_table_and_records(temp_path)
            )
            if output_count != len(self.entries):
                raise DV2Error("compact output entry count differs from source")
            if output_path_table != raw_path_table:
                raise DV2Error("compact output path table differs from source")
            if output_header.version != self.header.version:
                raise DV2Error("compact output version differs from source")
            if output_header.unknown_04 != self.header.unknown_04:
                raise DV2Error("compact output unknown_04 differs from source")
            if output_header.unknown_08 != self.header.unknown_08:
                raise DV2Error("compact output unknown_08 differs from source")
            if output_header.compression_mode != self.header.compression_mode:
                raise DV2Error("compact output compression mode differs from source")
            if output_header.path_table_size != self.header.path_table_size:
                raise DV2Error("compact output path-table size differs from source")
            if output_header.layout_mode != output_layout_mode:
                raise DV2Error("compact output layout mode differs from requested layout")
            if output_header.data_offset != output_data_offset:
                raise DV2Error("compact output data offset differs from requested layout")
            if output_records != tuple(records):
                raise DV2Error("compact output entry records differ from the requested relayout")

            source_paths = tuple(entry.path for entry in self.entries)
            output_paths = tuple(entry.path for entry in output_session.entries)
            if output_paths != source_paths:
                raise DV2Error("compact output entry order or paths differ from source")
            source_fields = tuple(
                (entry.packed_size, entry.unpacked_size, entry.storage_mode)
                for entry in self.entries
            )
            output_fields = tuple(
                (entry.packed_size, entry.unpacked_size, entry.storage_mode)
                for entry in output_session.entries
            )
            if output_fields != source_fields:
                raise DV2Error("compact output entry fields differ from source")

            # The only table-field changes permitted by this operation are the
            # relative offsets.  Stored and unpacked-size fields must remain
            # identical in every record.
            for source_record, output_record in zip(source_records, output_records):
                if source_record[1:] != output_record[1:]:
                    raise DV2Error("compact output changed a stored-size field")
            output_logical = output_session.deep_verify()
            output_stored = _all_stored_entry_hashes(output_session)
            stored_mismatches = [
                entry.path
                for entry in self.entries
                if source_stored.get(entry.key) != output_stored.get(entry.key)
            ]
            logical_mismatches = [
                entry.path
                for entry in self.entries
                if source_logical["logical_sha256"].get(entry.key)
                != output_logical["logical_sha256"].get(entry.key)
            ]
            if stored_mismatches:
                raise DV2Error(
                    f"compact output stored payload mismatch for {stored_mismatches[0]!r}"
                )
            if logical_mismatches:
                raise DV2Error(
                    f"compact output logical payload mismatch for {logical_mismatches[0]!r}"
                )

            output_sha256, output_size = hash_and_size_file(temp_path)
            if output_size % BLOCK_SIZE:
                raise DV2Error("compact output EOF is not aligned to 0x8000")
            source_sha_after, source_size_after = hash_and_size_file(self.path)
            if source_size_after != source_size or source_sha_after != source_sha256:
                raise DV2Error(
                    "source changed during compact rebuild; refusing to publish output"
                )

            published = _publish(temp_path, output_path)
            temp_path = None
            byte_identical = output_size == source_size and output_sha256 == source_sha256
            reduction = source_size - output_size
            reduction_percent = (reduction / source_size * 100.0) if source_size else 0.0
            unaligned_count = len(output_session.unaligned_mode0_entries)
            return {
                "status": "OK",
                "operation": "compact_rebuild",
                "layout": layout,
                "source": str(self.path),
                "output": str(published),
                "source_sha256": source_sha256,
                "source_sha256_after": source_sha_after,
                "output_sha256": output_sha256,
                "sha256": output_sha256,
                "source_size": source_size,
                "source_size_after": source_size_after,
                "output_size": output_size,
                "size": output_size,
                "size_reduction": reduction,
                "reduction_bytes": reduction,
                "reduction_percent": round(reduction_percent, 6),
                "layout_source": self.header.layout_mode,
                "layout_output": output_header.layout_mode,
                "data_offset_source": self.header.data_offset,
                "data_offset_output": output_header.data_offset,
                "entry_count": output_count,
                "stored_payloads_verified": len(self.entries),
                "logical_payloads_verified": len(self.entries),
                "stored_payload_mismatches": len(stored_mismatches),
                "logical_payload_mismatches": len(logical_mismatches),
                "entry_order_identical": True,
                "path_table_identical": True,
                "header_fields_preserved": True,
                "mode0_unaligned_count": unaligned_count,
                "source_unchanged": True,
                "byte_identical": byte_identical,
                "byte_identical_to_source": byte_identical,
                "deep_verify": True,
                "added": [],
                "replaced": [],
                "removed": [],
                "storage_modes": _mode_counts(output_session.entries),
                "duration_s": round(time.perf_counter() - started, 6),
            }
        except Exception:
            _discard_temp(temp_path)
            raise

    # -- internals -------------------------------------------------------------

    def _copy_verbatim(self, output_path: str | os.PathLike, progress: Callable[[str], None] | None) -> dict:
        output_path = _prepare_output(self.path, output_path)
        if progress is not None:
            progress("copying source verbatim")
        source_digest = hashlib.sha256()
        size_copied = 0
        temp_path: Path | None = None
        try:
            handle = tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{output_path.name}.",
                suffix=".dsh-dv2tmp",
                dir=output_path.parent,
                delete=False,
            )
            temp_path = Path(handle.name)
            with handle, self.path.open("rb") as source:
                while True:
                    chunk = source.read(IO_CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
                    source_digest.update(chunk)
                    size_copied += len(chunk)
                if size_copied != self.file_size:
                    raise DV2Error(
                        f"source changed during copy: expected {self.file_size}, read {size_copied}"
                    )
                handle.flush()
                os.fsync(handle.fileno())
            if progress is not None:
                progress("deep-verifying written copy")
            verify = parse_archive(temp_path)
            if verify[0] != self.file_size:
                raise DV2Error("copied file size differs from source")
            deep = DV2Session(temp_path).deep_verify()
            output_digest, output_size = hash_and_size_file(temp_path)
            source_sha = source_digest.hexdigest()
            published = _publish(temp_path, output_path)
            temp_path = None
            added, replaced, removed = self.added_replaced_removed()
            return {
                "status": "OK",
                "output": str(published),
                "size": output_size,
                "sha256": output_digest,
                "source_sha256": source_sha,
                "byte_identical_to_source": True,
                "entry_count": len(deep["logical_sha256"]),
                "added": added,
                "replaced": replaced,
                "removed": removed,
                "deep_verify": True,
                "layout_mode": self.header.layout_mode,
                "storage_modes": _mode_counts(self.entries),
            }
        except Exception:
            _discard_temp(temp_path)
            raise

    def _rebuild(self, output_path: str | os.PathLike, force: bool, progress: Callable[[str], None] | None) -> dict:
        output_path = _prepare_output(self.path, output_path)
        added_ops, replaced_ops, removed_ops = [], [], []
        if not force:
            added, replaced, removed = self.added_replaced_removed()
            added_ops, replaced_ops, removed_ops = (
                [self._pending[k.casefold()] for k in added],
                [self._pending[k.casefold()] for k in replaced],
                [self._pending[k.casefold()] for k in removed],
            )

        # Final logical entry list: originals minus removals (positions kept),
        # replacements keep their slot, adds appended in staged order.
        final_items: list[_FinalItem] = []
        for index, entry in enumerate(self.entries):
            op = self._pending.get(entry.key)
            if op is not None and op.kind == OP_REMOVE:
                continue
            if op is not None and op.kind == OP_SET:
                final_items.append(_FinalItem(entry.path, op.source, entry.storage_mode, ("replace", index)))
            else:
                final_items.append(_FinalItem(entry.path, None, entry.storage_mode, ("keep", index)))
        for key in self._add_order:
            op = self._pending[key]
            final_items.append(_FinalItem(op.path, op.source, op.storage, ("add", op.op_id)))

        # Placement order: originals by their original relative offset, then
        # adds in staged user order.
        placement = sorted(
            (item for item in final_items if item.origin[0] in ("keep", "replace")),
            key=lambda item: self.entries[item.origin[1]].relative_offset,
        )
        placement.extend(item for item in final_items if item.origin[0] == "add")

        if progress is not None:
            progress("hashing logical payloads of the source")
        source_logical = self.deep_verify()

        path_table = b"".join(item.path.encode("ascii") + b"\0" for item in final_items)
        entry_table_offset = HEADER.size + len(path_table)
        entry_table_end = entry_table_offset + 4 + len(final_items) * ENTRY.size
        data_offset = canonical_data_offset(self.header.layout_mode, entry_table_end)

        temp_path: Path | None = None
        records: dict[str, tuple[int, int, int]] = {}
        built_logical: dict[str, str] = {}
        try:
            handle = tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{output_path.name}.",
                suffix=".dsh-dv2tmp",
                dir=output_path.parent,
                delete=False,
            )
            temp_path = Path(handle.name)
            if progress is not None:
                progress("writing container structure")
            with handle, self.path.open("rb") as source_stream:
                handle.write(
                    HEADER.pack(
                        self.header.version,
                        self.header.unknown_04,
                        self.header.unknown_08,
                        self.header.layout_mode,
                        self.header.compression_mode,
                        data_offset,
                        len(path_table),
                    )
                )
                handle.write(path_table)
                handle.write(struct.pack("<I", len(final_items)))
                write_zeros(handle, len(final_items) * ENTRY.size)
                write_zeros(handle, data_offset - handle.tell())

                for item in placement:
                    if self.header.layout_mode == 0:
                        aligned = align_up(handle.tell(), BLOCK_SIZE)
                        write_zeros(handle, aligned - handle.tell())
                    relative_offset = handle.tell() - data_offset
                    if relative_offset > MAX_FIELD32:
                        raise DV2Error(
                            f"relative payload offset exceeds the 32-bit field for {item.path!r}"
                        )
                    if item.origin[0] == "keep":
                        entry = self.entries[item.origin[1]]
                        _copy_stored_region(source_stream, self.header, entry, handle)
                        stored_size = entry.stored_size
                        unpacked_field = entry.unpacked_size
                        built_logical[entry.key] = source_logical["logical_sha256"][entry.key]
                    else:
                        stored_size, unpacked_field, logical_digest = _write_payload(
                            item.source, handle, item.storage_mode
                        )
                        built_logical[item.path.casefold()] = logical_digest
                    records[item.path.casefold()] = (relative_offset, stored_size, unpacked_field)
                    if progress is not None:
                        progress(f"wrote {item.path}")

                write_zeros(handle, align_up(handle.tell(), BLOCK_SIZE) - handle.tell())

                handle.seek(entry_table_offset + 4)
                for item in final_items:
                    handle.write(ENTRY.pack(*records[item.path.casefold()]))
                handle.flush()
                os.fsync(handle.fileno())

            if progress is not None:
                progress("deep-verifying rebuilt archive")
            verify_session = DV2Session(temp_path)
            verify = verify_session.deep_verify()
            if verify["logical_sha256"] != built_logical:
                differing = sorted(
                    key
                    for key in set(verify["logical_sha256"]) | set(built_logical)
                    if verify["logical_sha256"].get(key) != built_logical.get(key)
                )
                raise DV2Error(
                    f"rebuilt logical payload mismatch for {len(differing)} entry(ies), "
                    f"first: {differing[0]!r}"
                )
            output_digest, output_size = hash_and_size_file(temp_path)
            source_sha = self.source_sha256
            published = _publish(temp_path, output_path)
            temp_path = None
            raw_entries = sum(1 for e in verify_session.entries if not e.is_compressed)
            zlib_entries = len(verify_session.entries) - raw_entries
            return {
                "status": "OK",
                "output": str(published),
                "size": output_size,
                "sha256": output_digest,
                "source_sha256": source_sha,
                "byte_identical_to_source": output_size == self.file_size
                and output_digest == source_sha,
                "entry_count": len(verify_session.entries),
                "added": [op.path for op in added_ops],
                "replaced": [op.path for op in replaced_ops],
                "removed": [op.path for op in removed_ops],
                "deep_verify": True,
                "logical_payloads_verified": len(verify["logical_sha256"]),
                "layout_mode": self.header.layout_mode,
                "storage_modes": {"raw": raw_entries, "zlib": zlib_entries},
            }
        except Exception:
            _discard_temp(temp_path)
            raise


# ---------------------------------------------------------------------------
# Build helpers
# ---------------------------------------------------------------------------


class _FinalItem:
    __slots__ = ("path", "source", "storage_mode", "origin")

    def __init__(self, path: str, source: PayloadSource | None, storage_mode: str, origin: tuple):
        self.path = path
        self.source = source
        self.storage_mode = storage_mode
        self.origin = origin  # ("keep", idx) | ("replace", idx) | ("add", op_id)


class _ListSink:
    __slots__ = ("chunks",)

    def __init__(self, chunks: list):
        self.chunks = chunks

    def write(self, data: bytes) -> int:
        self.chunks.append(data)
        return len(data)


class _HashSinkFactory:
    class _Sink:
        __slots__ = ("digest",)

        def __init__(self):
            self.digest = hashlib.sha256()

        def write(self, data: bytes) -> int:
            self.digest.update(data)
            return len(data)

        @property
        def hexdigest(self) -> str:
            return self.digest.hexdigest()

    def new(self):
        return self._Sink()


def _check_storage(storage: str) -> str:
    if storage not in STORAGE_MODES:
        raise DV2Error(
            f"storage mode must be one of {STORAGE_MODES} (explicitly chosen; "
            f"never guessed from extensions), got {storage!r}"
        )
    return storage


def _copy_stored_region(source_stream: BinaryIO, header: DV2Header, entry: DV2Entry, output: BinaryIO) -> None:
    source_stream.seek(header.data_offset + entry.relative_offset)
    remaining = entry.stored_size
    while remaining:
        chunk = source_stream.read(min(IO_CHUNK, remaining))
        if not chunk:
            raise DV2Error(f"source payload truncated for {entry.path!r}")
        output.write(chunk)
        remaining -= len(chunk)


def _write_payload(payload: PayloadSource, output: BinaryIO, storage_mode: str) -> tuple[int, int, str]:
    """Write one new payload; returns (stored_size, unpacked_field, logical_sha256)."""
    digest = hashlib.sha256()
    compressor = zlib.compressobj(level=ZLIB_LEVEL) if storage_mode == "zlib" else None
    stored_size = 0
    logical_size = 0
    for chunk in payload.stream():
        logical_size += len(chunk)
        digest.update(chunk)
        encoded = compressor.compress(chunk) if compressor is not None else chunk
        if encoded:
            output.write(encoded)
            stored_size += len(encoded)
    if compressor is not None:
        encoded = compressor.flush()
        output.write(encoded)
        stored_size += len(encoded)
    if logical_size > MAX_FIELD32 or stored_size > MAX_FIELD32:
        raise DV2Error(f"payload sizes exceed the 32-bit DV2 fields for {payload.describe()}")
    unpacked_field = logical_size if storage_mode == "zlib" else 0
    return stored_size, unpacked_field, digest.hexdigest()


def _prepare_output(source_path: Path, output_path: str | os.PathLike) -> Path:
    output_path = Path(output_path).resolve()
    if output_path == source_path:
        raise DV2Error("the source archive can never be overwritten; choose a different output path")
    if output_path.exists():
        raise DV2Error(f"output already exists; Save As writes NEW files only: {output_path}")
    parent = output_path.parent
    if not parent.is_dir():
        raise DV2Error(f"output directory does not exist: {parent}")
    return output_path


def _reject_link_components(path: Path, label: str) -> None:
    """Reject symlink/reparse traversal in all existing path components."""

    absolute = path.absolute()
    anchor = Path(absolute.anchor) if absolute.anchor else Path.cwd().anchor
    current = Path(anchor) if anchor else Path()
    parts = absolute.parts
    if absolute.anchor and parts and parts[0] == absolute.anchor:
        parts = parts[1:]
    for part in parts:
        current = current / part
        try:
            metadata = current.lstat()
        except OSError as error:
            raise DV2Error(f"cannot inspect {label} path component: {current}") from error
        if current.is_symlink() or bool(getattr(metadata, "st_file_attributes", 0) & 0x400):
            raise DV2Error(
                f"{label} must not traverse a symlink or reparse point: {current}"
            )


def _prepare_new_output(output_path: str | os.PathLike) -> Path:
    """Validate a destination that must not exist and return its absolute path."""

    try:
        output = Path(output_path).absolute()
    except (TypeError, ValueError) as error:
        raise DV2Error(f"invalid output path: {output_path!r}") from error
    parent = output.parent
    _reject_link_components(parent, "output parent")
    try:
        parent_metadata = parent.lstat()
    except OSError as error:
        raise DV2Error(f"cannot inspect output directory: {parent}") from error
    if parent.is_symlink() or bool(getattr(parent_metadata, "st_file_attributes", 0) & 0x400):
        raise DV2Error(f"output parent must not be a symlink or reparse point: {parent}")
    if not stat.S_ISDIR(parent_metadata.st_mode):
        raise DV2Error(f"output directory does not exist: {parent}")
    try:
        output_metadata = output.lstat()
    except FileNotFoundError:
        output_metadata = None
    except OSError as error:
        raise DV2Error(f"cannot inspect output path: {output}") from error
    if output_metadata is not None:
        raise DV2Error(f"output already exists; new files only: {output}")
    return output


def _file_identity(path: Path) -> tuple[int, int] | None:
    """Return a no-follow identity suitable for guarded cleanup."""

    try:
        metadata = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    return metadata.st_dev, metadata.st_ino


def _publish(temporary_path: Path, output_path: Path) -> Path:
    """Publish a sibling temporary file without ever replacing a target.

    ``os.rename`` is atomic, but on POSIX it replaces a destination created in
    the check/publish race.  A same-directory hard link gives us the required
    atomic create-if-absent operation.  The temporary name is then removed so
    the destination is the sole published name on success.
    """

    temporary_identity = _file_identity(temporary_path)
    if temporary_identity is None:
        raise DV2Error(f"cannot inspect temporary output before publish: {temporary_path}")
    try:
        os.link(temporary_path, output_path)
    except FileExistsError as error:
        raise DV2Error(
            f"output appeared while saving; refusing to replace it: {output_path}"
        ) from error
    except OSError as error:
        raise DV2Error(f"cannot publish output without overwrite: {output_path}") from error

    try:
        os.unlink(temporary_path)
    except OSError as error:
        # The link succeeded, so clean up only if the destination still names
        # the inode we just published.  Never remove an externally substituted
        # path merely because it has the expected filename.
        if _file_identity(output_path) == temporary_identity:
            try:
                os.unlink(output_path)
            except OSError as cleanup_error:
                raise DV2Error(
                    f"cannot finalize publication cleanup: {output_path}"
                ) from cleanup_error
        raise DV2Error(f"cannot finalize output publication: {output_path}") from error
    return output_path


def _discard_temp(temporary_path: Path | None) -> None:
    if temporary_path is None:
        return
    try:
        if temporary_path.exists():
            temporary_path.unlink()
    except OSError:
        pass


def _mode_counts(entries: Sequence[DV2Entry]) -> dict:
    raw_entries = sum(1 for entry in entries if not entry.is_compressed)
    return {"raw": raw_entries, "zlib": len(entries) - raw_entries}


def _all_stored_entry_hashes(session: DV2Session) -> dict[str, str]:
    """Return stored-payload hashes for every entry, including empty raw ones."""
    hashes = session.stored_region_hashes()
    empty_hash = hashlib.sha256(b"").hexdigest()
    return {
        entry.key: hashes.get(entry.key, empty_hash)
        for entry in session.entries
    }


def create_empty_archive(
    output_path: str | os.PathLike,
    *,
    version: int = 5,
    unknown_04: int = 1,
    unknown_08: int = 4,
    layout_mode: int = 0,
    compression_mode: int = 1,
) -> dict:
    """Create and verify a canonical empty DV2 archive.

    This constructor has no source archive and does not depend on test
    fixtures.  The output contains an empty path table, a zero entry count,
    the canonical data offset for the selected layout, and zero padding to the
    next ``BLOCK_SIZE`` boundary.  Publication is atomic and never overwrites
    an existing destination.
    """

    def validate_u32(value: object, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise DV2Error(f"{label} must be an integer")
        if not 0 <= value <= MAX_FIELD32:
            raise DV2Error(f"{label} must fit an unsigned 32-bit field")
        return value

    version = validate_u32(version, "version")
    unknown_04 = validate_u32(unknown_04, "unknown_04")
    unknown_08 = validate_u32(unknown_08, "unknown_08")
    if isinstance(layout_mode, bool) or not isinstance(layout_mode, int):
        raise DV2Error("layout_mode must be an integer")
    if layout_mode not in (0, 1):
        raise DV2Error(f"unsupported layout mode {layout_mode}; expected 0 or 1")
    if isinstance(compression_mode, bool) or not isinstance(compression_mode, int):
        raise DV2Error("compression_mode must be an integer")
    if compression_mode != 1:
        raise DV2Error(
            f"unsupported compression mode {compression_mode}; only mode 1 is supported"
        )

    output = _prepare_new_output(output_path)
    path_table = b""
    entry_count = 0
    entry_table_end = HEADER.size + len(path_table) + 4
    data_offset = canonical_data_offset(layout_mode, entry_table_end)
    final_size = align_up(data_offset, BLOCK_SIZE)
    expected_header = DV2Header(
        version,
        unknown_04,
        unknown_08,
        layout_mode,
        compression_mode,
        data_offset,
        len(path_table),
    )
    header_bytes = HEADER.pack(
        expected_header.version,
        expected_header.unknown_04,
        expected_header.unknown_08,
        expected_header.layout_mode,
        expected_header.compression_mode,
        expected_header.data_offset,
        expected_header.path_table_size,
    )

    temporary_path: Path | None = None
    try:
        handle = tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{output.name}.",
            suffix=".dsh-dv2tmp",
            dir=output.parent,
            delete=False,
        )
        temporary_path = Path(handle.name)
        with handle:
            handle.write(header_bytes)
            handle.write(struct.pack("<I", entry_count))
            write_zeros(handle, data_offset - handle.tell())
            write_zeros(handle, final_size - handle.tell())
            handle.flush()
            os.fsync(handle.fileno())

        verify_session = DV2Session(temporary_path)
        deep = verify_session.deep_verify()
        if verify_session.header != expected_header:
            raise DV2Error("empty archive header changed before publication")
        if verify_session.entries or deep.get("entries") != 0:
            raise DV2Error("empty archive deep verification found entries")
        if deep.get("logical_sha256") != {}:
            raise DV2Error("empty archive deep verification found payload hashes")
        output_sha256, output_size = hash_and_size_file(temporary_path)
        if output_size != final_size:
            raise DV2Error(
                f"empty archive size is {output_size}; expected {final_size}"
            )
        published = _publish(temporary_path, output)
        temporary_path = None
        return {
            "status": "OK",
            "operation": "create_empty_archive",
            "output": str(published),
            "size": output_size,
            "sha256": output_sha256,
            "entry_count": 0,
            "deep_verify": True,
            "deep_verify_report": deep,
            "header": {
                "version": expected_header.version,
                "unknown_04": expected_header.unknown_04,
                "unknown_08": expected_header.unknown_08,
                "layout_mode": expected_header.layout_mode,
                "compression_mode": expected_header.compression_mode,
                "data_offset": expected_header.data_offset,
                "path_table_size": expected_header.path_table_size,
            },
            "entry_table_end": entry_table_end,
            "data_offset": data_offset,
            "layout_mode": layout_mode,
            "compression_mode": compression_mode,
        }
    except Exception:
        _discard_temp(temporary_path)
        raise


__all__ = [
    "BLOCK_SIZE",
    "DV2Entry",
    "DV2Error",
    "DV2Header",
    "DV2Session",
    "ENTRY",
    "EXPECTED_HEADER_SIZE",
    "HEADER",
    "OP_ADD",
    "OP_REMOVE",
    "OP_SET",
    "PendingOp",
    "PayloadSource",
    "STORAGE_MODES",
    "align_up",
    "canonical_data_offset",
    "consume_entry",
    "create_empty_archive",
    "decode_paths",
    "hash_and_size_file",
    "mode0_unaligned_paths",
    "normalize_archive_path",
    "parse_archive",
    "validate_structure",
]
