# Changelog

## 1.0.0 — 2026-11-03

First versioned release of xBloom Local, a self-hosted recipe library and NFC-card manager for xBloom Studio. The source is distributed under the [MIT license](LICENSE); the app and personal library run on your own computer or LAN.

### Included

- Responsive overview, bean library, recipe editor, card inventory, and settings for desktop and mobile browsers.
- Coffee facts, source links and field-level provenance, tasting notes, ratings, roast dates, and uploaded photos.
- Public-HTTPS product-page lookup and JSON profile import, with editable preview and explicit approval before saving. Missing facts remain unknown, and brewing suggestions stay separate from roaster facts.
- Recipe version history; 18 g starting defaults; grind/RPM and grinder on/off; dose, ratio, water, temperature, flow, pattern, pauses, and agitation settings.
- Saved Omni and Hario V60 01 Clear + Melodrip preferences. The V60 default enables automatic grinding for a suitable moving adapter; diffuser placement after the bloom remains manual. Existing recipes and explicit JSON profiles retain their settings.
- Windows Proxmark bridge with simulation, read-only hardware mode, card-specific backups, compatibility previews, separate approval for ordinary writes/restores, local typed confirmations, and full readback verification.
- Proxmark `15693 v4` and `15693 v5` JSON parsing, the documented historical v4 binary wrapper, protected-region checks, interrupted-job recovery, and prevention of automatic write replay.
- Separately enabled factory Gen2 initialization for a narrowly supported blank custom-card profile, with bound preview approval and explicit identity/header/hidden-setting disclosures.
- Password or trusted-LAN access, scoped bridge/API tokens, LAN/host/same-origin checks, and Debian LXC + Caddy HTTPS deployment documentation.
- Full application backup/export and validated restore into an empty directory, preserving private recovery material and cancelling stale jobs.
- A shared **1.0.0** app/bridge version, separate runtime and development dependency locks, and a [private vulnerability reporting policy](SECURITY.md).

### Verification and limits

The [verification record](docs/verification.md) separates synthetic tests and browser exercises from limited physical-card checks. A successful V60 moving-adapter brew has been **reported by the user**; it was not independently observed or measured, and it does not validate every recipe field, accessory, card, or firmware combination.

Physical v5 reads remain untested. Initialization changes two hidden chip settings that ordinary backups cannot capture or recover and ordinary readback cannot verify. There is no automatic initialization rollback. Ordinary recipe writes/restores continue to preserve identity and configuration outside the supported recipe region. Every physical operation still requires its own approval.

### Upgrading from an earlier checkout

1. Export a full application backup in **Settings** and keep it private. Also preserve the Windows bridge data, original card backups, and pre-write snapshots outside the source checkout.
2. Stop the bridge and application. Retain the previous source checkout, environment configuration, and dependency environment for recovery; keep your data directory in place.
3. Install the reviewed 1.0.0 source and its runtime dependencies from `requirements.txt`, using the hash-checking instructions in the [deployment guide](docs/deployment.md#updating). Use `requirements-dev.txt` in an isolated development/test environment when running the suite.
4. Restart the app, check its version, access mode, library/history, preferences, photos, and a known card backup, then reconnect the bridge. Start hardware access read-only before approving any new operation.

This release does not require deleting the library, resetting cards, initializing an existing card, or changing reader firmware. A source update and an application-data restore do not modify physical cards. If startup or validation fails, stop and use the preserved source/environment and [recovery procedure](docs/deployment.md#recovery); do not overwrite the only copy of your data.
