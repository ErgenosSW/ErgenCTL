# Changelog

These entries describe ErgenOS package revisions of the 1.1.0.dev source snapshot.

## 1.1.0.dev-9

- Redesign the GUI as an ErgenOS control center with diagnostic cards, recovery
  actions, a separate report view and a consistent Secure Boot interface.
- Add dashboard return navigation and remove duplicated action buttons.
- 113 tests passed in the original development environment. Six repair tests
  later failed during the Arch-container ISO source build; this remains an open
  test-isolation/integration investigation, not a resolved issue.

## 1.1.0.dev-8

Add verify-update with JSON output: Snapper pre/post pairs, boot files, GRUB,
Secure Boot, kernel/NVIDIA modules, failed services and reboot guidance.
ErgenPac calls this backend after supported update operations.

## 1.1.0.dev-7

Expand hybrid GPU diagnostics to include active drivers, default GPU, offload
environment and PCI runtime power state; add Radeon 860M + RTX 5050 coverage.

## 1.1.0.dev-6

Detect laptop dual-GPU configurations and inspect switcheroo-control availability.

## Existing recovery integration

Live-media repair accepts an explicitly mounted installation through --root.
Snapshot recovery mounts the base installation rather than modifying the overlay.
Secure Boot recovery uses the installed backend and existing key material.
These are not automatic Live disk discovery or a new Gaming Check feature.

## Deferred

Gaming Check and the broader 1.3 recovery/power improvements are not implemented
in the 1.2 package. Existing snapshot/rollback and historical Secure Boot tests
must not be confused with a fresh complete 1.2 regression run.
