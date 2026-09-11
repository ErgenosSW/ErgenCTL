import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ergenctl_secureboot_core import gui_command, parse_report, password_error, state_text, MOK_TITLE

spec = importlib.util.spec_from_file_location('ctl_secureboot_tests', Path(__file__).resolve().parents[1] / 'ergenctl.py')
ctl = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ctl
spec.loader.exec_module(ctl)


class SecureBootGuiTests(unittest.TestCase):
    def test_secret_is_not_a_command_argument(self):
        self.assertEqual(gui_command('enable'), ['pkexec', '/usr/bin/ergenos-secureboot', 'gui', 'enable'])
        with self.assertRaises(ValueError):
            gui_command('enable; reboot')

    def test_password_validation(self):
        self.assertIsNone(password_error('Once1234', 'Once1234'))
        for value in ('', 'abc', 'a'*17, 'abcdefgh\n', 'Zażółć123', 'haslo 123'):
            self.assertIsNotNone(password_error(value, value))
        self.assertIsNotNone(password_error('Once1234', 'Other123'))
        self.assertEqual(MOK_TITLE, 'THE MOK PASSWORD IS USED ONLY ONCE')

    def test_incomplete_or_incompatible_report_is_rejected(self):
        for report in ({}, [], {'schema_version': 2, 'success': True},
                       {'schema_version': 1, 'success': True, 'status': {'state': 'active'}}):
            with self.assertRaises(ValueError):
                parse_report(json.dumps(report))

    def test_zero_exit_is_not_enough_for_active_state(self):
        status = dict(uefi=True, removal_requested=False, state='active', secure_boot='disabled',
                      mok_enrolled=True, booted_through_shim=True, problems=[])
        self.assertNotEqual(state_text(status)[0], 'Secure Boot is active')
        status['secure_boot'] = 'enabled'
        self.assertEqual(state_text(status)[0], 'Secure Boot is active')

    def test_missing_enrollment_request_requires_retry(self):
        status = dict(uefi=True, removal_requested=False, state='enrollment-pending', enrollment_requested=False)
        self.assertIn('Complete', state_text(status)[0])


class RecoveryTests(unittest.TestCase):
    def setup_root(self, root, configured=True):
        data = root / 'var/lib/ergenos/secureboot'
        (data / 'keys').mkdir(parents=True)
        (data / 'state.json').write_text(json.dumps({'configured': configured}))
        for name in ('MOK.key', 'MOK.cer', 'MOK.crt'):
            (data / 'keys' / name).write_text('same')
        (root / 'usr/bin').mkdir(parents=True)
        (root / 'usr/bin/ergenos-secureboot').touch()

    def test_old_snapshot_blocked_and_matching_snapshot_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            current, source = Path(directory)/'current', Path(directory)/'source'
            self.setup_root(current)
            self.assertIsNotNone(ctl.secureboot_rollback_error(current, source))
            self.setup_root(source)
            self.assertIsNone(ctl.secureboot_rollback_error(current, source))
            (source / 'var/lib/ergenos/secureboot/keys/MOK.key').write_text('different')
            self.assertIsNotNone(ctl.secureboot_rollback_error(current, source))

    def test_corrupt_state_never_silently_disables_signing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.setup_root(root)
            (root/'var/lib/ergenos/secureboot/state.json').write_text('broken')
            with self.assertRaises(ValueError):
                ctl.secureboot_recovery_commands(root)

    def test_refresh_then_check_in_repair_dry_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.setup_root(root)
            environment = ctl.RepairEnvironment(root=root)
            with patch.object(ctl, 'repair_preflight', return_value=None):
                report = ctl.execute_repair_in_environment('resume', True, False, environment)
            self.assertTrue(report.success)
            self.assertEqual(report.executed_commands[-2:], [
                ['/usr/bin/ergenos-secureboot', 'refresh'],
                ['/usr/bin/ergenos-secureboot', 'check', '--json'],
            ])

    def test_missing_backend_blocks_signing(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            self.setup_root(root)
            (root/'usr/bin/ergenos-secureboot').unlink()
            with self.assertRaises(ValueError):
                ctl.secureboot_recovery_commands(root)


class GuidedSetupTests(unittest.TestCase):
    def configured(self, **updates):
        status = dict(uefi=True, configured=True, mok_created=True, shim_installed=True,
                      grub_signed=True, unsigned_kernels=[], boot_entry=True, mok_enrolled=True,
                      secure_boot="enabled", booted_through_shim=True, problems=[],
                      removal_requested=False, enrollment_requested=False)
        status.update(updates)
        return status

    def test_missing_report_is_not_a_completed_checklist(self):
        from ergenctl_secureboot_core import status_items
        self.assertTrue(all(item.state == "unknown" for item in status_items(None)))

    def test_unconfigured_empty_kernel_list_is_not_verified(self):
        from ergenctl_secureboot_core import status_items
        rows = status_items(dict(configured=False, unsigned_kernels=[]))
        self.assertEqual(next(row.state for row in rows if row.title == "Kernel signatures"), "unknown")

    def test_active_system_needs_no_finalize_button(self):
        from ergenctl_secureboot_core import next_step, status_items
        status = self.configured()
        self.assertEqual(next_step(status).title, "Setup complete")
        self.assertIsNone(next_step(status).action)
        self.assertTrue(all(item.state == "done" for item in status_items(status)))

    def test_missing_fields_never_finish_setup(self):
        from ergenctl_secureboot_core import next_step
        status = self.configured()
        del status['grub_signed']
        self.assertNotEqual(next_step(status).title, 'Setup complete')

    def test_enrollment_and_firmware_steps_explain_external_action(self):
        from ergenctl_secureboot_core import next_step
        pending = next_step(self.configured(mok_enrolled=False, enrollment_requested=True))
        self.assertIn('Enroll MOK', pending.detail)
        self.assertIsNone(pending.action)
        firmware = next_step(self.configured(secure_boot='disabled'))
        self.assertIn('firmware', firmware.title)
        self.assertIsNone(firmware.action)

    def test_setup_requires_readiness(self):
        from ergenctl_secureboot_core import next_step
        status=self.configured(configured=False)
        self.assertEqual(next_step(status).action, 'preflight')
        self.assertEqual(next_step(status, True).action, 'enable')

    def test_missing_key_does_not_offer_signing_with_a_new_key(self):
        from ergenctl_secureboot_core import next_step
        step=next_step(self.configured(mok_created=False, problems=['MOK key material is incomplete']))
        self.assertIsNone(step.action)
        self.assertIn('original', step.title)

    def test_broken_signatures_have_a_named_repair_step(self):
        from ergenctl_secureboot_core import next_step, status_items
        status=self.configured(grub_signed=False, problems=['GRUB signature missing'])
        self.assertEqual(next_step(status).action, 'refresh')
        self.assertEqual(next(row.state for row in status_items(status) if row.title == 'GRUB signed'), 'attention')
