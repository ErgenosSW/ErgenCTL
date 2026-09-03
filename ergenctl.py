#!/usr/bin/env python3
"""ErgenCTL - diagnostics and recovery for ErgenOS."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Sequence


APP_NAME = "ErgenCTL"
VERSION = "0.1.1-alpha"
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Check:
    id: str
    title: str
    status: str
    summary: str
    evidence: str | None = None


@dataclass(frozen=True)
class SnapshotReport:
    boot_mode: str
    current_snapshot: int | None
    available: bool
    listing: str | None
    message: str | None = None


@dataclass(frozen=True)
class BootLogReport:
    boot: str
    priority: str
    category: str
    available: bool
    entries: list[str]
    groups: list[dict[str, object]]
    message: str | None = None


@dataclass(frozen=True)
class ResumeReport:
    boot_mode: str
    hibernation_configured: bool
    noresume: bool
    resume_parameter: str | None
    checks: list[Check]


@dataclass(frozen=True)
class RepairStep:
    id: str
    title: str
    commands: tuple[tuple[str, ...], ...]
    backup_paths: tuple[str, ...] = ()
    reboot_required: bool = False
    internal_actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepairReport:
    target: str
    dry_run: bool
    success: bool
    steps: list[str]
    executed_commands: list[list[str]]
    backup_directory: str | None = None
    safety_snapshot: str | None = None
    reboot_required: bool = False
    message: str | None = None
    executed_actions: list[str] = field(default_factory=list)
    system_root: str = "/"
    recovery_mode: bool = False


@dataclass
class RepairEnvironment:
    root: Path = Path("/")
    recovery: bool = False
    mount_directory: Path | None = None
    mounted_paths: list[Path] = field(default_factory=list)


@dataclass(frozen=True)
class RollbackReport:
    snapshot: int
    dry_run: bool
    success: bool
    source_subvolume: str | None = None
    replaced_subvolume: str | None = None
    preserved_subvolume: str | None = None
    commands: list[list[str]] = field(default_factory=list)
    reboot_required: bool = False
    message: str | None = None


REPAIR_TARGETS = ("grub-snapshots", "pacman-hooks", "services", "resume", "repositories", "all")
BTRFS_REPAIR_TARGETS = {"grub-snapshots", "pacman-hooks", "services"}

REPAIR_STEPS = {
    "repositories": RepairStep(
        "repositories",
        "Enable required Pacman repositories",
        (),
        ("/etc/pacman.conf",),
        internal_actions=("enable-multilib",),
    ),
    "pacman-hooks": RepairStep(
        "pacman-hooks",
        "Restore Pacman snapshot hooks",
        (("/usr/bin/pacman", "-S", "--noconfirm", "snap-pac"),),
    ),
    "services": RepairStep(
        "services",
        "Enable snapshot services",
        (
            ("/usr/bin/systemctl", "enable", "--now", "grub-btrfsd.service"),
            ("/usr/bin/systemctl", "enable", "--now", "snapper-cleanup.timer"),
        ),
    ),
    "resume": RepairStep(
        "resume",
        "Rebuild hibernation resume configuration",
        (
            ("/usr/bin/mkinitcpio", "-P"),
            ("/usr/bin/grub-mkconfig", "-o", "/boot/grub/grub.cfg"),
        ),
        ("/etc/default/grub", "/etc/mkinitcpio.conf", "/boot/grub/grub.cfg"),
        True,
        ("configure-resume",),
    ),
    "grub-snapshots": RepairStep(
        "grub-snapshots",
        "Regenerate the GRUB snapshot menu",
        (("/etc/grub.d/41_snapshots-btrfs",),),
        ("/boot/grub/grub-btrfs.cfg",),
    ),
}
LOG_CATEGORY_PATTERNS = {
    "resume": (r"\bresume\b", r"\bhibernat", r"\bsuspend", r"\bswap(?:file|space)?\b"),
    "boot": ("kernel", "systemd", "boot", "mount", "initramfs", "grub"),
    "audio": ("pipewire", "wireplumber", "pulse", "rtkit", "sink", "audio"),
    "graphics": ("vulkan", "gpu", "drm", "kms", "mutter", "gnome-shell", "nvidia", "nouveau", "amdgpu"),
}

RESUME_SOURCES = (
    "kernel",
    "systemd",
    "systemd-hibernate-resume",
    "systemd-sleep",
    "dracut",
    "mkinitcpio",
)

STATUS_LABELS = {
    "pass": "OK",
    "warning": "WARN",
    "fail": "FAIL",
    "skipped": "SKIP",
    "unknown": "?",
}

STATUS_COLORS = {
    "pass": "32",
    "warning": "33",
    "fail": "31",
    "skipped": "36",
    "unknown": "35",
}


def read_cmdline() -> str:
    try:
        return Path("/proc/cmdline").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def resume_parameter_from_cmdline(cmdline: str) -> str | None:
    for parameter in cmdline.split():
        if parameter.startswith("resume="):
            value = parameter.partition("=")[2]
            return value or None
    return None


def resolve_resume_device(parameter: str) -> str | None:
    prefixes = {"UUID": "/dev/disk/by-uuid", "PARTUUID": "/dev/disk/by-partuuid"}
    kind, separator, value = parameter.partition("=")
    candidate = Path(prefixes[kind]) / value if separator and kind in prefixes else Path(parameter)
    try:
        return str(candidate.resolve(strict=True))
    except OSError:
        return None


def active_swap_devices() -> set[str]:
    try:
        lines = Path("/proc/swaps").read_text(encoding="utf-8").splitlines()[1:]
    except OSError:
        return set()

    devices: set[str] = set()
    for line in lines:
        fields = line.split()
        if not fields:
            continue
        device = fields[0].replace("\\040", " ")
        try:
            devices.add(str(Path(device).resolve(strict=True)))
        except OSError:
            devices.add(device)
    return devices


def resume_hook_configured() -> bool:
    try:
        content = Path("/etc/mkinitcpio.conf").read_text(encoding="utf-8")
    except OSError:
        return False
    return re.search(r"^HOOKS=.*(?:\s|\()resume(?:\s|\))", content, re.MULTILINE) is not None


def run(command: Sequence[str]) -> tuple[int, str, str]:
    """Run a read-only command without invoking a shell."""
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        return 127, "", str(error)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def read_os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = Path("/etc/os-release").read_text(encoding="utf-8").splitlines()
    except OSError:
        return values

    for line in lines:
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"')
    return values


def distribution_check() -> Check:
    release = read_os_release()
    name = release.get("PRETTY_NAME", release.get("NAME", "Unknown Linux"))
    is_ergenos = release.get("ID") == "ergenos"
    return Check(
        id="distribution",
        title="Operating system",
        status="pass" if is_ergenos else "warning",
        summary=name,
        evidence=f"ID={release.get('ID', 'unknown')}",
    )


def firmware_check() -> Check:
    uefi = Path("/sys/firmware/efi").is_dir()
    return Check(
        id="firmware",
        title="Firmware",
        status="pass",
        summary="UEFI" if uefi else "BIOS/legacy",
    )


def root_check() -> Check:
    code, output, error = run(["findmnt", "--noheadings", "--output", "FSTYPE,OPTIONS", "/"])
    if code != 0 or not output:
        return Check("root", "Root filesystem", "unknown", "Could not inspect root filesystem", error or None)

    fstype, _, options = output.partition(" ")
    return Check("root", "Root filesystem", "pass", fstype, options or None)


def root_filesystem_type() -> str | None:
    code, output, _ = run(["findmnt", "--noheadings", "--output", "FSTYPE", "/"])
    return output.splitlines()[0].strip() if code == 0 and output else None


def snapshot_boot_detected() -> tuple[bool, str]:
    """Detect a grub-btrfs snapshot boot without changing system state."""
    code, output, _ = run(["findmnt", "--noheadings", "--output", "FSTYPE", "/"])
    fstype = output.splitlines()[0].strip() if code == 0 and output else "unknown"
    cmdline = read_cmdline()

    snapshot_subvolume = "@snapshots/" in cmdline and "/snapshot" in cmdline
    snapshot_boot = fstype == "overlay" or snapshot_subvolume
    evidence = f"root_fstype={fstype}, snapshot_subvolume={'yes' if snapshot_subvolume else 'no'}"
    return snapshot_boot, evidence


def boot_mode_check(detection: tuple[bool, str] | None = None) -> Check:
    snapshot_boot, evidence = detection if detection is not None else snapshot_boot_detected()
    if "root_fstype=unknown" in evidence:
        return Check("boot-mode", "Boot mode", "unknown", "Could not determine boot mode", evidence)
    return Check(
        "boot-mode",
        "Boot mode",
        "pass",
        "snapshot" if snapshot_boot else "normal",
        evidence,
    )


def disk_space_check(detection: tuple[bool, str] | None = None) -> Check:
    snapshot_boot, evidence = detection if detection is not None else snapshot_boot_detected()
    if snapshot_boot:
        return Check(
            "disk-space",
            "Disk space",
            "skipped",
            "Unavailable while booted from snapshot overlay",
            evidence,
        )

    try:
        usage = shutil.disk_usage("/")
    except OSError as error:
        return Check("disk-space", "Disk space", "unknown", "Could not inspect disk space", str(error))

    gib = 1024**3
    free_percent = usage.free / usage.total * 100 if usage.total else 0.0
    if usage.free < 2 * gib:
        status = "fail"
    elif free_percent < 10:
        status = "warning"
    else:
        status = "pass"
    return Check(
        "disk-space",
        "Disk space",
        status,
        f"{usage.free / gib:.1f} GiB free ({free_percent:.1f}%)",
        f"total={usage.total / gib:.1f} GiB, used={usage.used / gib:.1f} GiB",
    )


def snapper_check(root_fstype: str | None = None) -> Check:
    if root_fstype is not None and root_fstype != "btrfs":
        return Check("snapper", "Snapper", "skipped", f"not applicable on {root_fstype}")
    if not shutil.which("snapper"):
        return Check("snapper", "Snapper", "skipped", "snapper is not installed")

    code, output, error = run(["snapper", "list-configs", "--columns", "config"])
    if code != 0:
        return Check("snapper", "Snapper", "warning", "Could not read Snapper configurations", error or None)

    configs = [
        stripped
        for line in output.splitlines()
        if (stripped := line.strip())
        and stripped.lower() != "config"
        and stripped.strip("-")
    ]
    root_configured = "root" in configs
    return Check(
        "snapper",
        "Snapper",
        "pass" if root_configured else "warning",
        "root configuration found" if root_configured else "root configuration not found",
        ", ".join(configs) if configs else None,
    )


def snapshot_count_check(
    detection: tuple[bool, str] | None = None,
    root_fstype: str | None = None,
) -> Check:
    if root_fstype is not None and root_fstype != "btrfs":
        return Check("snapshots", "Snapshots", "skipped", f"not applicable on {root_fstype}")
    if not shutil.which("snapper"):
        return Check("snapshots", "Snapshots", "skipped", "snapper is not installed")

    snapshot_boot, evidence = detection if detection is not None else snapshot_boot_detected()
    if snapshot_boot:
        return Check(
            "snapshots",
            "Snapshots",
            "skipped",
            "Unavailable while booted from snapshot overlay",
            evidence,
        )

    code, output, error = run(["snapper", "-c", "root", "list", "--columns", "number"])
    if code != 0:
        diagnostic = "\n".join(part for part in (output, error) if part).lower()
        if "no permissions" in diagnostic or "permission denied" in diagnostic:
            return Check(
                "snapshots",
                "Snapshots",
                "skipped",
                "Insufficient permissions",
                error or output or None,
            )
        return Check("snapshots", "Snapshots", "warning", "Could not count snapshots", error or None)

    numbers: set[int] = set()
    for line in output.splitlines():
        first_field = line.strip().split(maxsplit=1)[0].strip("|") if line.strip() else ""
        if first_field.isdigit():
            number = int(first_field)
            if number != 0:
                numbers.add(number)
    count = len(numbers)
    return Check("snapshots", "Snapshots", "pass", f"{count} found", ", ".join(map(str, sorted(numbers))) or None)


def service_check(root_fstype: str | None = None) -> Check:
    if root_fstype is not None and root_fstype != "btrfs":
        return Check("grub-btrfsd", "grub-btrfsd", "skipped", f"not applicable on {root_fstype}")
    if not shutil.which("systemctl"):
        return Check("grub-btrfsd", "grub-btrfsd", "unknown", "systemctl is unavailable")

    code, output, error = run(
        [
            "systemctl",
            "show",
            "grub-btrfsd.service",
            "--property=LoadState",
            "--property=ActiveState",
        ]
    )
    states: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            states[key.strip()] = value.strip()

    loaded = states.get("LoadState") == "loaded"
    active = states.get("ActiveState") == "active"
    if code == 0 and loaded and active:
        return Check("grub-btrfsd", "grub-btrfsd", "pass", "active")
    return Check("grub-btrfsd", "grub-btrfsd", "warning", "not active", error or output or None)


def live_environment_detected() -> bool:
    cmdline = read_cmdline()
    return Path("/run/archiso/bootmnt").exists() or "archisobasedir=" in cmdline


def pacman_hooks_check(root_fstype: str | None = None) -> Check:
    if root_fstype is not None and root_fstype != "btrfs":
        return Check("pacman-hooks", "Pacman snapshot hooks", "skipped", f"not applicable on {root_fstype}")
    hook_directories = (Path("/etc/pacman.d/hooks"), Path("/usr/share/libalpm/hooks"))
    required = ("05-snap-pac-pre.hook", "zz-snap-pac-post.hook")
    found = {
        name: next((directory / name for directory in hook_directories if (directory / name).is_file()), None)
        for name in required
    }
    missing = [name for name, path in found.items() if path is None]
    if missing:
        return Check(
            "pacman-hooks",
            "Pacman snapshot hooks",
            "fail",
            f"{len(missing)} required hook(s) missing",
            ", ".join(missing),
        )
    return Check(
        "pacman-hooks",
        "Pacman snapshot hooks",
        "pass",
        "pre/post hooks installed",
        ", ".join(str(found[name]) for name in required),
    )


def boot_files_check() -> Check:
    if live_environment_detected():
        return Check("boot-files", "Boot files", "skipped", "Not applicable in live environment")

    required = (Path("/boot/vmlinuz-linux-zen"), Path("/boot/initramfs-linux-zen.img"))
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        return Check("boot-files", "Boot files", "fail", "linux-zen boot files missing", ", ".join(missing))
    return Check("boot-files", "Boot files", "pass", "linux-zen kernel and initramfs present")


def grub_config_check(root_fstype: str | None = None) -> Check:
    if live_environment_detected():
        return Check("grub-config", "GRUB configuration", "skipped", "Not applicable in live environment")

    main_config = Path("/boot/grub/grub.cfg")
    snapshot_config = Path("/boot/grub/grub-btrfs.cfg")
    if not main_config.is_file():
        return Check("grub-config", "GRUB configuration", "fail", "grub.cfg is missing", str(main_config))
    if root_fstype is not None and root_fstype != "btrfs":
        return Check("grub-config", "GRUB configuration", "pass", "main configuration present")
    if not snapshot_config.is_file():
        return Check(
            "grub-config",
            "GRUB configuration",
            "warning",
            "snapshot menu configuration is missing",
            str(snapshot_config),
        )
    return Check("grub-config", "GRUB configuration", "pass", "main and snapshot configurations present")


def repositories_check() -> Check:
    if not shutil.which("pacman-conf"):
        return Check("repositories", "Pacman repositories", "unknown", "pacman-conf is unavailable")

    code, output, error = run(["pacman-conf", "--repo-list"])
    if code != 0:
        return Check("repositories", "Pacman repositories", "unknown", "Could not read repositories", error or None)

    repositories = {line.strip() for line in output.splitlines() if line.strip()}
    required = {"core", "extra", "multilib"}
    missing = sorted(required - repositories)
    if missing:
        return Check(
            "repositories",
            "Pacman repositories",
            "fail",
            f"Required repositories missing: {', '.join(missing)}",
            ", ".join(sorted(repositories)) or None,
        )
    return Check(
        "repositories",
        "Pacman repositories",
        "pass",
        "core, extra and multilib enabled",
        ", ".join(sorted(repositories)),
    )


def flathub_check() -> Check:
    if not shutil.which("flatpak"):
        return Check("flathub", "Flathub", "warning", "flatpak is not installed")

    code, output, error = run(["flatpak", "remotes", "--system", "--columns=name"])
    if code != 0:
        return Check("flathub", "Flathub", "unknown", "Could not read system remotes", error or None)
    remotes = {line.strip() for line in output.splitlines() if line.strip()}
    if "flathub" not in remotes:
        return Check("flathub", "Flathub", "warning", "system remote is not configured")
    return Check("flathub", "Flathub", "pass", "system remote configured")


def failed_units_check() -> Check:
    if not shutil.which("systemctl"):
        return Check("failed-units", "Failed systemd units", "unknown", "systemctl is unavailable")

    code, output, error = run(["systemctl", "--failed", "--no-legend", "--plain"])
    if code != 0:
        return Check("failed-units", "Failed systemd units", "unknown", "Could not query systemd", error or None)
    units = [line.split()[0] for line in output.splitlines() if line.strip()]
    if units:
        return Check(
            "failed-units",
            "Failed systemd units",
            "warning",
            f"{len(units)} failed",
            ", ".join(units),
        )
    return Check("failed-units", "Failed systemd units", "pass", "none")


def collect_checks(root_fstype: str | None = None) -> list[Check]:
    if root_fstype is None:
        root_fstype = root_filesystem_type()
    snapshot_detection = snapshot_boot_detected()
    return [
        distribution_check(),
        Check("kernel", "Kernel", "pass", platform.release()),
        firmware_check(),
        root_check(),
        boot_mode_check(snapshot_detection),
        disk_space_check(snapshot_detection),
        snapper_check(root_fstype),
        snapshot_count_check(snapshot_detection, root_fstype),
        service_check(root_fstype),
    ]


def collect_doctor_checks() -> list[Check]:
    root_fstype = root_filesystem_type()
    return collect_checks(root_fstype) + [
        pacman_hooks_check(root_fstype),
        boot_files_check(),
        grub_config_check(root_fstype),
        repositories_check(),
        flathub_check(),
        failed_units_check(),
    ]


def snapshot_number_from_cmdline(cmdline: str) -> int | None:
    match = re.search(r"@snapshots/(\d+)/snapshot", cmdline)
    return int(match.group(1)) if match else None


def collect_snapshot_report() -> tuple[SnapshotReport, bool]:
    cmdline = read_cmdline()
    current_snapshot = snapshot_number_from_cmdline(cmdline)
    snapshot_boot, _ = snapshot_boot_detected()
    if snapshot_boot:
        return SnapshotReport(
            boot_mode="snapshot",
            current_snapshot=current_snapshot,
            available=False,
            listing=None,
            message="Snapshot listing is unavailable while booted from snapshot overlay",
        ), False

    if not shutil.which("snapper"):
        return SnapshotReport(
            boot_mode="normal",
            current_snapshot=current_snapshot,
            available=False,
            listing=None,
            message="snapper is not installed",
        ), True

    code, output, error = run(["snapper", "-c", "root", "list"])
    if code == 0:
        return SnapshotReport(
            boot_mode="normal",
            current_snapshot=current_snapshot,
            available=True,
            listing=output or None,
            message=None if output else "No snapshots found",
        ), False

    diagnostic = "\n".join(part for part in (output, error) if part)
    lowered = diagnostic.lower()
    if "no permissions" in lowered or "permission denied" in lowered:
        message = "Insufficient permissions. Run this command with sudo."
    else:
        message = f"Could not list snapshots: {diagnostic}" if diagnostic else "Could not list snapshots"
    return SnapshotReport("normal", current_snapshot, False, None, message), True


def collect_boot_log(
    previous: bool = False,
    priority: str = "error",
    lines: int = 100,
    category: str = "all",
) -> tuple[BootLogReport, bool]:
    boot = "previous" if previous else "current"
    if not shutil.which("journalctl"):
        return BootLogReport(boot, priority, category, False, [], [], "journalctl is not available"), True

    boot_id = "-1" if previous else "0"
    journal_priority = "warning..alert" if priority == "warning" else "err..alert"
    query_lines = lines if category == "all" else min(max(lines * 10, 500), 5000)
    code, output, error = run(
        [
            "journalctl",
            "--boot",
            boot_id,
            "--priority",
            journal_priority,
            "--lines",
            str(query_lines),
            "--no-pager",
            "--output",
            "short-iso",
        ]
    )
    if code != 0:
        diagnostic = "\n".join(part for part in (output, error) if part)
        lowered = diagnostic.lower()
        if "permission" in lowered or "not authorized" in lowered:
            message = "Insufficient permissions. Run this command with sudo."
        elif previous and ("no journal" in lowered or "not found" in lowered):
            message = "Previous boot journal is not available"
        else:
            message = f"Could not read boot journal: {diagnostic}" if diagnostic else "Could not read boot journal"
        return BootLogReport(boot, priority, category, False, [], [], message), True

    entries = parse_journal_entries(output)
    if category != "all":
        entries = [entry for entry in entries if log_matches_category(entry, category)][-lines:]
    groups = group_log_entries(entries)
    message = None if entries else "No errors found"
    return BootLogReport(boot, priority, category, True, entries, groups, message), False


def parse_journal_entries(output: str) -> list[str]:
    entries: list[str] = []
    for line in output.splitlines():
        if not line.strip() or line.strip() == "-- No entries --":
            continue
        if line[0].isspace() and entries:
            entries[-1] = f"{entries[-1]}\n{line.strip()}"
        else:
            entries.append(line.strip())
    return entries


def split_journal_entry(entry: str) -> tuple[str, str]:
    first_line, _, continuation = entry.partition("\n")
    match = re.match(r"^\S+\s+\S+\s+([^:]+):\s*(.*)$", first_line)
    if match:
        source = re.sub(r"\[\d+\]$", "", match.group(1)).strip()
        message = match.group(2).strip()
    else:
        source = "journal"
        message = first_line.strip()
    if continuation:
        message = f"{message}\n{continuation}"
    return source, message


def log_matches_category(entry: str, category: str) -> bool:
    if category == "all":
        return True

    source, message = split_journal_entry(entry)
    source_lower = source.lower()
    searchable = f"{source} {message}"
    if category == "resume":
        if not any(source_lower == allowed or source_lower.startswith(f"{allowed}-") for allowed in RESUME_SOURCES):
            return False
        return any(re.search(pattern, message, re.IGNORECASE) for pattern in LOG_CATEGORY_PATTERNS[category])
    return any(re.search(pattern, searchable, re.IGNORECASE) for pattern in LOG_CATEGORY_PATTERNS[category])


def group_log_entries(entries: list[str]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str], int] = {}
    for entry in entries:
        source, message = split_journal_entry(entry)
        if not message:
            continue
        key = (source, message)
        grouped[key] = grouped.get(key, 0) + 1
    return [
        {"source": source, "message": message, "count": count}
        for (source, message), count in grouped.items()
    ]


def collect_resume_report() -> ResumeReport:
    cmdline = read_cmdline()
    snapshot_boot, boot_evidence = snapshot_boot_detected()
    boot_mode = "snapshot" if snapshot_boot else "normal"
    noresume = "noresume" in cmdline.split()
    resume_parameter = resume_parameter_from_cmdline(cmdline)
    hibernation_configured = bool(resume_parameter or resume_hook_configured())
    checks: list[Check] = []

    if snapshot_boot and noresume:
        checks.append(Check("resume-policy", "Resume policy", "pass", "disabled for snapshot boot", boot_evidence))
    elif snapshot_boot:
        checks.append(
            Check("resume-policy", "Resume policy", "warning", "noresume is missing for snapshot boot", boot_evidence)
        )
    elif noresume:
        checks.append(Check("resume-policy", "Resume policy", "warning", "disabled during normal boot", "noresume"))
    else:
        checks.append(Check("resume-policy", "Resume policy", "pass", "enabled for normal boot"))

    if resume_parameter:
        parameter_status = "skipped" if noresume else "pass"
        parameter_summary = "ignored because noresume is active" if noresume else resume_parameter
        checks.append(Check("resume-parameter", "Resume parameter", parameter_status, parameter_summary, resume_parameter))
    else:
        status = "skipped" if noresume or not hibernation_configured else "warning"
        if noresume:
            summary = "not required while noresume is active"
        elif not hibernation_configured:
            summary = "hibernation is not configured"
        else:
            summary = "missing from kernel command line"
        checks.append(Check("resume-parameter", "Resume parameter", status, summary))

    if noresume:
        checks.append(Check("resume-device", "Resume device", "skipped", "not checked while noresume is active"))
    elif not resume_parameter:
        status = "skipped" if not hibernation_configured else "warning"
        summary = "hibernation is not configured" if not hibernation_configured else "cannot check without resume parameter"
        checks.append(Check("resume-device", "Resume device", status, summary))
    else:
        resolved = resolve_resume_device(resume_parameter)
        swaps = active_swap_devices()
        if resolved is None:
            checks.append(Check("resume-device", "Resume device", "fail", "configured device was not found", resume_parameter))
        elif resolved not in swaps:
            checks.append(Check("resume-device", "Resume device", "warning", "configured device is not active swap", resolved))
        else:
            checks.append(Check("resume-device", "Resume device", "pass", "configured device is active", resolved))

    log_report, log_failed = collect_boot_log(priority="warning", lines=200, category="resume")
    if log_failed:
        checks.append(Check("resume-log", "Resume log", "unknown", log_report.message or "could not read journal"))
    elif log_report.groups:
        checks.append(
            Check(
                "resume-log",
                "Resume log",
                "warning",
                f"{len(log_report.entries)} relevant message(s)",
                "; ".join(str(group["message"]) for group in log_report.groups[:3]),
            )
        )
    else:
        checks.append(Check("resume-log", "Resume log", "pass", "no warnings found"))

    return ResumeReport(boot_mode, hibernation_configured, noresume, resume_parameter, checks)


def selected_repair_steps(target: str) -> list[RepairStep]:
    if target == "all":
        order = ("repositories", "pacman-hooks", "services", "resume", "grub-snapshots")
        return [REPAIR_STEPS[name] for name in order]
    return [REPAIR_STEPS[target]]


def fstab_entries(fstab: Path) -> list[tuple[str, str, str, str]]:
    try:
        lines = fstab.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split()
        if len(fields) >= 4:
            entries.append((fields[0], fields[1].replace("\\040", " "), fields[2], fields[3]))
    return entries


def configured_subvolume(fstab: Path, requested_mountpoint: str) -> str | None:
    for _, mountpoint, fstype, options in fstab_entries(fstab):
        if mountpoint != requested_mountpoint or fstype != "btrfs":
            continue
        for option in options.split(","):
            if option.startswith("subvol="):
                return option.partition("=")[2].lstrip("/")
    return None


def configured_root_subvolume(fstab: Path = Path("/etc/fstab")) -> str | None:
    return configured_subvolume(fstab, "/")


def root_device_from_cmdline(cmdline: str | None = None) -> str | None:
    for parameter in (cmdline if cmdline is not None else read_cmdline()).split():
        if parameter.startswith("root="):
            return resolve_resume_device(parameter.partition("=")[2])
    return None


def mount_recovery_filesystem(
    source: str,
    target: Path,
    fstype: str | None = None,
    options: str | None = None,
    read_only: bool = False,
) -> str | None:
    target.mkdir(parents=True, exist_ok=True)
    command = ["/usr/bin/mount"]
    if fstype and fstype != "auto":
        command.extend(("-t", fstype))
    mount_options = [item for item in (options or "").split(",") if item and item not in {"defaults", "rw"}]
    if read_only and "ro" not in mount_options:
        mount_options.append("ro")
    if mount_options:
        command.extend(("-o", ",".join(mount_options)))
    command.extend((source, str(target)))
    code, output, error = run_repair_command(command)
    return None if code == 0 else error or output or f"mount exited with code {code}"


def existing_mount_source(mountpoint: str) -> str | None:
    code, output, _ = run(["findmnt", "--noheadings", "--output", "TARGET,SOURCE", "--target", mountpoint])
    if code != 0 or not output:
        return None
    fields = output.splitlines()[0].split(maxsplit=1)
    if len(fields) != 2 or fields[0] != mountpoint:
        return None
    return fields[1]


def mount_recovery_bind(source: str, target: Path, read_only: bool) -> str | None:
    options = "bind,ro" if read_only else "bind"
    code, output, error = run_repair_command(["/usr/bin/mount", "-o", options, source, str(target)])
    return None if code == 0 else error or output or f"bind mount exited with code {code}"


def prepare_repair_environment(dry_run: bool) -> tuple[RepairEnvironment | None, str | None]:
    snapshot_boot, _ = snapshot_boot_detected()
    if not snapshot_boot:
        return RepairEnvironment(), None
    if os.geteuid() != 0:
        return None, "Root privileges are required to access the base system. Run this command with sudo."
    device = root_device_from_cmdline()
    subvolume = configured_root_subvolume()
    if not device:
        return None, "Could not resolve the base system device from the kernel command line"
    if not subvolume or subvolume.startswith("@snapshots/"):
        return None, "Could not determine the base root subvolume from /etc/fstab"
    base = Path("/run/ergenctl")
    base.mkdir(parents=True, exist_ok=True)
    mount_directory = Path(tempfile.mkdtemp(prefix="target-", dir=base))
    environment = RepairEnvironment(mount_directory, True, mount_directory)
    error = mount_recovery_filesystem(device, mount_directory, "btrfs", f"subvol={subvolume}", dry_run)
    if error:
        try:
            mount_directory.rmdir()
        except OSError:
            pass
        return None, f"Could not mount the base system: {error}"
    environment.mounted_paths.append(mount_directory)

    release = read_os_release_file(mount_directory / "etc/os-release")
    if release.get("ID") != "ergenos":
        cleanup_repair_environment(environment)
        return None, "The mounted base system is not ErgenOS"

    entries = sorted(fstab_entries(mount_directory / "etc/fstab"), key=lambda item: item[1].count("/"))
    for source, mountpoint, fstype, options in entries:
        if mountpoint not in {"/.snapshots", "/boot", "/boot/efi"} or "noauto" in options.split(","):
            continue
        resolved = resolve_resume_device(source) or source
        target = mount_directory / mountpoint.lstrip("/")
        if not target.is_dir():
            cleanup_repair_environment(environment)
            return None, f"Required mount point is missing in the base system: {mountpoint}"
        mounted_source = existing_mount_source(mountpoint)
        if mounted_source:
            mounted_device = mounted_source.partition("[")[0]
            current_device = resolve_resume_device(mounted_device) or mounted_device
            if Path(current_device).resolve() != Path(resolved).resolve():
                cleanup_repair_environment(environment)
                return None, f"Mounted {mountpoint} does not match the device configured in /etc/fstab"
            error = mount_recovery_bind(mountpoint, target, dry_run)
        else:
            error = mount_recovery_filesystem(resolved, target, fstype, options, dry_run)
        if error:
            cleanup_repair_environment(environment)
            return None, f"Could not mount {mountpoint} from the base system: {error}"
        environment.mounted_paths.append(target)
    return environment, None


def cleanup_repair_environment(environment: RepairEnvironment) -> str | None:
    errors = []
    for path in reversed(environment.mounted_paths):
        code, output, error = run_repair_command(["/usr/bin/umount", str(path)])
        if code != 0:
            errors.append(error or output or str(path))
    if environment.mount_directory and not errors:
        try:
            environment.mount_directory.rmdir()
        except OSError as error:
            errors.append(str(error))
    return "; ".join(errors) or None


def read_os_release_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value.strip().strip('"')
    return values


def target_has_multilib(root: Path) -> bool:
    try:
        content = (root / "etc/pacman.conf").read_text(encoding="utf-8")
    except OSError:
        return False
    return re.search(r"(?m)^\[multilib\][ \t]*$", content) is not None


def target_has_pacman_hooks(root: Path) -> bool:
    required = ("05-snap-pac-pre.hook", "zz-snap-pac-post.hook")
    directories = (root / "etc/pacman.d/hooks", root / "usr/share/libalpm/hooks")
    return all(any((directory / name).is_file() for directory in directories) for name in required)


def target_resume_is_consistent(root: Path) -> bool:
    try:
        mkinitcpio = (root / "etc/mkinitcpio.conf").read_text(encoding="utf-8")
        grub = (root / "etc/default/grub").read_text(encoding="utf-8")
    except OSError:
        return False
    if re.search(r"^HOOKS=.*(?:\s|\()resume(?:\s|\))", mkinitcpio, re.MULTILINE) is None:
        return True
    uuid, error = swap_uuid_from_fstab(root / "etc/fstab")
    return error is None and uuid is not None and f"resume=UUID={uuid}" in grub


def target_service_is_enabled(root: Path, unit: str) -> bool:
    code, _, _ = run_repair_command(["/usr/bin/systemctl", "--root", str(root), "is-enabled", "--quiet", unit])
    return code == 0


def needed_repair_steps(environment: RepairEnvironment | None = None) -> list[RepairStep]:
    if environment and environment.recovery:
        root = environment.root
        steps = []
        if not target_has_multilib(root):
            steps.append(REPAIR_STEPS["repositories"])
        if not target_has_pacman_hooks(root):
            steps.append(REPAIR_STEPS["pacman-hooks"])
        if not all(target_service_is_enabled(root, unit) for unit in ("grub-btrfsd.service", "snapper-cleanup.timer")):
            steps.append(REPAIR_STEPS["services"])
        if not target_resume_is_consistent(root):
            steps.append(REPAIR_STEPS["resume"])
        if not (root / "boot/grub/grub-btrfs.cfg").is_file():
            steps.append(REPAIR_STEPS["grub-snapshots"])
        return steps

    steps: list[RepairStep] = []
    root_fstype = root_filesystem_type()
    if repositories_check().status != "pass":
        steps.append(REPAIR_STEPS["repositories"])
    if root_fstype == "btrfs":
        if pacman_hooks_check(root_fstype).status != "pass":
            steps.append(REPAIR_STEPS["pacman-hooks"])
        if validate_repair_step(REPAIR_STEPS["services"]).status != "pass":
            steps.append(REPAIR_STEPS["services"])
    resume_report = collect_resume_report()
    resume_config_checks = [check for check in resume_report.checks if check.id != "resume-log"]
    if resume_report.hibernation_configured and any(
        check.status in {"warning", "fail", "unknown"} for check in resume_config_checks
    ):
        steps.append(REPAIR_STEPS["resume"])
    if root_fstype == "btrfs" and grub_config_check(root_fstype).status != "pass":
        steps.append(REPAIR_STEPS["grub-snapshots"])
    return steps


def repair_preflight(
    steps: list[RepairStep],
    dry_run: bool,
    environment: RepairEnvironment | None = None,
) -> str | None:
    environment = environment or RepairEnvironment()
    release = read_os_release() if environment.root == Path("/") else read_os_release_file(environment.root / "etc/os-release")
    if release.get("ID") != "ergenos":
        return "Repairs are available only on ErgenOS"
    if live_environment_detected():
        return "Repairs are disabled in the live environment"
    root_fstype = "btrfs" if environment.recovery else root_filesystem_type()
    if any(step.id in BTRFS_REPAIR_TARGETS for step in steps) and root_fstype != "btrfs":
        return f"Selected repair requires Btrfs root filesystem, found {root_fstype or 'unknown'}"
    if (not dry_run or environment.recovery) and os.geteuid() != 0:
        return "Root privileges are required. Run this command with sudo."

    if environment.recovery and not Path("/usr/bin/arch-chroot").is_file():
        return "Required recovery tool is missing: /usr/bin/arch-chroot"

    missing = sorted(
        {
            command[0]
            for step in steps
            for command in step.commands
            if not (environment.root / command[0].lstrip("/")).is_file()
        }
    )
    if missing:
        return f"Required repair tool(s) missing: {', '.join(missing)}"
    return None


def atomic_write_text(path: Path, content: str) -> None:
    stat = path.stat()
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
        temporary.write(content)
        temporary_path = Path(temporary.name)
    try:
        os.chmod(temporary_path, stat.st_mode)
        os.chown(temporary_path, stat.st_uid, stat.st_gid)
        os.replace(temporary_path, path)
    except OSError:
        temporary_path.unlink(missing_ok=True)
        raise


def enable_multilib(pacman_conf: Path = Path("/etc/pacman.conf")) -> tuple[bool, str]:
    try:
        content = pacman_conf.read_text(encoding="utf-8")
    except OSError as error:
        return False, f"Could not read {pacman_conf}: {error}"
    if re.search(r"(?m)^\[multilib\]\s*$", content):
        return True, "multilib is already enabled"

    commented = re.compile(
        r"(?m)^#\[multilib\]\s*\n#\s*(Include\s*=\s*/etc/pacman\.d/mirrorlist\s*)$"
    )
    if commented.search(content):
        updated = commented.sub(r"[multilib]\n\1", content, count=1)
    else:
        updated = content.rstrip() + "\n\n[multilib]\nInclude = /etc/pacman.d/mirrorlist\n"
    try:
        atomic_write_text(pacman_conf, updated)
    except OSError as error:
        return False, f"Could not update {pacman_conf}: {error}"
    return True, "multilib enabled"


def swap_uuid_from_fstab(fstab: Path = Path("/etc/fstab")) -> tuple[str | None, str | None]:
    try:
        lines = fstab.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        return None, f"Could not read {fstab}: {error}"
    swap_spec = next(
        (fields[0] for line in lines if line.strip() and not line.lstrip().startswith("#")
         if len(fields := line.split()) >= 3 and fields[2] == "swap"),
        None,
    )
    if not swap_spec:
        return None, "No swap entry found in /etc/fstab"
    if swap_spec.startswith("UUID="):
        return swap_spec.partition("=")[2], None

    device = resolve_resume_device(swap_spec)
    if device is None:
        return None, f"Could not resolve swap device {swap_spec}"
    code, output, error = run(["blkid", "-s", "UUID", "-o", "value", device])
    if code != 0 or not output:
        return None, f"Could not read swap UUID: {error or output or 'unknown error'}"
    return output.splitlines()[0].strip(), None


def configure_resume(
    mkinitcpio_conf: Path = Path("/etc/mkinitcpio.conf"),
    grub_defaults: Path = Path("/etc/default/grub"),
    fstab: Path = Path("/etc/fstab"),
) -> tuple[bool, str]:
    try:
        mkinitcpio_content = mkinitcpio_conf.read_text(encoding="utf-8")
        grub_content = grub_defaults.read_text(encoding="utf-8")
    except OSError as error:
        return False, f"Could not read resume configuration: {error}"
    if re.search(r"^HOOKS=.*(?:\s|\()resume(?:\s|\))", mkinitcpio_content, re.MULTILINE) is None:
        return True, "resume hook is not configured, no change needed"

    swap_uuid, error = swap_uuid_from_fstab(fstab)
    if error or not swap_uuid:
        return False, error or "Could not determine swap UUID"

    setting = re.search(r'(?m)^GRUB_CMDLINE_LINUX_DEFAULT="([^"]*)"[ \t]*$', grub_content)
    current = setting.group(1) if setting else ""
    arguments = [argument for argument in current.split() if not argument.startswith("resume=")]
    arguments.append(f"resume=UUID={swap_uuid}")
    replacement = f'GRUB_CMDLINE_LINUX_DEFAULT="{" ".join(arguments)}"'
    if setting:
        updated = grub_content[: setting.start()] + replacement + grub_content[setting.end() :]
    else:
        updated = grub_content.rstrip() + f"\n{replacement}\n"
    if updated == grub_content:
        return True, "resume parameter is already correct"
    try:
        atomic_write_text(grub_defaults, updated)
    except OSError as write_error:
        return False, f"Could not update {grub_defaults}: {write_error}"
    return True, f"resume parameter set to UUID={swap_uuid}"


def run_internal_repair(action: str, root: Path = Path("/")) -> tuple[bool, str]:
    if action == "enable-multilib":
        return enable_multilib(root / "etc/pacman.conf")
    if action == "configure-resume":
        return configure_resume(
            root / "etc/mkinitcpio.conf",
            root / "etc/default/grub",
            root / "etc/fstab",
        )
    return False, f"Unknown internal repair action: {action}"


def backup_configuration(steps: list[RepairStep], root: Path = Path("/")) -> str | None:
    paths = sorted(
        {
            root / path.lstrip("/")
            for step in steps
            for path in step.backup_paths
            if (root / path.lstrip("/")).is_file()
        }
    )
    if not paths:
        return None

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_directory = root / "var/lib/ergenctl/backups" / timestamp
    backup_directory.mkdir(parents=True, mode=0o700)
    for source in paths:
        destination = backup_directory / source.relative_to(root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return str(Path("/") / backup_directory.relative_to(root))


def run_repair_command(command: Sequence[str]) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=1200,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        return 127, "", str(error)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def command_for_environment(command: Sequence[str], environment: RepairEnvironment) -> list[str]:
    adjusted = list(command)
    if environment.recovery and adjusted[:3] == ["/usr/bin/systemctl", "enable", "--now"]:
        adjusted.remove("--now")
        return [adjusted[0], "--root", str(environment.root), *adjusted[1:]]
    if environment.recovery:
        return ["/usr/bin/arch-chroot", str(environment.root), *adjusted]
    return adjusted


def create_safety_snapshot(
    target: str,
    environment: RepairEnvironment | None = None,
) -> tuple[str | None, str | None, list[str]]:
    environment = environment or RepairEnvironment()
    if environment.recovery:
        snapshots = environment.root / ".snapshots"
        if not snapshots.is_dir():
            return None, "Could not create safety snapshot: /.snapshots is not mounted", []
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        name = f"ergenctl-safety-{timestamp}"
        destination = snapshots / name
        command = [
            "/usr/bin/btrfs",
            "subvolume",
            "snapshot",
            "-r",
            str(environment.root),
            str(destination),
        ]
        code, output, error = run_repair_command(command)
        if code != 0:
            diagnostic = error or output or "unknown error"
            return None, f"Could not create Btrfs safety snapshot: {diagnostic}", command
        return f"/.snapshots/{name}", None, command
    if not (environment.root / "usr/bin/snapper").is_file():
        return None, None, []
    inner_command = [
        "/usr/bin/snapper",
        "-c",
        "root",
        "create",
        "--type",
        "single",
        "--cleanup-algorithm",
        "number",
        "--print-number",
        "--description",
        f"ErgenCTL safety snapshot before fix {target}",
    ]
    command = command_for_environment(inner_command, environment)
    code, output, error = run_repair_command(command)
    if code != 0:
        diagnostic = error or output or "unknown error"
        return None, f"Could not create safety snapshot: {diagnostic}", command
    number = output.splitlines()[-1].strip() if output else "created"
    return number, None, command


def validate_repair_step(
    step: RepairStep,
    environment: RepairEnvironment | None = None,
) -> Check:
    environment = environment or RepairEnvironment()
    root = environment.root
    if step.id == "grub-snapshots":
        path = root / "boot/grub/grub-btrfs.cfg"
        return Check(
            step.id,
            step.title,
            "pass" if path.is_file() else "fail",
            "snapshot menu generated" if path.is_file() else "snapshot menu is still missing",
            str(path),
        )
    if step.id == "pacman-hooks":
        if environment.recovery:
            return Check(step.id, step.title, "pass" if target_has_pacman_hooks(root) else "fail", "hooks installed" if target_has_pacman_hooks(root) else "hooks are still missing")
        return pacman_hooks_check()
    if step.id == "services":
        states = []
        for unit in ("grub-btrfsd.service", "snapper-cleanup.timer"):
            if environment.recovery:
                states.append(target_service_is_enabled(root, unit))
                continue
            active_code, _, _ = run(["systemctl", "is-active", "--quiet", unit])
            enabled_code, _, _ = run(["systemctl", "is-enabled", "--quiet", unit])
            states.append(active_code == 0 and enabled_code == 0)
        return Check(
            step.id,
            step.title,
            "pass" if all(states) else "fail",
            "snapshot service and cleanup timer active" if all(states) else "snapshot service or cleanup timer is inactive",
        )
    if step.id == "repositories":
        if environment.recovery:
            return Check(step.id, step.title, "pass" if target_has_multilib(root) else "fail", "multilib enabled" if target_has_multilib(root) else "multilib is still disabled")
        return repositories_check()
    return Check(step.id, step.title, "pass", "commands completed, reboot required")


def execute_repair_in_environment(
    target: str,
    dry_run: bool,
    create_snapshot: bool,
    environment: RepairEnvironment,
) -> RepairReport:
    steps = needed_repair_steps(environment) if target == "all" else selected_repair_steps(target)
    step_titles = [step.title for step in steps]
    if not steps:
        return RepairReport(target, dry_run, True, [], [], message="No repairs are needed", system_root=str(environment.root))
    preflight_error = repair_preflight(steps, dry_run, environment)
    if preflight_error:
        return RepairReport(target, dry_run, False, step_titles, [], message=preflight_error, system_root=str(environment.root))

    if dry_run:
        commands = [command_for_environment(command, environment) for step in steps for command in step.commands]
        actions = [action for step in steps for action in step.internal_actions]
        return RepairReport(
            target,
            True,
            True,
            step_titles,
            commands,
            reboot_required=any(step.reboot_required for step in steps),
            message="Dry run completed. No changes were made.",
            executed_actions=actions,
            system_root=str(environment.root),
        )

    try:
        backup_directory = backup_configuration(steps, environment.root)
    except OSError as error:
        return RepairReport(target, False, False, step_titles, [], message=f"Could not create backup: {error}")

    executed: list[list[str]] = []
    executed_actions: list[str] = []
    safety_snapshot = None
    if create_snapshot:
        safety_snapshot, snapshot_error, snapshot_command = create_safety_snapshot(target, environment)
        if snapshot_command:
            executed.append(snapshot_command)
        if snapshot_error:
            return RepairReport(
                target,
                False,
                False,
                step_titles,
                executed,
                backup_directory,
                message=snapshot_error,
            )

    for step in steps:
        for action in step.internal_actions:
            success, message = run_internal_repair(action, environment.root)
            executed_actions.append(action)
            if not success:
                return RepairReport(
                    target,
                    False,
                    False,
                    step_titles,
                    executed,
                    backup_directory,
                    safety_snapshot,
                    any(item.reboot_required for item in steps),
                    f"Repair step failed: {step.title}: {message}",
                    executed_actions,
                )
        for command in step.commands:
            command_list = command_for_environment(command, environment)
            code, output, error = run_repair_command(command_list)
            executed.append(command_list)
            if code != 0:
                diagnostic = error or output or f"exit code {code}"
                return RepairReport(
                    target,
                    False,
                    False,
                    step_titles,
                    executed,
                    backup_directory,
                    safety_snapshot,
                    any(item.reboot_required for item in steps),
                    f"Repair step failed: {step.title}: {diagnostic}",
                    executed_actions,
                )
        validation = validate_repair_step(step, environment)
        if validation.status == "fail":
            return RepairReport(
                target,
                False,
                False,
                step_titles,
                executed,
                backup_directory,
                safety_snapshot,
                any(item.reboot_required for item in steps),
                f"Validation failed: {validation.summary}",
                executed_actions,
            )

    return RepairReport(
        target,
        False,
        True,
        step_titles,
        executed,
        backup_directory,
        safety_snapshot,
        any(step.reboot_required for step in steps),
        "Repair completed successfully",
        executed_actions,
        str(environment.root),
    )


def execute_repair(target: str, dry_run: bool, create_snapshot: bool) -> RepairReport:
    snapshot_boot, _ = snapshot_boot_detected()
    environment, preparation_error = prepare_repair_environment(dry_run)
    if preparation_error or environment is None:
        return RepairReport(
            target,
            dry_run,
            False,
            [],
            [],
            message=preparation_error or "Could not prepare repair environment",
            recovery_mode=snapshot_boot,
        )
    try:
        report = execute_repair_in_environment(target, dry_run, create_snapshot, environment)
    finally:
        cleanup_error = cleanup_repair_environment(environment)
    report = replace(report, system_root=str(environment.root), recovery_mode=environment.recovery)
    if cleanup_error:
        report = RepairReport(
            report.target,
            report.dry_run,
            False,
            report.steps,
            report.executed_commands,
            report.backup_directory,
            report.safety_snapshot,
            report.reboot_required,
            f"{report.message or 'Repair finished'}; cleanup failed: {cleanup_error}",
            report.executed_actions,
            report.system_root,
            report.recovery_mode,
        )
    return report


def prepare_top_level_environment(read_only: bool) -> tuple[RepairEnvironment | None, str | None]:
    device = root_device_from_cmdline()
    if not device:
        return None, "Could not resolve the Btrfs device from the kernel command line"
    base = Path("/run/ergenctl")
    base.mkdir(parents=True, exist_ok=True)
    mount_directory = Path(tempfile.mkdtemp(prefix="top-", dir=base))
    environment = RepairEnvironment(mount_directory, True, mount_directory)
    error = mount_recovery_filesystem(device, mount_directory, "btrfs", "subvolid=5", read_only)
    if error:
        try:
            mount_directory.rmdir()
        except OSError:
            pass
        return None, f"Could not mount the Btrfs top level: {error}"
    environment.mounted_paths.append(mount_directory)
    return environment, None


def is_btrfs_subvolume(path: Path) -> bool:
    code, _, _ = run_repair_command(["/usr/bin/btrfs", "subvolume", "show", str(path)])
    return code == 0


def execute_rollback(snapshot: int, dry_run: bool) -> RollbackReport:
    snapshot_boot, _ = snapshot_boot_detected()
    if not snapshot_boot:
        return RollbackReport(snapshot, dry_run, False, message="Rollback is available only while booted from a snapshot")
    if live_environment_detected():
        return RollbackReport(snapshot, dry_run, False, message="Rollback is disabled in the live environment")
    if os.geteuid() != 0:
        return RollbackReport(snapshot, dry_run, False, message="Root privileges are required. Run this command with sudo.")

    root_subvolume = configured_root_subvolume()
    snapshots_subvolume = configured_subvolume(Path("/etc/fstab"), "/.snapshots")
    if not root_subvolume or not snapshots_subvolume:
        return RollbackReport(snapshot, dry_run, False, message="Could not determine root and snapshot subvolumes from /etc/fstab")

    top, error = prepare_top_level_environment(dry_run)
    if error or top is None:
        return RollbackReport(snapshot, dry_run, False, message=error)

    source_relative = f"{snapshots_subvolume}/{snapshot}/snapshot"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    preserved_relative = f"{root_subvolume}-broken-{timestamp}"
    staging_relative = f"{root_subvolume}-rollback-{timestamp}"
    source = top.root / source_relative
    current_root = top.root / root_subvolume
    preserved = top.root / preserved_relative
    staging = top.root / staging_relative
    commands: list[list[str]] = []

    top_result: RollbackReport | None = None
    try:
        if not is_btrfs_subvolume(source):
            top_result = RollbackReport(snapshot, dry_run, False, source_relative, message="Selected snapshot does not exist or is not a Btrfs subvolume")
        elif not is_btrfs_subvolume(current_root):
            top_result = RollbackReport(snapshot, dry_run, False, source_relative, message="The base root is not a Btrfs subvolume")
        else:
            snapshot_command = ["/usr/bin/btrfs", "subvolume", "snapshot", str(source), str(staging)]
            commands.append(snapshot_command)
        if top_result is None and dry_run:
            top_result = RollbackReport(
                snapshot,
                True,
                True,
                source_relative,
                root_subvolume,
                preserved_relative,
                commands,
                True,
                "Dry run completed. No changes were made.",
            )
        if top_result is None:
            code, output, command_error = run_repair_command(snapshot_command)
        if top_result is None and code != 0:
            top_result = RollbackReport(
                snapshot,
                False,
                False,
                source_relative,
                root_subvolume,
                preserved_relative,
                commands,
                message=f"Could not create writable rollback snapshot: {command_error or output}",
            )
        if top_result is None:
            try:
                os.rename(current_root, preserved)
                try:
                    os.rename(staging, current_root)
                except OSError:
                    os.rename(preserved, current_root)
                    raise
            except OSError as rename_error:
                top_result = RollbackReport(
                    snapshot,
                    False,
                    False,
                    source_relative,
                    root_subvolume,
                    preserved_relative,
                    commands,
                    message=f"Could not switch root subvolumes: {rename_error}",
                )
    finally:
        cleanup_error = cleanup_repair_environment(top)

    if cleanup_error:
        message = top_result.message if top_result else "Root was switched"
        return RollbackReport(snapshot, dry_run, False, source_relative, root_subvolume, preserved_relative, commands, message=f"{message}; cleanup failed: {cleanup_error}")
    if top_result is not None:
        return top_result

    environment, preparation_error = prepare_repair_environment(False)
    if preparation_error or environment is None:
        return RollbackReport(snapshot, False, False, source_relative, root_subvolume, preserved_relative, commands, message=f"Root was switched, but the restored system could not be mounted: {preparation_error}")
    boot_error: str | None = None
    try:
        for inner_command in (
            ("/usr/bin/mkinitcpio", "-P"),
            ("/usr/bin/grub-mkconfig", "-o", "/boot/grub/grub.cfg"),
            ("/etc/grub.d/41_snapshots-btrfs",),
        ):
            command = command_for_environment(inner_command, environment)
            commands.append(command)
            code, output, command_error = run_repair_command(command)
            if code != 0:
                boot_error = command_error or output or f"command exited with code {code}"
                break
    finally:
        cleanup_error = cleanup_repair_environment(environment)

    if boot_error:
        suffix = f"; cleanup failed: {cleanup_error}" if cleanup_error else ""
        return RollbackReport(snapshot, False, False, source_relative, root_subvolume, preserved_relative, commands, True, f"Root was restored, but boot files could not be rebuilt: {boot_error}{suffix}")
    if cleanup_error:
        return RollbackReport(snapshot, False, False, source_relative, root_subvolume, preserved_relative, commands, True, f"Rollback completed, but cleanup failed: {cleanup_error}")
    return RollbackReport(
        snapshot,
        False,
        True,
        source_relative,
        root_subvolume,
        preserved_relative,
        commands,
        True,
        "Rollback completed successfully",
    )


def status_payload(checks: list[Check]) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "application": APP_NAME,
        "version": VERSION,
        "hostname": platform.node(),
        "checks": [asdict(check) for check in checks],
    }


def snapshots_payload(report: SnapshotReport) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "application": APP_NAME,
        "version": VERSION,
        "hostname": platform.node(),
        "snapshot_report": asdict(report),
    }


def boot_log_payload(report: BootLogReport) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "application": APP_NAME,
        "version": VERSION,
        "hostname": platform.node(),
        "boot_log": asdict(report),
    }


def resume_payload(report: ResumeReport) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "application": APP_NAME,
        "version": VERSION,
        "hostname": platform.node(),
        "resume_report": asdict(report),
    }


def repair_payload(report: RepairReport) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "application": APP_NAME,
        "version": VERSION,
        "hostname": platform.node(),
        "repair_report": asdict(report),
    }


def rollback_payload(report: RollbackReport) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "application": APP_NAME,
        "version": VERSION,
        "hostname": platform.node(),
        "rollback_report": asdict(report),
    }


def colors_enabled() -> bool:
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


def colorize(text: str, color: str) -> str:
    if not colors_enabled():
        return text
    return f"\033[{color}m{text}\033[0m"


def print_header(section: str) -> None:
    print(colorize(f"{APP_NAME} {VERSION}", "1;36"))
    print(section)
    print("-" * max(28, len(section)))


def print_human(checks: list[Check]) -> None:
    print_header("System status")
    title_width = max((len(check.title) for check in checks), default=0)
    for check in checks:
        label = f"[{STATUS_LABELS.get(check.status, '?'):4}]"
        label = colorize(label, STATUS_COLORS.get(check.status, "0"))
        print(f"{label} {check.title:<{title_width}}  {check.summary}")

    counts = {status: sum(check.status == status for check in checks) for status in STATUS_LABELS}
    summary = [f"{counts['pass']} passed"]
    if counts["warning"]:
        summary.append(f"{counts['warning']} warning(s)")
    if counts["fail"]:
        summary.append(f"{counts['fail']} failed")
    if counts["skipped"]:
        summary.append(f"{counts['skipped']} skipped")
    if counts["unknown"]:
        summary.append(f"{counts['unknown']} unknown")
    print(f"\nSummary: {', '.join(summary)}")


def print_doctor(checks: list[Check]) -> None:
    print_human(checks)
    details = [check for check in checks if check.status in {"warning", "fail", "unknown"} and check.evidence]
    if details:
        print("\nDetails:")
        for check in details:
            print(f"- {check.title}: {check.evidence}")


def print_snapshots(report: SnapshotReport) -> None:
    print_header("Snapshots")
    print(f"Boot mode         {report.boot_mode}")
    if report.current_snapshot is not None:
        print(f"Current snapshot  {report.current_snapshot}")
    if report.listing:
        print()
        print(report.listing)
    elif report.message:
        print(report.message)


def print_boot_log(report: BootLogReport, raw: bool = False) -> None:
    print_header("Boot journal")
    print(f"Boot      {report.boot}")
    print(f"Priority  {report.priority}")
    print(f"Category  {report.category}")
    print(f"Entries   {len(report.entries)} raw, {len(report.groups)} grouped")
    if raw and report.entries:
        print()
        print("\n".join(report.entries))
    elif report.groups:
        print()
        for group in report.groups:
            count = int(group["count"])
            suffix = colorize(f" x{count}", "33") if count > 1 else ""
            print(colorize(f"[{group['source']}]", "36"), f"{group['message']}{suffix}")
    elif report.message:
        print(report.message)


def print_resume(report: ResumeReport) -> None:
    print_header("Resume diagnostics")
    print(f"Boot mode  {report.boot_mode}")
    print(f"Configured {'yes' if report.hibernation_configured else 'no'}")
    print(f"noresume   {'yes' if report.noresume else 'no'}")
    print(f"Resume     {report.resume_parameter or 'not configured'}\n")
    title_width = max((len(check.title) for check in report.checks), default=0)
    for check in report.checks:
        label = f"[{STATUS_LABELS.get(check.status, '?'):4}]"
        label = colorize(label, STATUS_COLORS.get(check.status, "0"))
        print(f"{label} {check.title:<{title_width}}  {check.summary}")

    details = [check for check in report.checks if check.status in {"warning", "fail", "unknown"} and check.evidence]
    if details:
        print("\nDetails:")
        for check in details:
            print(f"- {check.title}: {check.evidence}")


def print_repair(report: RepairReport) -> None:
    print_header("Repair plan" if report.dry_run else "Repair result")
    print(f"Target   {report.target}")
    print(f"Mode     {'dry run' if report.dry_run else 'execute'}")
    print(f"System   {'base installation from snapshot' if report.recovery_mode else 'current installation'}")
    print(f"Result   {'success' if report.success else 'failed'}")
    if report.steps:
        print("\nSteps:")
        for index, title in enumerate(report.steps, start=1):
            print(f"{index}. {title}")
    if report.executed_actions:
        print("\nBuilt-in actions:")
        for action in report.executed_actions:
            print(f"- {action}")
    if report.executed_commands:
        print("\nCommands:")
        for command in report.executed_commands:
            print(f"- {shlex.join(command)}")
    if report.backup_directory:
        print(f"\nBackup          {report.backup_directory}")
    if report.safety_snapshot:
        print(f"Safety snapshot {report.safety_snapshot}")
    if report.reboot_required:
        print(colorize("\nA reboot is required to apply all changes.", "33"))
    if report.message:
        print(f"\n{report.message}")


def print_rollback(report: RollbackReport) -> None:
    print_header("Rollback plan" if report.dry_run else "Rollback result")
    print(f"Snapshot   {report.snapshot}")
    print(f"Mode       {'dry run' if report.dry_run else 'execute'}")
    print(f"Result     {'success' if report.success else 'failed'}")
    if report.source_subvolume:
        print(f"Source     {report.source_subvolume}")
    if report.replaced_subvolume:
        print(f"New root   {report.replaced_subvolume}")
    if report.preserved_subvolume:
        print(f"Old root   {report.preserved_subvolume}")
    if report.commands:
        print("\nCommands:")
        for command in report.commands:
            print(f"- {shlex.join(command)}")
    if report.reboot_required:
        print(colorize("\nA reboot is required to start the restored system.", "33"))
    if report.message:
        print(f"\n{report.message}")


def confirm_repair(target: str) -> bool:
    try:
        response = input(f"Apply repair '{target}'? [y/N] ")
    except EOFError:
        return False
    return response.strip().lower() in {"y", "yes"}


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ergenctl", description="ErgenOS diagnostics and repair tool")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    status = subparsers.add_parser("status", help="show the current ErgenOS recovery state")
    status.add_argument("--json", action="store_true", dest="as_json", help="emit machine-readable JSON")
    doctor = subparsers.add_parser("doctor", help="run extended read-only ErgenOS diagnostics")
    doctor.add_argument("--json", action="store_true", dest="as_json", help="emit machine-readable JSON")
    snapshots = subparsers.add_parser("snapshots", help="list Snapper snapshots without modifying them")
    snapshots.add_argument("--json", action="store_true", dest="as_json", help="emit machine-readable JSON")
    logs = subparsers.add_parser("logs", help="show errors from the system boot journal")
    logs.add_argument("--previous", action="store_true", help="inspect the previous boot instead of the current boot")
    logs.add_argument(
        "--priority",
        choices=("error", "warning"),
        default="error",
        help="minimum message priority to include (default: error)",
    )
    logs.add_argument("--lines", type=positive_int, default=100, help="maximum number of entries (default: 100)")
    logs.add_argument(
        "--category",
        choices=("all", *LOG_CATEGORY_PATTERNS),
        default="all",
        help="show only a selected diagnostic category (default: all)",
    )
    logs.add_argument("--raw", action="store_true", help="show complete journal entries without grouping")
    logs.add_argument("--json", action="store_true", dest="as_json", help="emit machine-readable JSON")
    resume = subparsers.add_parser("resume", help="inspect hibernation resume configuration")
    resume.add_argument("--json", action="store_true", dest="as_json", help="emit machine-readable JSON")
    fix = subparsers.add_parser("fix", help="repair selected ErgenOS components")
    fix.add_argument("target", choices=REPAIR_TARGETS, help="component to repair")
    fix.add_argument("--dry-run", action="store_true", help="show the repair plan without changing the system")
    fix.add_argument("--yes", action="store_true", help="apply the repair without an interactive confirmation")
    fix.add_argument("--no-snapshot", action="store_true", help="do not create a safety snapshot")
    fix.add_argument("--json", action="store_true", dest="as_json", help="emit machine-readable JSON")
    rollback = subparsers.add_parser("rollback", help="restore the base system from a Snapper snapshot")
    rollback.add_argument("snapshot", type=positive_int, help="snapshot number to restore")
    rollback.add_argument("--dry-run", action="store_true", help="show the rollback plan without changing the system")
    rollback.add_argument("--yes", action="store_true", help="apply the rollback without an interactive confirmation")
    rollback.add_argument("--json", action="store_true", dest="as_json", help="emit machine-readable JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "status":
        checks = collect_checks()
        if args.as_json:
            print(json.dumps(status_payload(checks), indent=2, ensure_ascii=False))
        else:
            print_human(checks)
        return 0
    if args.command == "doctor":
        checks = collect_doctor_checks()
        if args.as_json:
            print(json.dumps(status_payload(checks), indent=2, ensure_ascii=False))
        else:
            print_doctor(checks)
        return 1 if any(check.status == "fail" for check in checks) else 0
    if args.command == "snapshots":
        report, failed = collect_snapshot_report()
        if args.as_json:
            print(json.dumps(snapshots_payload(report), indent=2, ensure_ascii=False))
        else:
            print_snapshots(report)
        return 1 if failed else 0
    if args.command == "logs":
        report, failed = collect_boot_log(args.previous, args.priority, args.lines, args.category)
        if args.as_json:
            print(json.dumps(boot_log_payload(report), indent=2, ensure_ascii=False))
        else:
            print_boot_log(report, args.raw)
        return 1 if failed else 0
    if args.command == "resume":
        report = collect_resume_report()
        if args.as_json:
            print(json.dumps(resume_payload(report), indent=2, ensure_ascii=False))
        else:
            print_resume(report)
        return 1 if any(check.status == "fail" for check in report.checks) else 0
    if args.command == "fix":
        if not args.dry_run and not args.yes and not confirm_repair(args.target):
            print("Repair cancelled. No changes were made.")
            return 2
        report = execute_repair(args.target, args.dry_run, not args.no_snapshot)
        if args.as_json:
            print(json.dumps(repair_payload(report), indent=2, ensure_ascii=False))
        else:
            print_repair(report)
        return 0 if report.success else 1
    if args.command == "rollback":
        if not args.dry_run and not args.yes and not confirm_repair(f"rollback to snapshot {args.snapshot}"):
            print("Rollback cancelled. No changes were made.")
            return 2
        report = execute_rollback(args.snapshot, args.dry_run)
        if args.as_json:
            print(json.dumps(rollback_payload(report), indent=2, ensure_ascii=False))
        else:
            print_rollback(report)
        return 0 if report.success else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
