#!/usr/bin/env python3
"""ErgenCTL — read-only diagnostics for ErgenOS."""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
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
    try:
        cmdline = Path("/proc/cmdline").read_text(encoding="utf-8").strip()
    except OSError:
        cmdline = ""

    snapshot_subvolume = "@snapshots/" in cmdline and "/snapshot" in cmdline
    snapshot_boot = fstype == "overlay" or snapshot_subvolume
    evidence = f"root_fstype={fstype}, snapshot_subvolume={'yes' if snapshot_subvolume else 'no'}"
    return snapshot_boot, evidence


def boot_mode_check() -> Check:
    snapshot_boot, evidence = snapshot_boot_detected()
    if "root_fstype=unknown" in evidence:
        return Check("boot-mode", "Boot mode", "unknown", "Could not determine boot mode", evidence)
    return Check(
        "boot-mode",
        "Boot mode",
        "pass",
        "snapshot" if snapshot_boot else "normal",
        evidence,
    )


def disk_space_check() -> Check:
    snapshot_boot, evidence = snapshot_boot_detected()
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


def snapshot_count_check() -> Check:
    if not shutil.which("snapper"):
        return Check("snapshots", "Snapshots", "skipped", "snapper is not installed")

    snapshot_boot, evidence = snapshot_boot_detected()
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
        ["systemctl", "show", "grub-btrfsd.service", "--property=LoadState,ActiveState", "--value"]
    )
    states = output.splitlines()
    loaded = len(states) > 0 and states[0] == "loaded"
    active = len(states) > 1 and states[1] == "active"
    if code == 0 and loaded and active:
        return Check("grub-btrfsd", "grub-btrfsd", "pass", "active")
    return Check("grub-btrfsd", "grub-btrfsd", "warning", "not active", error or output or None)


def live_environment_detected() -> bool:
    try:
        cmdline = Path("/proc/cmdline").read_text(encoding="utf-8")
    except OSError:
        cmdline = ""
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
    return [
        distribution_check(),
        Check("kernel", "Kernel", "pass", platform.release()),
        firmware_check(),
        root_check(),
        boot_mode_check(),
        disk_space_check(),
        snapper_check(),
        snapshot_count_check(),
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


def status_payload(checks: list[Check]) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "application": APP_NAME,
        "version": VERSION,
        "hostname": platform.node(),
        "checks": [asdict(check) for check in checks],
    }


def print_human(checks: list[Check]) -> None:
    symbols = {"pass": "OK", "warning": "WARN", "fail": "FAIL", "skipped": "SKIP", "unknown": "?"}
    print(f"{APP_NAME} {VERSION}\n")
    for check in checks:
        print(f"[{symbols.get(check.status, '?'):4}] {check.title}: {check.summary}")


def print_doctor(checks: list[Check]) -> None:
    print_human(checks)
    details = [check for check in checks if check.status in {"warning", "fail", "unknown"} and check.evidence]
    if details:
        print("\nDetails:")
        for check in details:
            print(f"- {check.title}: {check.evidence}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ergenctl", description="Read-only ErgenOS diagnostics")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    status = subparsers.add_parser("status", help="show the current ErgenOS recovery state")
    status.add_argument("--json", action="store_true", dest="as_json", help="emit machine-readable JSON")
    doctor = subparsers.add_parser("doctor", help="run extended read-only ErgenOS diagnostics")
    doctor.add_argument("--json", action="store_true", dest="as_json", help="emit machine-readable JSON")
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
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
