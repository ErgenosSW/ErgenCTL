"""Unit tests for ErgenCTL diagnostics."""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "ergenctl.py"
SPEC = importlib.util.spec_from_file_location("ergenctl", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load {MODULE_PATH}")

ergenctl = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ergenctl
SPEC.loader.exec_module(ergenctl)


class DistributionCheckTests(unittest.TestCase):
    def test_ergenos_is_reported_as_pass(self) -> None:
        with patch.object(
            ergenctl,
            "read_os_release",
            return_value={"ID": "ergenos", "PRETTY_NAME": "ErgenOS Linux"},
        ):
            result = ergenctl.distribution_check()

        self.assertEqual(result.status, "pass")
        self.assertEqual(result.summary, "ErgenOS Linux")

    def test_other_distribution_is_reported_as_warning(self) -> None:
        with patch.object(
            ergenctl,
            "read_os_release",
            return_value={"ID": "arch", "PRETTY_NAME": "Arch Linux"},
        ):
            result = ergenctl.distribution_check()

        self.assertEqual(result.status, "warning")


class BootModeTests(unittest.TestCase):
    def test_snapshot_boot_is_detected(self) -> None:
        with patch.object(
            ergenctl,
            "snapshot_boot_detected",
            return_value=(True, "overlay root"),
        ):
            result = ergenctl.boot_mode_check()

        self.assertEqual(result.status, "pass")
        self.assertEqual(result.summary, "snapshot")

    def test_normal_boot_is_detected(self) -> None:
        with patch.object(
            ergenctl,
            "snapshot_boot_detected",
            return_value=(False, "regular root"),
        ):
            result = ergenctl.boot_mode_check()

        self.assertEqual(result.status, "pass")
        self.assertEqual(result.summary, "normal")


class DiskSpaceTests(unittest.TestCase):
    def test_disk_space_is_skipped_for_snapshot_overlay(self) -> None:
        with patch.object(
            ergenctl,
            "snapshot_boot_detected",
            return_value=(True, "overlay root"),
        ):
            result = ergenctl.disk_space_check()

        self.assertEqual(result.status, "skipped")

    def test_critically_low_disk_space_is_reported_as_failure(self) -> None:
        usage = shutil._ntuple_diskusage(total=100, used=96, free=4)
        with (
            patch.object(
                ergenctl,
                "snapshot_boot_detected",
                return_value=(False, "regular root"),
            ),
            patch.object(ergenctl.shutil, "disk_usage", return_value=usage),
        ):
            result = ergenctl.disk_space_check()

        self.assertEqual(result.status, "fail")


class SnapshotTests(unittest.TestCase):
    def test_snapshot_number_is_read_from_kernel_command_line(self) -> None:
        cmdline = "rootflags=subvol=@snapshots/42/snapshot quiet"

        self.assertEqual(ergenctl.snapshot_number_from_cmdline(cmdline), 42)
        self.assertIsNone(ergenctl.snapshot_number_from_cmdline("quiet splash"))

    def test_snapshot_count_is_skipped_for_snapshot_overlay(self) -> None:
        with (
            patch.object(ergenctl.shutil, "which", return_value="/usr/bin/snapper"),
            patch.object(
                ergenctl,
                "snapshot_boot_detected",
                return_value=(True, "overlay root"),
            ),
        ):
            result = ergenctl.snapshot_count_check()

        self.assertEqual(result.status, "skipped")

    def test_snapshot_count_uses_snapper_output(self) -> None:
        with (
            patch.object(ergenctl.shutil, "which", return_value="/usr/bin/snapper"),
            patch.object(
                ergenctl,
                "snapshot_boot_detected",
                return_value=(False, "regular root"),
            ),
            patch.object(ergenctl, "run", return_value=(0, "0\n1\n2\n3\n", "")),
        ):
            result = ergenctl.snapshot_count_check()

        self.assertEqual(result.status, "pass")
        self.assertEqual(result.summary, "3 found")

    def test_snapshot_tools_are_skipped_on_non_btrfs_root(self) -> None:
        self.assertEqual(ergenctl.snapper_check("ext4").status, "skipped")
        self.assertEqual(ergenctl.snapshot_count_check(root_fstype="ext4").status, "skipped")
        self.assertEqual(ergenctl.service_check("ext4").status, "skipped")
        self.assertEqual(ergenctl.pacman_hooks_check("ext4").status, "skipped")


class SnapshotReportTests(unittest.TestCase):
    def test_snapshot_report_skips_listing_for_overlay_boot(self) -> None:
        with (
            patch.object(
                ergenctl,
                "read_cmdline",
                return_value="rootflags=subvol=@snapshots/7/snapshot",
            ),
            patch.object(
                ergenctl,
                "snapshot_boot_detected",
                return_value=(True, "overlay root"),
            ),
            patch.object(ergenctl, "run") as run_mock,
        ):
            report, failed = ergenctl.collect_snapshot_report()

        self.assertFalse(failed)
        self.assertEqual(report.boot_mode, "snapshot")
        self.assertEqual(report.current_snapshot, 7)
        self.assertIsNone(report.listing)
        run_mock.assert_not_called()

    def test_snapshot_report_returns_snapper_listing(self) -> None:
        with (
            patch.object(ergenctl, "read_cmdline", return_value="quiet"),
            patch.object(
                ergenctl,
                "snapshot_boot_detected",
                return_value=(False, "regular root"),
            ),
            patch.object(ergenctl.shutil, "which", return_value="/usr/bin/snapper"),
            patch.object(ergenctl, "run", return_value=(0, "snapshot table", "")),
        ):
            report, failed = ergenctl.collect_snapshot_report()

        self.assertFalse(failed)
        self.assertTrue(report.available)
        self.assertEqual(report.listing, "snapshot table")

    def test_snapshot_report_explains_permission_failure(self) -> None:
        with (
            patch.object(ergenctl, "read_cmdline", return_value="quiet"),
            patch.object(
                ergenctl,
                "snapshot_boot_detected",
                return_value=(False, "regular root"),
            ),
            patch.object(ergenctl.shutil, "which", return_value="/usr/bin/snapper"),
            patch.object(ergenctl, "run", return_value=(1, "", "No permissions.")),
        ):
            report, failed = ergenctl.collect_snapshot_report()

        self.assertTrue(failed)
        self.assertIn("sudo", report.message)


class BootLogTests(unittest.TestCase):
    def test_current_boot_errors_are_collected(self) -> None:
        with (
            patch.object(ergenctl.shutil, "which", return_value="/usr/bin/journalctl"),
            patch.object(ergenctl, "run", return_value=(0, "first error\nsecond error", "")) as run_mock,
        ):
            report, failed = ergenctl.collect_boot_log()

        self.assertFalse(failed)
        self.assertEqual(report.boot, "current")
        self.assertEqual(report.priority, "error")
        self.assertEqual(report.category, "all")
        self.assertEqual(report.entries, ["first error", "second error"])
        self.assertIn("0", run_mock.call_args.args[0])
        self.assertIn("err..alert", run_mock.call_args.args[0])
        self.assertIn("100", run_mock.call_args.args[0])

    def test_previous_boot_uses_previous_journal(self) -> None:
        with (
            patch.object(ergenctl.shutil, "which", return_value="/usr/bin/journalctl"),
            patch.object(ergenctl, "run", return_value=(0, "-- No entries --", "")) as run_mock,
        ):
            report, failed = ergenctl.collect_boot_log(previous=True)

        self.assertFalse(failed)
        self.assertEqual(report.boot, "previous")
        self.assertEqual(report.entries, [])
        self.assertEqual(report.message, "No errors found")
        self.assertIn("-1", run_mock.call_args.args[0])

    def test_warning_priority_and_line_limit_are_forwarded(self) -> None:
        with (
            patch.object(ergenctl.shutil, "which", return_value="/usr/bin/journalctl"),
            patch.object(ergenctl, "run", return_value=(0, "warning", "")) as run_mock,
        ):
            report, failed = ergenctl.collect_boot_log(priority="warning", lines=25)

        self.assertFalse(failed)
        self.assertEqual(report.priority, "warning")
        self.assertIn("warning..alert", run_mock.call_args.args[0])
        self.assertIn("25", run_mock.call_args.args[0])

    def test_log_category_filters_entries(self) -> None:
        output = (
            "2026-09-03T21:00:00+02:00 host kernel: PM: hibernation failed\n"
            "2026-09-03T21:00:01+02:00 host pipewire: RTKit error\n"
            "2026-09-03T21:00:02+02:00 host kgx: Vulkan swapchain error"
        )
        with (
            patch.object(ergenctl.shutil, "which", return_value="/usr/bin/journalctl"),
            patch.object(ergenctl, "run", return_value=(0, output, "")),
        ):
            report, failed = ergenctl.collect_boot_log(category="resume")

        self.assertFalse(failed)
        self.assertEqual(report.category, "resume")
        self.assertEqual(
            report.entries,
            ["2026-09-03T21:00:00+02:00 host kernel: PM: hibernation failed"],
        )

    def test_resume_category_ignores_gnome_hibernate_shortcut(self) -> None:
        output = (
            "2026-09-03T21:00:00+02:00 host gsd-media-keys[1]: "
            "Failed to grab accelerator for keybinding settings:hibernate"
        )
        with (
            patch.object(ergenctl.shutil, "which", return_value="/usr/bin/journalctl"),
            patch.object(ergenctl, "run", return_value=(0, output, "")),
        ):
            report, failed = ergenctl.collect_boot_log(category="resume")

        self.assertFalse(failed)
        self.assertEqual(report.entries, [])
        self.assertEqual(report.message, "No errors found")

    def test_duplicate_messages_are_grouped_without_process_ids(self) -> None:
        entries = [
            "2026-09-03T21:00:00+02:00 host pipewire[10]: RTKit error",
            "2026-09-03T21:00:01+02:00 host pipewire[11]: RTKit error",
        ]

        groups = ergenctl.group_log_entries(entries)

        self.assertEqual(groups, [{"source": "pipewire", "message": "RTKit error", "count": 2}])

    def test_empty_journal_messages_are_removed(self) -> None:
        output = "2026-09-03T21:00:00+02:00 host kernel: \nreal entry"

        groups = ergenctl.group_log_entries(ergenctl.parse_journal_entries(output))

        self.assertEqual(groups, [{"source": "journal", "message": "real entry", "count": 1}])

    def test_boot_log_explains_permission_failure(self) -> None:
        with (
            patch.object(ergenctl.shutil, "which", return_value="/usr/bin/journalctl"),
            patch.object(ergenctl, "run", return_value=(1, "", "Permission denied")),
        ):
            report, failed = ergenctl.collect_boot_log()

        self.assertTrue(failed)
        self.assertFalse(report.available)
        self.assertIn("sudo", report.message)


class ResumeTests(unittest.TestCase):
    def empty_resume_log(self) -> ergenctl.BootLogReport:
        return ergenctl.BootLogReport("current", "warning", "resume", True, [], [], "No errors found")

    def test_resume_parameter_is_read_from_kernel_command_line(self) -> None:
        self.assertEqual(
            ergenctl.resume_parameter_from_cmdline("quiet resume=UUID=abc root=/dev/sda2"),
            "UUID=abc",
        )
        self.assertIsNone(ergenctl.resume_parameter_from_cmdline("quiet noresume"))

    def test_hibernation_without_hook_or_parameter_is_not_reported_as_broken(self) -> None:
        with (
            patch.object(ergenctl, "read_cmdline", return_value="quiet"),
            patch.object(ergenctl, "resume_hook_configured", return_value=False),
            patch.object(ergenctl, "snapshot_boot_detected", return_value=(False, "normal root")),
            patch.object(ergenctl, "collect_boot_log", return_value=(self.empty_resume_log(), False)),
        ):
            report = ergenctl.collect_resume_report()

        statuses = {check.id: check.status for check in report.checks}
        self.assertFalse(report.hibernation_configured)
        self.assertEqual(statuses["resume-parameter"], "skipped")
        self.assertEqual(statuses["resume-device"], "skipped")

    def test_normal_boot_with_active_resume_device_passes(self) -> None:
        with (
            patch.object(ergenctl, "read_cmdline", return_value="quiet resume=UUID=abc"),
            patch.object(ergenctl, "snapshot_boot_detected", return_value=(False, "normal root")),
            patch.object(ergenctl, "resolve_resume_device", return_value="/dev/sda2"),
            patch.object(ergenctl, "active_swap_devices", return_value={"/dev/sda2"}),
            patch.object(ergenctl, "collect_boot_log", return_value=(self.empty_resume_log(), False)),
        ):
            report = ergenctl.collect_resume_report()

        self.assertEqual(report.boot_mode, "normal")
        self.assertFalse(report.noresume)
        self.assertTrue(all(check.status == "pass" for check in report.checks))

    def test_snapshot_boot_with_noresume_skips_device(self) -> None:
        with (
            patch.object(ergenctl, "read_cmdline", return_value="resume=UUID=abc noresume"),
            patch.object(ergenctl, "snapshot_boot_detected", return_value=(True, "overlay root")),
            patch.object(ergenctl, "collect_boot_log", return_value=(self.empty_resume_log(), False)),
        ):
            report = ergenctl.collect_resume_report()

        statuses = {check.id: check.status for check in report.checks}
        self.assertEqual(statuses["resume-policy"], "pass")
        self.assertEqual(statuses["resume-parameter"], "skipped")
        self.assertEqual(statuses["resume-device"], "skipped")

    def test_missing_resume_device_fails(self) -> None:
        with (
            patch.object(ergenctl, "read_cmdline", return_value="resume=UUID=missing"),
            patch.object(ergenctl, "snapshot_boot_detected", return_value=(False, "normal root")),
            patch.object(ergenctl, "resolve_resume_device", return_value=None),
            patch.object(ergenctl, "active_swap_devices", return_value=set()),
            patch.object(ergenctl, "collect_boot_log", return_value=(self.empty_resume_log(), False)),
        ):
            report = ergenctl.collect_resume_report()

        device = next(check for check in report.checks if check.id == "resume-device")
        self.assertEqual(device.status, "fail")


class RepairTests(unittest.TestCase):
    def test_all_repair_uses_safe_order(self) -> None:
        steps = ergenctl.selected_repair_steps("all")

        self.assertEqual(
            [step.id for step in steps],
            ["repositories", "pacman-hooks", "services", "resume", "grub-snapshots"],
        )
        self.assertEqual(steps[-1].commands, (("/etc/grub.d/41_snapshots-btrfs",),))

    def test_preflight_allows_repair_of_mounted_base_system(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "etc").mkdir()
            (root / "etc/os-release").write_text("ID=ergenos\n")
            environment = ergenctl.RepairEnvironment(root=root, recovery=True)
            with (
                patch.object(ergenctl, "live_environment_detected", return_value=False),
                patch.object(ergenctl.os, "geteuid", return_value=0),
            ):
                error = ergenctl.repair_preflight(ergenctl.selected_repair_steps("repositories"), True, environment)

        self.assertIsNone(error)

    def test_recovery_commands_run_inside_base_system(self) -> None:
        environment = ergenctl.RepairEnvironment(root=Path("/run/ergenctl/target"), recovery=True)

        command = ergenctl.command_for_environment(("/usr/bin/mkinitcpio", "-P"), environment)

        self.assertEqual(
            command,
            ["/usr/bin/arch-chroot", "/run/ergenctl/target", "/usr/bin/mkinitcpio", "-P"],
        )

    def test_recovery_service_is_enabled_without_starting_snapshot_session(self) -> None:
        environment = ergenctl.RepairEnvironment(root=Path("/run/ergenctl/target"), recovery=True)

        command = ergenctl.command_for_environment(
            ("/usr/bin/systemctl", "enable", "--now", "grub-btrfsd.service"),
            environment,
        )

        self.assertEqual(
            command,
            ["/usr/bin/systemctl", "--root", "/run/ergenctl/target", "enable", "grub-btrfsd.service"],
        )

    def test_base_subvolume_is_read_from_fstab(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fstab = Path(directory) / "fstab"
            fstab.write_text("UUID=root / btrfs defaults,subvol=@ 0 0\n")

            subvolume = ergenctl.configured_root_subvolume(fstab)

        self.assertEqual(subvolume, "@")

    def test_cleanup_never_removes_a_still_mounted_system(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mount_directory = Path(directory) / "target"
            mount_directory.mkdir()
            environment = ergenctl.RepairEnvironment(
                root=mount_directory,
                recovery=True,
                mount_directory=mount_directory,
                mounted_paths=[mount_directory],
            )
            with patch.object(ergenctl, "run_repair_command", return_value=(1, "", "busy")):
                error = ergenctl.cleanup_repair_environment(environment)

            self.assertEqual(error, "busy")
            self.assertTrue(mount_directory.is_dir())

    def test_existing_boot_mount_is_detected(self) -> None:
        with patch.object(ergenctl, "run", return_value=(0, "/boot /dev/vda2", "")):
            source = ergenctl.existing_mount_source("/boot")

        self.assertEqual(source, "/dev/vda2")

    def test_parent_mount_is_not_treated_as_existing_boot_mount(self) -> None:
        with patch.object(ergenctl, "run", return_value=(0, "/ rootfs", "")):
            source = ergenctl.existing_mount_source("/boot")

        self.assertIsNone(source)

    def test_read_only_recovery_bind_uses_bind_mount(self) -> None:
        with patch.object(ergenctl, "run_repair_command", return_value=(0, "", "")) as run_mock:
            error = ergenctl.mount_recovery_bind("/boot", Path("/run/ergenctl/target/boot"), True)

        self.assertIsNone(error)
        self.assertEqual(
            run_mock.call_args.args[0],
            ["/usr/bin/mount", "-o", "bind,ro", "/boot", "/run/ergenctl/target/boot"],
        )

    def test_recovery_safety_snapshot_uses_btrfs_directly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".snapshots").mkdir()
            environment = ergenctl.RepairEnvironment(root=root, recovery=True)
            with patch.object(ergenctl, "run_repair_command", return_value=(0, "", "")) as run_mock:
                snapshot, error, command = ergenctl.create_safety_snapshot("all", environment)

        self.assertIsNone(error)
        self.assertTrue(snapshot.startswith("/.snapshots/ergenctl-safety-"))
        self.assertEqual(command[:4], ["/usr/bin/btrfs", "subvolume", "snapshot", "-r"])
        self.assertEqual(run_mock.call_args.args[0], command)

    def test_preflight_blocks_btrfs_repair_on_ext4(self) -> None:
        with (
            patch.object(ergenctl, "read_os_release", return_value={"ID": "ergenos"}),
            patch.object(ergenctl, "live_environment_detected", return_value=False),
            patch.object(ergenctl, "snapshot_boot_detected", return_value=(False, "normal")),
            patch.object(ergenctl, "root_filesystem_type", return_value="ext4"),
        ):
            error = ergenctl.repair_preflight(ergenctl.selected_repair_steps("services"), True)

        self.assertIn("requires Btrfs", error)

    def test_all_repair_executes_only_needed_steps(self) -> None:
        needed = [ergenctl.REPAIR_STEPS["repositories"], ergenctl.REPAIR_STEPS["resume"]]
        with (
            patch.object(ergenctl, "needed_repair_steps", return_value=needed),
            patch.object(ergenctl, "repair_preflight", return_value=None),
        ):
            report = ergenctl.execute_repair("all", dry_run=True, create_snapshot=True)

        self.assertEqual(
            report.steps,
            ["Enable required Pacman repositories", "Rebuild hibernation resume configuration"],
        )

    def test_dry_run_does_not_execute_commands(self) -> None:
        with (
            patch.object(ergenctl, "repair_preflight", return_value=None),
            patch.object(ergenctl, "run_repair_command") as run_mock,
            patch.object(ergenctl, "run_internal_repair") as action_mock,
            patch.object(ergenctl, "backup_configuration") as backup_mock,
        ):
            report = ergenctl.execute_repair("services", dry_run=True, create_snapshot=True)

        self.assertTrue(report.success)
        self.assertTrue(report.dry_run)
        self.assertEqual(len(report.executed_commands), 2)
        run_mock.assert_not_called()
        action_mock.assert_not_called()
        backup_mock.assert_not_called()

    def test_internal_action_is_shown_but_not_run_in_dry_run(self) -> None:
        with (
            patch.object(ergenctl, "repair_preflight", return_value=None),
            patch.object(ergenctl, "run_internal_repair") as action_mock,
        ):
            report = ergenctl.execute_repair("repositories", dry_run=True, create_snapshot=False)

        self.assertEqual(report.executed_actions, ["enable-multilib"])
        action_mock.assert_not_called()

    def test_repair_stops_after_internal_action_failure(self) -> None:
        with (
            patch.object(ergenctl, "repair_preflight", return_value=None),
            patch.object(ergenctl, "backup_configuration", return_value=None),
            patch.object(ergenctl, "run_internal_repair", return_value=(False, "bad config")),
            patch.object(ergenctl, "run_repair_command") as command_mock,
        ):
            report = ergenctl.execute_repair("resume", dry_run=False, create_snapshot=False)

        self.assertFalse(report.success)
        self.assertEqual(report.executed_actions, ["configure-resume"])
        self.assertIn("bad config", report.message)
        command_mock.assert_not_called()

    def test_successful_repair_is_validated(self) -> None:
        validation = ergenctl.Check("services", "Services", "pass", "active")
        with (
            patch.object(ergenctl, "repair_preflight", return_value=None),
            patch.object(ergenctl, "backup_configuration", return_value="/backup"),
            patch.object(ergenctl, "run_repair_command", return_value=(0, "", "")) as run_mock,
            patch.object(ergenctl, "validate_repair_step", return_value=validation),
        ):
            report = ergenctl.execute_repair("services", dry_run=False, create_snapshot=False)

        self.assertTrue(report.success)
        self.assertEqual(run_mock.call_count, 2)
        self.assertEqual(report.backup_directory, "/backup")

    def test_repair_stops_after_command_failure(self) -> None:
        with (
            patch.object(ergenctl, "repair_preflight", return_value=None),
            patch.object(ergenctl, "backup_configuration", return_value=None),
            patch.object(ergenctl, "run_repair_command", return_value=(1, "", "failed")) as run_mock,
        ):
            report = ergenctl.execute_repair("services", dry_run=False, create_snapshot=False)

        self.assertFalse(report.success)
        self.assertEqual(run_mock.call_count, 1)
        self.assertIn("failed", report.message)

    def test_services_validation_checks_daemon_and_cleanup_timer(self) -> None:
        with patch.object(ergenctl, "run", return_value=(0, "", "")) as run_mock:
            result = ergenctl.validate_repair_step(ergenctl.REPAIR_STEPS["services"])

        self.assertEqual(result.status, "pass")
        self.assertEqual(run_mock.call_count, 4)

    def test_enable_multilib_uncomments_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "pacman.conf"
            config.write_text("[core]\nInclude = /etc/pacman.d/mirrorlist\n\n#[multilib]\n#Include = /etc/pacman.d/mirrorlist\n")

            success, message = ergenctl.enable_multilib(config)

            self.assertTrue(success)
            self.assertEqual(message, "multilib enabled")
            self.assertIn("[multilib]\nInclude = /etc/pacman.d/mirrorlist", config.read_text())

    def test_configure_resume_replaces_stale_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mkinitcpio = root / "mkinitcpio.conf"
            grub = root / "grub"
            fstab = root / "fstab"
            mkinitcpio.write_text("HOOKS=(base udev resume filesystems)\n")
            grub.write_text('GRUB_CMDLINE_LINUX_DEFAULT="quiet resume=UUID=old"\n')
            fstab.write_text("UUID=new none swap defaults 0 0\n")

            success, message = ergenctl.configure_resume(mkinitcpio, grub, fstab)

            self.assertTrue(success)
            self.assertIn("UUID=new", message)
            self.assertEqual(grub.read_text(), 'GRUB_CMDLINE_LINUX_DEFAULT="quiet resume=UUID=new"\n')

    def test_configure_resume_skips_system_without_resume_hook(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mkinitcpio = root / "mkinitcpio.conf"
            grub = root / "grub"
            fstab = root / "fstab"
            mkinitcpio.write_text("HOOKS=(base udev filesystems)\n")
            grub.write_text('GRUB_CMDLINE_LINUX_DEFAULT="quiet"\n')

            success, message = ergenctl.configure_resume(mkinitcpio, grub, fstab)

            self.assertTrue(success)
            self.assertIn("no change", message)
            self.assertEqual(grub.read_text(), 'GRUB_CMDLINE_LINUX_DEFAULT="quiet"\n')


class RollbackTests(unittest.TestCase):
    def test_rollback_is_blocked_during_normal_boot(self) -> None:
        with patch.object(ergenctl, "snapshot_boot_detected", return_value=(False, "normal")):
            report = ergenctl.execute_rollback(4, dry_run=True)

        self.assertFalse(report.success)
        self.assertIn("only while booted from a snapshot", report.message)

    def test_rollback_dry_run_selects_snapper_snapshot_and_preserves_old_root(self) -> None:
        environment = ergenctl.RepairEnvironment(
            root=Path("/run/ergenctl/top-test"),
            recovery=True,
            mount_directory=Path("/run/ergenctl/top-test"),
        )
        with (
            patch.object(ergenctl, "snapshot_boot_detected", return_value=(True, "overlay")),
            patch.object(ergenctl, "live_environment_detected", return_value=False),
            patch.object(ergenctl.os, "geteuid", return_value=0),
            patch.object(ergenctl, "configured_root_subvolume", return_value="@"),
            patch.object(ergenctl, "configured_subvolume", return_value="@snapshots"),
            patch.object(ergenctl, "prepare_top_level_environment", return_value=(environment, None)),
            patch.object(ergenctl, "is_btrfs_subvolume", return_value=True),
            patch.object(ergenctl, "cleanup_repair_environment", return_value=None) as cleanup_mock,
            patch.object(ergenctl, "run_repair_command") as command_mock,
        ):
            report = ergenctl.execute_rollback(4, dry_run=True)

        self.assertTrue(report.success)
        self.assertEqual(report.source_subvolume, "@snapshots/4/snapshot")
        self.assertEqual(report.replaced_subvolume, "@")
        self.assertTrue(report.preserved_subvolume.startswith("@-broken-"))
        self.assertEqual(report.commands[0][:4], ["/usr/bin/btrfs", "subvolume", "snapshot", "/run/ergenctl/top-test/@snapshots/4/snapshot"])
        cleanup_mock.assert_called_once_with(environment)
        command_mock.assert_not_called()


class ServiceCheckTests(unittest.TestCase):
    def test_service_state_does_not_depend_on_property_order(self) -> None:
        output = "ActiveState=active\nLoadState=loaded\n"
        with (
            patch.object(ergenctl.shutil, "which", return_value="/usr/bin/systemctl"),
            patch.object(ergenctl, "run", return_value=(0, output, "")),
        ):
            result = ergenctl.service_check()

        self.assertEqual(result.status, "pass")
        self.assertEqual(result.summary, "active")


class MainExitCodeTests(unittest.TestCase):
    def test_doctor_returns_failure_when_a_check_fails(self) -> None:
        checks = [ergenctl.Check("test", "Test", "fail", "broken")]
        with (
            patch.object(ergenctl, "collect_doctor_checks", return_value=checks),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            exit_code = ergenctl.main(["doctor"])

        self.assertEqual(exit_code, 1)

    def test_doctor_accepts_warnings(self) -> None:
        checks = [ergenctl.Check("test", "Test", "warning", "uncertain")]
        with (
            patch.object(ergenctl, "collect_doctor_checks", return_value=checks),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            exit_code = ergenctl.main(["doctor"])

        self.assertEqual(exit_code, 0)

    def test_snapshots_json_emits_report_and_success(self) -> None:
        report = ergenctl.SnapshotReport(
            boot_mode="normal",
            current_snapshot=None,
            available=True,
            listing="snapshot table",
        )
        with (
            patch.object(ergenctl, "collect_snapshot_report", return_value=(report, False)),
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            exit_code = ergenctl.main(["snapshots", "--json"])

        payload = ergenctl.json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["snapshot_report"]["boot_mode"], "normal")
        self.assertEqual(payload["snapshot_report"]["listing"], "snapshot table")

    def test_snapshots_returns_failure_when_listing_fails(self) -> None:
        report = ergenctl.SnapshotReport(
            boot_mode="normal",
            current_snapshot=None,
            available=False,
            listing=None,
            message="snapper is not installed",
        )
        with (
            patch.object(ergenctl, "collect_snapshot_report", return_value=(report, True)),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            exit_code = ergenctl.main(["snapshots"])

        self.assertEqual(exit_code, 1)

    def test_logs_json_emits_boot_report(self) -> None:
        report = ergenctl.BootLogReport(
            boot="previous",
            priority="warning",
            category="resume",
            available=True,
            entries=["boot error"],
            groups=[{"source": "kernel", "message": "boot error", "count": 1}],
        )
        with (
            patch.object(ergenctl, "collect_boot_log", return_value=(report, False)),
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            exit_code = ergenctl.main(
                ["logs", "--previous", "--priority", "warning", "--category", "resume", "--json"]
            )

        payload = ergenctl.json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["boot_log"]["boot"], "previous")
        self.assertEqual(payload["boot_log"]["priority"], "warning")
        self.assertEqual(payload["boot_log"]["category"], "resume")
        self.assertEqual(payload["boot_log"]["entries"], ["boot error"])

    def test_logs_rejects_non_positive_line_limit(self) -> None:
        with (
            patch("sys.stdout", new_callable=io.StringIO),
            patch("sys.stderr", new_callable=io.StringIO),
            self.assertRaises(SystemExit) as raised,
        ):
            ergenctl.main(["logs", "--lines", "0"])

        self.assertEqual(raised.exception.code, 2)

    def test_resume_returns_failure_for_failed_check(self) -> None:
        report = ergenctl.ResumeReport(
            boot_mode="normal",
            hibernation_configured=True,
            noresume=False,
            resume_parameter="UUID=missing",
            checks=[ergenctl.Check("resume-device", "Resume device", "fail", "missing")],
        )
        with (
            patch.object(ergenctl, "collect_resume_report", return_value=report),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            exit_code = ergenctl.main(["resume"])

        self.assertEqual(exit_code, 1)

    def test_fix_can_be_cancelled_before_execution(self) -> None:
        with (
            patch.object(ergenctl, "confirm_repair", return_value=False),
            patch.object(ergenctl, "execute_repair") as execute_mock,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            exit_code = ergenctl.main(["fix", "services"])

        self.assertEqual(exit_code, 2)
        execute_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
