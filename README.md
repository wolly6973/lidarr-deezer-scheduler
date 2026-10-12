# lidarr-deezer-scheduler

A small Docker service that slowly fills gaps in a large Lidarr library from Deezer (through the Lidarr Deemix plugin) while keeping automated activity on your Deezer account low and irregular.

It never talks to Deezer directly. It only drives Lidarr's API: it picks missing albums, makes Deezer the only active indexer for a short search, then puts everything back. It is **missing-only** (it does not schedule upgrades) and does not replace Lidarr's normal RSS and new-release workflow.

> **Account risk.** Deezer limits and bans accounts used with download tools. This scheduler is built to keep activity modest and varied, but nothing here removes that risk.

## How it works

Each cycle ("chunk") does the following:

1. **Safety check.** Skips the cycle (and sleeps) if a safety pause is active, if Lidarr reports a download client or indexer error for Deezer, or if recent Deezer downloads are failing. See [Safety check](#safety-check).
2. **Daily caps.** Picks today's random grab cap and search cap (see [Daily caps](#daily-caps)). If either is used up, it sleeps until shortly after local midnight.
3. **Chooses albums.** Takes the next few monitored, missing albums, ordered by how recently their artist was added to Lidarr (newest first), skipping albums still on cooldown.
4. **Swaps indexers.** Turns **Automatic Search off** on every other indexer (the list is saved to the database first) and **on** for Deezer.
5. **Searches.** Sends one `AlbumSearch` command for the batch and waits for it to finish.
6. **Restores.** Turns Deezer off and re-enables exactly the indexers it disabled.
7. **Sleeps** a random 15-45 minutes (configurable), then repeats.

Only the Automatic Search toggle is changed. RSS and Interactive Search are left alone, so your normal RSS grabs and manual searches keep working.

### Daily caps

Each local day the scheduler picks a **random number within a range** for both caps, keeps it for the whole day (restarts don't re-roll it), and logs it. This makes the daily volume vary.

| Cap | Range settings | What is counted |
|---|---|---|
| Searches | `MIN_SEARCHES_PER_DAY` to `MAX_SEARCHES_PER_DAY` | Albums searched since local midnight |
| Grabs | `MIN_DOWNLOADS_PER_DAY` to `MAX_DOWNLOADS_PER_DAY` | Deezer grabs in Lidarr's history since local midnight (including ones you start by hand) |

The last batch of the day is shortened to fit, so a cap isn't overshot (an album yields at most one grab). "Day" follows the container's `TZ`.

### Safety check

Before each chunk, the scheduler looks at Lidarr:

- **Health.** An error-level health item about the download client or an indexer pauses searching only if its message mentions your Deezer indexer or client name. Other errors are logged and ignored.
- **Download history.** If at least `FAIL_MIN_FAILURES` Deezer downloads failed in the last `FAIL_LOOKBACK_HOURS` and they make up at least `FAIL_RATIO` of Deezer grabs in that window, searching pauses for `FAIL_PAUSE_HOURS`. This catches things like an expired ARL.
  - Only downloads from your Deezer indexer count. Failures from other indexers and clients (for example torrents) are ignored.
  - Failures you mark by hand ("Manually marked as failed") are ignored.

A pause is saved in the database, so it survives restarts. After it ends, only failures that happen afterwards are counted.

### Crash and error recovery

The list of indexers the scheduler disabled is saved **before** any change is made. It is restored after every chunk, after any error, and at startup, so a crash mid-search does not leave your other indexers off.

## Safety model

The scheduler only submits the same Lidarr `AlbumSearch` command you can run by hand in Lidarr. Lidarr stays responsible for release evaluation, quality profiles, custom formats, delay profiles, download clients, and importing.

No Deezer credentials or other secrets belong in this repository. The scheduler never sees your Deezer ARL; that lives in the Deemix plugin and server. Keep your Lidarr API key out of Git (the `.gitignore` excludes `.env`).

## Requirements

- Lidarr with the **Deemix plugin** (`ta264/Lidarr.Plugin.Deemix`), plus a Deemix server, with the Deemix **indexer** and **download client** configured.
- An API key from Lidarr (Settings > General).
- Docker and Docker Compose (or Portainer).

### Lidarr setup that this depends on

1. **Enable the Deemix protocol in your Delay Profiles** (Settings > Profiles > Delay Profiles > wrench icon). Check every profile, including tag-specific ones. Without it, Deezer releases are rejected with "DeemixDownloadProtocol is not enabled for this artist" and nothing is ever grabbed.
2. **Make download paths match.** Lidarr must be able to read the folder Deemix writes to. Mount the same folder in both places, or add a **Remote Path Mapping** (Settings > Download Clients) from Deemix's path (for example `/downloads/`) to the path Lidarr sees. Otherwise Lidarr shows a "remote download client places downloads in ... but this directory does not appear to exist" warning and imports fail.
3. **Prowlarr users:** if your other indexers are synced from Prowlarr, set the Lidarr app's sync level in Prowlarr to **Add and Remove Only**. With Full Sync, Prowlarr can overwrite the toggles this scheduler changes.
4. **Metadata profiles.** The scheduler searches whatever Lidarr considers monitored and missing. To control which release types (albums, singles, and so on) are wanted for each artist, use Lidarr's metadata profiles.

## Install

### Docker Compose

```bash
git clone https://github.com/wolly6973/lidarr-deezer-scheduler.git
cd lidarr-deezer-scheduler
cp .env.example .env      # then edit .env
docker compose up -d --build
docker logs -f lidarr-deezer-scheduler
```

### Portainer (deploy from Git)

The repository includes `docker-compose.yml` and a `Dockerfile`, so Portainer can deploy the stack straight from Git.

1. Create a Portainer stack from this repository.
2. Deploy with the `feature/webui` branch (or the branch you intend to use).
3. Set `WEBUI_HOST_PORT` only if the default host port `8081` is already in use.
4. Deploy. Open `http://<docker-host>:<host-port>/`, sign in with the generated password, then configure Lidarr and scheduler options under **Settings**.
5. When updating code, use **Pull and redeploy** and turn on rebuilding the image, because the compose file uses `build: .`.

Search history and state are stored in the `scheduler-data` volume (`/data/scheduler.db`). Keep it between redeploys.

## Configuration

**Configure the application in the Web UI.** New installs do not need scheduler environment variables.

1. Open **Settings** in the Web UI.
2. Enter the Lidarr base URL (for example, `http://192.168.1.10:8686`) and API key.
3. Use **Test Lidarr connection** to confirm the URL and key work.
4. Review the scheduler, indexer-management, timezone, logging, and safety settings, then click **Save changes**.

Settings are applied immediately and saved in `/data/webui-settings.json` with restrictive file permissions. The API key is never sent back to the browser after saving; leave its password field blank to keep the saved key. Keep the existing `scheduler-data` volume when redeploying.

### Docker/Portainer deployment settings

These are deployment-level settings, not scheduler settings:

| Variable | Default | Purpose |
|---|---|---|
| `WEBUI_HOST_PORT` | `8081` | Host port published by Docker; change this if it conflicts with another service |
| `WEBUI_PORT` | `8080` | Internal container port; normally leave unchanged |

For compatibility, the Compose file still accepts the old scheduler environment variables as initial defaults. On startup, the application imports them into persistent Web UI settings if no settings file exists, and upgrades older settings files by filling in the new connection fields. Once `/data/webui-settings.json` has been created, the Web UI settings take precedence. Do not delete the `scheduler-data` volume.

## Reading the logs

```
New daily search cap for 2026-10-10: 124 (random from 105-150)
New daily grab cap for 2026-10-10: 23 (random from 10-30)
Safety check OK window=24h: grabbed=2 failed=0 (...)
Starting search chunk: 5 album(s) | missing=30418 | searches today=0/124 | grabs today=0/23
Disabled Automatic Search on 2 other indexer(s): ...
Deezer Automatic Search enabled (indexer id 5)
Completed AlbumSearch command 64102: Album search completed. 1 reports downloaded.
Deezer Automatic Search disabled (indexer id 5)
Restored Automatic Search on 2 other indexer(s)
```

## Troubleshooting

**Searches complete but nothing is grabbed.** Run an Interactive Search in Lidarr on one of the searched albums and hover over the rejection reasons. Common ones: the Deemix protocol is not enabled in a Delay Profile, a quality profile mismatch, or the album simply isn't on Deezer.

**"SAFETY PAUSE" in the log.** Open Lidarr > Activity > History and read the failure messages. A login or authentication message points to an expired Deezer ARL. After fixing the cause, clear the pause (it also tells the check to ignore failures from before now), then restart the container:

```bash
docker exec lidarr-deezer-scheduler python -c "import sqlite3,time;d=sqlite3.connect('/data/scheduler.db');d.execute(\"insert or replace into kv values('paused_until','0')\");d.execute(\"insert or replace into kv values('failure_ignore_before',?)\",(str(int(time.time())),));d.commit()"
```

**Other indexers stuck off.** Restart the container; the startup step restores any indexers it disabled. You can also re-enable Automatic Search on them manually in Lidarr.

**Nothing happens after the first log lines.** On a large library, the first load of the missing list can take many minutes (about 20 for roughly 30,000 albums). Wait for the `Missing list refreshed` line.

**See how much has been searched:**

```bash
docker exec lidarr-deezer-scheduler python -c "
import sqlite3
db = sqlite3.connect('/data/scheduler.db')
print('distinct albums searched:', db.execute('select count(*) from album_state').fetchone()[0])
print('total search events:', db.execute('select count(*) from search_events').fetchone()[0])
"
```

## Limitations

- Missing albums only. Upgrading existing files is not scheduled.
- Caps count albums, not tracks. How fast tracks are fetched from Deezer is decided by the Deemix server and plugin settings, not by this scheduler.
- A release held as "pending" by a delay profile appears in history only when it is released, so the grab count can lag slightly.
- The scheduler relies on Lidarr API fields (history `data.indexer`, `artistId` on missing albums, artist `added`). If a Lidarr update changes them, the ordering or the failure attribution may need adjusting.
- State lives in one SQLite database in `/data`. Deleting it clears search history, cooldowns, caps and any pause.
