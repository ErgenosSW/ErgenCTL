#!/usr/bin/env python3
"""ErgenCTL - read-only diagnostics for ErgenOS."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence


APP_NAME = "ErgenCTL"
VERSION = "0.1.0-dev"
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
    noresume: bool
    resume_parameter: str | None
    checks: list[Check]


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


def snapper_check() -> Check:
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


def snapshot_count_check(detection: tuple[bool, str] | None = None) -> Check:
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


def service_check() -> Check:
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


def pacman_hooks_check() -> Check:
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


def grub_config_check() -> Check:
    if live_environment_detected():
        return Check("grub-config", "GRUB configuration", "skipped", "Not applicable in live environment")

    main_config = Path("/boot/grub/grub.cfg")
    snapshot_config = Path("/boot/grub/grub-btrfs.cfg")
    if not main_config.is_file():
        return Check("grub-config", "GRUB configuration", "fail", "grub.cfg is missing", str(main_config))
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


def collect_checks() -> list[Check]:
    snapshot_detection = snapshot_boot_detected()
    return [
        distribution_check(),
        Check("kernel", "Kernel", "pass", platform.release()),
        firmware_check(),
        root_check(),
        boot_mode_check(snapshot_detection),
        disk_space_check(snapshot_detection),
        snapper_check(),
        snapshot_count_check(snapshot_detection),
        service_check(),
    ]


def collect_doctor_checks() -> list[Check]:
    return collect_checks() + [
        pacman_hooks_check(),
        boot_files_check(),
        grub_config_check(),
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
        status = "skipped" if noresume else "warning"
        summary = "not required while noresume is active" if noresume else "missing from kernel command line"
        checks.append(Check("resume-parameter", "Resume parameter", status, summary))

    if noresume:
        checks.append(Check("resume-device", "Resume device", "skipped", "not checked while noresume is active"))
    elif not resume_parameter:
        checks.append(Check("resume-device", "Resume device", "warning", "cannot check without resume parameter"))
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

    return ResumeReport(boot_mode, noresume, resume_parameter, checks)


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


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ergenctl", description="Read-only ErgenOS diagnostics")
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
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
