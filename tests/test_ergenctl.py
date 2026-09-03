"""Unit tests for ErgenCTL diagnostics."""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import shutil
import sys
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


if __name__ == "__main__":
    unittest.main()
