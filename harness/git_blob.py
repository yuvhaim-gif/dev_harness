#!/usr/bin/env python3
"""Shared helper: read a file's content at a git ref (``git show <ref>:<path>``).

Several call sites need "the bytes of ``path`` as they exist at ``ref``, or
``None`` when that path does not exist there (e.g. a deletion)": the CI re-check,
the optimistic staleness guard, and the post-hoc containment gate. Encoding it
once keeps their behaviour identical. Depends only on the standard library.
"""

from __future__ import annotations

import os
import subprocess


def read_blob(repo_dir: str | os.PathLike[str] | None, ref: str, path: str) -> str | None:
    """Content of ``path`` at ``ref``; ``None`` when absent there or on error.

    ``repo_dir`` is the working directory to run git in (``None`` uses the
    process CWD). The result is git's raw stdout (trailing newline preserved),
    so callers that compare two reads see byte-for-byte equality.
    """
    res = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    return res.stdout if res.returncode == 0 else None
