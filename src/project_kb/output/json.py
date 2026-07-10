"""Shared JSON response envelope helpers."""

import json as stdlib_json
from typing import Any, NotRequired, TypedDict

from project_kb.errors import ProjectKbError
from project_kb.version import __version__

REQUIRED_ENVELOPE_FIELDS = (
    "ok",
    "result",
    "code",
    "command",
    "message",
    "data",
    "warnings",
    "error",
    "meta",
)


class WarningObject(TypedDict):
    code: str
    message: str
    details: NotRequired[dict[str, Any]]


def default_meta() -> dict[str, str | int]:
    return {
        "tool": "project-kb",
        "version": __version__,
        "contract_version": 1,
    }


def build_response(
    *,
    ok: bool,
    result: str,
    code: str,
    command: str,
    message: str,
    data: dict[str, Any] | None = None,
    warnings: list[WarningObject] | None = None,
    error: ProjectKbError | dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    serialized_error = error.to_dict() if isinstance(error, ProjectKbError) else error

    return {
        "ok": ok,
        "result": result,
        "code": code,
        "command": command,
        "message": message,
        "data": data or {},
        "warnings": warnings or [],
        "error": serialized_error,
        "meta": meta or default_meta(),
    }


def build_error_response(
    error: ProjectKbError,
    *,
    command: str,
    result: str = "blocked",
    message: str | None = None,
    data: dict[str, Any] | None = None,
    warnings: list[WarningObject] | None = None,
) -> dict[str, Any]:
    return build_response(
        ok=False,
        result=result,
        code=error.code,
        command=command,
        message=message or error.message,
        data=data,
        warnings=warnings,
        error=error,
    )


def dumps(envelope: dict[str, Any]) -> str:
    return stdlib_json.dumps(envelope, ensure_ascii=False)
