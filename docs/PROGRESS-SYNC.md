# Reading-position sync in Passage

The Mac app includes a private KOSync-compatible service. KOReader on Kindle and CrossPoint use their existing **Progress sync** features to save their place to Passage. This service is optional; a single-reader highlight archive can finish setup without it. No public sync account, Docker installation, or hosting subscription is required.

## Connect once

1. In **Settings → Reading positions**, choose **Turn on position sync**. Reader Bridge starts the service and creates a dedicated local account.
2. In **Setup → Reading positions**, close KOReader, connect Kindle in USB drive mode, and choose **Connect Kindle**. The app preserves its old settings, installs the Reader Bridge progress patch, and copies existing server positions for EPUBs on that Kindle. A current KOReader version with user patches enabled is required. It checks the old server before changing the account. Internet access is needed for this one-time copy if the old server is online.
3. Connect your selected CrossPoint reader in **USB drive mode**, or insert its SD card into the Mac. Choose its mounted folder in Passage and select **Connect reader**. Current CrossPoint firmware protects progress credentials from Wi-Fi file transfers. No firmware change is needed for position sync if the reader already supports custom KOSync servers.
4. Eject Kindle and reopen KOReader. Restart Xteink so it loads the new settings. Keep the exact same EPUB bytes on both readers.

Setup records the selected readers' pairings. **Readers paired** means settings are saved; received timestamps show actual uploads. Verify a physical round trip before relying on a two-reader connection. You can choose **Finish without position sync** and return later.

Keep **Sync automatically** selected when connecting Kindle to sync on book open/close and sleep/wake. Passage also sets KOReader's **Action when Wi-Fi is off** to **Turn on**, which KOReader requires for automatic sync. Uncheck it for manual sync. Pairing backs up these settings and leaves Wi-Fi disconnect and sleep preferences unchanged. CrossPoint's Upload Local and Apply Remote actions remain manual. Position sync does not add a polling loop, keep Wi-Fi continuously on, or create Amazon Whispersync compatibility.

## Daily use

On Xteink, choose **More → Sync Progress → Upload Local** before continuing on Kindle, or **Apply Remote** to pick up the position saved by KOReader.

The service starts after Mac login and stays running when you close the app. The Mac must be awake and reachable from the readers; it cannot sync during shutdown, FileVault unlock, sleep, or when the readers are away from its network. Readers retain their local reading place while offline. KOReader versions that support an offline sync queue retain it. Xteink can upload its local place once the Mac is reachable.

Automatic KOReader uploads carry the revision of the last position the reader acknowledged. If Xteink has changed that position, an older queued Kindle upload is rejected; reconnecting cannot silently replace the Xteink position. KOReader then checks the remote position using your existing sync preferences. A failed manual upload loses its override permission before being queued for later.

Reading backwards is supported. After accepting the remote position you can read or navigate in either direction. **Push progress from this device now** explicitly chooses the Kindle's current location, even when another device has changed the saved place. Xteink's **Upload Local** is likewise an explicit choice. Reader Bridge does not force the furthest percentage.

For existing installations from 0.6.0 or 0.6.1, install the newer app, restart position sync in Settings, and **Reconnect Kindle** once with KOReader closed. Eject and reopen KOReader to load the patch. No Xteink firmware change is needed. The server requires protected uploads from the paired Kindle even if its patch is later disabled; restore the patch instead of bypassing that protection.

Conflicting queued positions are preserved on Kindle in `koreader/settings/readerbridge-progress-state.lua` for recovery; they are not blindly retried. The normal offline queue removes entries only after the server acknowledges them or the patch preserves a conflict. Merely fetching a position without accepting it does not authorize an overwrite. If you want to retain your offline Kindle location instead, deliberately push it from KOReader. The service stores the exact position string and percentage supplied by each reader; the readers remain responsible for mapping that position to their layout.

**Settings → Reading positions** shows saved-book counts and actual upload receipts. A pairing file alone does not prove a reader has synced. Technical addresses are under **Settings → Advanced**. If the Mac's address changes, update it there and reconnect both readers. A router DHCP reservation helps keep a stable address.

## Backups and recovery

Automatic iCloud/folder snapshots include reading positions along with highlights, dates and covers. A snapshot containing positions uses backup format version 2 and requires Reader Bridge 0.6.0 or later to restore. Version 1 highlight-only snapshots remain supported. Credentials are excluded from snapshots; reconnect readers after restoring onto another Mac. Restore adds missing positions and preserves current ones.

Pairing saves old device configuration files in the Mac's private Reader Bridge backups directory before replacing them. Original book files and their local reading positions are not edited. Existing queued KOReader positions are merged using their recorded queue times, then removed from the old queue only after being stored locally; the old queue is backed up. Re-pairing the same local account preserves pending queue entries. Readers configured with different old accounts must be reconciled before automatic migration.

**Use Wi-Fi with older firmware** is available for first-time setup on firmware that permits it. A protected-file response (HTTP 403), or an existing config needing an update, requires USB/SD provisioning instead. Turn off that option, choose the mounted reader folder, and reconnect. Passage retains the previous settings and pairing on these failures; do not delete `.crosspoint` or disable firmware protection. The JSON command `pair_progress_xteink` accepts `mount` for the USB/SD folder; `device_url` is the legacy Wi-Fi option.

Turning off position sync stops its background job and retains the position database and credentials. It does not silently switch the readers back to a public server. Turn it on again to resume with the same account. Keep the app at its installed location because the background jobs use its bundled interpreter.

## Implementation and boundaries

The service implements authenticated `GET /users/auth`, `GET /syncs/progress/:document`, and `PUT /syncs/progress`, following the [KOReader client API](https://github.com/koreader/koreader/blob/master/plugins/kosync.koplugin/api.json). Reader Bridge adds opaque `reader_bridge_revision` tokens and an optional `metadata.reader_bridge` update condition; conflicts return HTTP 409. Its KOReader user patch activates only for the paired Reader Bridge endpoint and account. Device requirements and revisions persist across service restarts. Existing records gain revisions without changing their position or recorded time; restores issue fresh revisions. Authentication uses the existing `x-auth-user` and `x-auth-key` headers. Account creation is local to the app; network registration is disabled. It is a single-account service, not a public multi-user KOSync hosting product.

Requests use HTTP on the trusted LAN, like Reader Bridge's highlight connection. Do not expose the port to the internet or use it on an untrusted network. The dedicated generated password is unrelated to Amazon, GitHub, iCloud or the prior sync account. CrossPoint imports the provisioned password and rewrites it in its normal obfuscated format after restarting. Account credentials and request contents are not written to service logs.

Positions are durable SQLite records under `~/Library/Application Support/Reader Bridge/progress_sync`. The service normally uses port 8085, selecting another available port before pairing if necessary. The separate background job preserves compatibility with older highlight collectors already running on port 8084; the app manages both services.
