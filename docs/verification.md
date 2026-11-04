# Verification record

This record separates automated checks, browser exercises, and limited physical-card acceptance. Passing software tests or matching card bytes does not establish correct behavior on every reader, card, firmware, or brewer.

## Automated checks

| Date | Environment | Result |
| --- | --- | --- |
| November 3, 2026 | Windows, Python 3.12, final error-response hardening | **300 passed**, plus 3 subtests, in 28.88 seconds. One existing Starlette/httpx deprecation warning. |
| November 3, 2026 | Fresh hash-locked Windows environments, Python 3.12.10 and 3.13.3 | **289 passed** on each version (28.07 and 28.71 seconds). One existing Starlette/httpx deprecation warning per run. |
| November 3, 2026 | Windows, Python 3.12 | Full suite: **289 passed** in 30.30 seconds. One existing Starlette/httpx deprecation warning. |
| November 2, 2026 | Debian, Python 3.13.5 | Full suite: **289 passed**. |

The latest Windows run used a fresh temporary test directory. Tests use temporary application storage, synthetic card data, and mocked device calls; they do not open a reader or perform physical card operations.

Release dependency checks verified both runtime and development locks for Windows/Linux x64 with Python 3.11, 3.12, and 3.13: all 12 target wheel installations passed hash validation. Foreign-target wheel checks establish package availability and integrity, not execution on that operating system. A strict audit of all 55 unique packages across platform markers reported no known vulnerabilities at the time of the audit. Lock regeneration checks passed on Python 3.12 and 3.13. GitHub CI separately runs the full suite on each of the six supported OS/Python combinations.

Coverage includes authentication and trusted-LAN access, token scopes, public-HTTPS import restrictions and provenance, recipe history, saved brewing preferences, backup/restore, NFC encoding, v4/v5 dump parsing, locked and protected regions, approval expiry, mode separation, interrupted operations, recovery, and initialization contracts. Initialization checks include exact preview approval, native backup provenance, retention of originals, identity adoption only after full observable readback, and refusal to replay uncertain operations.

Recorded syntax checks also cover the Windows launcher scripts, application/import Python modules, and browser JavaScript.

Error-response regressions cover manual model validation without submitted-value representations, product-fetch transport errors without internal exception details, and both NFC preview endpoints. Unexpected codec failures retain no executable write payload; deliberate NFC validation keeps actionable guidance.

## Browser and simulation checks

Recorded desktop (**1440 px**) and mobile (**390 px**) browser exercises used disposable synthetic libraries. They covered navigation and access, profile review/import, recipe editing and version creation, compatibility previews, card registration, and simulated read/backup/write/readback. No browser page errors were reported during those exercises.

Product-link checks covered explicit fetching, editable facts, unknown fields, preserving an existing recipe and agitation settings, clearing unrelated facts when switching products, source notes for edits, escaped page text, preview without persistence, and an explicitly approved save. A public product page was also fetched in a read-only integration check; extraction support is not guaranteed for every roaster.

Initialization browser checks exercised the separate review, disabled approval on a read-only bridge, and an intercepted confirmation request. No physical job was submitted by those browser checks. A local HTTP simulation bridge separately completed the dashboard's read and write workflow without opening USB hardware or marking physical cards verified.

Saved equipment preferences were checked for persistence across restarts and application to new recipes and first product-link recommendations. Existing versions and explicit JSON recipes retain their settings. The V60/Melodrip default enables automatic grinding with a suitable moving adapter; diffuser placement after the direct bloom remains manual.

## Limited physical-card acceptance

Physical checks were performed with a **Proxmark5** and a client built from the factory-reported upstream base, **`6df95a2dc`**. The normal capability size/version checks passed for this combination. A mismatched newer client was refused. Factory firmware contains additional modifications, so these results establish the tested operations only, not an exact firmware match or general Proxmark5 qualification.

The recorded acceptance includes:

- **Read and backup:** complete native v4 reads matched UID, all 64 four-byte memory blocks, and lock status, with a valid recipe checksum. A dashboard/Windows-bridge read retained an original hardware backup.
- **Ordinary recipe writes:** explicitly approved writes completed through the normal backup, dashboard approval, and interactive bridge workflow. Full 256-byte readback matched the approved target. Independent checks confirmed the recipe/checksum and preserved UID, header, tail memory, locks, and readable metadata. A later recipe-only grinder update was also readback verified without running initialization commands.
- **Explicit factory Gen2 initialization:** one approved blank-card initialization completed with the separate identity/header and hidden-setting approvals. Intermediate identity readback and final full-memory readback matched the observable plan. Bytes 160–255, readable block locks, and IC/AFI/DSFID metadata remained unchanged; the original target backup was retained and the template card was not modified.
- **Backup integrity:** retained application exports passed manifest checksums and SQLite integrity validation.

These results apply only to the tested cards, client/firmware combination, and operations. Actual v5 physical reads remain untested; v5 format coverage is synthetic. Personal dumps and application backups are not included in the repository or test fixtures.

## Preservation and initialization limits

**Ordinary recipe writes and restores** preserve UID, the opaque authentication/header, and configuration outside the recipe region. Recipe restoration is limited to blocks 8–39; it is not a full-card restore.

**Separately enabled factory Gen2 initialization** is the explicit exception: it adopts a template UID/header and changes two hidden chip settings. Ordinary memory backups cannot capture or recover those settings, and ordinary readback cannot verify their final values. A successful initialization result establishes only the expected observable UID, memory, readable locks, and metadata. A failed, interrupted, or uncertain attempt requires manual identity reconciliation; there is no automatic retry or rollback, and a recipe restore cannot undo initialization.

## User-reported brew

On November 3, 2026, the user reported a successful brew with the V60 moving-adapter setup. This is a user-reported result for that setup, not an independently observed or instrumented brew test. The report does not establish that every recipe field, automatic-grinding step, diffuser placement, or firmware behavior was separately measured and verified.

## Remaining acceptance limits

Card readback and the reported successful brew do not establish behavior across other cards, readers, recipes, accessories, or firmware versions. Actual v5 physical reads remain untested. The hidden initialization settings remain unverifiable by ordinary readback. The software does not validate taste or automate manual accessory placement.

Follow the [physical-card acceptance checklist](deployment.md#first-physical-card-acceptance-checklist) for each new hardware setup and review the [initialization limits](deployment.md#separately-approved-blank-card-initialization) before enabling that separate workflow. Every future physical operation still requires its own current backup and explicit approval.
