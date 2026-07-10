"""Lightweight local Git repository identity metadata."""

import hashlib
import json
import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

FINGERPRINT_STRENGTHS = {"strong", "weak", "unavailable"}


@dataclass(frozen=True)
class RepositoryFingerprint:
    """Persistable repository signals that never include a raw remote URL."""

    git_root_norm: str
    git_common_dir_norm: str | None
    root_commits: tuple[str, ...]
    remote_origin_hash: str | None
    head_commit_at_registration: str | None
    fingerprint_strength: str

    def to_json(self) -> str:
        payload = asdict(self)
        payload["root_commits"] = list(self.root_commits)
        return json.dumps(payload, sort_keys=True)

    @classmethod
    def from_json(cls, value: str) -> RepositoryFingerprint:
        try:
            payload: Any = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("Repository fingerprint is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("Repository fingerprint must be a JSON object")

        git_root_norm = payload.get("git_root_norm")
        common_dir = payload.get("git_common_dir_norm")
        root_commits = payload.get("root_commits")
        remote_hash = payload.get("remote_origin_hash")
        head_commit = payload.get("head_commit_at_registration")
        strength = payload.get("fingerprint_strength")

        if not isinstance(git_root_norm, str):
            raise ValueError("Repository fingerprint git_root_norm is invalid")
        if common_dir is not None and not isinstance(common_dir, str):
            raise ValueError("Repository fingerprint git_common_dir_norm is invalid")
        if not isinstance(root_commits, list) or not all(
            isinstance(commit, str) for commit in root_commits
        ):
            raise ValueError("Repository fingerprint root_commits is invalid")
        if remote_hash is not None and not isinstance(remote_hash, str):
            raise ValueError("Repository fingerprint remote_origin_hash is invalid")
        if head_commit is not None and not isinstance(head_commit, str):
            raise ValueError("Repository fingerprint head_commit_at_registration is invalid")
        if strength not in FINGERPRINT_STRENGTHS:
            raise ValueError("Repository fingerprint strength is invalid")

        return cls(
            git_root_norm=git_root_norm,
            git_common_dir_norm=common_dir,
            root_commits=tuple(root_commits),
            remote_origin_hash=remote_hash,
            head_commit_at_registration=head_commit,
            fingerprint_strength=strength,
        )


@dataclass(frozen=True)
class FingerprintComparison:
    matches: bool
    status: str
    reason: str


def build_repository_fingerprint(repo_root: Path) -> RepositoryFingerprint:
    """Build bounded identity metadata using local-only Git commands."""

    resolved_root = repo_root.expanduser().resolve()
    common_dir_output = _git_output(resolved_root, "rev-parse", "--git-common-dir")
    common_dir_norm: str | None = None
    if common_dir_output:
        common_dir = Path(common_dir_output)
        if not common_dir.is_absolute():
            common_dir = resolved_root / common_dir
        common_dir_norm = normalize_path(common_dir)

    roots_output = _git_output(resolved_root, "rev-list", "--max-parents=0", "--all")
    root_commits = tuple(sorted(set(roots_output.splitlines()))) if roots_output else ()
    head_commit = _git_output(resolved_root, "rev-parse", "--verify", "HEAD")
    is_shallow = _git_output(resolved_root, "rev-parse", "--is-shallow-repository") == "true"

    remote_origin = _git_output(resolved_root, "config", "--get", "remote.origin.url")
    remote_origin_hash = (
        hashlib.sha256(remote_origin.encode("utf-8")).hexdigest() if remote_origin else None
    )

    if root_commits and not is_shallow:
        strength = "strong"
    elif root_commits or head_commit or remote_origin_hash:
        strength = "weak"
    else:
        strength = "unavailable"

    return RepositoryFingerprint(
        git_root_norm=normalize_path(resolved_root),
        git_common_dir_norm=common_dir_norm,
        root_commits=root_commits,
        remote_origin_hash=remote_origin_hash,
        head_commit_at_registration=head_commit,
        fingerprint_strength=strength,
    )


def compare_repository_fingerprints(
    stored: RepositoryFingerprint,
    current: RepositoryFingerprint,
    *,
    repo_root: Path,
) -> FingerprintComparison:
    """Compare stable identity anchors without requiring the current HEAD to stay fixed."""

    if stored.git_root_norm != current.git_root_norm:
        return FingerprintComparison(False, "mismatch", "fingerprint_git_root_changed")

    if stored.fingerprint_strength == "strong" and stored.root_commits:
        if not set(stored.root_commits).issubset(current.root_commits):
            return FingerprintComparison(False, "mismatch", "root_commits_changed")
        return FingerprintComparison(True, "matched", "strong_fingerprint_matched")

    if stored.head_commit_at_registration:
        if not _git_commit_exists(repo_root, stored.head_commit_at_registration):
            return FingerprintComparison(False, "mismatch", "registration_commit_missing")
        return FingerprintComparison(True, "matched", "registration_commit_present")

    if stored.remote_origin_hash:
        if stored.remote_origin_hash != current.remote_origin_hash:
            return FingerprintComparison(False, "mismatch", "remote_origin_hash_changed")
        return FingerprintComparison(True, "matched", "remote_origin_hash_matched")

    if stored.root_commits and not set(stored.root_commits).issubset(current.root_commits):
        return FingerprintComparison(False, "mismatch", "root_commits_changed")

    return FingerprintComparison(True, "unavailable", "fingerprint_identity_unavailable")


def normalize_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path.expanduser().resolve(strict=False)))


def _git_commit_exists(repo_root: Path, commit: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            env=_safe_git_environment(),
        )
    except OSError:
        return False
    return result.returncode == 0


def _git_output(repo_root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            env=_safe_git_environment(),
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _safe_git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["GIT_NO_LAZY_FETCH"] = "1"
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    return environment
