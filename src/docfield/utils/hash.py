"""Content-hash utilities for idempotency."""

import hashlib
from pathlib import Path


def file_sha256(path: Path | str) -> str:
    """Compute the SHA-256 hex digest of a file's content.

    Reads the file in streaming chunks so large documents do not need
    to fit in memory all at once.

    Args:
        path: Path to the file to hash.

    Returns:
        Lowercase hex string of the SHA-256 digest.

    Raises:
        FileNotFoundError: If the path does not exist.
        IsADirectoryError: If the path is a directory.
    """
    target = Path(path)
    h = hashlib.sha256()
    with target.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
