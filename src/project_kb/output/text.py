"""Human-readable output helpers."""

from project_kb.errors import ProjectKbError


def format_error(error: ProjectKbError) -> str:
    return error.message


def format_capabilities(commands: dict[str, bool]) -> str:
    lines = ["Project KB capabilities:"]
    for command, implemented in commands.items():
        status = "available" if implemented else "not implemented"
        lines.append(f"- {command}: {status}")
    return "\n".join(lines)
