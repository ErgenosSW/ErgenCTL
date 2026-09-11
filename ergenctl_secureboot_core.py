"""Validated commands and presentation for the Secure Boot GUI."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

BACKEND = "/usr/bin/ergenos-secureboot"
ACTIONS = {"status", "check", "preflight", "enable", "finalize", "refresh", "remove-mok", "disable"}
MOK_TITLE = "THE MOK PASSWORD IS USED ONLY ONCE"
MOK_NOTICE = (
    "After restarting, enter it in MokManager to authorize key enrollment. "
    "It will not be required for subsequent boots or updates. "
    "Remember it until this step is complete."
)


def password_error(password: str, confirmation: str) -> str | None:
    if not re.fullmatch(r"[A-Za-z0-9]{8,16}", password):
        return "Use 8–16 ASCII letters or digits (no spaces or accented characters)."
    if password != confirmation:
        return "The passwords must match."
    return None


def gui_command(action: str) -> list[str]:
    if action not in ACTIONS:
        raise ValueError("Unsupported Secure Boot action")
    return ["pkexec", BACKEND, "gui", action]


def parse_report(output: str) -> dict:
    report = json.loads(output)
    if not isinstance(report, dict) or report.get("schema_version") != 1 or type(report.get("success")) is not bool:
        raise ValueError("Unsupported Secure Boot response")
    if report["success"] and not isinstance(report.get("status"), dict):
        raise ValueError("Missing Secure Boot status")
    status = report.get("status")
    if status is not None:
        if not isinstance(status, dict) or status.get("state") not in {"unconfigured", "enrollment-pending", "configured", "active", "degraded"}:
            raise ValueError("Invalid Secure Boot state")
        for key in ("configured", "mok_enrolled", "booted_through_shim", "removal_requested", "enrollment_requested", "uefi"):
            if type(status.get(key)) is not bool:
                raise ValueError("Incomplete Secure Boot status")
        if not isinstance(status.get("problems"), list):
            raise ValueError("Missing diagnostic results")
    return report


def state_text(status: dict) -> tuple[str, str]:
    if not status["uefi"]:
        return "Not booted in UEFI mode", "Secure Boot requires the system to boot in UEFI mode."
    if status["removal_requested"]:
        return "Confirm MOK removal", "Restart using the ErgenOS Secure Boot entry and confirm key removal in MokManager with your one-time password. Then disable Secure Boot in firmware and boot the normal ErgenOS entry."
    state = status["state"]
    if state == "active":
        if status.get("secure_boot") == "enabled" and status["mok_enrolled"] and status["booted_through_shim"] and not status["problems"]:
            return "Secure Boot is active", "The system booted through shim. The MOK is enrolled and the configuration passed verification."
        return "Status needs verification", "Not all conditions for active Secure Boot have been confirmed."
    if state == "degraded":
        return "Configuration needs repair", "Check the details below. Refresh signatures or retry setup if preparation was interrupted."
    if state == "enrollment-pending":
        if status["enrollment_requested"]:
            return "Enroll the MOK after restarting", "Boot the ErgenOS Secure Boot entry. In MokManager, select Enroll MOK → Continue → Yes and enter your one-time password. Then enable Secure Boot in firmware, keeping the standard keys, and return here."
        return "Complete the MOK enrollment request", "The key is not enrolled. Retry setup to request enrollment and set a one-time password."
    if state == "configured":
        return "Complete activation", "Enable Secure Boot in firmware without removing the standard keys. Boot the ErgenOS Secure Boot entry, then check the status in this tab."
    return "Secure Boot is not configured", "Check readiness, then set up Secure Boot. ErgenOS-SecureBoot will configure signatures and request MOK enrollment."


@dataclass(frozen=True)
class StatusItem:
    title: str
    state: str
    detail: str


@dataclass(frozen=True)
class NextStep:
    title: str
    detail: str
    action: str | None = None
    label: str = ""


def status_items(status: dict | None) -> list[StatusItem]:
    """A missing check is never presented as a successful check."""
    status = status or {}
    configured = status.get("configured") is True
    def item(title, key, done, pending):
        value = status.get(key)
        state = "done" if value is True else ("pending" if value is False else "unknown")
        return StatusItem(title, state, done if value is True else (pending if value is False else "Run Check status to verify this item."))
    rows = [
        item("UEFI boot mode", "uefi", "This system is running in UEFI mode.", "Restart in UEFI mode. Legacy BIOS cannot use Secure Boot."),
        item("Signing key created", "mok_created", "The local MOK key and certificate are present.", "Setup needs to prepare a local signing key."),
        item("Trusted bootloader installed", "shim_installed", "The signed shim loader passed validation.", "Setup needs to install a valid signed shim loader."),
        item("GRUB signed", "grub_signed", "GRUB is signed with your MOK.", "The GRUB bootloader still needs a valid MOK signature."),
    ]
    unsigned = status.get("unsigned_kernels")
    if not configured or not isinstance(unsigned, list):
        rows.append(StatusItem("Kernel signatures", "unknown", "Kernel signatures will be checked after setup." if status else "Run Check status to verify this item."))
    elif unsigned:
        rows.append(StatusItem("Kernel signatures", "attention", "Unsigned: " + ", ".join(unsigned)))
    else:
        rows.append(StatusItem("Kernel signatures", "done", "The backend reports no unsigned installed kernels."))
    rows.extend([
        item("Secure Boot entry created", "boot_entry", "The ErgenOS Secure Boot entry exists in firmware.", "Setup needs to create the ErgenOS Secure Boot entry."),
        item("MOK enrolled in firmware", "mok_enrolled", "Firmware recognizes your signing key.", "Confirm Enroll MOK in MokManager after restarting." if status.get("enrollment_requested") else "The signing key has not been enrolled yet."),
    ])
    firmware = status.get("secure_boot")
    rows.append(StatusItem("Secure Boot enabled in firmware",
        "done" if firmware == "enabled" else ("pending" if firmware == "disabled" else "unknown"),
        "Firmware signature enforcement is enabled." if firmware == "enabled" else
        ("Enable Secure Boot in firmware after enrolling the MOK." if firmware == "disabled" else "Firmware Secure Boot state has not been confirmed.")))
    rows.append(item("Booted through the Secure Boot entry", "booted_through_shim",
                     "This session started through the ErgenOS shim loader.",
                     "Restart and select the ErgenOS Secure Boot entry."))
    if configured:
        rows = [StatusItem(row.title, "attention", row.detail) if row.state == "pending" and index in {1, 2, 3, 5} else row for index, row in enumerate(rows)]
    if status.get("uefi") is False:
        rows[0] = StatusItem(rows[0].title, "attention", rows[0].detail)
    return rows


def next_step(status: dict | None, preflight_ready: bool = False) -> NextStep:
    if not status:
        return NextStep("Start by checking your setup", "Check status shows what is already done and what still needs your attention. Check readiness tests whether this system can be configured.")
    if status.get("uefi") is not True:
        return NextStep("UEFI mode is required", "Restart the installed system in UEFI mode before setting up Secure Boot.")
    if status.get("removal_requested"):
        return NextStep("Next: confirm key removal after restarting", "Boot ErgenOS Secure Boot and confirm Delete MOK in MokManager. Then turn off Secure Boot in firmware, boot the normal ErgenOS entry and check status here.")
    if not status.get("configured"):
        if preflight_ready:
            return NextStep("Next: prepare Secure Boot", "Creates your signing key, signs the boot files and requests key enrollment. You will set a one-time MOK password and complete enrollment after restarting.", "enable", "Set up Secure Boot")
        return NextStep("Next: check this system is ready", "Checks the firmware mode, EFI partition and required boot tools before making changes.", "preflight", "Check readiness")
    if status.get("problems"):
        if status.get("mok_created") is not True:
            return NextStep("The original signing key needs attention", "The key material is missing or incomplete. Restore the original MOK files before repairing signatures. Check the results below.")
        return NextStep("Next: repair the signed boot files", "Rebuilds and signs GRUB, installed kernels and DKMS modules using your existing key. This does not enroll a new key or turn on Secure Boot in firmware.", "refresh", "Repair boot signatures")
    if not status.get("mok_enrolled"):
        if status.get("enrollment_requested"):
            return NextStep("Next: enroll your key after restarting", "Boot ErgenOS Secure Boot. In MokManager select Enroll MOK → Continue → Yes and enter your one-time password. Then enable Secure Boot in firmware, keeping its standard keys. Return here and select Check status.")
        if status.get("secure_boot") == "disabled":
            # Also covers the final step of an intentional MOK removal. Both
            # continuing setup and finishing removal remain accessible.
            detail = "The key is not enrolled. To continue setup, check readiness and request enrollment again. If you removed the key intentionally, finish disabling support under Maintenance and removal."
        else:
            detail = "The key is not enrolled. Check readiness before submitting a new enrollment request."
        return NextStep("Next: request key enrollment", detail, "enable" if preflight_ready else "preflight", "Set up Secure Boot" if preflight_ready else "Check readiness")
    if status.get("secure_boot") != "enabled":
        return NextStep("Next: enable Secure Boot in firmware", "Your key is enrolled. Enable Secure Boot in firmware without clearing its standard keys, then boot the ErgenOS Secure Boot entry. Return here and select Check status.")
    if not status.get("booted_through_shim"):
        return NextStep("Next: boot through the Secure Boot entry", "Restart and select ErgenOS Secure Boot in the firmware boot menu. Then return here and select Check status.")
    if any(item.state != "done" for item in status_items(status)):
        return NextStep("Review the remaining checks", "Some items could not be verified. Check the results below before considering setup complete.")
    return NextStep("Setup complete", "All reported checks passed. Secure Boot is active. Normal kernel and GRUB package updates are signed automatically.")
