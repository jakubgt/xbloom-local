# Verification record

Validated on November 1, 2026 using Python 3.12 on Windows.

- 45 automated tests pass for authentication, token scopes, profile review, version history, full backup/restore, NFC encoding, immutable headers, simulated and physical mode separation, job expiration, interrupted operations, corruption recovery, and bridge replay prevention.
- The supplied Proxmark JSON and packed binary dumps were inspected read-only. Their decoded memory bytes match and the stored recipe CRC verifies. Neither supplied dump is in this repository.
- Browser checks passed at 390 px mobile and 1440 px desktop widths: navigation, sign-in, example-profile review/import, recipe editing, version creation, compatibility preview, card registration, simulated read/backup, and simulated write/readback. Browser console reported no errors during these checks.
- A live local HTTP simulation bridge completed the dashboard's read and write workflows. It did not open a USB device. Simulation results did not mark physical cards verified.
- The Windows launcher scripts passed PowerShell parsing; the import helper and application modules passed Python syntax checks. JavaScript syntax checks passed.

## Remaining acceptance work

The Debian deployment configuration has not been run in the user's LXC; that container will be supplied later. The Proxmark adapter's command construction and validation are tested against synthetic outputs and inspected upstream source, but have not been exercised against the user's physical reader. No card was written and no offline brew was performed. Follow the physical acceptance checklist in [deployment.md](deployment.md#first-physical-card-acceptance-checklist) when the user is ready to explicitly approve a card write.

Restoration is scoped to the saved recipe region (blocks 8–39). UID, signature/header, chip configuration, and locked blocks are never rewritten by this application. Tea recipes and changes to card identity are outside this release.
