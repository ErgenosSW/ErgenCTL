"""Secure Boot page; secrets are only sent on the privileged process stdin."""
from __future__ import annotations

import json
from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk

from ergenctl_secureboot_core import (
    BACKEND, MOK_TITLE, MOK_NOTICE, gui_command, parse_report, password_error, state_text, status_items, next_step,
)


class SecureBootPage(Gtk.ScrolledWindow):
    def __init__(self, window):
        super().__init__(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.window = window
        self.ready = False
        self.preflight_ready = False
        self.report = None
        self.buttons = {}
        self.available = Path(BACKEND).is_file()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + side)(24)
        clamp = Adw.Clamp(maximum_size=760)
        clamp.set_child(box)
        self.set_child(clamp)
        heading_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        icon = Gtk.Image.new_from_icon_name("security-high-symbolic")
        icon.set_pixel_size(32)
        heading_box.append(icon)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        self.heading = Gtk.Label(label="Secure Boot", xalign=0, wrap=True)
        self.heading.add_css_class("title-2")
        self.summary = Gtk.Label(label="See what is ready and what to do next.", xalign=0, wrap=True)
        self.summary.add_css_class("dim-label")
        titles.append(self.heading)
        titles.append(self.summary)
        heading_box.append(titles)
        box.append(heading_box)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for action, label in (("status", "Check status"), ("preflight", "Check readiness")):
            button = Gtk.Button(label=label)
            button.connect("clicked", lambda _b, a=action: self.activate(a))
            self.buttons[action] = button
            controls.append(button)
        self.buttons["status"].add_css_class("suggested-action")
        box.append(controls)
        self.message = Gtk.Label(wrap=True, xalign=0, selectable=True)
        box.append(self.message)
        self.spinner = Gtk.Spinner()
        self.spinner.set_visible(False)
        box.append(self.spinner)

        next_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        next_card.add_css_class("card")
        next_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(next_content, "set_margin_" + side)(16)
        next_card.append(next_content)
        self.next_title = Gtk.Label(xalign=0, wrap=True)
        self.next_title.add_css_class("heading")
        self.next_detail = Gtk.Label(xalign=0, wrap=True)
        next_content.append(self.next_title)
        next_content.append(self.next_detail)
        self.next_action = None
        self.next_button = Gtk.Button(halign=Gtk.Align.START)
        self.next_button.add_css_class("suggested-action")
        self.next_button.connect("clicked", lambda _b: self.activate(self.next_action) if self.next_action else None)
        next_content.append(self.next_button)
        box.append(next_card)

        self.notice_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        title = Gtk.Label(label=MOK_TITLE, wrap=True, xalign=0)
        title.add_css_class("heading")
        self.notice_box.append(title)
        self.notice_text = Gtk.Label(label=MOK_NOTICE, wrap=True, xalign=0)
        self.notice_box.append(self.notice_text)
        self.notice_box.set_visible(False)
        box.append(self.notice_box)

        self.check_group = Adw.PreferencesGroup(title="Setup checklist",
            description="Each item is checked separately. Details show what is complete and what still needs attention.")
        self.check_rows = []
        box.append(self.check_group)
        self.problems = Gtk.Label(wrap=True, xalign=0, selectable=True)
        self.problems.add_css_class("warning")
        self.problems.set_visible(False)
        box.append(self.problems)

        self.maintenance = Gtk.Expander(label="Maintenance and removal")
        maintenance_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        maintenance_box.set_margin_top(12)
        self.maintenance_rows = {}
        for action, label, description in (
            ("refresh", "Rebuild signed boot files", "Rebuilds and signs GRUB, installed kernels and DKMS modules with your existing key. Use after manual boot changes, a DKMS driver update, or a failed update. Normal kernel and GRUB updates are signed automatically."),
            ("remove-mok", "Start removing Secure Boot", "Requests removal of your signing key. You will set a new one-time password, confirm removal in MokManager after restarting, then disable Secure Boot in firmware. Your normal ErgenOS boot entry is kept."),
            ("disable", "Finish disabling support", "After confirming MOK removal, turn off Secure Boot in firmware and boot the normal ErgenOS entry. This removes the Secure Boot entry and its DKMS signing configuration; key files and EFI backups are retained."),
        ):
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            button = Gtk.Button(label=label, halign=Gtk.Align.START)
            button.connect("clicked", lambda _b, a=action: self.activate(a))
            row.append(Gtk.Label(label=description, xalign=0, wrap=True))
            row.append(button)
            self.buttons[action] = button
            self.maintenance_rows[action] = row
            maintenance_box.append(row)
        self.maintenance.set_child(maintenance_box)
        box.append(self.maintenance)
        details = Gtk.Expander(label="Technical details")
        self.details = Gtk.Label(wrap=True, xalign=0, selectable=True)
        details.set_child(self.details)
        box.append(details)
        self.render_status()
        self.update_buttons()
        if self.available:
            self._probe()
        else:
            self.heading.set_label("Install ErgenOS-SecureBoot")
            self.summary.set_label("Install ergenos-secureboot from the ErgenOS repository using ErgenPac, then reopen ErgenCTL.")
            install = Gtk.Button(label="Open ErgenPac", halign=Gtk.Align.START)
            install.connect("clicked", self._open_packages)
            box.append(install)

    def render_status(self):
        for row in self.check_rows:
            self.check_group.remove(row)
        self.check_rows.clear()
        items = status_items(self.report)
        labels = {"done": "Done", "pending": "Pending", "attention": "Needs attention", "unknown": "Not checked"}
        symbols = {"done": "✓", "pending": "…", "attention": "!", "unknown": "?"}
        for item in items:
            row = Adw.ActionRow(title=GLib.markup_escape_text(item.title), subtitle=GLib.markup_escape_text(item.detail))
            row.set_subtitle_lines(0)
            icon = Gtk.Label(label=symbols[item.state], width_chars=2)
            icon.add_css_class("heading")
            state_label = Gtk.Label(label=labels[item.state], valign=Gtk.Align.CENTER)
            style = "success" if item.state == "done" else ("warning" if item.state == "attention" else "dim-label")
            icon.add_css_class(style)
            state_label.add_css_class(style)
            row.add_prefix(icon)
            row.add_suffix(state_label)
            self.check_group.add(row)
            self.check_rows.append(row)
        self.check_group.set_visible(self.report is not None)
        if self.report:
            title, _ = state_text(self.report)
            self.heading.set_label(title)
            complete = sum(item.state == "done" for item in items)
            self.summary.set_label(f"{complete} of {len(items)} checks complete.")
        step = next_step(self.report, self.preflight_ready)
        self.next_title.set_label(step.title)
        self.next_detail.set_label(step.detail)
        self.next_action = step.action
        self.next_button.set_label(step.label)
        self.next_button.set_visible(step.action is not None)
        problems = (self.report or {}).get("problems", [])
        self.problems.set_text("Reported problems:\n" + "\n".join(problems))
        self.problems.set_visible(bool(problems))

    def _open_packages(self, _button):
        try:
            Gio.Subprocess.new(["ergenpac"], Gio.SubprocessFlags.NONE)
        except GLib.Error:
            self.message.set_text("Could not open ErgenPac. Install ergenos-secureboot using your software manager.")

    def _probe(self):
        try:
            process = Gio.Subprocess.new([BACKEND, "capabilities"], Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE)
            process.communicate_utf8_async(None, None, self._probed)
        except GLib.Error:
            self.message.set_text("Could not start ErgenOS-SecureBoot.")

    def _probed(self, process, result):
        try:
            _, output, _ = process.communicate_utf8_finish(result)
            value = json.loads(output)
            self.ready = process.get_successful() and isinstance(value, dict) and value.get("gui_protocol") == 1 and value.get("password_stdin") is True
        except (GLib.Error, ValueError):
            self.ready = False
        self.message.set_text("Select Check status. The system will request administrator authentication if needed." if self.ready else "Update ErgenOS-SecureBoot: this version does not support GUI setup.")
        self.update_buttons()

    def update_buttons(self):
        status = self.report or {}
        available = self.ready and not self.window.operation_busy
        for action, button in self.buttons.items():
            enabled = available
            if action in {"refresh", "disable", "remove-mok"}:
                enabled = enabled and bool(status.get("configured"))
            if action == "remove-mok":
                enabled = enabled and bool(status.get("mok_enrolled")) and not status.get("removal_requested")
            if action == "disable":
                enabled = enabled and status.get("secure_boot") == "disabled" and not status.get("mok_enrolled")
            button.set_sensitive(bool(enabled))
        self.next_button.set_sensitive(available and (self.next_action != "enable" or self.preflight_ready))
        self.maintenance.set_visible(bool(status.get("configured")))
        self.maintenance_rows["remove-mok"].set_visible(bool(status.get("mok_enrolled")) and not status.get("removal_requested"))
        self.maintenance_rows["disable"].set_visible(not status.get("mok_enrolled") and status.get("secure_boot") == "disabled")

    def activate(self, action):
        if self.window.operation_busy:
            return
        if action in {"enable", "remove-mok"}:
            self._password_dialog(action)
        elif action in {"refresh", "disable"}:
            descriptions = {
                "refresh": "Rebuild and sign GRUB, installed kernels and DKMS modules using your existing MOK. This may take several minutes. It does not enroll a new key or enable Secure Boot in firmware.",
                "disable": "Remove the Secure Boot entry and DKMS signing configuration. You must have removed the MOK, disabled Secure Boot in firmware and booted the normal ErgenOS entry. Key files and EFI backups will be kept.",
            }
            dialog = Adw.AlertDialog(heading=self.buttons[action].get_label() + "?", body=descriptions[action])
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("continue", "Continue")
            dialog.set_default_response("cancel")
            dialog.set_close_response("cancel")
            dialog.connect("response", lambda _d, response: self.run(action) if response == "continue" else None)
            dialog.present(self.window)
        else:
            self.run(action)

    def _password_dialog(self, action):
        removal = action == "remove-mok"
        notice = MOK_NOTICE.replace("enrollment", "removal") if removal else MOK_NOTICE
        dialog = Adw.AlertDialog(heading="Set a one-time MOK password", body=MOK_TITLE + "\n\n" + notice)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        explanation = Gtk.Label(label="This is separate from your administrator password. Use 8–16 ASCII letters or digits; the keyboard layout may differ after restarting.", wrap=True, xalign=0)
        box.append(explanation)
        password = Adw.PasswordEntryRow(title="One-time MOK password")
        confirmation = Adw.PasswordEntryRow(title="Confirm MOK password")
        rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        rows.add_css_class("boxed-list")
        rows.append(password)
        rows.append(confirmation)
        box.append(rows)
        error = Gtk.Label(wrap=True, xalign=0)
        box.append(error)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("continue", "Continue")
        dialog.set_response_appearance("continue", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_enabled("continue", False)
        def changed(_row):
            message = password_error(password.get_text(), confirmation.get_text())
            error.set_text(message or "This password is only needed to authorize this operation in MokManager.")
            dialog.set_response_enabled("continue", message is None)
        password.connect("changed", changed)
        confirmation.connect("changed", changed)
        def response(_dialog, choice):
            secret = password.get_text() if choice == "continue" else None
            password.set_text("")
            confirmation.set_text("")
            if secret is not None:
                self.run(action, secret)
            secret = None
        dialog.connect("response", response)
        dialog.present(self.window)

    def run(self, action, password=None):
        if self.window.operation_busy:
            return
        self.window.set_operation_busy(True)
        self.spinner.set_visible(True)
        self.spinner.start()
        self.message.set_text({
            "status": "Checking your current setup. Administrator authentication may be required.",
            "preflight": "Checking readiness without changing your boot configuration.",
            "enable": "Preparing signed boot files and requesting MOK enrollment. This may take several minutes.",
            "refresh": "Rebuilding and signing boot files and DKMS modules. This may take several minutes.",
            "remove-mok": "Requesting MOK removal. Confirmation will be required after restarting.",
            "disable": "Removing the Secure Boot entry and signing configuration.",
        }.get(action, "Running the operation…"))
        self.preflight_ready = False
        self.details.set_text("")
        try:
            flags = Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE
            if password is not None:
                flags |= Gio.SubprocessFlags.STDIN_PIPE
            process = Gio.Subprocess.new(gui_command(action), flags)
            data = json.dumps({"password": password}) + "\n" if password is not None else None
            process.communicate_utf8_async(data, None, self._finished, action)
            data = None
        except GLib.Error:
            self._end()
            self.message.set_text("Could not start the operation. Check that Polkit and ErgenOS-SecureBoot are installed.")
        finally:
            password = None

    def _end(self):
        self.spinner.stop()
        self.spinner.set_visible(False)
        self.window.set_operation_busy(False)

    def _finished(self, process, result, action):
        try:
            _, output, stderr = process.communicate_utf8_finish(result)
            if process.get_if_exited() and process.get_exit_status() in {126, 127}:
                self.message.set_text("Authentication was cancelled or is unavailable. The operation was not started.")
                return
            report = parse_report(output)
            success = process.get_successful() and report["success"]
            self.preflight_ready = action == "preflight" and success
            self.report = report.get("status")
            if self.report:
                self.details.set_text(json.dumps(self.report, indent=2, ensure_ascii=False))
            elif not success:
                self.heading.set_label("Check the status before continuing")
                self.summary.set_label("The operation did not complete. Check status to see which changes were applied.")
            self.render_status()
            message = {
                "status": "Status updated. Review the checklist below.",
                "preflight": "Readiness check passed. Follow the next step below.",
                "enable": "Boot files prepared. Follow the enrollment instructions below.",
                "refresh": "Signed boot files rebuilt. Review the updated checks below.",
                "remove-mok": "Removal requested. Follow the restart instructions below.",
                "disable": "Secure Boot support disabled. Your key files and EFI backups have been kept.",
            }.get(action, "Operation completed.") if success else "The operation did not complete. " + report.get("message", "Check the configuration.")
            self.message.set_text(message)
            pending = self.report and (self.report.get("enrollment_requested") or self.report.get("removal_requested"))
            self.notice_box.set_visible(bool(pending))
            self.notice_text.set_text(MOK_NOTICE.replace("enrollment", "removal") if self.report and self.report.get("removal_requested") else MOK_NOTICE)
        except (GLib.Error, ValueError, TypeError):
            self.report = None
            self.render_status()
            self.heading.set_label("Status could not be verified")
            self.summary.set_label("Run Check status again before continuing.")
            self.message.set_text("Could not read the result. Check the status again; do not assume setup completed.")
        finally:
            self._end()
