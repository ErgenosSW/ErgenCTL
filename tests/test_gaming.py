"""Offline gaming diagnostics, including incomplete and software-only systems."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ergenctl
from ergenctl_gui_core import COMMANDS


class GamingTests(unittest.TestCase):
    def test_non_graphics_pci_functions_do_not_overwrite_gpu_driver(self):
        output = "0000:63:00.0 VGA compatible controller: NVIDIA GPU\n\tKernel driver in use: nvidia\n0000:63:00.1 Audio device: NVIDIA audio\n\tKernel driver in use: snd_hda_intel\n0000:64:00.0 Display controller: AMD GPU\n\tKernel driver in use: amdgpu\n0000:64:00.4 USB controller: AMD USB\n\tKernel driver in use: xhci_hcd"
        devices = ergenctl.parse_graphics_device_details(output)
        self.assertEqual([device.driver for device in devices], ["nvidia", "amdgpu"])

    def test_missing_vulkan_probe_is_not_a_driver_failure(self):
        with patch.object(ergenctl.shutil, "which", return_value=None), patch.object(ergenctl, "run") as run:
            check = ergenctl.gaming_vulkan_check()
        self.assertEqual(check.status, "unknown")
        run.assert_not_called()

    def test_vulkan_failure_preserves_diagnostic(self):
        with patch.object(ergenctl.shutil, "which", return_value="/bin/vulkaninfo"), patch.object(
            ergenctl, "run", return_value=(1, "", "Cannot create Vulkan instance")
        ):
            check = ergenctl.gaming_vulkan_check()
        self.assertEqual(check.status, "fail")
        self.assertIn("Cannot create", check.evidence)

    def test_software_only_vulkan_is_not_ready(self):
        with patch.object(ergenctl.shutil, "which", return_value="/bin/vulkaninfo"), patch.object(
            ergenctl, "run", return_value=(0, "GPU0:\n deviceType = PHYSICAL_DEVICE_TYPE_CPU\n deviceName = llvmpipe", "")
        ):
            self.assertEqual(ergenctl.gaming_vulkan_check().status, "warning")

    def test_vulkan_reports_both_hardware_devices(self):
        output = "GPU0:\n deviceType = PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU\n deviceName = Radeon 860M\nGPU1:\n deviceType = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU\n deviceName = RTX 5050"
        with patch.object(ergenctl.shutil, "which", return_value="/bin/vulkaninfo"), patch.object(
            ergenctl, "run", return_value=(0, output, "")
        ):
            check = ergenctl.gaming_vulkan_check()
        self.assertEqual(check.status, "pass")
        self.assertIn("Radeon 860M", check.summary)
        self.assertIn("RTX 5050", check.summary)

    def test_unrecognized_vulkan_output_remains_unknown(self):
        with patch.object(ergenctl.shutil, "which", return_value="/bin/vulkaninfo"), patch.object(
            ergenctl, "run", return_value=(0, "Unexpected format", "")
        ):
            self.assertEqual(ergenctl.gaming_vulkan_check().status, "unknown")

    def test_hybrid_packages_and_missing_32_bit_driver(self):
        packages = dict.fromkeys(("vulkan-icd-loader", "lib32-vulkan-icd-loader", "vulkan-radeon", "lib32-vulkan-radeon", "nvidia-utils", "mesa", "lib32-mesa"), "1.0")
        devices = [ergenctl.GraphicsDevice("0000:01:00.0", "AMD", "amdgpu"),
                   ergenctl.GraphicsDevice("0000:02:00.0", "NVIDIA", "nvidia")]
        checks = ergenctl.gaming_library_checks(packages, devices)
        self.assertEqual(checks[0].status, "pass")
        self.assertEqual(checks[1].status, "warning")
        self.assertIn("lib32-nvidia-utils", checks[1].summary)
        self.assertIn("Flatpak uses its own libraries", checks[1].evidence)

    def test_unreadable_database_does_not_claim_packages_missing(self):
        checks = ergenctl.gaming_library_checks(None, [])
        self.assertEqual([check.status for check in checks], ["unknown"])

    def test_unknown_driver_does_not_claim_complete_coverage(self):
        checks = ergenctl.gaming_library_checks(
            {"vulkan-icd-loader": "1", "lib32-vulkan-icd-loader": "1"},
            [ergenctl.GraphicsDevice("0000:00:00.0", "GPU", None)])
        self.assertEqual(checks[-1].status, "unknown")

    def test_runner_inventory_does_not_execute_wine(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            runner = home / ".local/share/Steam/compatibilitytools.d/ErgenProton-WoW-test"
            runner.mkdir(parents=True)
            (runner / "proton").write_text("never execute this")
            with patch.object(ergenctl, "run") as run:
                check = ergenctl.gaming_runtime_check(home)
            self.assertEqual(check.status, "pass")
            self.assertIn("ErgenProton-WoW-test", check.summary)
            run.assert_not_called()

    def test_offline_inventory_and_optional_tools(self):
        calls = []
        def run(command):
            calls.append(command)
            if command == ["pacman", "-Q"]:
                return 0, "vulkan-icd-loader 1\nlib32-vulkan-icd-loader 1", ""
            if command == ["flatpak", "list", "--app", "--columns=application,version"]:
                return 0, "io.github.Faugus.faugus-launcher\t2.5.0", ""
            return 1, "", "not available"
        with tempfile.TemporaryDirectory() as home, patch.object(ergenctl.shutil, "which", return_value="/bin/tool"), patch.object(
            ergenctl, "run", side_effect=run
        ), patch.object(ergenctl, "hybrid_graphics_check", return_value=ergenctl.Check("hybrid", "GPU", "unknown", "unavailable")):
            checks = {check.id: check for check in ergenctl.collect_gaming_checks(Path(home))}
        self.assertIn("Faugus (Flatpak 2.5.0)", checks["gaming-launchers"].summary)
        for name in ("gamemode", "mangohud", "gamescope"):
            self.assertEqual(checks["gaming-" + name].status, "skipped")
        self.assertEqual(checks["gaming-game-gpu"].status, "unknown")
        self.assertTrue(all(command in (["pacman", "-Q"], ["lspci", "-Dk"], ["vulkaninfo", "--summary"],
                                       ["flatpak", "list", "--app", "--columns=application,version"]) for command in calls))

    def test_cli_json_and_exit_status(self):
        for status, expected in (("fail", 1), ("unknown", 0), ("skipped", 0)):
            output = io.StringIO()
            with patch.object(ergenctl, "collect_gaming_checks", return_value=[ergenctl.Check("probe", "Probe", status, "result")]), contextlib.redirect_stdout(output):
                self.assertEqual(ergenctl.main(["gaming-check", "--json"]), expected)
            self.assertEqual(json.loads(output.getvalue())["checks"][0]["status"], status)

    def test_gui_command_never_requests_root(self):
        self.assertEqual(COMMANDS["gaming-check"].argv(), ["ergenctl", "gaming-check", "--json"])


if __name__ == "__main__":
    unittest.main()
