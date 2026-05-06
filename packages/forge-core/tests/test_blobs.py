"""Tests für forge_core.blobs."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from forge_core.blobs import HASH_PREFIX, BlobStore, compute_sha256


def test_put_get_roundtrip(tmp_path: Path) -> None:
    bs = BlobStore(tmp_path)
    h = bs.put(b"hello forge")
    assert h.startswith(HASH_PREFIX)
    assert bs.get(h) == b"hello forge"


def test_dedup_returns_same_hash(tmp_path: Path) -> None:
    bs = BlobStore(tmp_path)
    a = bs.put(b"identical bytes")
    b = bs.put(b"identical bytes")
    assert a == b == compute_sha256(b"identical bytes")
    # Filesystem hat exakt eine Datei
    files = list(bs.iter_blobs())
    assert len(files) == 1


def test_text_roundtrip_with_unicode(tmp_path: Path) -> None:
    bs = BlobStore(tmp_path)
    text = "Prompt mit Umlauten ÄÖÜß und Emoji 🔧"
    h = bs.put_text(text)
    assert bs.get_text(h) == text


def test_has_with_invalid_hash_returns_false(tmp_path: Path) -> None:
    bs = BlobStore(tmp_path)
    assert bs.has("not-a-hash") is False
    assert bs.has("sha256:short") is False


def test_get_missing_raises(tmp_path: Path) -> None:
    bs = BlobStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        bs.get("sha256:" + "0" * 64)


def test_layout_uses_two_char_shard(tmp_path: Path) -> None:
    bs = BlobStore(tmp_path)
    h = bs.put(b"x")
    hex_part = h[len(HASH_PREFIX) :]
    expected = tmp_path / hex_part[:2] / hex_part
    assert expected.is_file()


def test_random_round_trip_many_blobs(tmp_path: Path) -> None:
    bs = BlobStore(tmp_path)
    written: dict[str, bytes] = {}
    for i in range(50):
        data = os.urandom(64 + i)
        h = bs.put(data)
        written[h] = data
    for h, data in written.items():
        assert bs.get(h) == data


def test_gc_keeps_referenced_and_recent(tmp_path: Path) -> None:
    bs = BlobStore(tmp_path)
    h_keep = bs.put(b"keep")
    h_old = bs.put(b"old")
    h_recent = bs.put(b"recent")

    # Mark h_old as old: backdate its mtime
    target_old = tmp_path / h_old[len(HASH_PREFIX) : len(HASH_PREFIX) + 2] / h_old[len(HASH_PREFIX) :]
    backdate = time.time() - 100 * 86400
    os.utime(target_old, (backdate, backdate))

    stats = bs.gc(keep={h_keep}, older_than_days=90)
    assert h_old not in {h for h, _ in bs.iter_blobs()}
    assert bs.has(h_keep)
    assert bs.has(h_recent)
    assert stats.deleted_count == 1
    assert stats.kept_count == 2


def test_atomic_write_no_partial_files(tmp_path: Path) -> None:
    """Nach erfolgreichem put darf keine .tmp-Datei mehr existieren."""
    bs = BlobStore(tmp_path)
    bs.put(b"atomic write")
    for entry in tmp_path.rglob("*"):
        assert not entry.name.startswith(".tmp-"), f"leftover tmp: {entry}"
