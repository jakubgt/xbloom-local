# Deployment and recovery

## Architecture

The Debian LXC runs FastAPI and SQLite. Caddy terminates LAN HTTPS and forwards to Uvicorn bound to `127.0.0.1:8000`. The Windows bridge makes authenticated outbound requests to the LXC; it does not open a Proxmark control port. Desktop and mobile browsers access the same dashboard. The Windows PC and connected Proxmark must be available to perform physical card operations.

The app does not fetch coffee URLs automatically. Codex reads the product page, proposes a structured profile, and imports it after review. It does not send recipes to the machine over Bluetooth. Brewing uses the written card with the xBloom offline.

## Prepare the LXC

Use a Debian container with Python 3.11 or newer, a reserved LAN address, and persistent storage. A practical initial allocation is 1–2 CPU cores, 1 GB RAM, and space for the database, photos, and retained backups. Storage needs grow with photos and snapshots.

The following commands are intended for the container administrator. No Proxmox login or container creation is performed by this repository.

```sh
apt-get update
apt-get install -y python3 python3-venv git caddy
useradd --system --home-dir /var/lib/xbloom-local --shell /usr/sbin/nologin xbloom
install -d -o xbloom -g xbloom -m 0700 /var/lib/xbloom-local
```

Clone the private repository to `/opt/xbloom-local` using your GitHub authentication, or copy the reviewed source checkout there. Keep `data/`, `.venv/`, credentials, and local card dumps out of that source transfer.

```sh
git clone https://github.com/jakubgt/xbloom-local.git /opt/xbloom-local
cd /opt/xbloom-local
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
install -m 0600 deploy/xbloom-local.env.example /etc/xbloom-local.env
```

Edit `/etc/xbloom-local.env`: set `XBLOOM_ALLOWED_HOSTS` to the LXC's actual reserved IP and any LAN hostname you will use. Keep `XBLOOM_SECURE_COOKIES=true` and `XBLOOM_LAN_ONLY=true`. Runtime configuration is read from process environment; the server does not automatically load a project `.env` file.

Initialize the user account before exposing the service:

```sh
cd /opt/xbloom-local
runuser -u xbloom -- env XBLOOM_DATA_DIR=/var/lib/xbloom-local /opt/xbloom-local/.venv/bin/python -m app.manage setup
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

Read-only physical access uses your existing Iceman client. The historical setup used COM6; check the current port in Device Manager if connection fails. Supply the actual executable, not a `.bat` launcher:

```powershell
.\scripts\start-bridge.ps1 -ServerUrl 'https://192.168.1.50' -Hardware -ProxmarkExecutable 'C:\proxmark3\client\proxmark3.exe' -Port COM6
```

Close any other Proxmark client using that serial port. Queue a read for the selected physical card. Match its physical label yourself because custom cards can share a UID.

When you are ready to approve individual physical writes, start a foreground session with `-Hardware -AllowWrites`. Each operation also needs dashboard approval and a local typed confirmation (`WRITE <card label>` or `RESTORE <card label>`). Keep one card on the reader. The bridge saves the current full-memory backup and obtains server acknowledgment before writing; it preserves the authentication/header and custom-card configuration regions, changes only the supported recipe region, then verifies by reading back. See [NFC details](nfc-format.md) for exact limits.

Bridge data is stored under `data/bridge` by default. Use `XBLOOM_BRIDGE_DATA_DIR` for an alternative private persistent directory. Keep the bridge foreground for interactive write confirmation. A background service would not supply that confirmation and should not be used to bypass it.

For a failed or interrupted write, treat the card as unverified. Do not automatically retry or brew with it based on a success status from an earlier job. Read it again, review the result, and explicitly approve a recipe restore if necessary.

If a job remains running after an interruption, stop the bridge process and wait for the dashboard to show it offline. Use **Resolve interruption** in Card activity, confirm the bridge is stopped, and enter the physical card label. This marks the job failed and requires a fresh read. Restart the bridge and use **Read & back up** before preparing another write. Resolving a job never repairs or retries a card write by itself.

## First physical-card acceptance checklist

This checklist is an acceptance test to perform later with your hardware. It is **not completed by installation, simulation, or automated tests**. Each actual write requires your explicit approval of that specific card and recipe.

1. **Label the card.** Register your already-working custom card in Cards with a unique physical label. Put only that labeled card on the Proxmark antenna. A shared UID is not enough to identify it.
2. **Save the baseline.** Start the bridge with `-Hardware` and no `-AllowWrites`. Use **Read & back up**, confirm the UID and full-memory backup are recorded as a hardware read, and download that original backup to separate storage. A community dump or simulated read does not satisfy this step.
3. **Review an 18 g recipe.** Save the intended dose, total water, grind/RPM, and pour schedule. Use **Prepare recipe** and inspect the NFC preview, including all rounding or encoding changes. Resolve compatibility errors before proceeding.
4. **Approve one physical write.** Restart the bridge with `-Hardware -AllowWrites`. In the dashboard, approve the reviewed recipe/version, accept any encoding differences, and type the exact card label. At the local bridge prompt, check the physical card again and type `WRITE <card label>`. Do not approve until you want that card modified.
5. **Verify the result.** Require a successful hardware job with matching readback. Confirm the card is marked **Read-back verified** and the expected recipe/version is assigned. Keep the original and pre-write backups. A simulation success or merely queued job is not a verified physical write.
6. **Test the offline machine.** With the xBloom disconnected from the internet and Bluetooth unused, tap the card, inspect the displayed settings, and run the supervised test brew. Record whether the machine accepted the card and followed the intended dose and pours. Readback proves stored bytes; this final test establishes actual machine behavior.

If any step fails, stop at that step. Preserve the job record and readback, perform a fresh read, and review a separately approved recipe restore when needed. The first release restores the recipe region while preserving identity/configuration; it does not rewrite a custom card's UID or authentication header.

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

The restore command validates archive paths and checksums and refuses a nonempty destination. The `xbloom` service user needs read access to the ZIP; keep the ZIP outside the directory being moved. Do not continue to `systemctl start` if restore reports an error. Verify login, bean/recipe history, photos, and a known card backup before restarting the bridge. Sessions and pending/running card jobs are cleared or cancelled during restoration so old approvals cannot trigger new work. Restoring application data does not modify any physical card. Physical recipe restoration is a separate approved and verified bridge job.

## Updating

Export a backup, stop the bridge, stop `xbloom`, update the reviewed source and dependencies, run tests, and restart the service. Keep the data directory persistent across upgrades. Do not run migrations or delete data simply because a checkout has changed. Review release notes and the backup restore procedure before an update.
