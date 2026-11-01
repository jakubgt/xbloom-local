# NFC support and evidence

This application targets **already initialized, working custom ISO 15693 cards**.
It changes coffee recipe bytes only. It does not create authentication headers,
change UIDs, initialize blank magic cards, lock memory, or control Bluetooth.

## Sources checked

- [XBRecipeWriterPlus Recipe.ts, commit 1a621df3](https://github.com/CrazyCoder/XBRecipeWriterPlus/blob/1a621df3da9146b43fd38278dd67049ec3d6cdd2/library/Recipe.ts): layout, parsing, dose/RPM encoding, and CRC calculation.
- [Pour.ts at the same commit](https://github.com/CrazyCoder/XBRecipeWriterPlus/blob/1a621df3da9146b43fd38278dd67049ec3d6cdd2/library/Pour.ts): pattern and agitation values.
- [NFC.ts at the same commit](https://github.com/CrazyCoder/XBRecipeWriterPlus/blob/1a621df3da9146b43fd38278dd67049ec3d6cdd2/library/NFC.ts): ISO 15693 single-block and multiple-block access; header preservation.
- [Editor validation at the same commit](https://github.com/CrazyCoder/XBRecipeWriterPlus/blob/1a621df3da9146b43fd38278dd67049ec3d6cdd2/app/editRecipe.tsx): supported UI ranges used conservatively here.
- [Proxmark iso15.h, commit 325376d7](https://github.com/RfidResearchGroup/proxmark3/blob/325376d7ee067f74a5f23566b4469493f870e5b8/include/iso15.h), [cmdhf15.c](https://github.com/RfidResearchGroup/proxmark3/blob/325376d7ee067f74a5f23566b4469493f870e5b8/client/src/cmdhf15.c), and [client command-line options](https://github.com/RfidResearchGroup/proxmark3/blob/325376d7ee067f74a5f23566b4469493f870e5b8/client/src/proxmark3.c): the user's historical client version, packed dump layout, dump progress, and addressed block writes.
- [denull0/xbloom-agent](https://github.com/denull0/xbloom-agent): reviewed as a recipe-management reference; its cloud/account workflow is not part of this application.
- [Janczykkkko/xbloom-ble recipe model](https://github.com/Janczykkkko/xbloom-ble/blob/main/xbloom_ble/recipe.py): reviewed as a separate BLE protocol reference. Its limits and packet representation are not assumed to be NFC limits. No BLE dependency is used.

These are community implementations, not an official xBloom NFC specification.
The supplied Gemini transcript is historical context; its claims are not protocol
authority. No upstream application source has been copied into this repository.

## Supplied dump validation

The user's two supplied files were inspected read-only. Neither was copied into
Git, a test fixture, or this documentation. The binary is a 2,235-byte Proxmark
`iso15_tag_t` structure, **not** a raw 2,235-byte NFC memory image. Its memory begins
at byte 175; the structure has a 2,048-byte data capacity plus metadata.

The JSON declares 40 blocks of four bytes. All 160 memory bytes match the
corresponding binary data. The current recipe has three pours, a 15 g dose,
120 RPM, grind 46, ratio 1:10, and 150 ml total water. The calculated CRC and the
stored CRC are both `D8`, and the recipe ends at byte 68. This verifies the
parser and checksum against the supplied reference, not a new physical write.

The original XBRecipeWriterPlus README still describes a fixed 15 g limitation.
The reviewed implementation instead stores a five-bit dose in the first pour.
This application follows that implementation and encodes 18 g directly. Whether
the user's installed Studio firmware interprets every supported field identically
must be established through a supervised offline brew acceptance test.

## Layout used by this codec

All offsets below are byte offsets in card memory, not the binary dump wrapper.
Blocks are four bytes and numbered from zero.

| Offset | Meaning |
| --- | --- |
| 0–31 (blocks 0–7) | Opaque card-specific authentication/header data. Preserved exactly. No claim of signature verification is made. |
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
never written. The first release does not change UID or authentication bytes.

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
4. The complete memory must still match the reviewed baseline. Locked target
   blocks, wrong UID, expired confirmation, malformed CRC, or any header/config
   change stop the job. Approval expires after 30 minutes.
5. Hardware writes require `--hardware --allow-writes` and the exact local
   phrase `WRITE <card label>` (or `RESTORE <card label>`). Only that labeled
   physical card should be on the antenna. The bridge rereads it after this
   confirmation. The software cannot distinguish two perfectly identical clones.
6. Addressed `hf 15 wrbl` operations write only changed recipe blocks, using an
   argument array with no shell. Responses must explicitly acknowledge success.
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

Simulation is the default. It uses conspicuously synthetic header bytes, never
calls Proxmark, and marks results as simulated with `hardware_verified=false`.
Simulation backups cannot establish a hardware baseline. A bridge OS lock avoids
two processes sharing the same data directory; run only one bridge for each
physical Proxmark. Heartbeats continue while waiting for local confirmation.

Every job has a durable local execution journal. An interrupted or failed write
is never automatically replayed. Pending results are retried as HTTP uploads
only; a restarted process reports interrupted jobs as unverified and requires a
new read and newly approved recovery job. Original backups remain available.
Hardware execution and the end-to-end offline brew test have not been performed
by development or automated tests.
