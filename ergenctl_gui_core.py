#!/usr/bin/env python3
"""Shared command definitions for the ErgenCTL graphical interface."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class GuiCommand:
    title: str
    arguments: tuple[str, ...]
    privileged: bool = False

    def argv(
        self,
        executable: str | Sequence[str] = "ergenctl",
        pkexec: str = "pkexec",
    ) -> list[str]:
        executable_parts = [executable] if isinstance(executable, str) else list(executable)
        command = [*executable_parts, *self.arguments]
        return [pkexec, *command] if self.privileged else command


COMMANDS = {
    "doctor": GuiCommand("System check", ("doctor", "--json")),
    "snapshots": GuiCommand("Snapshots", ("snapshots", "--json"), True),
    "resume": GuiCommand("Hibernation", ("resume", "--json")),
    "repair-plan": GuiCommand("Repair plan", ("fix", "all", "--dry-run", "--json"), True),
    "repair": GuiCommand("Repair system", ("fix", "all", "--yes", "--json"), True),
}

LOG_CATEGORIES = ("all", "resume", "boot", "audio", "graphics")


def log_command(previous: bool, priority: str, category: str, lines: int = 100) -> GuiCommand:
    if priority not in {"error", "warning"}:
        raise ValueError("invalid log priority")
    if category not in LOG_CATEGORIES:
        raise ValueError("invalid log category")
    if lines <= 0:
        raise ValueError("line limit must be greater than zero")
    arguments = ["logs"]
    if previous:
        arguments.append("--previous")
    arguments.extend(("--priority", priority, "--lines", str(lines), "--category", category, "--json"))
    return GuiCommand("Boot logs", tuple(arguments), True)


def rollback_command(snapshot: int, execute: bool = False) -> GuiCommand:
    if snapshot <= 0:
        raise ValueError("snapshot number must be greater than zero")
    mode = "--yes" if execute else "--dry-run"
    title = "Rollback" if execute else "Rollback plan"
    return GuiCommand(title, ("rollback", str(snapshot), mode, "--json"), True)


def format_json_output(output: str) -> str:
    """Pretty-print JSON while leaving useful non-JSON errors untouched."""
    try:
        value = json.loads(output)
    except json.JSONDecodeError:
        return output.strip()
    return json.dumps(value, indent=2, ensure_ascii=False)


def repair_plan_can_execute(report: object) -> bool:
    if not isinstance(report, dict):
        return False
    return bool(report.get("dry_run") and report.get("success") and report.get("steps"))


def is_kernel_trace_fragment(source: str, message: str) -> bool:
    if source != "kernel":
        return False
    stripped = message.strip()
    if stripped in {"Call Trace:", "<TASK>", "</TASK>"}:
        return True
    return re.match(r"^\??\s*[A-Za-z0-9_.]+\+0x[0-9a-f]+/0x[0-9a-f]+(?:\s.*)?$", stripped, re.IGNORECASE) is not None


def compact_log_message(message: str, limit: int = 240) -> str:
    first_line = message.strip().splitlines()[0] if message.strip() else "No message"
    if len(first_line) > limit:
        first_line = f"{first_line[: limit - 3].rstrip()}..."
    if "\n" in message:
        return f"{first_line} - full details hidden"
    return first_line
