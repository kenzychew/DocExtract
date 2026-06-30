"""Tests for the content-hash idempotency helper."""

import pytest

from doc_agent.utils.hash import file_sha256


def test_same_file_same_hash(tmp_path):
    """Hashing the same file twice returns the same digest."""
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"invoice content here")
    assert file_sha256(f) == file_sha256(f)


def test_different_content_different_hash(tmp_path):
    """Two files with different content produce different digests."""
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    a.write_bytes(b"content A")
    b.write_bytes(b"content B")
    assert file_sha256(a) != file_sha256(b)


def test_hash_is_hex_string(tmp_path):
    """The digest is a 64-character lowercase hex string (SHA-256)."""
    f = tmp_path / "receipt.png"
    f.write_bytes(b"\x00\x01\x02\x03")
    digest = file_sha256(f)
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_accepts_string_path(tmp_path):
    """file_sha256 accepts a plain string path as well as a Path object."""
    f = tmp_path / "doc.txt"
    f.write_bytes(b"hello")
    assert file_sha256(str(f)) == file_sha256(f)


def test_missing_file_raises(tmp_path):
    """FileNotFoundError is raised for a non-existent path."""
    with pytest.raises(FileNotFoundError):
        file_sha256(tmp_path / "nonexistent.pdf")
