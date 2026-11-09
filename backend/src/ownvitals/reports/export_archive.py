"""The export archive as a stream of chunks (ADR 0013 section 4; #211). No database, network or
file system: the zip is written into memory and handed out in chunks of at most 4 MiB, which the
builder stores in ``export_chunks`` as it goes, so only the current chunk and the open entry's
compressor state are held.

The zip is written as a stream (data descriptors, ZIP64 for every entry), so no entry needs to be
rewritten in place. Each entry's row count and SHA-256 (of its uncompressed bytes) go into the
manifest, which is the last entry. The archive's total size is checked after every write: over
``max_bytes`` raises ``TooLarge`` naming the entry that is largest so far.
"""

import hashlib
import io
import zipfile
from dataclasses import dataclass, field
from typing import IO, Any, Final

from ownvitals.reports.tables import CHUNK_MAX_BYTES

_STEM: Final = frozenset("abcdefghijklmnopqrstuvwxyz_")


class TooLarge(Exception):
    """The archive grew past its limit; ``largest`` is the file stem of the largest entry."""

    def __init__(self, largest: str, archive: "Archive") -> None:
        super().__init__("export_too_large")
        self.largest = largest
        self.archive = archive


class _Sink(io.RawIOBase):
    """A write-only, unseekable stream that keeps what was written until it is taken."""

    def __init__(self) -> None:
        super().__init__()
        self.pending = bytearray()
        self.total = 0
        self.digest = hashlib.sha256()

    def writable(self) -> bool:
        return True

    def write(self, b: bytes | bytearray | memoryview) -> int:  # pyright: ignore[reportIncompatibleMethodOverride]
        data = bytes(b)
        self.pending += data
        self.total += len(data)
        self.digest.update(data)
        return len(data)

    def tell(self) -> int:
        return self.total


@dataclass
class FileEntry:
    path: str
    stem: str
    rows: int = 0
    sha256: str = ""
    label: str | None = None
    compressed: int = 0


@dataclass
class Archive:
    """Entries are written one at a time: ``begin``, ``write`` (and ``row``), ``end``."""

    max_bytes: int
    files: list[FileEntry] = field(default_factory=list)
    _sink: _Sink = field(default_factory=_Sink)
    _zip: zipfile.ZipFile | None = None
    _entry: FileEntry | None = None
    _writer: IO[bytes] | None = None
    _digest: Any = None
    _start: int = 0
    _largest: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._zip = zipfile.ZipFile(self._sink, mode="w", compression=zipfile.ZIP_DEFLATED)

    def begin(self, path: str, stem: str, *, label: str | None = None) -> None:
        if self._entry is not None or self._zip is None:
            raise RuntimeError("an entry is open")
        if not stem or not set(stem) <= _STEM:
            raise ValueError("a file stem is a-z and _")
        info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        self._writer = self._zip.open(info, mode="w", force_zip64=True)
        self._digest = hashlib.sha256()
        self._entry = FileEntry(path, stem, label=label)
        self._start = self._sink.total

    def write(self, text: str, *, rows: int = 0) -> None:
        if self._entry is None or self._writer is None:
            raise RuntimeError("no entry is open")
        data = text.encode("utf-8")
        self._writer.write(data)
        self._digest.update(data)
        self._entry.rows += rows
        self._check()

    def end(self) -> FileEntry:
        if self._entry is None or self._writer is None:
            raise RuntimeError("no entry is open")
        self._writer.close()
        entry = self._entry
        entry.sha256 = self._digest.hexdigest()
        entry.compressed = self._sink.total - self._start
        self._largest[entry.stem] = self._largest.get(entry.stem, 0) + entry.compressed
        self.files.append(entry)
        self._entry = None
        self._writer = None
        self._check()
        return entry

    def close(self) -> None:
        if self._entry is not None or self._zip is None:
            raise RuntimeError("an entry is open")
        self._zip.close()
        self._zip = None
        self._check()

    def discard(self) -> None:
        """Close what is open and drop every byte not taken yet (after ``TooLarge``)."""
        writer, archive = self._writer, self._zip
        self._writer, self._zip, self._entry = None, None, None
        self.max_bytes = 1 << 62  # the closing writes must not raise again
        if writer is not None:
            writer.close()
        if archive is not None:
            archive.close()
        self._sink.pending.clear()

    def _check(self) -> None:
        if self._sink.total <= self.max_bytes:
            return
        sizes = dict(self._largest)
        if self._entry is not None:
            open_size = self._sink.total - self._start
            sizes[self._entry.stem] = sizes.get(self._entry.stem, 0) + open_size
        largest = max(sizes, key=lambda stem: sizes[stem]) if sizes else "manifest"
        raise TooLarge(largest, self)

    def take(self, *, final: bool = False) -> list[bytes]:
        """The full chunks written so far (and, with ``final``, the rest), in order."""
        pending = self._sink.pending
        out: list[bytes] = []
        while len(pending) >= CHUNK_MAX_BYTES or (final and pending):
            out.append(bytes(pending[:CHUNK_MAX_BYTES]))
            del pending[:CHUNK_MAX_BYTES]
        return out

    @property
    def size(self) -> int:
        return self._sink.total

    @property
    def sha256(self) -> bytes:
        return self._sink.digest.digest()
