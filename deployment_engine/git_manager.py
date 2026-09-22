import subprocess
from pathlib import Path


class GitError(Exception):
    """Raised when a Git operation fails."""


def clone_repository(
    clone_url: str,
    branch: str,
    destination: Path,
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)

    command = [
        "git",
        "clone",
        "--branch",
        branch,
        "--single-branch",
        clone_url,
        str(destination),
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise GitError(
            result.stderr.strip() or "Git clone failed."
        )

    return destination