# Lidarr Deezer Scheduler

A small, conservative scheduler that asks Lidarr to search monitored missing albums through its configured indexers, including Deezer.

## What it does

- Reads Lidarr's missing-album list.
- Skips albums searched within the configured cooldown.
- Sends Lidarr `AlbumSearch` commands in small batches.
- Waits for each Lidarr command to finish before continuing.
- Enforces a rolling 24-hour search limit using persistent SQLite state.
- Keeps per-album search history and last result.
- Does not download directly from Deezer and does not replace Lidarr's normal RSS/new-release workflow.

This first version is **missing-only**. Upgrade scheduling can be added later without changing the core throttling/state design.

## Configuration

Set these in Portainer or another deployment environment:

| Variable | Default | Purpose |
|---|---:|---|
| `LIDARR_URL` | required | Lidarr base URL, reachable from the scheduler container |
| `LIDARR_API_KEY` | required | Lidarr API key |
| `SEARCH_INTERVAL_SECONDS` | `30` | Delay between scheduler cycles and between batches |
| `BATCH_SIZE` | `5` | Albums per Lidarr AlbumSearch command |
| `MAX_SEARCHES_PER_RUN` | `20` | Maximum albums searched in one scheduler cycle |
| `MAX_SEARCHES_PER_DAY` | `100` | Rolling 24-hour album-search limit |
| `ALBUM_COOLDOWN_HOURS` | `72` | Minimum time before the same album is searched again |
| `MISSING_PAGE_SIZE` | `500` | Lidarr missing-list page size |
| `COMMAND_TIMEOUT_SECONDS` | `600` | Maximum time to wait for one Lidarr search command |
| `LOG_LEVEL` | `INFO` | Python logging level |

## Portainer

The repository includes `docker-compose.yml` and a `Dockerfile` so Portainer can deploy the stack from the Git repository.

1. Create a Portainer Stack using this repository.
2. Set the required environment variables.
3. Point `LIDARR_URL` at the Lidarr LXC's LAN address, for example `http://192.168.4.X:8686`.
4. Set `LIDARR_API_KEY` to the existing Lidarr API key. Do not commit it to GitHub.
5. Deploy the stack.

The SQLite database is stored in the persistent `scheduler-data` volume.

## Safety model

The scheduler only submits the same Lidarr `AlbumSearch` command that can be run manually through Lidarr. Lidarr remains responsible for release evaluation, quality profiles, custom formats, delay profiles, download clients, and importing.

No Deezer credentials or API secrets belong in this repository.
