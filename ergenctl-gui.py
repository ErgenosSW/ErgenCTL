#!/usr/bin/env python3
"""GTK interface for ErgenCTL diagnostics and recovery."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

installed_module_directory = Path("/usr/lib/ergenctl")
if installed_module_directory.is_dir():
    sys.path.insert(0, str(installed_module_directory))

from ergenctl_gui_core import COMMANDS, GuiCommand, format_json_output, rollback_command


APP_ID = "io.github.ergenossw.ErgenCTL"


class ErgenCTLWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application) -> None:
        super().__init__(application=application, title="ErgenCTL")
        self.set_default_size(900, 720)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        self.set_content(toolbar)

        split = Adw.OverlaySplitView()
        split.set_sidebar_width_fraction(0.34)
        split.set_min_sidebar_width(280)
        split.set_max_sidebar_width(360)
        toolbar.set_content(split)

        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        sidebar.set_margin_top(24)
        sidebar.set_margin_bottom(24)
        sidebar.set_margin_start(24)
        sidebar.set_margin_end(24)
        split.set_sidebar(sidebar)

        local_logo = Path(__file__).resolve().parent / "assets" / "ergenctl-logo.png"
        logo = Gtk.Image.new_from_file(str(local_logo)) if local_logo.exists() else Gtk.Image.new_from_icon_name("ergenctl")
        logo.set_pixel_size(96)
        sidebar.append(logo)

        heading = Gtk.Label(label="ErgenCTL")
        heading.add_css_class("title-1")
        sidebar.append(heading)

        description = Gtk.Label(label="Diagnose and recover ErgenOS")
        description.add_css_class("dim-label")
        sidebar.append(description)

        actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sidebar.append(actions)
        self._add_action(actions, "Check system", "Run complete system diagnostics", "doctor")
        self._add_action(actions, "Check snapshots", "List available recovery points", "snapshots")
        self._add_action(actions, "Check hibernation", "Inspect resume configuration", "resume")
        self._add_action(actions, "Plan repairs", "Show changes without applying them", "repair-plan")

        self.repair_button = Gtk.Button(
            label="Repair system",
            tooltip_text="Run Check system first",
            sensitive=False,
        )
        self.repair_button.add_css_class("suggested-action")
        self.repair_button.connect("clicked", self._confirm_repair)
        actions.append(self.repair_button)

        self.rollback_button = Gtk.Button(
            label="Restore a snapshot",
            tooltip_text="Available after booting from a snapshot",
            sensitive=False,
        )
        self.rollback_button.connect("clicked", self._show_rollback_dialog)
        actions.append(self.rollback_button)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(24)
        content.set_margin_bottom(24)
        content.set_margin_start(24)
        content.set_margin_end(24)
        split.set_content(content)

        self.status = Adw.StatusPage(
            title="Ready",
            description="Choose an action to inspect or repair this system.",
            icon_name="system-search-symbolic",
        )
        content.append(self.status)

        self.result_heading = Gtk.Label(xalign=0)
        self.result_heading.add_css_class("title-2")
        self.result_summary = Gtk.Label(xalign=0, wrap=True)
        self.result_summary.add_css_class("dim-label")
        self.results = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.results.add_css_class("boxed-list")
        self.skipped_results = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.skipped_results.add_css_class("boxed-list")
        self.skipped_expander = Gtk.Expander(label="Skipped checks")
        self.skipped_expander.set_child(self.skipped_results)
        self.raw_output = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
        self.raw_output.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.raw_expander = Gtk.Expander(label="Technical details")
        self.raw_expander.set_child(self.raw_output)
        result_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        result_content.append(self.result_heading)
        result_content.append(self.result_summary)
        result_content.append(self.results)
        result_content.append(self.skipped_expander)
        result_content.append(self.raw_expander)
        output_scroll = Gtk.ScrolledWindow(vexpand=True)
        output_scroll.set_child(result_content)
        output_scroll.set_visible(False)
        self.output_scroll = output_scroll
        content.append(output_scroll)

        self.spinner = Gtk.Spinner()
        content.append(self.spinner)

    def _add_action(self, box: Gtk.Box, label: str, tooltip: str, command: str) -> None:
        button = Gtk.Button(label=label, tooltip_text=tooltip)
        button.connect("clicked", lambda _button: self._run(COMMANDS[command]))
        box.append(button)

    def _confirm_repair(self, _button: Gtk.Button) -> None:
        dialog = Adw.AlertDialog(
            heading="Repair this system?",
            body="ErgenCTL will create a safety snapshot when possible and apply supported repairs.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("repair", "Repair")
        dialog.set_response_appearance("repair", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _dialog, response: self._run(COMMANDS["repair"]) if response == "repair" else None)
        dialog.present(self)

    def _show_rollback_dialog(self, _button: Gtk.Button) -> None:
        row = Adw.SpinRow.new_with_range(1, 999999, 1)
        row.set_title("Snapshot number")
        dialog = Adw.AlertDialog(
            heading="Restore a snapshot",
            body="Boot from a GRUB snapshot first. The damaged base system will be preserved.",
        )
        dialog.set_extra_child(row)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("plan", "Show plan")
        dialog.add_response("restore", "Restore")
        dialog.set_response_appearance("restore", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("plan")
        dialog.set_close_response("cancel")

        def respond(_dialog: Adw.AlertDialog, response: str) -> None:
            number = int(row.get_value())
            if response == "plan":
                self._run(rollback_command(number))
            elif response == "restore":
                self._confirm_rollback(number)

        dialog.connect("response", respond)
        dialog.present(self)

    def _confirm_rollback(self, snapshot: int) -> None:
        dialog = Adw.AlertDialog(
            heading=f"Restore snapshot {snapshot}?",
            body="The base root filesystem will be replaced. A reboot will be required.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("restore", "Restore")
        dialog.set_response_appearance("restore", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _dialog, response: self._run(rollback_command(snapshot, True)) if response == "restore" else None)
        dialog.present(self)

    def _run(self, command: GuiCommand) -> None:
        installed = shutil.which("ergenctl")
        executable: str | tuple[str, str]
        if installed:
            executable = installed
        else:
            executable = (sys.executable, str(Path(__file__).resolve().with_name("ergenctl.py")))
        if command.privileged and shutil.which("pkexec") is None:
            self._show_result(command.title, "pkexec is not installed", False)
            return

        self.status.set_visible(False)
        self.output_scroll.set_visible(True)
        self._clear_results()
        self.result_heading.set_label(command.title)
        self.result_summary.set_label("Running...")
        self.raw_output.get_buffer().set_text(f"Running {command.title.lower()}...\n")
        self.spinner.start()
        try:
            process = Gio.Subprocess.new(
                command.argv(executable),
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE,
            )
        except GLib.Error as error:
            self._show_result(command.title, str(error), False)
            return
        process.communicate_utf8_async(None, None, self._command_finished, (process, command))

    def _command_finished(self, process: Gio.Subprocess, result: Gio.AsyncResult, data: tuple[Gio.Subprocess, GuiCommand]) -> None:
        _process, command = data
        try:
            _ok, stdout, stderr = process.communicate_utf8_finish(result)
            success = process.get_successful()
            message = stdout.strip() if stdout and stdout.strip() else (stderr or "No output").strip()
        except GLib.Error as error:
            success = False
            message = str(error)
        self._show_result(command.title, format_json_output(message), success, command)

    def _show_result(
        self,
        title: str,
        message: str,
        success: bool,
        command: GuiCommand | None = None,
    ) -> None:
        self.spinner.stop()
        prefix = "Completed" if success else "Failed"
        self._clear_results()
        self.result_heading.set_label(title)
        self.raw_output.get_buffer().set_text(f"{title} - {prefix}\n\n{message}\n")
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            self.result_summary.set_label(prefix)
            self._append_result(title, prefix, "pass" if success else "fail")
            self.raw_expander.set_expanded(True)
            return

        checks = payload.get("checks")
        if checks is None:
            checks = payload.get("resume_report", {}).get("checks")
        if checks:
            counts = {status: sum(check["status"] == status for check in checks) for status in ("pass", "warning", "fail", "skipped")}
            summary = [f'{counts["pass"]} passed']
            if counts["warning"]:
                summary.append(f'{counts["warning"]} warning(s)')
            if counts["fail"]:
                summary.append(f'{counts["fail"]} failed')
            if counts["skipped"]:
                summary.append(f'{counts["skipped"]} skipped')
            self.result_summary.set_label(", ".join(summary))
            ordered = sorted(checks, key=lambda check: {"fail": 0, "warning": 1, "pass": 2, "skipped": 3}.get(check["status"], 4))
            for check in ordered:
                self._append_result(check["title"], check["summary"], check["status"])
            if command is COMMANDS["doctor"]:
                self.repair_button.set_sensitive(True)
                self.repair_button.set_tooltip_text("Apply supported repairs")
                snapshot_boot = any(check["id"] == "boot-mode" and check["summary"] == "snapshot" for check in checks)
                self.rollback_button.set_sensitive(snapshot_boot)
                self.rollback_button.set_tooltip_text(
                    "Restore the base system from a snapshot" if snapshot_boot else "Available after booting from a snapshot"
                )
            self.raw_expander.set_expanded(False)
            return

        report = next((value for key, value in payload.items() if key.endswith("_report")), None)
        if isinstance(report, dict):
            result = report.get("message") or ("Operation completed" if report.get("success", success) else "Operation failed")
            self.result_summary.set_label(prefix)
            self._append_result(title, str(result), "pass" if report.get("success", success) else "fail")
        else:
            self.result_summary.set_label(prefix)
            self._append_result(title, prefix, "pass" if success else "fail")
        self.raw_expander.set_expanded(not success)

    def _append_result(self, title: str, summary: str, status: str) -> None:
        labels = {
            "pass": "OK",
            "warning": "!",
            "fail": "X",
            "skipped": "-",
        }
        row = Adw.ActionRow(title=title, subtitle=summary)
        icon = Gtk.Label(label=labels.get(status, "?"), width_chars=2)
        if status == "pass":
            icon.add_css_class("success")
        elif status == "warning":
            icon.add_css_class("warning")
        elif status == "fail":
            icon.add_css_class("error")
        row.add_prefix(icon)
        target = self.skipped_results if status == "skipped" else self.results
        target.append(row)
        self.skipped_expander.set_visible(self.skipped_results.get_first_child() is not None)

    def _clear_results(self) -> None:
        for result_list in (self.results, self.skipped_results):
            child = result_list.get_first_child()
            while child is not None:
                next_child = child.get_next_sibling()
                result_list.remove(child)
                child = next_child
        self.skipped_expander.set_visible(False)
        self.skipped_expander.set_expanded(False)


class ErgenCTLApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self) -> None:
        window = self.props.active_window or ErgenCTLWindow(self)
        window.present()


def main() -> int:
    Adw.init()
    return ErgenCTLApplication().run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
