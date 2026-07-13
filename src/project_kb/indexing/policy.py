"""Deterministic Stage 4 V0 path and file classification policy."""

import os
from pathlib import PurePosixPath

from project_kb.indexing.models import ScanPolicy

PRUNED_DIRECTORY_NAMES = frozenset(
    {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__", ".tox"}
)
HARD_SECRET_NAMES = frozenset(
    {
        ".env",
        "credentials.json",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
        "secrets.toml",
        "credentials.yaml",
        "credentials.yml",
        "credentials.toml",
        "token.json",
        "tokens.json",
        "client_secret.json",
        "client_secrets.json",
        "service-account.json",
        "service_account.json",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)
SAFE_ENV_TEMPLATE_NAMES = frozenset({".env.example", ".env.sample", ".env.template", ".env.dist"})
HARD_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
SUPPORTED_TEXT_EXTENSIONS = frozenset(
    {
        ".py",
        ".pyi",
        ".md",
        ".rst",
        ".txt",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
        ".ini",
        ".cfg",
        ".csv",
        ".tsv",
        ".xml",
        ".html",
        ".css",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".sql",
        ".sh",
        ".ps1",
        ".bat",
        ".cmd",
        ".dockerfile",
    }
)
SUPPORTED_TEXT_NAMES = (
    frozenset({"readme", "license", "copying", "makefile", "dockerfile", "agents.md", ".gitignore"})
    | SAFE_ENV_TEMPLATE_NAMES
)


def normalize_relative_path(raw_path: str) -> str:
    normalized = raw_path.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("unsafe repository-relative path")
    return path.as_posix()


def path_key(relative_path: str) -> str:
    return relative_path.casefold() if os.name == "nt" else relative_path


def hard_secret_reason(relative_path: str) -> str | None:
    name = PurePosixPath(relative_path).name.casefold()
    if name in HARD_SECRET_NAMES or (
        name.startswith(".env.") and name not in SAFE_ENV_TEMPLATE_NAMES
    ):
        return "hard_secret_name"
    if name.endswith(HARD_SECRET_SUFFIXES):
        return "hard_secret_private_material"
    return None


def pruned_root(relative_path: str, policy: ScanPolicy) -> str | None:
    normalized = normalize_relative_path(relative_path)
    for configured in policy.discovered_pruned_roots:
        root = normalize_relative_path(configured)
        if normalized == root or normalized.startswith(f"{root}/"):
            return root
    parts = PurePosixPath(relative_path).parts
    for index, part in enumerate(parts[:-1]):
        if part.casefold() in PRUNED_DIRECTORY_NAMES:
            return "/".join(parts[: index + 1])
    return None


def supported_text(relative_path: str) -> bool:
    path = PurePosixPath(relative_path)
    name = path.name.casefold()
    return path.suffix.casefold() in SUPPORTED_TEXT_EXTENSIONS or name in SUPPORTED_TEXT_NAMES


def language_for(relative_path: str) -> str | None:
    suffix = PurePosixPath(relative_path).suffix.casefold()
    if suffix in {".py", ".pyi"}:
        return "python"
    return "text" if supported_text(relative_path) else None


def policy_metadata(policy: ScanPolicy) -> dict[str, object]:
    return {
        "policy_version": policy.policy_version,
        "max_text_bytes": policy.max_text_bytes,
        "binary_probe_bytes": policy.binary_probe_bytes,
        "discovered_metadata_paths": list(policy.discovered_metadata_paths),
        "discovered_pruned_roots": list(policy.discovered_pruned_roots),
    }
