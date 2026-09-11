"""Run in a graphical session; no actual Secure Boot operations are executed."""
import ctypes
import importlib.util
import os
import sys
from unittest.mock import patch
from pathlib import Path
project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project))
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gio, GLib, Gtk
import ergenctl_secureboot_gui as sb
spec = importlib.util.spec_from_file_location('gui', str(project / 'ergenctl-gui.py'))
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
app=Adw.Application(application_id='io.github.ergenossw.ErgenCTL.Test', flags=Gio.ApplicationFlags.NON_UNIQUE)
app.register(None)
if os.environ.get("ERGENCTL_SCREENSHOTS"):
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
with patch.object(sb.SecureBootPage, '_probe'), patch.object(sb, 'BACKEND', '/usr/bin/true'):
    window=module.ErgenCTLWindow(app)
assert window.pages.get_visible_child_name() == 'repair'
page=window.secureboot_page
page.ready=True
page.preflight_ready=True
page.report=dict(state='unconfigured', uefi=True, configured=False, removal_requested=False)
page.render_status()
page.update_buttons()
window.pages.set_visible_child_name('secureboot')
assert window.pages.get_page(page).get_title() == 'Secure Boot'
assert page.next_action == 'enable'
assert 'finalize' not in page.buttons
window.set_operation_busy(True)
assert not page.next_button.get_sensitive()
assert not window.repair_actions.get_sensitive()
window.set_operation_busy(False)
window.present()
page._password_dialog('enable')
while GLib.MainContext.default().pending():
    GLib.MainContext.default().iteration(False)
dialog = window.get_visible_dialog()
assert dialog is not None
assert 'THE MOK PASSWORD IS USED ONLY ONCE' in dialog.get_body()
assert not dialog.get_response_enabled('continue')
rows = dialog.get_extra_child().get_first_child().get_next_sibling()
password = rows.get_row_at_index(0)
confirm = rows.get_row_at_index(1)
password.set_text('Once1234')
confirm.set_text('Once1234')
assert dialog.get_response_enabled('continue')
with patch.object(page, 'run') as run:
    dialog.emit('response', 'cancel')
    run.assert_not_called()
assert password.get_text() == confirm.get_text() == ''
dialog.close()


def settle():
    loop=GLib.MainLoop()
    GLib.timeout_add(250, lambda: (loop.quit(), False)[1])
    loop.run()


def capture(name):
    directory=os.environ.get('ERGENCTL_SCREENSHOTS')
    if not directory:
        return
    settle()
    paintable=Gtk.WidgetPaintable.new(window)
    snapshot=Gtk.Snapshot.new()
    paintable.snapshot(snapshot, window.get_width(), window.get_height())
    # Native GSK conversion avoids PyGObject's unsupported concrete node types.
    lib=ctypes.CDLL('libgtk-4.so.1')
    capsule=ctypes.pythonapi.PyCapsule_GetPointer
    capsule.restype=ctypes.c_void_p
    capsule.argtypes=[ctypes.py_object,ctypes.c_char_p]
    pointer=lambda obj:capsule(obj.__gpointer__,None)
    lib.gtk_snapshot_to_node.argtypes=[ctypes.c_void_p]
    lib.gtk_snapshot_to_node.restype=ctypes.c_void_p
    lib.gsk_renderer_render_texture.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p]
    lib.gsk_renderer_render_texture.restype=ctypes.c_void_p
    lib.gdk_texture_save_to_png.argtypes=[ctypes.c_void_p,ctypes.c_char_p]
    lib.gdk_texture_save_to_png.restype=ctypes.c_int
    lib.gsk_render_node_unref.argtypes=[ctypes.c_void_p]
    lib.g_object_unref.argtypes=[ctypes.c_void_p]
    node=lib.gtk_snapshot_to_node(pointer(snapshot))
    assert node
    texture=lib.gsk_renderer_render_texture(pointer(window.get_renderer()),node,None)
    destination=Path(directory)/name
    destination.parent.mkdir(parents=True,exist_ok=True)
    assert lib.gdk_texture_save_to_png(texture,str(destination).encode())
    lib.g_object_unref(texture)
    lib.gsk_render_node_unref(node)


settle()
page.report=dict(state='active', uefi=True, configured=True, mok_created=True,
    shim_installed=True, grub_signed=True, unsigned_kernels=[], boot_entry=True,
    mok_enrolled=True, secure_boot='enabled', booted_through_shim=True, problems=[],
    removal_requested=False, enrollment_requested=False)
page.render_status()
page.update_buttons()
page.message.set_text('Status updated. Review the checklist below.')
assert len(page.check_rows) == 9
assert page.next_title.get_label() == 'Setup complete'
assert not page.next_button.get_visible()
assert not page.maintenance.get_expanded()
capture('secureboot-active.png')
page.get_vadjustment().set_value(page.get_vadjustment().get_upper())
capture('secureboot-checklist.png')
page.report.update(state='enrollment-pending', mok_enrolled=False, enrollment_requested=True,
                   secure_boot='disabled', booted_through_shim=False)
page.render_status()
page.notice_box.set_visible(True)
page.update_buttons()
page.get_vadjustment().set_value(0)
assert 'enroll' in page.next_title.get_label()
assert page.next_action is None
capture('secureboot-enrollment.png')
page.report.update(state='degraded', problems=['GRUB signature is missing'], grub_signed=False,
                   enrollment_requested=False)
page.render_status()
page.update_buttons()
assert page.next_action == 'refresh'
window.destroy()
print('GTK smoke passed: tabs, checklist, guided steps, maintenance, MOK warning, validation and cancellation.')
