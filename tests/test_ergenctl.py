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


if __name__ == "__main__":
    unittest.main()
