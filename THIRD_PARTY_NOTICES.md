# Third-party references and notices

Reviewed on 2026-11-01. This project is intended for a private GitHub repository and personal LAN deployment. No upstream application source files, artwork, personal card dumps, or Proxmark binaries are vendored in this checkout. The NFC codec and bridge were implemented using protocol information in the references below. This notice records provenance; it does not grant rights to upstream material or replace its terms.

## CrazyCoder / XBRecipeWriterPlus

- Repository: [CrazyCoder/XBRecipeWriterPlus](https://github.com/CrazyCoder/XBRecipeWriterPlus).
- Reviewed commit: [`1a621df3da9146b43fd38278dd67049ec3d6cdd2`](https://github.com/CrazyCoder/XBRecipeWriterPlus/tree/1a621df3da9146b43fd38278dd67049ec3d6cdd2).
- Reference use: recipe layout, per-pour fields, CRC, NFC memory access, header preservation, and editor validation ranges. Exact source paths are listed in [NFC support and evidence](docs/nfc-format.md).
- License evidence: no file named LICENSE, COPYING, or NOTICE was found in the complete recursive tree at that commit. The reviewed [README](https://github.com/CrazyCoder/XBRecipeWriterPlus/blob/1a621df3da9146b43fd38278dd67049ec3d6cdd2/README.md) and [package.json](https://github.com/CrazyCoder/XBRecipeWriterPlus/blob/1a621df3da9146b43fd38278dd67049ec3d6cdd2/package.json) do not state a project license. Its package metadata marks the package private. License permission is therefore not established by the inspected files; public repository visibility is not treated as a license grant.
- Attribution: CrazyCoder and the XBRecipeWriter contributors. The upstream README links its data-format illustration to terminaldisclaimer/XBRecipeWriter; that illustration is not included here.

## denull0 / xbloom-agent

- Repository: [denull0/xbloom-agent](https://github.com/denull0/xbloom-agent).
- License review commit: [`79e55c78bdb0448b695bcf62566866f0ffc4d604`](https://github.com/denull0/xbloom-agent/tree/79e55c78bdb0448b695bcf62566866f0ffc4d604).
- Reference use: recipe-management workflow and separation of coffee information from recipe operations. Its cloud integration and account-authentication implementation are not used by this app.
- License evidence: the [README License section](https://github.com/denull0/xbloom-agent/blob/79e55c78bdb0448b695bcf62566866f0ffc4d604/README.md#license) declares **MIT**. No standalone LICENSE, COPYING, or NOTICE file was found in the complete recursive tree at that commit. The declaration is recorded as found; this notice does not invent a missing copyright notice or license text.
- Attribution: denull0 and the xbloom-agent contributors.

## Janczykkkko / xbloom-ble

- Repository: [Janczykkkko/xbloom-ble](https://github.com/Janczykkkko/xbloom-ble).
- License review commit: [`cfa29daa771fdd147de78c1844d2251ca81ea583`](https://github.com/Janczykkkko/xbloom-ble/tree/cfa29daa771fdd147de78c1844d2251ca81ea583).
- Reference use: a separate recipe-model/protocol comparison. BLE is not a dependency or a brewing transport in this application.
- License: **MIT**, as supplied in the pinned [LICENSE](https://github.com/Janczykkkko/xbloom-ble/blob/cfa29daa771fdd147de78c1844d2251ca81ea583/LICENSE).
- Copyright notice: Copyright (c) 2026 Janczykkkko.

## RfidResearchGroup / proxmark3

- Repository: [RfidResearchGroup/proxmark3](https://github.com/RfidResearchGroup/proxmark3).
- Reviewed historical client commit: [`325376d7ee067f74a5f23566b4469493f870e5b8`](https://github.com/RfidResearchGroup/proxmark3/tree/325376d7ee067f74a5f23566b4469493f870e5b8).
- Reference use: Proxmark dump structure, read-completion output, and command-line options. The bridge invokes the user's separately installed Proxmark executable; it does not include or compile that software.
- License evidence: the repository includes [GNU GPL version 3 text](https://github.com/RfidResearchGroup/proxmark3/blob/325376d7ee067f74a5f23566b4469493f870e5b8/LICENSE.txt). The reviewed [iso15.h header](https://github.com/RfidResearchGroup/proxmark3/blob/325376d7ee067f74a5f23566b4469493f870e5b8/include/iso15.h) specifies GPL version 3 or any later version and attributes the Proxmark3 contributors.

## Scope of this record

Python runtime dependencies are installed separately and retain their own licenses and package metadata. This reference inventory is not an exhaustive dependency-license audit. Keep the provenance links with the source, and review the upstream terms and unresolved license evidence before publishing, redistributing, or incorporating upstream code or assets. Private hosting does not change upstream license terms.
