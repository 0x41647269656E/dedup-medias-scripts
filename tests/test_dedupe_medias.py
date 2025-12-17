import hashlib
from pathlib import Path

import pytest

from dedupe_medias import (
    CHUNK_SIZE,
    IOStats,
    MatchTracker,
    group_by_hash,
    group_by_size,
    iter_media_files,
    sha256_file,
)


def test_iter_media_files_skips_symlinks_and_inaccessible(tmp_path, monkeypatch):
    valid = tmp_path / "ok.jpg"
    valid.write_bytes(b"data")
    symlink = tmp_path / "link.jpg"
    symlink.symlink_to(valid)
    inaccessible = tmp_path / "bad.jpg"
    inaccessible.write_bytes(b"nope")

    original_stat = Path.stat

    def fake_stat(self, *args, **kwargs):
        if self == inaccessible:
            raise OSError("denied")
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fake_stat)

    counters = {}
    files = list(iter_media_files(tmp_path, allowed_exts={".jpg"}, counters=counters))

    assert valid in files
    assert symlink not in files
    assert inaccessible not in files
    assert counters.get("symlinks") == 1
    assert counters.get("inaccessible") == 1


def test_sha256_file_hashes_by_chunk(tmp_path):
    payload = b"a" * (CHUNK_SIZE + 10)
    target = tmp_path / "big.bin"
    target.write_bytes(payload)

    expected = hashlib.sha256(payload).hexdigest()

    iostats = IOStats()
    digest = sha256_file(target, iostats=iostats)

    assert digest == expected
    assert iostats.total_bytes == len(payload)


def test_group_by_size_and_hash_parallel(tmp_path):
    dup_a = tmp_path / "dup1.jpg"
    dup_b = tmp_path / "dup2.jpg"
    unique = tmp_path / "unique.jpg"

    content_dup = b"duplicate-content"
    content_unique = b"other"

    dup_a.write_bytes(content_dup)
    dup_b.write_bytes(content_dup)
    unique.write_bytes(content_unique)

    by_size = group_by_size([dup_a, dup_b, unique])

    tracker = MatchTracker()
    iostats = IOStats()

    # Only hash files sharing the same size to mimic main flow
    by_hash = group_by_hash(by_size[len(content_dup)], iostats=iostats, counter_offset=0,
                            total_to_hash=len(by_size[len(content_dup)]), tracker=tracker,
                            max_workers=2)

    assert len(by_hash) == 1
    digest, paths = next(iter(by_hash.items()))
    assert set(paths) == {dup_a, dup_b}
    assert tracker.matched_files == 2
    assert digest == hashlib.sha256(content_dup).hexdigest()
