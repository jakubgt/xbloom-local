<p align="center">
  <img src="assets/heimdall/xbloom-local.png" width="96" height="96" alt="xBloom Local coffee and NFC icon">
</p>

# xBloom Local

**Your coffees, recipes, and NFC cards. In one local workspace.**

A self-hosted recipe library and card manager for **xBloom Studio**, with a responsive dashboard, a FastAPI/SQLite backend, and a separate Windows Proxmark bridge. Keep coffee facts, brewing experiments, recipe history, and card backups together on your own computer or LAN.

The source is public; your running app and library stay local. Brewing uses custom NFC cards. The app does not control the brewer over Bluetooth or require an xBloom cloud account. An optional product-link lookup fetches public coffee pages when you request it.

![xBloom Local dashboard with synthetic coffee, recipe, and simulation-card examples](docs/images/dashboard.jpg)

*The screenshot uses a disposable example library. A fresh installation starts empty.*

## What you can do

| Area | Included features |
| --- | --- |
| **Overview** | Coffee, recipe, and card counts; recent card activity; bridge status; desktop and mobile layouts. |
| **Bean library** | Roaster and product links, origin and processing facts, tasting notes, ratings, roast dates, and uploaded coffee-bag photos. Unknown facts remain unknown. |
| **Profile import** | Fetch facts from a public HTTPS product page, or paste/upload a structured JSON profile. Review and edit before saving. Field-level source notes distinguish fetched facts from your edits. |
| **Recipes** | 18 g starting defaults; dose, ratio, total water, grind, RPM, grinder on/off, dripper, and ordered pours with temperature, flow, pattern, pauses, and agitation. Saved edits retain version history. |
| **Brewing preferences** | Save your usual Omni or Hario V60 01 Clear + Melodrip setup for new recipes and first product-link recommendations. Existing recipes and imported JSON remain yours to control. |
| **NFC cards** | Inventory by unique physical label, read and preserve original backups, preview encoding differences, approve writes of an exact recipe version, and verify the result by reading it back. |
| **Recovery** | Per-card recipe restoration, interrupted-job handling, retained write evidence, full application backup/export, and validated restore into an empty data directory. |
| **Local access** | Password mode or explicit trusted-LAN access, separate bridge/API tokens, and a documented Debian LXC + Caddy HTTPS deployment. |

### Import facts, then choose a recipe

Product lookup extracts structured metadata and clearly labeled coffee facts; it does not invoke an AI service. Its 18 g starting recipe is a rule-based suggestion, separate from the roaster's product information. Extraction varies by site, and pages that require sign-in or JavaScript rendering may need manual entry or a JSON profile instead.

Review the source notes and edit the result before saving. Fetching new facts preserves an existing recipe draft; changing products clears unrelated facts. A supplied JSON recipe is not silently replaced by your saved equipment preferences. Profiles prepared with an assistant can use the same [documented import format](docs/import-schema.md).

### Automatic grinding with V60 + Melodrip

The V60 preference starts at **18 g, 1:16, 288 ml, grind 56, and 80 RPM**, with the card's **grinder enabled** and **Other dripper** selected. It assumes a suitable moving adapter carries the V60 under the grinder and into the brewing position.

The bloom runs without the Melodrip; placement after the bloom is manual. Keep it clear of the moving adapter and spout. The card uses fixed pauses and cannot sense the water level. These are adjustable starting recommendations, not a validated Hario, Melodrip, or roaster recipe. Changing the preference affects future defaults, not saved recipe versions.

## Start on Windows

You need **Python 3.11 or newer**. Clone or download this repository, then double-click **Start xBloom.cmd**. It prepares a local Python environment, installs dependencies on first launch, starts the app, and opens the browser. Keep its window open; press **Ctrl+C** there to stop the server.

Or run from the project folder:

```powershell
.\scripts\start-local.ps1 -OpenBrowser
```

Open **http://127.0.0.1:8000** if the browser does not open automatically. This preview binds to loopback and is accessible only on the same computer. The launcher changes PowerShell execution policy only for its own process. Use `-PythonPath 'C:\path\to\python.exe'` if Python autodetection needs help, and `-Install` after dependency changes.

The default `XBLOOM_AUTH_MODE=password` asks you to create a local account. There is no supplied default password. Optional command-line account setup is also available:

```powershell
.\.venv\Scripts\python.exe -m app.manage setup
```

To open the dashboard directly without an account or sign-in screen:

```powershell
$env:XBLOOM_AUTH_MODE = 'trusted_lan'
.\scripts\start-local.ps1 -OpenBrowser
```

Restart an existing server after changing its access mode. In trusted-LAN mode, **anyone permitted to reach the app can edit the library, download backups, and create client tokens**. Keep the host, LAN, same-origin, firewall, and HTTPS restrictions described in the deployment guide. The bridge and import API still require their separate scoped tokens.

Configuration comes from process environment variables. Copying `.env.example` to `.env` alone does not load it.

## From coffee to card

1. **Add your coffee.** Enter it manually or use **Import profile** to fetch a product page or load a JSON profile. Review facts, source notes, and the separate recipe proposal before saving.
2. **Make the recipe yours.** Set the dose, grinder, water, and pours. Save tasting changes as new versions.
3. **Register the physical card.** Use a unique label you can identify on the actual card. Custom cards may share a UID, so a UID alone is not an inventory label.
4. **Read and back it up.** Connect a compatible Proxmark through the Windows bridge and retain the original full-memory backup for that exact card.
5. **Review the NFC preview.** Check the selected recipe version and every rounding or encoding difference, then explicitly approve that card's operation.
6. **Confirm locally and verify.** The foreground hardware bridge requires a typed card-label confirmation, saves a fresh pre-write backup, writes the supported recipe region, and reads it back.
7. **Check the brewer.** Tap the card on your offline xBloom and supervise the first brew. Matching stored bytes establishes readback verification; it does not establish the brewer's behavior or taste.

For a first run without a reader, issue a bridge token in **Settings** and start the bridge in simulation:

```powershell
.\scripts\start-bridge.ps1 -ServerUrl 'http://127.0.0.1:8000'
```

The launcher prompts for the token with hidden input. Simulation generates synthetic card data and never opens a Proxmark device. Simulated success is visibly separate from physical verification. Use separate simulation and physical inventories.

For hardware access, provide your actual reader-compatible Proxmark executable and current COM port. Start read-only first; ordinary physical writes additionally require `-Hardware -AllowWrites`, dashboard approval, and the local prompt. The bridge verifies HTTPS certificates for LAN connections. See the [Windows bridge setup](docs/deployment.md#windows-bridge) for complete commands and certificate configuration.

## NFC support and safeguards

The parser supports **Proxmark `15693 v4` and `15693 v5` JSON** plus the documented historical v4 binary wrapper. Dump-format support is separate from compatibility between a particular Proxmark client and reader firmware. Check that combination before using hardware; the project does not bundle Proxmark software or update firmware.

Ordinary recipe writes and restores are limited to the supported recipe region, **blocks 8–39**. They preserve the existing UID/header and custom-card configuration. The workflow checks card identity and memory, locks, recipe compatibility, backup acknowledgment, approvals, and readback. Imported or community dumps do not substitute for the selected physical card's original hardware backup. Expired approvals, mismatched bridge modes, and changed card contents block execution.

After an interrupted ordinary write, keep the evidence, resolve the interrupted job only after stopping the bridge, and perform a fresh read before reviewing another operation. Nothing automatically retries a write or restores a card.

### Blank-card initialization is a separate operation

An explicit factory Gen2 initialization workflow exists for a supported, entirely blank **64-block custom card**. It requires native reads of the target and a different working template card, a selected recipe version, a bridge advertising the initialization capability, separate command-line opt-ins, both card labels, and a local `INITIALIZE` confirmation.

**Initialization changes identity/header data and two hidden chip settings.** A normal full-memory backup cannot capture or recover those hidden settings, and ordinary readback cannot verify them. There is no automatic retry or rollback; an interrupted or uncertain attempt requires manual identity reconciliation. An ordinary recipe restore cannot undo initialization. Compatibility with the offline brewer still needs a supervised acceptance check.

Read the [initialization limits and procedure](docs/deployment.md#separately-approved-blank-card-initialization) before enabling it. This is not a general-purpose blank-tag writer. [NFC format and limits](docs/nfc-format.md) documents the supported layout, encoding rules, and evidence.

## Run it on your LAN

The [deployment guide](docs/deployment.md) covers a Debian LXC with **FastAPI + SQLite**, **systemd**, and **Caddy LAN HTTPS**. The Windows bridge polls the server through authenticated outbound requests; it does not expose a reader-control port. Desktop and mobile browsers use the same dashboard. The Windows PC and reader must be available for physical card operations.

Publishing this repository does not expose your app. Keep the deployed service restricted to your intended LAN, avoid router port forwarding, and use your own allowed hosts, subnet, and certificate trust settings. The brewer can remain offline. Only explicit product-page lookup needs outbound internet access during normal application use.

## Keep your library recoverable

Runtime storage defaults to `./data`; set `XBLOOM_DATA_DIR` for a persistent location. The Windows bridge uses its own private data directory, configurable with `XBLOOM_BRIDGE_DATA_DIR`. Databases, photos, credentials, personal dumps, and local recovery material are excluded from source control.

Download a full backup in **Settings** regularly and before upgrades. It includes a consistent database snapshot, uploaded photos, card backups, and authentication data, so store it privately. Preserve the Windows bridge's original and pre-write snapshots separately as well.

Restore with `python -m app.manage restore /path/to/backup.zip` only after stopping the app and bridge and preparing an empty destination. The restore validates archive paths and checksums, clears sessions, and cancels old pending/running jobs. Restoring application data does not modify physical cards. Follow the full [backup and recovery procedure](docs/deployment.md#backups), including keeping the current data as a separate recovery copy.

## Development and verification

The app uses Python, FastAPI, Pydantic, SQLite, and plain JavaScript/CSS. Install the dependencies from `requirements.txt`, then run the synthetic suite:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

On Linux:

```sh
.venv/bin/python -m pytest -q
```

Tests cover authentication, scoped tokens, import validation and provenance, recipe history, brewing preferences, backup/restore, NFC formats, protected regions, bridge operations, initialization guards, and interruption recovery. Test fixtures use temporary storage, synthetic cards, and mocked device calls. Keep personal dumps out of fixtures and commits.

See the [verification record](docs/verification.md) for recorded checks and their limits, and the [physical-card acceptance checklist](docs/deployment.md#first-physical-card-acceptance-checklist) for supervised hardware validation. Passing tests or a simulated write does not qualify every card, reader, firmware, or brewing setup.

## Documentation and references

- [Deployment, access modes, Windows bridge, backups, and recovery](docs/deployment.md)
- [Product-link and JSON profile import](docs/import-schema.md)
- [NFC format, supported operations, and initialization limits](docs/nfc-format.md)
- [Verification record](docs/verification.md)
- [Third-party references and notices](THIRD_PARTY_NOTICES.md)
- [Heimdall icon](assets/heimdall/xbloom-local.png) and [generation notes](assets/heimdall/prompt.txt)

The project references community work including XBRecipeWriterPlus, xbloom-agent, xbloom-ble, and Proxmark3. Attribution and the recorded license evidence are kept in the notices. No repository-wide software license is currently included; upstream license declarations do not define this project's license.
