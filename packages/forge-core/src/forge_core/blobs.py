"""Content-Addressed Storage für forge.

Prompts, Diffs, Eval-Outputs, Tool-Versionen werden NICHT inline ins Event geschrieben.
Stattdessen liegen sie als Blobs in `<root>/<hash[:2]>/<hash>` und das Event hält
nur den `sha256:`-Hash (Spec Teil 4.1).

Vorteile: Event-Stream bleibt kompakt, Deduplication (gleicher Prompt = gleicher
Hash), Replay deterministisch.

Garbage Collection: nach 90 Tagen, ausgenommen Blobs, die in Events der letzten
365 Tage referenziert sind (Spec Teil 4.3) — die Referenz-Sammlung erfolgt extern;
diese Klasse nimmt eine Set von "keep"-Hashes entgegen.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

HASH_PREFIX = "sha256:"


def compute_sha256(data: bytes) -> str:
    """Liefert den `sha256:`-Hash von `data` im forge-Wire-Format."""
    return HASH_PREFIX + hashlib.sha256(data).hexdigest()


def _strip_prefix(hash_str: str) -> str:
    if not hash_str.startswith(HASH_PREFIX):
        raise ValueError(f"hash must be prefixed with {HASH_PREFIX!r}, got {hash_str!r}")
    hex_part = hash_str[len(HASH_PREFIX) :]
    if len(hex_part) != 64:
        raise ValueError(f"expected 64-char hex hash, got {len(hex_part)}: {hash_str!r}")
    return hex_part


@dataclass(frozen=True)
class GCStats:
    """Ergebnis eines Garbage-Collection-Laufs."""

    deleted_count: int
    deleted_bytes: int
    kept_count: int


class BlobStore:
    """Filesystem-CAS mit atomarem Schreiben.

    Layout::

        <root>/
        ├── ab/
        │   └── abcdef0123...    # Datei-Inhalt (raw bytes)
        └── cd/
            └── cdefab0123...

    Schreiben ist atomar via `tempfile` + `os.replace`. Mehrere Writer auf
    denselben Hash sind safe: der zweite überschreibt mit identischen Bytes.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, hex_hash: str) -> Path:
        return self.root / hex_hash[:2] / hex_hash

    def put(self, data: bytes) -> str:
        """Schreibt `data` und liefert den Hash im `sha256:<hex>`-Format zurück."""
        hash_str = compute_sha256(data)
        hex_hash = _strip_prefix(hash_str)
        target = self._path_for(hex_hash)

        if target.exists():
            return hash_str

        target.parent.mkdir(parents=True, exist_ok=True)

        # Atomare Write: temp file im selben Verzeichnis, dann os.replace
        fd, tmp_path = tempfile.mkstemp(dir=target.parent, prefix=".tmp-", suffix=".blob")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, target)
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_path)
            raise

        return hash_str

    def put_text(self, text: str, encoding: str = "utf-8") -> str:
        """Convenience für Textdaten."""
        return self.put(text.encode(encoding))

    def get(self, hash_str: str) -> bytes:
        """Liest die Bytes zum Hash. Wirft `FileNotFoundError`, wenn unbekannt."""
        hex_hash = _strip_prefix(hash_str)
        target = self._path_for(hex_hash)
        return target.read_bytes()

    def get_text(self, hash_str: str, encoding: str = "utf-8") -> str:
        return self.get(hash_str).decode(encoding)

    def has(self, hash_str: str) -> bool:
        try:
            hex_hash = _strip_prefix(hash_str)
        except ValueError:
            return False
        return self._path_for(hex_hash).exists()

    def iter_blobs(self) -> Iterator[tuple[str, Path]]:
        """Iteriert über alle Blobs als (hash_str, path)."""
        if not self.root.exists():
            return
        for shard in self.root.iterdir():
            if not shard.is_dir() or len(shard.name) != 2:
                continue
            for blob_file in shard.iterdir():
                if blob_file.is_file() and len(blob_file.name) == 64:
                    yield HASH_PREFIX + blob_file.name, blob_file

    def gc(
        self,
        *,
        keep: set[str] | None = None,
        older_than_days: int = 90,
        now: float | None = None,
    ) -> GCStats:
        """Löscht Blobs, die älter als `older_than_days` UND nicht in `keep` sind.

        Args:
            keep: Set von Hashes (im `sha256:<hex>`-Format), die garantiert
                nicht gelöscht werden — typischerweise alle Hashes, die in
                Events der letzten 365 Tage referenziert sind.
            older_than_days: Mindestalter in Tagen, ab dem ein Blob als
                Lösch-Kandidat gilt.
            now: Override für Tests; default = `time.time()`.
        """
        keep = keep or set()
        now = now if now is not None else time.time()
        cutoff = now - older_than_days * 86400

        deleted_count = 0
        deleted_bytes = 0
        kept_count = 0

        for hash_str, path in self.iter_blobs():
            if hash_str in keep:
                kept_count += 1
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            # mtime statt atime — atime ist auf vielen FS deaktiviert
            if stat.st_mtime > cutoff:
                kept_count += 1
                continue
            size = stat.st_size
            try:
                path.unlink()
                deleted_count += 1
                deleted_bytes += size
            except FileNotFoundError:
                pass

        # Leere Shard-Verzeichnisse aufräumen
        for shard in list(self.root.iterdir()):
            if shard.is_dir() and len(shard.name) == 2 and not any(shard.iterdir()):
                with contextlib.suppress(OSError):
                    shard.rmdir()

        return GCStats(
            deleted_count=deleted_count,
            deleted_bytes=deleted_bytes,
            kept_count=kept_count,
        )
