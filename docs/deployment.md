# Deployment and recovery

## Architecture

The Debian LXC runs FastAPI and SQLite. Caddy terminates LAN HTTPS and forwards to Uvicorn bound to `127.0.0.1:8000`. The Windows bridge makes authenticated outbound requests to the LXC; it does not open a Proxmark control port. Desktop and mobile browsers access the same dashboard. The Windows PC and connected Proxmark must be available to perform physical card operations.

The app can fetch a public coffee product page when you choose Fetch bean details in Import profile. It extracts available source facts and presents them for review with a separate starting recipe. Codex can also prepare a tailored JSON profile. The server needs outbound HTTPS for this explicit lookup; the xBloom machine remains offline. Nothing is sent to the machine over Bluetooth, and fetching a page does not save a profile or authorize a card write.

## Prepare the LXC

Use a Debian container with Python 3.11 or newer, a reserved LAN address, and persistent storage. A practical initial allocation is 1–2 CPU cores, 1 GB RAM, and space for the database, photos, and retained backups. Storage needs grow with photos and snapshots.

The following commands are intended for the container administrator. No Proxmox login or container creation is performed by this repository.

```sh
apt-get update
apt-get install -y python3 python3-venv git caddy
useradd --system --home-dir /var/lib/xbloom-local --shell /usr/sbin/nologin xbloom
install -d -o xbloom -g xbloom -m 0700 /var/lib/xbloom-local
```

Use a reviewed release tag for deployment. The example below installs `v1.0.0` into `/opt/xbloom-local`; a reviewed source archive for that tag is an alternative. Keep `data/`, `.venv/`, credentials, and local card dumps out of the source transfer. Public source availability does not expose your running application; keep the deployment restricted to your LAN.

```sh
git clone --branch v1.0.0 --depth 1 https://github.com/jakubgt/xbloom-local.git /opt/xbloom-local
cd /opt/xbloom-local
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements.txt
install -m 0600 deploy/xbloom-local.env.example /etc/xbloom-local.env
```

`requirements.txt` is the runtime dependency lock. `requirements-dev.txt` adds the test and maintenance tools and is intended for a development/test environment. Installing dependencies requires access to their package source; ordinary local library use does not.

Edit `/etc/xbloom-local.env`: set `XBLOOM_ALLOWED_HOSTS` to the LXC's actual reserved IP and any LAN hostname you will use. Keep `XBLOOM_SECURE_COOKIES=true` and `XBLOOM_LAN_ONLY=true`. Runtime configuration is read from process environment; the server does not automatically load a project `.env` file.

Choose the dashboard access mode in that file:

- `XBLOOM_AUTH_MODE=trusted_lan` opens the dashboard directly, without a username, password, or sign-in screen. Skip account setup. Anyone permitted by your LAN firewall and Caddy rules can use the dashboard, edit the library, download backups, and create bridge/import tokens.
- `XBLOOM_AUTH_MODE=password` is the default. Initialize the local account before exposing the service, using the command below.

Both modes retain allowed-host, LAN-only, and same-origin checks. Trusted-LAN mode automatically establishes a strict browser session; it does not remove the scoped tokens required by the Windows bridge and import API. Physical card backup and approval requirements are unchanged. Keep HTTPS and restrict the permitted LAN devices/subnet through Caddy and the firewall in either mode.

For **password mode only**:

```sh
cd /opt/xbloom-local
runuser -u xbloom -- env XBLOOM_DATA_DIR=/var/lib/xbloom-local /opt/xbloom-local/.venv/bin/python -m app.manage setup
```

Then install and start the service in either mode:

```sh
cd /opt/xbloom-local
install -m 0644 deploy/xbloom.service /etc/systemd/system/xbloom.service
systemctl daemon-reload
systemctl enable --now xbloom
systemctl status xbloom
```

The service is restricted to writing `/var/lib/xbloom-local`. Some container configurations restrict systemd's filesystem namespacing; if startup fails, inspect `journalctl -u xbloom` and the specific failed sandbox directive before changing it. Do not solve a permissions error by running the application as root. The behavior of `ProtectSystem` and `ReadWritePaths` is documented in [systemd.exec](https://www.freedesktop.org/software/systemd/man/systemd.exec.html).

## LAN HTTPS

Edit a copy of `deploy/Caddyfile` with your LXC's IP and **your exact LAN subnet**, then install it as `/etc/caddy/Caddyfile`. The example address and subnet are samples, not detected values. Add local DNS if you prefer a hostname and update both the Caddy address and `XBLOOM_ALLOWED_HOSTS` accordingly.

```sh
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy
```

The supplied configuration uses Caddy's local certificate authority through [`tls internal`](https://caddyserver.com/docs/caddyfile/directives/tls), so certificate issuance does not require public DNS or internet exposure. Trust the local CA on each desktop/mobile device. On the packaged Linux service its public root certificate is normally at `/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt`; confirm the actual Caddy data directory. Transfer only `root.crt`, never a CA private key. Install it into the device's trusted root store using the device's certificate settings.

Use the LXC/Proxmox firewall to permit HTTPS only from the intended LAN subnet, including equivalent IPv6 rules if IPv6 is enabled. Restrict SSH to your administration devices. Do not forward router ports, publish the application, or expose Uvicorn port 8000. The sample Caddy config additionally checks direct client IP addresses; [Caddy request matchers](https://caddyserver.com/docs/caddyfile/matchers) document this behavior.

Uvicorn trusts forwarded headers only from its local proxy, matching FastAPI's [HTTPS proxy deployment guidance](https://fastapi.tiangolo.com/deployment/https/). Open the dashboard using the HTTPS address configured in Caddy.

## Windows bridge

Settings stores a usual brewing setup for future recipes: Omni, or Hario V60 01
Clear with Melodrip. The selected defaults are used for new recipes and the
starting recommendation on a first product-link import. They do not rewrite
saved recipe versions, an existing import draft, or a supplied JSON recipe.
The V60 setup assumes the user's moving adapter carries the dripper beneath the
grinder and water outlet. The recipe card uses Other dripper with automatic
grinding enabled at the recipe's grind setting and RPM. Placing the
Melodrip after the bloom is a manual step recorded in recipe notes, not an NFC
instruction or an automatically controlled accessory.

Use the same checkout and Python environment on Windows. In Settings, issue a **bridge token**, save it in a password manager, and provide it only to the bridge. The dashboard shows it once; issuing a replacement revokes the previous bridge token. An API token is separate and is intended for approved profile imports.

Start with simulation:

```powershell
.\scripts\start-bridge.ps1 -ServerUrl 'https://192.168.1.50'
```

The script prompts for the token with hidden input. Simulation generates synthetic card data and never invokes the Proxmark. Its results are labeled as simulated and are not proof of a physical card write.

The bridge always verifies HTTPS certificates. If the Windows Python runtime does not use the root certificate you installed, point it explicitly to the public CA certificate before starting:

```powershell
$env:XBLOOM_CA_FILE = 'C:\certificates\xbloom-root.crt'
```

Use HTTPS for LAN access. Plain HTTP is suitable only for a loopback preview on the same computer.

Read-only physical access uses an Iceman client compatible with the connected
reader's firmware. Detect the current COM port before starting; historical ports
and client versions are not defaults for a new reader. Supply the actual
executable, not a `.bat` launcher. Replace the example path and COM port below:

```powershell
.\scripts\start-bridge.ps1 -ServerUrl 'https://192.168.1.50' -Hardware -ProxmarkExecutable 'C:\proxmark3\client\proxmark3.exe' -Port COM6
```

Close any other Proxmark client using that serial port. Queue a read for the selected physical card. Match its physical label yourself because custom cards can share a UID.

For a Proxmark5, use the computer USB-C port beside the orange/yellow button,
connecting normally without holding the button. The other USB-C port is for
expansion. See the [upstream Proxmark5 guide](https://github.com/RfidResearchGroup/proxmark3/blob/754bbae3d946429a6354cd34a97a2dd29ce6f157/doc/md/PM5_Start_Here/Getting_Started_With_PM5.md).
Check the client with `--version` and the connected reader with `hw version`
before attempting a card read. The historical Proxmark3 client is not assumed
compatible with a new Proxmark5. A version mismatch calls for investigation;
setup does not authorize a firmware update. Newer v5 JSON dumps are supported,
but hardware acceptance remains a separate read-only test.

When you are ready to approve individual ordinary recipe writes, start a foreground session with `-Hardware -AllowWrites`. Each operation also needs dashboard approval and a local typed confirmation (`WRITE <card label>` or `RESTORE <card label>`). Keep one card on the reader. The bridge saves the current full-memory backup and obtains server acknowledgment before writing; ordinary writes/restores preserve the authentication/header and custom-card configuration regions, change only the supported recipe region, then verify by reading back. Blank-card initialization is a separate opt-in described below. See [NFC details](nfc-format.md) for exact limits.

Bridge data is stored under `data/bridge` by default. Use `XBLOOM_BRIDGE_DATA_DIR` for an alternative private persistent directory. Keep the bridge foreground for interactive write confirmation. A background service would not supply that confirmation and should not be used to bypass it.

For a failed or interrupted write, treat the card as unverified. Do not automatically retry or brew with it based on a success status from an earlier job. Read it again, review the result, and explicitly approve a recipe restore if necessary.

If a job remains running after an interruption, stop the bridge process and wait for the dashboard to show it offline. Use **Resolve interruption** in Card activity, confirm the bridge is stopped, and enter the physical card label. This marks the job failed and requires a fresh read. Restart the bridge and use **Read & back up** before preparing another write. Resolving a job never repairs or retries a card write by itself.

### Separately approved blank-card initialization

Use **Review blank-card initialization** only for a saved native read of a
supported, entirely blank 64-block custom card. It appears separately from
**Prepare recipe**. Keep the card's original backup and physical label; select a
valid same-mode backup from a different working custom card as the identity
template, then select the exact saved recipe version. Imported dumps are not
initialization templates.

The review explicitly shows the target label and original UID, template label
and new UID, header/recipe block changes, and two hidden chip-setting changes:
selector `47=3F038B00` and selector `52=00000000`. The original 64-block memory
backup **does not include those hidden settings and cannot recover them**.
Ordinary readback cannot verify them. Visible memory at bytes 160–255 and
readable lock/AFI/DSFID/IC metadata must remain unchanged. This exception is
limited to the reviewed factory Gen2 initialization; ordinary writes and
restores still preserve all protected data.

Preparation does not authorize execution. A physical initialization additionally
needs an initialization-enabled foreground bridge, both typed card labels in
the dashboard, a separate acceptance of the hidden-setting changes, and the
local phrase `INITIALIZE <card label>`. Only the target blank card belongs on
the reader; keep the template card off it. The dashboard cannot queue this job
unless the connected bridge advertises `init_factory_gen2_v1`.

The Python bridge exposes `--allow-initialization` separately from
`--allow-writes`. After configuring the existing `XBLOOM_SERVER_URL`,
`XBLOOM_BRIDGE_TOKEN`, `XBLOOM_CA_FILE`, `PM3_EXECUTABLE`, `PM3_PORT`, and private
bridge-data directory for the current desktop, run from the project directory
in an interactive PowerShell window:

```powershell
.\.venv\Scripts\python.exe -m bridge.main --hardware --allow-writes --allow-initialization --once --wait-for-job 120
```

Do not assume a previous computer's port, client, or environment. The regular
PowerShell launcher does not supply the initialization flag. The Python opt-ins
do not bypass per-operation dashboard approval or the local prompt.

A failed, timed-out, or interrupted initialization requires **manual identity
reconciliation**. Stop the bridge and retain all evidence. A fresh read does
not clear this restriction, and **Resolve interruption** does not authorize a
retry or restore. There is no automatic rollback; a recipe restore cannot undo
the UID/header/hidden-setting changes. A successful full readback establishes
the expected readable data only. Check the displayed settings and perform the
supervised offline xBloom acceptance test before relying on the new card;
Bluetooth remains unused.

## First physical-card acceptance checklist

Use this checklist for your own hardware. It is **not completed by installation, simulation, or automated tests**. A successful V60 moving-adapter brew has been [reported by one user](verification.md#user-reported-brew); it was not independently observed or measured and does not qualify another setup. Each actual write requires your explicit approval of that specific card and recipe.

1. **Label the card.** Register your already-working custom card in Cards with a unique physical label. Put only that labeled card on the Proxmark antenna. A shared UID is not enough to identify it.
2. **Save the baseline.** Start the bridge with `-Hardware` and no `-AllowWrites`. Use **Read & back up**, confirm the UID and full-memory backup are recorded as a hardware read, and download that original backup to separate storage. A community dump or simulated read does not satisfy this step.
3. **Review an 18 g recipe.** Save the intended dose, total water, grind/RPM, and pour schedule. Use **Prepare recipe** and inspect the NFC preview, including all rounding or encoding changes. Resolve compatibility errors before proceeding.
4. **Approve one physical write.** Restart the bridge with `-Hardware -AllowWrites`. In the dashboard, approve the reviewed recipe/version, accept any encoding differences, and type the exact card label. At the local bridge prompt, check the physical card again and type `WRITE <card label>`. Do not approve until you want that card modified.
5. **Verify the result.** Require a successful hardware job with matching readback. Confirm the card is marked **Read-back verified** and the expected recipe/version is assigned. Keep the original and pre-write backups. A simulation success or merely queued job is not a verified physical write.
6. **Test the offline machine.** With the xBloom disconnected from the internet and Bluetooth unused, tap the card, inspect the displayed settings, and run the supervised test brew. Record whether the machine accepted the card and followed the intended dose and pours. Readback proves stored bytes; this final test establishes actual machine behavior.

If any ordinary recipe-write step fails, stop at that step. Preserve the job record and readback, perform a fresh read, and review a separately approved recipe restore when needed. Recipe restoration preserves identity/configuration and cannot undo initialization. A failed initialization follows the manual identity reconciliation procedure above.

## Backups

Download a full application backup from Settings regularly and before an upgrade. The ZIP includes an SQLite-consistent database snapshot, photos, original card backups, and authentication data. Store it privately and maintain a copy outside the LXC. It contains sensitive account/token hashes and card data even though plaintext tokens are not stored by the server.

Also preserve the Windows bridge data directory, including local pre-write snapshots. The community reference dump does not replace these per-card backups. Never delete or overwrite the first original backup simply to save space.

The live database may use SQLite journal/WAL files. Do not copy only the main database file while the app is running. Use the dashboard export or stop the service before copying the entire data directory. The live export uses SQLite's [backup API](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup).

## Recovery

Stop the bridge and app before restoring. Keep the current data directory as a separately named recovery copy, then restore into a fresh directory. On the LXC:

```sh
systemctl stop xbloom
recovery_stamp=$(date -u +%Y%m%dT%H%M%SZ)
mv /var/lib/xbloom-local "/var/lib/xbloom-local-before-$recovery_stamp"
install -d -o xbloom -g xbloom -m 0700 /var/lib/xbloom-local
cd /opt/xbloom-local
runuser -u xbloom -- env XBLOOM_DATA_DIR=/var/lib/xbloom-local /opt/xbloom-local/.venv/bin/python -m app.manage restore /path/to/backup.zip
runuser -u xbloom -- env XBLOOM_DATA_DIR=/var/lib/xbloom-local /opt/xbloom-local/.venv/bin/python -m app.manage check
systemctl start xbloom
```

The restore command validates archive paths and checksums and refuses a nonempty destination. The `xbloom` service user needs read access to the ZIP; keep the ZIP outside the directory being moved. Do not continue to `systemctl start` if restore reports an error. Verify dashboard access in your configured mode, bean/recipe history, photos, and a known card backup before restarting the bridge. Sessions and pending/running card jobs are cleared or cancelled during restoration so old approvals cannot trigger new work. Restoring application data does not modify any physical card. Physical recipe restoration is a separate approved and verified bridge job.

## Updating

Read the [release notes](../CHANGELOG.md) and export a full application backup before upgrading. Store it privately outside the source checkout and retain the bridge's original backups and pre-write snapshots. A release archive contains source and documentation, not your library or recovery data.

1. Stop the bridge and application. Keep the existing source, virtual environment, and configuration as a separately named recovery copy; preserve the persistent data directory.
2. Install the reviewed release source, using the same version for the app and Windows bridge. Install runtime dependencies into a fresh virtual environment with `python -m pip install --require-hashes --only-binary=:all: -r requirements.txt`.
3. In a separate test environment, install `requirements-dev.txt` with the same pip options and run `python -m pytest -q`. Do not point a test server or test environment at your live data directory.
4. Restart the application and check its reported version, configured access mode, bean/recipe history, preferences, photos, and a known card backup. Reconnect the Windows bridge and begin hardware access read-only.

Version 1.0.0 does not require a library reset, card reinitialization, or reader firmware update. Do not delete data or change physical cards merely because a checkout has changed. If validation fails, stop and return to the preserved source/environment, following the [recovery procedure](#recovery) if data restoration is necessary. Keep the failed/current data separately until recovery has been verified.

Report suspected security issues through the [private reporting channel](../SECURITY.md#report-a-vulnerability-privately), without attaching real credentials or card dumps to public issues.
