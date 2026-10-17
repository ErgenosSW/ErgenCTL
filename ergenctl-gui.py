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

from ergenctl_gui_core import (
    COMMANDS,
    GuiCommand,
    bootloader_recovery_command,
    compact_log_message,
    format_json_output,
    is_kernel_trace_fragment,
    live_iso_detected,
    log_command,
    repair_plan_can_execute,
    rollback_command,
)


from ergenctl_secureboot_gui import SecureBootPage


APP_ID = "io.github.ergenossw.ErgenCTL"


class ErgenCTLWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application) -> None:
        super().__init__(application=application, title="ErgenCTL")
        self.set_default_size(980, 760)
        self.add_css_class("ergen-shell")
        self.boot_mode: str | None = None
        self.selected_snapshot: int | None = None
        self.repair_plan_ready = False
        self.operation_busy = False
        self.live_iso = live_iso_detected()
        self.connect("close-request", self._close_requested)
        self._install_styles()

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        brand = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        brand.add_css_class("ergen-header-brand")
        local_logo = Path(__file__).resolve().parent / "assets" / "ergenctl-logo.png"
        brand_logo = Gtk.Image.new_from_file(str(local_logo)) if local_logo.exists() else Gtk.Image.new_from_icon_name("ergenctl")
        brand_logo.set_pixel_size(30)
        brand.append(brand_logo)
        brand_name = Gtk.Label(label="ErgenCTL")
        brand_name.add_css_class("ergen-header-title")
        brand.append(brand_name)
        header.pack_start(brand)
        self.pages = Adw.ViewStack()
        topnav = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        topnav.add_css_class("ergen-topnav")
        repair_nav = Gtk.ToggleButton(label="Repair")
        repair_nav.add_css_class("ergen-topnav-button")
        repair_nav.set_active(True)
        secureboot_nav = Gtk.ToggleButton(label="Secure Boot")
        secureboot_nav.add_css_class("ergen-topnav-button")
        secureboot_nav.set_group(repair_nav)
        repair_nav.connect("toggled", self._page_changed, "repair")
        secureboot_nav.connect("toggled", self._page_changed, "secureboot")
        topnav.append(repair_nav)
        topnav.append(secureboot_nav)
        header.set_title_widget(topnav)
        toolbar.add_top_bar(header)
        self.set_content(toolbar)

        toolbar.set_content(self.pages)
        repair_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.repair_scroll = repair_scroll
        repair_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)
        repair_page.add_css_class("ergen-dashboard")
        repair_scroll.set_child(repair_page)
        self.pages.add_titled_with_icon(repair_scroll, "repair", "Repair", "system-tools-symbolic")

        hero = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=28)
        hero.add_css_class("ergen-command-hero")
        hero_copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=True)
        eyebrow = Gtk.Label(label="ERGENOS · SYSTEM CONTROL", xalign=0)
        eyebrow.add_css_class("ergen-eyebrow")
        hero_copy.append(eyebrow)
        heading = Gtk.Label(label="Keep the system under control.", xalign=0, wrap=True)
        heading.add_css_class("ergen-display")
        hero_copy.append(heading)
        description = Gtk.Label(
            label="Diagnostics, recovery and boot protection in one control center.",
            xalign=0,
            wrap=True,
        )
        description.add_css_class("ergen-muted")
        hero_copy.append(description)
        hero.append(hero_copy)
        repair_page.append(hero)

        self.workspace = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.SLIDE_LEFT_RIGHT,
            transition_duration=220,
        )
        self.workspace.set_vexpand(True)
        self.workspace.set_hhomogeneous(False)
        self.workspace.set_vhomogeneous(False)
        repair_page.append(self.workspace)

        dashboard = Gtk.Grid(column_spacing=18, row_spacing=18)
        dashboard.set_column_homogeneous(True)
        self.status = dashboard
        diagnostics = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        diagnostics.add_css_class("ergen-panel")
        diagnostics.append(self._panel_heading("DIAGNOSTICS", "Inspect system health"))
        diagnostic_grid = Gtk.Grid(column_spacing=12, row_spacing=12, column_homogeneous=True)
        diagnostic_grid.attach(self._command_card("System scan", "Full health report", "system-search-symbolic", "doctor"), 0, 0, 1, 1)
        diagnostic_grid.attach(self._command_card("Snapshots", "Recovery points", "document-open-recent-symbolic", "snapshots"), 1, 0, 1, 1)
        diagnostic_grid.attach(self._command_card("Hibernation", "Resume integrity", "weather-clear-night-symbolic", "resume"), 0, 1, 1, 1)
        logs_card = self._custom_card("Boot logs", "Errors from startup", "utilities-terminal-symbolic")
        logs_card.connect("clicked", self._show_logs_dialog)
        diagnostic_grid.attach(logs_card, 1, 1, 1, 1)
        diagnostics.append(diagnostic_grid)
        dashboard.attach(diagnostics, 0, 0, 2, 1)

        recovery = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        recovery.add_css_class("ergen-panel")
        recovery.add_css_class("ergen-recovery-panel")
        recovery.append(self._panel_heading("RECOVERY", "Controlled repair actions"))
        self.repair_actions = recovery
        plan_button = self._wide_action("Plan repairs", "Preview every proposed change", "view-list-symbolic")
        plan_button.connect("clicked", lambda _button: self._run(COMMANDS["repair-plan"]))
        recovery.append(plan_button)
        self.bootloader_button = self._wide_action("Recover bootloader", "Live ISO emergency workflow", "drive-harddisk-symbolic")
        self.bootloader_button.connect("clicked", self._confirm_bootloader_repair)
        self.bootloader_button.set_visible(self.live_iso)
        recovery.append(self.bootloader_button)
        self.repair_button = self._wide_action("Repair system", "Available after a successful scan", "applications-engineering-symbolic")
        self.repair_button.add_css_class("ergen-primary-action")
        self.repair_button.set_sensitive(False)
        self.repair_button.connect("clicked", self._confirm_repair)
        recovery.append(self.repair_button)
        self.rollback_button = self._wide_action("Restore snapshot", "Available while booted from a snapshot", "edit-undo-symbolic")
        self.rollback_button.set_sensitive(False)
        self.rollback_button.connect("clicked", self._show_rollback_dialog)
        recovery.append(self.rollback_button)
        dashboard.attach(recovery, 2, 0, 1, 1)
        self.workspace.add_named(dashboard, "dashboard")

        result_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.back_button = Gtk.Button()
        self.back_button.add_css_class("ergen-back-button")
        back_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        back_content.append(Gtk.Image.new_from_icon_name("go-previous-symbolic"))
        back_content.append(Gtk.Label(label="Dashboard"))
        self.back_button.set_child(back_content)
        self.back_button.connect("clicked", self._show_dashboard)
        result_header.append(self.back_button)
        self.result_heading = Gtk.Label(xalign=0, hexpand=True)
        self.result_heading.add_css_class("title-2")
        result_header.append(self.result_heading)
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
        result_content.append(result_header)
        result_content.append(self.result_summary)
        result_content.append(self.results)
        result_content.append(self.skipped_expander)
        result_content.append(self.raw_expander)
        result_content.add_css_class("ergen-results-panel")
        self.output_scroll = result_content
        self.workspace.add_named(self.output_scroll, "results")
        self.workspace.set_visible_child_name("dashboard")

        self.spinner = Gtk.Spinner()
        self.spinner.set_halign(Gtk.Align.CENTER)
        repair_page.append(self.spinner)
        self.secureboot_page = SecureBootPage(self)
        self.pages.add_titled_with_icon(self.secureboot_page, "secureboot", "Secure Boot", "security-high-symbolic")
        self.pages.set_visible_child_name("repair")

        self.set_size_request(360, 240)
        compact = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 850sp"))
        def reflow(narrow):
            secureboot_content = self.secureboot_page.get_child()
            while not isinstance(secureboot_content, Gtk.Box):
                secureboot_content = secureboot_content.get_child()
            secureboot_content.set_margin_start(12 if narrow else 24)
            secureboot_content.set_margin_end(12 if narrow else 24)
            dashboard.remove(diagnostics)
            dashboard.remove(recovery)
            dashboard.attach(diagnostics, 0, 0, 1 if narrow else 2, 1)
            dashboard.attach(recovery, 0 if narrow else 2, 1 if narrow else 0, 1, 1)
            (self.add_css_class if narrow else self.remove_css_class)("compact")
        compact.connect("apply", lambda *_: reflow(True))
        compact.connect("unapply", lambda *_: reflow(False))
        self.add_breakpoint(compact)
        phone = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 500sp"))
        phone.add_setter(brand_name, "visible", False)
        def reflow_cards(narrow):
            cards = [diagnostic_grid.get_child_at(0, 0), diagnostic_grid.get_child_at(1, 0),
                     diagnostic_grid.get_child_at(0, 1), diagnostic_grid.get_child_at(1, 1)] if narrow else self._compact_cards
            self._compact_cards = cards
            for card in cards:
                diagnostic_grid.remove(card)
            for index, card in enumerate(cards):
                diagnostic_grid.attach(card, 0 if narrow else index % 2, index if narrow else index // 2, 1, 1)
        phone.connect("apply", lambda *_: (reflow(True), reflow_cards(True)))
        phone.connect("unapply", lambda *_: (reflow_cards(False), reflow(False)))
        self.add_breakpoint(phone)

    @staticmethod
    def _panel_heading(eyebrow: str, title: str) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        kicker = Gtk.Label(label=eyebrow, xalign=0)
        kicker.add_css_class("ergen-eyebrow")
        box.append(kicker)
        heading = Gtk.Label(label=title, xalign=0, wrap=True)
        heading.add_css_class("ergen-panel-title")
        box.append(heading)
        return box

    def _command_card(self, title: str, subtitle: str, icon: str, command: str) -> Gtk.Button:
        button = self._custom_card(title, subtitle, icon)
        button.connect("clicked", lambda _button: self._run(COMMANDS[command]))
        return button

    @staticmethod
    def _custom_card(title: str, subtitle: str, icon: str) -> Gtk.Button:
        button = Gtk.Button()
        button.add_css_class("ergen-command-card")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        image = Gtk.Image.new_from_icon_name(icon)
        image.set_pixel_size(28)
        image.set_halign(Gtk.Align.START)
        image.add_css_class("ergen-card-icon")
        box.append(image)
        label = Gtk.Label(label=title, xalign=0, wrap=True)
        label.add_css_class("ergen-card-title")
        box.append(label)
        detail = Gtk.Label(label=subtitle, xalign=0, wrap=True)
        detail.add_css_class("ergen-muted")
        box.append(detail)
        button.set_child(box)
        return button

    @staticmethod
    def _wide_action(title: str, subtitle: str, icon: str) -> Gtk.Button:
        button = Gtk.Button()
        button.add_css_class("ergen-wide-action")
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        image = Gtk.Image.new_from_icon_name(icon)
        image.set_pixel_size(22)
        row.append(image)
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        labels.set_hexpand(True)
        labels.append(Gtk.Label(label=title, xalign=0, wrap=True))
        detail = Gtk.Label(label=subtitle, xalign=0, wrap=True)
        detail.add_css_class("ergen-muted")
        labels.append(detail)
        row.append(labels)
        row.append(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        button.set_child(row)
        return button

    def _page_changed(self, button: Gtk.ToggleButton, page: str) -> None:
        if button.get_active():
            self.pages.set_visible_child_name(page)

    def _show_dashboard(self, _button: Gtk.Button | None = None) -> None:
        if self.operation_busy:
            return
        self.workspace.set_visible_child_name("dashboard")
        self.repair_scroll.get_vadjustment().set_value(0)

    def _install_styles(self) -> None:
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.PREFER_DARK)
        provider = Gtk.CssProvider()
        provider.load_from_data(b"""
          .compact .ergen-dashboard { padding: 12px; }
          .compact .ergen-command-hero { padding: 18px; border-radius: 18px; }
          .compact .ergen-display { font-size: 25px; }
          .compact .ergen-panel { padding: 14px; }
          .compact .ergen-command-card { min-height: 90px; padding: 12px; }
          .compact .ergen-topnav-button { min-width: 0; padding: 4px 8px; }
          .compact .ergen-topnav { padding: 2px; }
          .compact .ergen-results-panel { padding: 12px; }
          .compact .secureboot-hero { padding: 16px; }

          window.ergen-shell {
            background: linear-gradient(145deg, #030a14, #071525);
            color: #eaf3ff;
          }
          window.ergen-shell headerbar {
            background: alpha(#030a14, 0.94);
            border-bottom: 1px solid alpha(#8fbeeb, 0.16);
            box-shadow: none;
          }
          .ergen-header-brand { margin-left: 4px; }
          .ergen-header-title { color: #eaf3ff; font-size: 16px; font-weight: 800; }
          .ergen-dashboard { padding: 28px 32px 34px; }
          .ergen-command-hero {
            padding: 32px 36px;
            border-radius: 24px;
            border: 1px solid alpha(#31b6ff, 0.30);
            background: linear-gradient(120deg, #0b3152, #08182b 68%, #07111f);
            box-shadow: 0 18px 48px alpha(black, 0.30);
          }
          .ergen-display { color: #f4f9ff; font-size: 34px; font-weight: 850; }
          .ergen-panel {
            padding: 22px;
            border-radius: 20px;
            border: 1px solid alpha(#8fbeeb, 0.16);
            background: alpha(#081424, 0.94);
          }
          .ergen-recovery-panel {
            border-color: alpha(#31b6ff, 0.24);
            background: linear-gradient(155deg, alpha(#0b2138, 0.98), alpha(#07111f, 0.98));
          }
          .ergen-panel-title { color: #f4f9ff; font-size: 20px; font-weight: 800; }
          .ergen-command-card {
            min-height: 126px;
            padding: 18px;
            border-radius: 16px;
            border: 1px solid alpha(#8fbeeb, 0.14);
            background: alpha(#0b2035, 0.66);
          }
          .ergen-command-card:hover {
            border-color: alpha(#31b6ff, 0.55);
            background: linear-gradient(145deg, alpha(#10446e, 0.72), alpha(#0b2035, 0.82));
            box-shadow: 0 10px 26px alpha(black, 0.22);
          }
          .ergen-card-icon { color: #54c3ff; }
          .ergen-card-title { color: #f4f9ff; font-size: 16px; font-weight: 800; }
          .ergen-wide-action {
            min-height: 62px;
            padding: 8px 12px;
            border-radius: 14px;
            border: 1px solid alpha(#8fbeeb, 0.14);
            background: alpha(#0b2035, 0.72);
          }
          .ergen-wide-action:hover { border-color: alpha(#31b6ff, 0.50); }
          .ergen-primary-action {
            background: linear-gradient(135deg, #0aa7ff, #0068ff);
            color: white;
          }
          .ergen-results-panel {
            padding: 24px;
            border-radius: 20px;
            border: 1px solid alpha(#31b6ff, 0.22);
            background: alpha(#081424, 0.96);
          }
          .ergen-back-button {
            min-height: 38px;
            border-radius: 11px;
            border: 1px solid alpha(#31b6ff, 0.24);
            background: alpha(#0b2035, 0.82);
          }
          .secureboot-dashboard { padding: 8px; }
          .secureboot-hero {
            padding: 28px 30px;
            border-radius: 22px;
            border: 1px solid alpha(#31b6ff, 0.28);
            background: linear-gradient(125deg, #0b3152, #07111f);
          }
          .secureboot-controls {
            padding: 12px;
            border-radius: 15px;
            background: alpha(#081424, 0.92);
          }
          .secureboot-next, .secureboot-notice, .secureboot-maintenance {
            padding: 18px;
            border-radius: 18px;
            border: 1px solid alpha(#8fbeeb, 0.16);
            background: alpha(#081424, 0.94);
          }
          .secureboot-next { border-color: alpha(#31b6ff, 0.32); }
          .secureboot-checklist list {
            border: 1px solid alpha(#8fbeeb, 0.16);
            border-radius: 16px;
            background: alpha(#081424, 0.94);
          }
          .ergen-sidebar {
            padding: 34px;
            border-right: 1px solid alpha(#8fbeeb, 0.16);
            background: linear-gradient(155deg, alpha(#0a2744, 0.96), alpha(#07111f, 0.96));
          }
          .ergen-topnav {
            padding: 4px;
            border-radius: 13px;
            border: 1px solid alpha(#8fbeeb, 0.14);
            background: alpha(#081424, 0.82);
          }
          .ergen-topnav-button {
            min-width: 120px;
            min-height: 36px;
            border: 0;
            border-radius: 9px;
            background: transparent;
          }
          .ergen-topnav-button:checked {
            background: linear-gradient(135deg, alpha(#0aa7ff, 0.32), alpha(#0068ff, 0.38));
            color: white;
          }
          .ergen-eyebrow {
            color: #54c3ff;
            font-size: 10px;
            font-weight: 800;
            letter-spacing: 1.5px;
          }
          .ergen-muted, .dim-label { color: #9db0c8; }
          .ergen-actions button {
            min-height: 42px;
            border-radius: 12px;
            border: 1px solid alpha(#8fbeeb, 0.15);
            background: alpha(#0b2035, 0.78);
          }
          .ergen-actions button:hover {
            border-color: alpha(#31b6ff, 0.42);
            background: alpha(#0d2c49, 0.94);
          }
          .ergen-content { padding: 4px; }
          .ergen-status {
            margin: 8px;
            padding: 38px;
            border-radius: 22px;
            border: 1px solid alpha(#31b6ff, 0.24);
            background: linear-gradient(135deg, alpha(#0a2744, 0.82), alpha(#07111f, 0.88));
          }
          .ergen-status-title { color: #f4f9ff; font-size: 27px; font-weight: 800; }
          .boxed-list {
            border: 1px solid alpha(#8fbeeb, 0.16);
            border-radius: 16px;
            background: alpha(#081424, 0.92);
          }
          row { background: transparent; }
          button.suggested-action {
            background: linear-gradient(135deg, #0aa7ff, #0068ff);
            color: white;
            font-weight: 700;
            box-shadow: 0 8px 24px alpha(#0068ff, 0.24);
          }
          view, textview { background: alpha(#030a14, 0.82); color: #cfe2f5; }
        """)
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def set_operation_busy(self, busy: bool) -> None:
        self.operation_busy = busy
        self.repair_actions.set_sensitive(not busy)
        self.back_button.set_sensitive(not busy)
        self.secureboot_page.update_buttons()

    def _close_requested(self, _window) -> bool:
        if not self.operation_busy:
            return False
        dialog = Adw.AlertDialog(heading="Operation in progress", body="Wait for the current operation to finish before closing ErgenCTL.")
        dialog.add_response("ok", "OK")
        dialog.present(self)
        return True

    def _add_action(self, box: Gtk.Box, label: str, tooltip: str, command: str) -> None:
        button = Gtk.Button(label=label, tooltip_text=tooltip)
        button.add_css_class("ergen-action")
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

    def _confirm_bootloader_repair(self, _button: Gtk.Button) -> None:
        if not self.live_iso:
            return
        root = Adw.EntryRow(title="Mounted ErgenOS root")
        root.set_text("/mnt/ergenos")
        options = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        options.add_css_class("boxed-list")
        options.append(root)
        dialog = Adw.AlertDialog(
            heading="Recover the installed bootloader",
            body=(
                "Select the mounted ErgenOS root. ErgenCTL will back up /boot, rebuild GRUB and "
                "the snapshot menu, restore the Secure Boot entry and verify the result."
            ),
        )
        dialog.set_extra_child(options)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("plan", "Show plan")
        dialog.add_response("repair", "Repair bootloader")
        dialog.set_response_appearance("repair", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("plan")
        dialog.set_close_response("cancel")

        def respond(_dialog: Adw.AlertDialog, response: str) -> None:
            if response not in {"plan", "repair"}:
                return
            try:
                command = bootloader_recovery_command(root.get_text().strip(), execute=response == "repair")
            except ValueError as error:
                self._show_result("Bootloader recovery", str(error), False)
                return
            self._run(command)

        dialog.connect("response", respond)
        dialog.present(self)

    def _show_rollback_dialog(self, _button: Gtk.Button) -> None:
        if self.boot_mode == "snapshot" and self.selected_snapshot is not None:
            self._confirm_rollback(self.selected_snapshot)
            return
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

    def _show_logs_dialog(self, _button: Gtk.Button) -> None:
        boot = Adw.ComboRow(title="Boot", model=Gtk.StringList.new(["Current", "Previous"]))
        priority = Adw.ComboRow(title="Minimum priority", model=Gtk.StringList.new(["Errors", "Warnings"]))
        category = Adw.ComboRow(
            title="Category",
            model=Gtk.StringList.new(["All", "Resume", "Boot", "Audio", "Graphics"]),
        )
        options = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        options.add_css_class("boxed-list")
        options.append(boot)
        options.append(priority)
        options.append(category)
        dialog = Adw.AlertDialog(
            heading="Check boot logs",
            body="Choose which journal entries ErgenCTL should inspect.",
        )
        dialog.set_extra_child(options)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("check", "Check")
        dialog.set_response_appearance("check", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("check")
        dialog.set_close_response("cancel")

        def respond(_dialog: Adw.AlertDialog, response: str) -> None:
            if response != "check":
                return
            categories = ("all", "resume", "boot", "audio", "graphics")
            self._run(
                log_command(
                    previous=boot.get_selected() == 1,
                    priority="warning" if priority.get_selected() == 1 else "error",
                    category=categories[category.get_selected()],
                )
            )

        dialog.connect("response", respond)
        dialog.present(self)

    def _run(self, command: GuiCommand) -> None:
        if self.operation_busy:
            return
        self.set_operation_busy(True)
        installed = shutil.which("ergenctl")
        executable: str | tuple[str, str]
        if installed:
            executable = installed
        else:
            executable = (sys.executable, str(Path(__file__).resolve().with_name("ergenctl.py")))
        if command is COMMANDS["repair"]:
            self.repair_plan_ready = False
            self.repair_button.set_sensitive(False)
            self.repair_button.set_tooltip_text("Run Plan repairs again after this operation")
        if command.privileged and shutil.which("pkexec") is None:
            self._show_result(command.title, "pkexec is not installed", False)
            return

        self.workspace.set_visible_child_name("results")
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
        self.set_operation_busy(False)
        GLib.idle_add(self._scroll_results_to_top)
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
                self.repair_plan_ready = False
                self.repair_button.set_sensitive(False)
                self.repair_button.set_tooltip_text("Run Plan repairs before applying changes")
                snapshot_boot = any(check["id"] == "boot-mode" and check["summary"] == "snapshot" for check in checks)
                self.boot_mode = "snapshot" if snapshot_boot else "normal"
                self.rollback_button.set_sensitive(snapshot_boot and self.selected_snapshot is not None)
                self.rollback_button.set_tooltip_text("Run Check snapshots to select the current snapshot" if snapshot_boot else "Available after booting from a snapshot")
            self.raw_expander.set_expanded(False)
            return

        report = next((value for key, value in payload.items() if key.endswith("_report")), None)
        log_report = payload.get("boot_log")
        if command is not None and command.title == "Boot logs" and isinstance(log_report, dict):
            self._show_log_report(log_report)
            return
        if command is COMMANDS["snapshots"] and isinstance(report, dict):
            self._show_snapshots(report)
            return
        is_bootloader_recovery = command is not None and command.arguments[:2] == ("fix", "bootloader")
        if (
            command in (COMMANDS["repair-plan"], COMMANDS["repair"], COMMANDS["bootloader-repair"])
            or is_bootloader_recovery
        ) and isinstance(report, dict):
            self._show_repair_report(report, "--dry-run" in command.arguments)
            return
        if isinstance(report, dict):
            result = report.get("message") or ("Operation completed" if report.get("success", success) else "Operation failed")
            self.result_summary.set_label(prefix)
            self._append_result(title, str(result), "pass" if report.get("success", success) else "fail")
        else:
            self.result_summary.set_label(prefix)
            self._append_result(title, prefix, "pass" if success else "fail")
        self.raw_expander.set_expanded(not success)

    def _show_log_report(self, report: dict[str, object]) -> None:
        groups = report.get("groups")
        grouped = groups if isinstance(groups, list) else []
        boot = str(report.get("boot", "current"))
        priority = str(report.get("priority", "error"))
        category = str(report.get("category", "all"))
        if not grouped:
            self.result_summary.set_label(f"{boot.capitalize()} boot - {priority} - {category} - no entries")
            self._append_result("Boot journal", str(report.get("message") or "No errors found"), "pass")
            self.raw_expander.set_expanded(False)
            return
        row_status = "fail" if priority == "error" else "warning"
        visible_groups = [
            group
            for group in grouped
            if isinstance(group, dict)
            and not is_kernel_trace_fragment(str(group.get("source") or "journal"), str(group.get("message") or ""))
        ]
        hidden_count = len(grouped) - len(visible_groups)
        summary = f"{boot.capitalize()} boot - {priority} - {category} - {len(visible_groups)} message(s)"
        if hidden_count:
            summary += f" - {hidden_count} trace line(s) in Technical details"
        self.result_summary.set_label(summary)
        for group in visible_groups:
            source = str(group.get("source") or "journal")
            message = compact_log_message(str(group.get("message") or "No message"))
            count = group.get("count", 1)
            suffix = f" - repeated {count} times" if isinstance(count, int) and count > 1 else ""
            self._append_result(source, f"{message}{suffix}", row_status)
        self.raw_expander.set_expanded(False)

    def _show_repair_report(self, report: dict[str, object], is_plan: bool) -> None:
        success = bool(report.get("success"))
        steps = report.get("steps")
        step_names = [str(step) for step in steps] if isinstance(steps, list) else []
        system = "Base installation from snapshot" if report.get("recovery_mode") else "Current installation"
        self._append_result("Target system", system, "pass")

        if step_names:
            for number, step in enumerate(step_names, start=1):
                self._append_result(f"Step {number}", step, "pass" if success else "fail")
        else:
            message = str(report.get("message") or "No repairs are needed")
            self._append_result("Changes", message, "pass" if success else "fail")

        actions = report.get("executed_actions")
        if isinstance(actions, list):
            for action in actions:
                self._append_result("Built-in action", str(action), "pass" if success else "fail")

        commands = report.get("executed_commands")
        command_count = len(commands) if isinstance(commands, list) else 0
        if command_count:
            self._append_result("System commands", f"{command_count} command(s) listed in Technical details", "pass")

        if report.get("reboot_required"):
            self._append_result("Restart required", "Reboot after applying these changes", "warning")

        if is_plan:
            self.repair_plan_ready = repair_plan_can_execute(report)
            self.repair_button.set_sensitive(self.repair_plan_ready)
            self.repair_button.set_tooltip_text(
                "Apply the displayed repair plan" if self.repair_plan_ready else "No repairs are available to apply"
            )
            self.result_summary.set_label(
                f"{len(step_names)} repair step(s) ready" if self.repair_plan_ready else str(report.get("message") or "No repairs are needed")
            )
        else:
            self.repair_plan_ready = False
            self.repair_button.set_sensitive(False)
            self.repair_button.set_tooltip_text("Run Plan repairs before applying changes")
            self.result_summary.set_label("Repair completed" if success else "Repair failed")
        self.raw_expander.set_expanded(not success)

    def _show_snapshots(self, report: dict[str, object]) -> None:
        self.boot_mode = str(report.get("boot_mode", "normal"))
        current = report.get("current_snapshot")
        self.selected_snapshot = int(current) if isinstance(current, int) else None
        entries = report.get("entries", [])
        if isinstance(entries, list):
            for entry in reversed(entries):
                if not isinstance(entry, dict) or not isinstance(entry.get("number"), int):
                    continue
                number = int(entry["number"])
                description = str(entry.get("description") or "No description")
                date = str(entry.get("date") or "Current system")
                kind = str(entry.get("type") or "snapshot")
                self._append_snapshot(number, description, f"{date} - {kind}")

        if self.boot_mode == "snapshot" and self.selected_snapshot is not None:
            self._append_snapshot(self.selected_snapshot, "Currently booted snapshot", "Ready for rollback")
            self.rollback_button.set_sensitive(True)
            self.rollback_button.set_tooltip_text(f"Restore snapshot {self.selected_snapshot}")
            self.result_summary.set_label(f"Booted from snapshot {self.selected_snapshot}")
        elif entries:
            self.result_summary.set_label(f"{len(entries)} snapshot(s) found")
        else:
            self.result_summary.set_label(str(report.get("message") or "No snapshots found"))
        self.raw_expander.set_expanded(False)

    def _append_snapshot(self, number: int, title: str, subtitle: str) -> None:
        row = Adw.ActionRow(title=f"Snapshot {number} - {title}", subtitle=subtitle)
        badge = Gtk.Label(label=str(number), width_chars=3)
        badge.add_css_class("accent")
        row.add_prefix(badge)
        self.results.append(row)

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

    def _scroll_results_to_top(self) -> bool:
        adjustment = self.repair_scroll.get_vadjustment()
        adjustment.set_value(adjustment.get_lower())
        return GLib.SOURCE_REMOVE


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
