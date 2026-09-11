<p align="center">
  <img src="assets/ergenctl-logo.png" alt="ErgenCTL logo" width="420">
</p>

<h1 align="center">ErgenCTL</h1>

<p align="center">
  Diagnostics, repair and snapshot recovery for ErgenOS.
</p>

<p align="center">
  <a href="https://github.com/ErgenosSW/ErgenCTL/releases"><img src="https://img.shields.io/badge/release-v1.0.0-E36F3D" alt="Release v1.0.0"></a>
  <a href="https://github.com/ErgenosSW/ErgenCTL/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-GPL--3.0--or--later-1682B4" alt="License GPL-3.0-or-later"></a>
</p>

> [!CAUTION]
> Recovery operations modify Btrfs subvolumes and boot configuration. Keep a verified backup before using repair or rollback commands.

## Overview

ErgenCTL is a recovery utility built for ErgenOS. It provides both a command-line interface and a graphical interface for system checks, supported repairs and Btrfs snapshot recovery.

The recovery workflow is designed for a system that no longer starts normally:

1. Boot a working snapshot from the GRUB snapshot menu.
2. Run ErgenCTL from that snapshot.
3. Inspect the base installation with a dry run.
4. Repair selected components or restore a known-good snapshot.
5. Reboot into the normal ErgenOS entry.

When started from a snapshot, ErgenCTL mounts the real base installation and performs repairs there. It does not write changes into the temporary snapshot overlay.

## Current capabilities

- Compact system status and extended diagnostics
- Human-readable and JSON output
- Current and previous boot journal inspection
- Log filtering for resume, boot, audio and graphics problems
- Btrfs and Snapper snapshot inspection
- Detection of normal, live and snapshot boot modes
- Hibernation resume configuration checks
- Repair planning with `--dry-run`
- Configuration backups before repair
- Btrfs safety snapshots during recovery
- Repair of Pacman repositories and snapshot hooks
- Repair of snapshot-related systemd services
- Regeneration of GRUB snapshot configuration
- Rebuilding of resume configuration, initramfs and GRUB
- Restoration of the base `@` subvolume from a selected Snapper snapshot
- Preservation of the replaced root subvolume for manual recovery
- Guided Secure Boot setup, verification, maintenance and removal through
  `ergenos-secureboot`

## Requirements

ErgenCTL targets ErgenOS installations using:

- Btrfs with a dedicated root subvolume
- Snapper with the `root` configuration
- grub-btrfs
- GRUB
- systemd
- mkinitcpio
- arch-chroot
- Python 3.10 or newer

Diagnostic commands can run without root privileges where system permissions allow it. Repair and rollback operations require root privileges.

## Usage

ErgenCTL is installed in ErgenOS as the native `ergenctl` command:

```bash
ergenctl --help
```

The graphical interface can be opened from the application menu or with:

```bash
ergenctl-gui
```

Actions that modify the system request administrator authentication when needed. Read-only checks remain available without authentication where system permissions allow it.

### System status

```bash
ergenctl status
ergenctl status --json
```

### Extended diagnostics

```bash
sudo ergenctl doctor
```

### Snapshot information

```bash
sudo ergenctl snapshots
sudo ergenctl snapshots --json
```

### Boot journal

```bash
sudo ergenctl logs
sudo ergenctl logs --previous --priority warning
sudo ergenctl logs --category resume --priority warning
sudo ergenctl logs --category boot --raw
```

Available log categories:

- `all`
- `resume`
- `boot`
- `audio`
- `graphics`

### Resume diagnostics

```bash
sudo ergenctl resume
sudo ergenctl resume --json
```

## Repair

Supported repair targets:

- `repositories`
- `pacman-hooks`
- `services`
- `resume`
- `grub-snapshots`
- `all`

Always inspect a repair plan first:

```bash
sudo ergenctl fix all --dry-run
```

Apply the required repairs:

```bash
sudo ergenctl fix all --yes
```

Without `--yes`, ErgenCTL asks for confirmation. By default, it creates a safety snapshot before changing a recoverable Btrfs installation. The `--no-snapshot` option disables that protection and should be used only when another verified backup exists.

## Rollback from a snapshot

Rollback is available only when ErgenOS is running from a snapshot. It replaces the base root subvolume with a writable copy of the selected snapshot.

List snapshots and choose the required number:

```bash
sudo ergenctl snapshots
```

Inspect the rollback plan:

```bash
sudo ergenctl rollback 8 --dry-run
```

Apply the rollback:

```bash
sudo ergenctl rollback 8 --yes
```

During rollback, ErgenCTL:

1. Mounts the top level of the Btrfs filesystem.
2. Verifies the selected Snapper snapshot.
3. Creates a writable copy of that snapshot.
4. Preserves the damaged root as `@-broken-<timestamp>`.
5. Promotes the restored copy to the configured root subvolume.
6. Rebuilds initramfs and GRUB.
7. Regenerates the GRUB snapshot menu.
8. Unmounts the recovery environment.

Do not remove the preserved `@-broken-*` subvolume until the restored system has booted and passed verification.

## JSON output

Machine-readable output is available for diagnostics, logs, repairs and rollback operations:

```bash
ergenctl doctor --json
sudo ergenctl fix all --dry-run --json
sudo ergenctl rollback 8 --dry-run --json
```

The current JSON schema version is `1`.

## Tests

Run the test suite with:

```bash
python3 -m unittest discover -s tests -v
```

The recovery and rollback paths have also been tested in a QEMU/KVM ErgenOS installation. The integration test covered a non-booting base system, startup from a GRUB snapshot, restoration of the root subvolume and a successful normal boot after recovery.

## Secure Boot

The top bar has **Repair** (all existing recovery tools) and **Secure Boot**.
The Secure Boot page requires `ergenos-secureboot` with GUI protocol 1
(0.2.0.dev or later). If the backend is missing, ErgenCTL offers a shortcut to
install it through ErgenPac.

1. Open Secure Boot and select **Check readiness**.
2. Select **Set up Secure Boot** and enter a one-time MOK password twice.
   Use 8–16 ASCII letters or digits for firmware keyboard compatibility.
3. Authenticate in the system Polkit dialog, as in ErgenPac.
4. Reboot through **ErgenOS Secure Boot**, choose **Enroll MOK** in MokManager
   and enter the same one-time password. Enable Secure Boot in firmware using
   the standard keys, then boot the Secure Boot entry again.
5. Return to the page and select **Check status**. Nine separate checks show
   what is complete and whether Secure Boot is active.

**THE MOK PASSWORD IS USED ONLY ONCE** — the password authorizes this enrollment in
MokManager. It is not needed at subsequent boots or updates. Removing the MOK
uses a new one-time authorization. The administrator password is separate
and is handled only by the system authentication agent.

Configuration runs without a terminal. Secrets are passed through stdin;
they are never included in command arguments, logs or saved configuration.
The GUI preserves Repair results when switching tabs and prevents concurrent
operations. Configure only from the normal installed ErgenOS session.

Repairs that regenerate boot configuration refresh and check Secure Boot
signatures. Rollback rejects snapshots with missing or different MOK setup
before replacing the root. EFI and firmware state are not restored by a root
snapshot.

CLI access is also available: `sudo ergenctl secureboot status --json`,
`enable --dry-run`, `enable`, `finalize`, `refresh`, `remove-mok`, and `disable`.

The next-step card guides users through readiness, preparation, MokManager
enrollment and firmware activation. After restarting, use Check status again;
there is no separate Finalize setup button. The backend's finalize command
remains available in the CLI, but its bookkeeping flag is not required to
activate Secure Boot or to verify it in the GUI.

Maintenance and removal is collapsed by default. Rebuild signed boot files
explains that it rebuilds/signs GRUB, kernels and DKMS modules with the existing
key, and when to use it. Key removal and finishing disablement are shown only
when relevant to the detected state. Repair functions and the MOK password
transport are unchanged by this UI revision.

GUI smoke test in a graphical session: `python tests/gui_smoke.py`. It uses
a temporary window and fake password; it never configures Secure Boot.

## Project status

ErgenCTL 1.1 development packages include the guided Secure Boot integration.
Recovery targets remain limited to the tested ErgenOS Btrfs and Snapper layout;
other distributions and custom storage layouts are not supported.

## License

ErgenCTL is licensed under the GNU General Public License v3.0. See [LICENSE](LICENSE).
