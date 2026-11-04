# NFC support and evidence

Ordinary writes and restores target **already initialized, working custom ISO
15693 cards** and change recipe bytes only. A separate, explicitly approved
initialization workflow supports one narrowly defined blank custom-card profile.
Initialization copies a saved template's UID and header and makes two disclosed
hidden configuration changes; it is never a fallback for a failed recipe write.
The application does not derive authentication headers, lock memory, or control
Bluetooth.

## Sources checked

- [XBRecipeWriterPlus Recipe.ts, commit 1a621df3](https://github.com/CrazyCoder/XBRecipeWriterPlus/blob/1a621df3da9146b43fd38278dd67049ec3d6cdd2/library/Recipe.ts): layout, parsing, dose/RPM encoding, and CRC calculation.
- [Pour.ts at the same commit](https://github.com/CrazyCoder/XBRecipeWriterPlus/blob/1a621df3da9146b43fd38278dd67049ec3d6cdd2/library/Pour.ts): pattern and agitation values.
- [NFC.ts at the same commit](https://github.com/CrazyCoder/XBRecipeWriterPlus/blob/1a621df3da9146b43fd38278dd67049ec3d6cdd2/library/NFC.ts): ISO 15693 single-block and multiple-block access; header preservation.
- [Factory-base Gen2 UID implementation, commit 6df95a2dc](https://github.com/RfidResearchGroup/proxmark3/blob/6df95a2dc9a8e529037ebe75256e8dee5555f60d/armsrc/iso15693.c) and [its client command](https://github.com/RfidResearchGroup/proxmark3/blob/6df95a2dc9a8e529037ebe75256e8dee5555f60d/client/src/cmdhf15.c): the full `csetuid --v2` sequence, including special selectors `0x47` and `0x52`. This source inspection does not establish physical acceptance of a particular blank card or xBloom firmware.
- [Editor validation at the same commit](https://github.com/CrazyCoder/XBRecipeWriterPlus/blob/1a621df3da9146b43fd38278dd67049ec3d6cdd2/app/editRecipe.tsx): supported UI ranges used conservatively here.
- [Proxmark iso15.h, commit 325376d7](https://github.com/RfidResearchGroup/proxmark3/blob/325376d7ee067f74a5f23566b4469493f870e5b8/include/iso15.h), [cmdhf15.c](https://github.com/RfidResearchGroup/proxmark3/blob/325376d7ee067f74a5f23566b4469493f870e5b8/client/src/cmdhf15.c), and [client command-line options](https://github.com/RfidResearchGroup/proxmark3/blob/325376d7ee067f74a5f23566b4469493f870e5b8/client/src/proxmark3.c): the historical client version, packed dump layout, dump progress, and addressed block writes.
- [Proxmark JSON serialization, commit 754bbae3](https://github.com/RfidResearchGroup/proxmark3/blob/754bbae3d946429a6354cd34a97a2dd29ce6f157/client/src/fileutils.c#L615) and [ISO 15693 dump command](https://github.com/RfidResearchGroup/proxmark3/blob/754bbae3d946429a6354cd34a97a2dd29ce6f157/client/src/cmdhf15.c#L2532): newer `15693 v5` JSON output, including its two-byte little-endian block count. This client generation supports Proxmark5; parser support does not establish physical-reader acceptance.
- [denull0/xbloom-agent](https://github.com/denull0/xbloom-agent): reviewed as a recipe-management reference; its cloud/account workflow is not part of this application.
- [Janczykkkko/xbloom-ble recipe model](https://github.com/Janczykkkko/xbloom-ble/blob/main/xbloom_ble/recipe.py): reviewed as a separate BLE protocol reference. Its limits and packet representation are not assumed to be NFC limits. No BLE dependency is used.

These are community implementations, not an official xBloom NFC specification.
Protocol claims are tied to the reviewed source and tests. No upstream
application source has been copied into this repository.

## Supported dump containers

The parser accepts Proxmark `15693 v4` and `15693 v5` JSON, plus the historical
2,235-byte v4 binary wrapper described below. Both JSON versions retain the same
conservative limit of 13–160 four-byte blocks. The v5 block count is exactly two
little-endian bytes (`4000` means 64 blocks); every declared block and its lock
status must be present. Extra v5 block keys, malformed metadata, incomplete
memory, and unknown binary formats are rejected. Newer clients may save only
JSON; the bridge retains that native file and still requires complete read
progress. Portable application backups continue using the existing v4 JSON
representation, without changing card memory or lock bytes.

## Format validation

The supported historical binary wrapper is a 2,235-byte Proxmark
`iso15_tag_t` structure, **not** a raw 2,235-byte NFC memory image. Its memory
begins at byte 175; the structure has a 2,048-byte data capacity plus metadata.
Only the declared supported blocks are interpreted as card memory. Unknown
binary layouts are rejected.

Synthetic fixtures check equivalent JSON/binary memory decoding, UID byte
order, checksum validation, and v4/v5 parsing. They also cover incomplete blocks,
invalid lock data, malformed metadata, unsupported formats, and corrupted
recipes. Personal card dumps are not included in the repository or fixtures.
See the [verification record](verification.md) for the separate, limited physical
read and write checks.

The reviewed recipe implementation stores dose in five bits of the first pour.
This codec encodes 18 g directly within its supported whole-gram range. How a
particular Studio firmware interprets the supported fields must still be
established through a supervised offline brew acceptance test.

## Layout used by this codec

All offsets below are byte offsets in card memory, not the binary dump wrapper.
Blocks are four bytes and numbered from zero.

| Offset | Meaning |
| --- | --- |
| 0–31 (blocks 0–7) | Opaque card-specific authentication/header data. Preserved exactly by ordinary writes/restores; copied only in separately approved initialization. No claim of signature verification is made. |
| 32–38 | Seven-byte ASCII XID, padded with zeroes. Recipe title is stored in the database, not this field. |
| 39 | Cup type: 0 xPod, 1 Other, 2 Omni. Tea is outside this release's scope. |
| 40 | Pour count shifted left by three bits. Unsupported low-bit flags are rejected. |
| 41 onward | Eight-byte entries, one per pour. |
| 41 + 8 × count | Grind setting minus 40; byte 41 disables the grinder. |
| 42 + 8 × count | Whole-number ratio. |
| 43 + 8 × count | CRC-8/MAXIM-DOW of every preceding byte, including the 32-byte header; reflected polynomial 0x8C, initial 0, xor-out 0. |

Each pour entry contains: volume ml, temperature byte, pattern (0 center / 1
circle / 2 spiral), agitation bits (before=1, after=2), negated pause seconds,
dose/minutes byte, RPM byte, and flow rate in tenths of ml/s. The first pour's
low five dose bits and RPM byte carry the recipe's dose/RPM; subsequent pours use
zero there. The upstream format has long-pause minute bits, but this coffee-only
release uses the upstream coffee editor's 0–59 second pause range. A pause is
after that pour; a bloom duration can be represented by the first pour's pause.

## Supported encoding and explicit changes

The conservative profile supports 1–31 whole grams, grind 40–80, grinder RPM
60–120 in steps of 10, 1–240 ml per pour, temperature bytes 39–99, flow 3.0–3.5
ml/s in steps of 0.1, pauses 0–59 seconds, and integer ratios 5–100. These are
the reference editor's accepted encoding values, not a claim that every firmware
or dripper should use the whole range. This release allows 1–14 pours so the
payload fits below byte 160 even on the 40-block reference layout.

The intended recipe must first be internally consistent: pours sum to total
water and dose × intended ratio. A fractional ratio is proposed as the nearest
whole ratio, with pours redistributed proportionally using largest remainders.
For example, 18 g at 1:16.5 (297 ml) proposes 1:17 (306 ml). The preview lists
each changed ratio, volume, temperature, grind, RPM, flow, or pause. It preserves
the intended values and requires review; no card write is implied by previewing.
Unsupported ranges and inconsistent totals are rejected instead of clamped.

A card-independent preview has no usable checksum or write payload. A real
payload is bound to the backed-up card's header and current complete memory hash.
The encoder preserves the header and every byte after the larger of the old and
new block-rounded recipe extents. It only clears obsolete bytes within that
extent. It never pads/zeros the entire tag, unlike the upstream writer's general
padding behavior. In particular, custom-chip configuration at blocks 56–63 is
never written by ordinary recipe operations. They never change UID or
authentication bytes.

## Separate blank-card initialization

The `iso15693_gen2_64_factory` profile requires exactly 64 four-byte blocks,
entirely zero ordinary memory, and native metadata `IC=8B`, `AFI=00`, `DSFID=00`.
The target is a separately labeled custom inventory card. Its original and
latest same-mode native JSON backups must agree in identity, memory, lock state,
and metadata. Imported references, partial reads, unsupported profiles, and cards
awaiting manual identity review are rejected.

The template must be a saved native backup from a different custom inventory
card in the same hardware/simulation mode, with the same supported profile, a
nonzero header, and a valid recipe checksum. The dashboard lists the newest
valid backup for each eligible source card. It never accepts an arbitrary UID
or uploaded template as an initialization target. The template card itself is
not written and should be kept off the reader.

Initialization uses the documented **full factory Gen2 `csetuid --v2` operation**,
not an invented UID-only command. Besides setting the UID, that sequence writes:

| Hidden chip-setting selector (hex) | Value written |
| --- | --- |
| `47` | `3F038B00` |
| `52` | `00000000` |

These selectors are not ordinary memory block numbers. **Their original values
are absent from the 64-block backup and cannot be verified or recovered from
that backup. Ordinary readback also cannot verify their final values.** This is
the narrow configuration-preservation exception disclosed for initialization;
no claim that all chip configuration remains unchanged is made.

The plan copies the template's 32-byte opaque header and the reviewed encoded
recipe region, writing only changed ordinary blocks below byte 160. The blank
target's visible bytes 160–255, all readable block locks, IC, AFI, DSFID and their
available lock metadata must remain unchanged. The saved original backup is
retained permanently. A recipe restore is not a full undo of initialization.

The preview binds the target label and original UID, source label and backup,
new UID, exact recipe version, ordinary block changes, hidden-setting writes,
and bridge mode into a preview hash. Queueing requires that exact hash, both
typed labels, explicit initialization approval, and a separate
`accept_special_configuration_changes` confirmation. Recipe encoding changes
require their own acceptance when present. A live bridge must advertise
`init_factory_gen2_v1`; physical execution additionally requires write and
initialization opt-ins plus the local interactive confirmation.

Before mutation, the bridge validates a fresh complete native backup against
the approved plan and obtains the server's backup acknowledgment. Afterward,
full readback must match the approved UID, memory, readable locks and metadata.
A timeout, interruption, or uncertain result requires **manual identity
reconciliation**. There is no automatic retry or rollback, and an ordinary new
read does not remove that restriction.

The upstream [README](https://github.com/CrazyCoder/XBRecipeWriterPlus/blob/1a621df3da9146b43fd38278dd67049ec3d6cdd2/README.md)
only hypothesizes that the header is UID-derived. Its [release notes](https://github.com/CrazyCoder/XBRecipeWriterPlus/releases/tag/2.1.0)
describe cloning the UID with an original card. Neither establishes that copying
a donor header while retaining a different blank UID works. This workflow does
not offer that unverified alternative. Even a successful initialization readback
does not establish xBloom acceptance: inspect the settings and perform a
supervised offline test, with Bluetooth unused, before treating it as a working
brew card.

## Bridge write and recovery guarantees

1. The server creates a job for a physical inventory ID and label. UID is a
   checked radio identifier, but is not unique inventory identity when clones
   share the same UID.
2. The bridge reads the complete card. Proxmark's dump progress must reach the
   declared block count; this matters because the historical client can save a
   zero-padded structure after an incomplete read.
3. The raw native dumps remain on Windows. The bridge durably saves a per-card,
   per-job backup and uploads the JSON backup. It refuses to write unless the
   server acknowledges the exact memory hash.
4. The complete memory must still match the reviewed baseline. For ordinary
   writes/restores, locked target blocks, wrong UID, expired confirmation,
   malformed CRC, or any header/config change stop the job. Initialization has
   the separately bound identity/configuration exception described above.
   Approval expires after 30 minutes.
5. Hardware writes require `--hardware --allow-writes` and the exact local
   phrase `WRITE <card label>` (or `RESTORE <card label>`). Only that labeled
   physical card should be on the antenna. The bridge rereads it after this
   confirmation. The software cannot distinguish two perfectly identical clones.
6. Ordinary writes use addressed `hf 15 wrbl` operations on changed recipe
   blocks only, using an argument array with no shell. Responses must explicitly
   acknowledge success. Initialization additionally uses its separately approved
   UID command and header-block writes.
7. A complete readback must equal the approved target memory, including all
   unchanged header/configuration bytes. Otherwise the card is unverified.

An explicitly approved restore may replace the entire known **recipe region,
blocks 8–39 (bytes 32–159)**, from a saved valid backup of the same inventory
card and bridge mode. It preserves the current 32-byte opaque header and every
byte at offset 160 or later, including custom-chip configuration. The restored
target must decode successfully and have a valid CRC. Unlike ordinary recipe
writes, restore does not require the current recipe to decode: an interrupted
write may have corrupted the pour count itself. A complete fresh read, matching
UID and memory hash, saved current backup, unlocked target blocks, explicit
dashboard approval, local `RESTORE <label>` approval, and full readback are still
required. This is not a UID reset, blank-card initializer, or unrestricted
full-memory restore. If a complete current read is unavailable or protected
identity/configuration bytes differ from the saved backup, automated restore is
refused.

Simulation is the default. It uses conspicuously synthetic card data, never
calls Proxmark, and marks results as simulated with `hardware_verified=false`.
Simulation backups cannot establish a hardware baseline. A bridge OS lock avoids
two processes sharing the same data directory; run only one bridge for each
physical Proxmark. Heartbeats continue while waiting for local confirmation.

Every job has a durable local execution journal. An interrupted or failed write
is never automatically replayed. Pending results are retried as HTTP uploads
only; a restarted process reports interrupted jobs as unverified and requires a
new read and newly approved recovery job for ordinary recipe operations.
Interrupted initialization instead retains the manual identity reconciliation
restriction. Original backups remain available.
Limited physical reads, ordinary recipe writes, and separately approved factory
initialization are recorded in [verification.md](verification.md), including the
unverifiable hidden-setting limits. The end-to-end offline brew test remains
outstanding; each future physical operation still requires new approval.
