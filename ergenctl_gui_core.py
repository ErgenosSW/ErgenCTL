#!/usr/bin/env python3
"""Shared command definitions for the ErgenCTL graphical interface."""

from __future__ import annotations

import json
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
