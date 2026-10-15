#!/usr/bin/env python3
"""Persist validated paper outputs without overwriting concurrent state changes."""
from pathlib import Path
import subprocess

STATE_FILES = ("crypto_daytrade_state.json", "scan_live.json", "signal.json")


def git(root, *arguments, check=True):
    result = subprocess.run(["git", *arguments], cwd=root, text=True,
                            capture_output=True, timeout=60)
    if check and result.returncode:
        raise RuntimeError(f"git {arguments[0]} failed: {result.stderr[-2000:]}")
    return result


def save_state(root=None, attempts=3):
    root = Path(root or Path(__file__).resolve().parent)
    if attempts < 1:
        raise ValueError("attempts must be positive")
    base = git(root, "rev-parse", "HEAD").stdout.strip()
    # Commit only canonical outputs; never sweep code, backups or credentials.
    git(root, "add", "--", *STATE_FILES)
    staged = set(git(root, "diff", "--cached", "--name-only").stdout.splitlines())
    if not staged:
        return False
    if not staged.issubset(STATE_FILES):
        raise RuntimeError("Unexpected staged files; refusing to commit")
    git(root, "commit", "-m", "chore: paper scan")
    for _ in range(attempts):
        git(root, "fetch", "origin", "main")
        ancestor = git(root, "merge-base", "--is-ancestor", base, "origin/main", check=False)
        if ancestor.returncode:
            raise RuntimeError("main history changed; refusing automatic state publication")
        changed = git(root, "diff", "--quiet", base, "origin/main", "--", *STATE_FILES, check=False)
        if changed.returncode:
            raise RuntimeError("Canonical state changed remotely during this scan; refusing overwrite")
        rebased = git(root, "rebase", "origin/main", check=False)
        if rebased.returncode:
            git(root, "rebase", "--abort", check=False)
            raise RuntimeError("Rebase failed; no state was pushed")
        pushed = git(root, "push", "origin", "HEAD:main", check=False)
        if pushed.returncode == 0:
            return True
    raise RuntimeError("State push failed after bounded retries; no force-push attempted")


if __name__ == "__main__":
    save_state()
