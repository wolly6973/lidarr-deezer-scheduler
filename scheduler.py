import json
import logging
import os
import random
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests


LIDARR_URL = os.getenv("LIDARR_URL", "").strip().rstrip("/")
LIDARR_API_KEY = os.getenv("LIDARR_API_KEY", "").strip()
BATCH_SIZE = max(1, int(os.getenv("BATCH_SIZE", "5")))
# Daily search cap: each local day a random cap between MIN and MAX is chosen.
# MIN_SEARCHES_PER_DAY defaults to 70% of the max when not set.
MAX_SEARCHES_PER_DAY = max(1, int(os.getenv("MAX_SEARCHES_PER_DAY", "100")))
_min_searches_env = os.getenv("MIN_SEARCHES_PER_DAY", "").strip()
MIN_SEARCHES_PER_DAY = int(_min_searches_env) if _min_searches_env else int(MAX_SEARCHES_PER_DAY * 0.7)
MIN_SEARCHES_PER_DAY = max(1, min(MIN_SEARCHES_PER_DAY, MAX_SEARCHES_PER_DAY))
MIN_CHUNK_DELAY_SECONDS = max(60, int(os.getenv("MIN_CHUNK_DELAY_SECONDS", "900")))
MAX_CHUNK_DELAY_SECONDS = max(MIN_CHUNK_DELAY_SECONDS, int(os.getenv("MAX_CHUNK_DELAY_SECONDS", "2700")))
IDLE_RETRY_SECONDS = max(60, int(os.getenv("IDLE_RETRY_SECONDS", "300")))
ALBUM_COOLDOWN_HOURS = max(0, float(os.getenv("ALBUM_COOLDOWN_HOURS", "72")))
MISSING_PAGE_SIZE = max(50, int(os.getenv("MISSING_PAGE_SIZE", "500")))
COMMAND_TIMEOUT_SECONDS = max(60, int(os.getenv("COMMAND_TIMEOUT_SECONDS", "600")))
MANAGE_DEEZER_AUTOMATIC_SEARCH = os.getenv("MANAGE_DEEZER_AUTOMATIC_SEARCH", "true").lower() in {"1", "true", "yes", "on"}
DEEZER_INDEXER_NAME = os.getenv("DEEZER_INDEXER_NAME", "Deezer")
# While a scheduled search runs, turn Automatic Search OFF on every other indexer and
# restore it afterwards, so the search only queries Deezer. Only the "Automatic Search"
# toggle is touched (RSS and Interactive Search stay as they are).
MANAGE_OTHER_INDEXERS = os.getenv("MANAGE_OTHER_INDEXERS", "true").lower() in {"1", "true", "yes", "on"}
# Comma-separated indexer names that should never be touched.
OTHER_INDEXERS_EXCLUDE = {n.strip().lower() for n in os.getenv("OTHER_INDEXERS_EXCLUDE", "").split(",") if n.strip()}
DB_PATH = os.getenv("DB_PATH", "/data/scheduler.db")
TZ = os.getenv("TZ", "America/Chicago")

# Ordering: "newest_artist" searches albums from the most recently added artists first.
# "random" picks albums in random order.
SEARCH_ORDER = os.getenv("SEARCH_ORDER", "newest_artist").strip().lower()
MISSING_REFRESH_MINUTES = max(5, int(os.getenv("MISSING_REFRESH_MINUTES", "360")))

# Safety check: pause searching if downloads are failing or Lidarr reports a problem.
SAFETY_CHECK_ENABLED = os.getenv("SAFETY_CHECK_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
FAIL_LOOKBACK_HOURS = max(1.0, float(os.getenv("FAIL_LOOKBACK_HOURS", "24")))
FAIL_MIN_FAILURES = max(1, int(os.getenv("FAIL_MIN_FAILURES", "3")))
FAIL_RATIO = min(1.0, max(0.0, float(os.getenv("FAIL_RATIO", "0.5"))))
FAIL_PAUSE_HOURS = max(0.5, float(os.getenv("FAIL_PAUSE_HOURS", "12")))

# Daily grab cap: each local day, a random cap between MIN and MAX is chosen, and searching
# stops once that many Deezer grabs have happened. Set MAX_DOWNLOADS_PER_DAY=0 to disable.
MAX_DOWNLOADS_PER_DAY = max(0, int(os.getenv("MAX_DOWNLOADS_PER_DAY", "30")))
_min_downloads_env = os.getenv("MIN_DOWNLOADS_PER_DAY", "").strip()
# An unset minimum defaults to 10 but never exceeds the max; an explicit backwards range is swapped.
MIN_DOWNLOADS_PER_DAY = max(0, int(_min_downloads_env)) if _min_downloads_env else min(10, MAX_DOWNLOADS_PER_DAY)
if MAX_DOWNLOADS_PER_DAY > 0 and MIN_DOWNLOADS_PER_DAY > MAX_DOWNLOADS_PER_DAY:
    MIN_DOWNLOADS_PER_DAY, MAX_DOWNLOADS_PER_DAY = MAX_DOWNLOADS_PER_DAY, MIN_DOWNLOADS_PER_DAY
# Optional: exact name of the Lidarr download client that handles Deezer grabs.
# Failures are attributed to this scheduler's Deezer traffic when they come from
# DEEZER_INDEXER_NAME or this download client.
DEEZER_DOWNLOAD_CLIENT_NAME = os.getenv("DEEZER_DOWNLOAD_CLIENT_NAME", "").strip()


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("lidarr-deezer-scheduler")


class Scheduler:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"X-Api-Key": LIDARR_API_KEY})
        self.db = sqlite3.connect(DB_PATH, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self._init_db()
        self.counter_reset_event = threading.Event()
        self.missing_cache: list[int] = []
        self.missing_total = 0
        self.missing_cache_time = 0.0

    def _init_db(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS album_state (
                album_id INTEGER PRIMARY KEY,
                last_search INTEGER,
                search_count INTEGER NOT NULL DEFAULT 0,
                last_result TEXT,
                last_success INTEGER
            );

            CREATE TABLE IF NOT EXISTS search_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                album_id INTEGER NOT NULL,
                searched_at INTEGER NOT NULL,
                command_id INTEGER,
                result TEXT
            );

            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_search_events_time
                ON search_events(searched_at);
            CREATE INDEX IF NOT EXISTS idx_search_events_album
                ON search_events(album_id, searched_at);

            CREATE TABLE IF NOT EXISTS search_batches (
                command_id INTEGER PRIMARY KEY,
                searched_at INTEGER NOT NULL,
                album_count INTEGER NOT NULL,
                completed INTEGER NOT NULL DEFAULT 0,
                downloads_found INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS imported_track_events (
                event_id TEXT PRIMARY KEY,
                imported_at INTEGER NOT NULL,
                album_id INTEGER
            );
            """
        )
        # Backfill historical search batches from the existing per-album search log.
        # This preserves a useful hit-rate baseline when upgrading an existing database.
        historical = self.db.execute(
            """SELECT command_id, MIN(searched_at) AS searched_at,
                      COUNT(DISTINCT album_id) AS album_count, MAX(result) AS result
               FROM search_events WHERE command_id IS NOT NULL GROUP BY command_id"""
        ).fetchall()
        for row in historical:
            result = str(row["result"] or "")
            if "reports downloaded" not in result.lower():
                continue
            self.db.execute(
                """INSERT OR IGNORE INTO search_batches
                   (command_id, searched_at, album_count, completed, downloads_found)
                   VALUES (?, ?, ?, 1, ?)""",
                (int(row["command_id"]), int(row["searched_at"]), int(row["album_count"]),
                 self.parse_download_count(result)),
            )
        self.db.commit()

    @staticmethod
    def now() -> int:
        return int(time.time())

    def get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> Any:
        """GET a Lidarr resource; allow a longer timeout only for known expensive endpoints."""
        url = f"{LIDARR_URL}/api/v1/{path.lstrip('/')}"
        started = time.monotonic()
        try:
            response = self.session.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            log.warning(
                "Lidarr GET %s failed after %.1fs (timeout=%ss): %s",
                path, time.monotonic() - started, timeout, exc,
            )
            raise
        finally:
            log.debug("Lidarr GET %s completed in %.2fs", path, time.monotonic() - started)

    def post_json(self, path: str, payload: dict[str, Any]) -> Any:
        url = f"{LIDARR_URL}/api/v1/{path.lstrip('/')}"
        response = self.session.post(url, json=payload, timeout=30)
        response.raise_for_status()
        return response.json()

    def put_json(self, path: str, payload: dict[str, Any]) -> Any:
        url = f"{LIDARR_URL}/api/v1/{path.lstrip('/')}"
        response = self.session.put(url, json=payload, timeout=30)
        response.raise_for_status()
        return response.json()

    def set_indexer_search(self, indexer_id: int, enabled: bool) -> None:
        """Set Automatic Search on one indexer, changing nothing else about it."""
        indexer = self.get_json(f"indexer/{indexer_id}")
        if bool(indexer.get("enableAutomaticSearch", False)) == enabled:
            return
        indexer["enableAutomaticSearch"] = enabled
        # forceSave skips the connection test, so a flaky indexer can't block a toggle.
        self.put_json(f"indexer/{indexer_id}?forceSave=true", indexer)

    def disable_other_indexers(self) -> None:
        """Turn off Automatic Search on all other indexers, remembering which ones."""
        if not MANAGE_OTHER_INDEXERS:
            return

        targets = [
            i for i in self.get_json("indexer")
            if i.get("id") is not None
            and bool(i.get("enableAutomaticSearch", False))
            and str(i.get("name", "")).lower() != DEEZER_INDEXER_NAME.lower()
            and str(i.get("name", "")).lower() not in OTHER_INDEXERS_EXCLUDE
        ]
        if not targets:
            return

        # Persist BEFORE changing anything, so a crash can always be undone.
        stored = set(json.loads(self.get_kv("disabled_indexers") or "[]"))
        stored |= {int(i["id"]) for i in targets}
        self.set_kv("disabled_indexers", json.dumps(sorted(stored)))

        for indexer in targets:
            self.set_indexer_search(int(indexer["id"]), False)
        log.info(
            "Disabled Automatic Search on %d other indexer(s): %s",
            len(targets), ", ".join(str(i.get("name")) for i in targets),
        )

    def restore_other_indexers(self) -> None:
        """Re-enable Automatic Search on every indexer this scheduler disabled."""
        ids = [int(i) for i in json.loads(self.get_kv("disabled_indexers") or "[]")]
        if not ids:
            return

        remaining: list[int] = []
        restored = 0
        for indexer_id in ids:
            try:
                self.set_indexer_search(indexer_id, True)
                restored += 1
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 404:
                    log.warning("Indexer %s no longer exists; dropping it from the restore list", indexer_id)
                else:
                    log.error("Could not restore indexer %s: %s", indexer_id, exc)
                    remaining.append(indexer_id)
            except requests.RequestException as exc:
                log.error("Could not restore indexer %s: %s", indexer_id, exc)
                remaining.append(indexer_id)

        self.set_kv("disabled_indexers", json.dumps(remaining))
        if restored:
            log.info("Restored Automatic Search on %d other indexer(s)", restored)
        if remaining:
            log.error("Still need to restore indexer id(s) %s; will retry", remaining)

    def reset_indexers(self, context: str) -> None:
        """Return to the idle state: Deezer off, all other indexers back on."""
        if MANAGE_DEEZER_AUTOMATIC_SEARCH:
            try:
                self.set_deezer_automatic_search(False)
            except Exception:
                log.exception("Failed to disable Deezer Automatic Search (%s)", context)
        try:
            self.restore_other_indexers()
        except Exception:
            log.exception("Failed to restore other indexers (%s)", context)

    def set_deezer_automatic_search(self, enabled: bool) -> bool:
        """Toggle Deezer's Lidarr Automatic Search setting without changing other fields."""
        if not MANAGE_DEEZER_AUTOMATIC_SEARCH:
            return True

        indexers = self.get_json("indexer")
        indexer = next(
            (item for item in indexers if str(item.get("name", "")).lower() == DEEZER_INDEXER_NAME.lower()),
            None,
        )
        if indexer is None:
            raise RuntimeError(f"Lidarr indexer '{DEEZER_INDEXER_NAME}' was not found")

        current = bool(indexer.get("enableAutomaticSearch", False))
        if current == enabled:
            return True

        indexer_id = indexer.get("id")
        if indexer_id is None:
            raise RuntimeError(f"Lidarr indexer '{DEEZER_INDEXER_NAME}' has no id")

        updated = dict(indexer)
        updated["enableAutomaticSearch"] = enabled
        self.put_json(f"indexer/{indexer_id}", updated)
        log.info(
            "Deezer Automatic Search %s (indexer id %s)",
            "enabled" if enabled else "disabled",
            indexer_id,
        )
        return True

    @staticmethod
    def parse_iso(value: Any) -> float:
        if not value:
            return 0.0
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0

    def get_kv(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_kv(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.db.commit()

    def refresh_missing_cache(self) -> None:
        """Fetch the current page of missing albums without crawling the entire library.

        Lidarr's wanted/missing endpoint performs an expensive count query. Fetching every
        page repeatedly multiplies that cost by the number of pages, so keep one page cached
        and persist its page number. Advance only after its entries are exhausted/on cooldown.
        """
        page = max(1, int(self.get_kv("missing_page") or "1"))
        started = time.monotonic()
        data = self.get_json(
            "wanted/missing",
            params={
                "page": page,
                "pageSize": MISSING_PAGE_SIZE,
                "sortKey": "title",
                "sortDirection": "ascending",
            },
            # This endpoint can take longer than ordinary Lidarr API calls on large libraries.
            timeout=90,
        )
        records = data.get("records", []) if isinstance(data, dict) else []
        total = int(data.get("totalRecords", 0)) if isinstance(data, dict) else 0

        entries: list[tuple[int, int, float]] = []  # (album_id, artist_id, artist_added)
        seen: set[int] = set()
        missing_artist_dates = 0
        page_seen = {int(value) for value in json.loads(self.get_kv("missing_page_seen") or "[]")}
        for record in records:
            album_id = record.get("id")
            if album_id is None:
                album_id = record.get("albumId")
            if album_id is None:
                continue
            album_id = int(album_id)
            if album_id in seen or album_id in page_seen:
                continue
            seen.add(album_id)
            artist = record.get("artist") or {}
            artist_id = record.get("artistId")
            if artist_id is None:
                artist_id = artist.get("id", 0)
            artist_added = self.parse_iso(artist.get("added"))
            if SEARCH_ORDER == "newest_artist" and not artist_added:
                missing_artist_dates += 1
            entries.append((album_id, int(artist_id or 0), artist_added))

        if SEARCH_ORDER == "random":
            random.shuffle(entries)
        else:
            # The API response embeds artist data; use it rather than issuing a separate
            # full /artist request. Ordering is within this page, not across the whole library.
            entries.sort(key=lambda e: (-e[2], e[1], e[0]))

        self.missing_cache = [album_id for album_id, _, _ in entries]
        self.missing_total = total
        self.missing_cache_time = time.time()

        page_count = max(1, (total + MISSING_PAGE_SIZE - 1) // MISSING_PAGE_SIZE)
        if total == 0:
            self.set_kv("missing_page_seen", "[]")
        if not records and page > page_count:
            # The missing list shrank since the cursor was saved. Restart at page 1.
            self.set_kv("missing_page", "1")
            self.set_kv("missing_page_seen", "[]")
            if total > 0:
                self.missing_cache_time = 0.0
                return self.refresh_missing_cache()
        log.info(
            "Missing page loaded: page=%d/%d cached=%d album(s), order=%s, total_missing=%d, elapsed=%.1fs",
            page, page_count, len(self.missing_cache), SEARCH_ORDER, total,
            time.monotonic() - started,
        )
        if missing_artist_dates:
            log.debug(
                "%d record(s) lacked embedded artist-added dates; tie-breaking by artist/album id",
                missing_artist_dates,
            )

    def cooldown_ids(self) -> set[int]:
        if ALBUM_COOLDOWN_HOURS <= 0:
            return set()
        cutoff = self.now() - int(ALBUM_COOLDOWN_HOURS * 3600)
        rows = self.db.execute("SELECT album_id FROM album_state WHERE last_search >= ?", (cutoff,))
        return {int(row[0]) for row in rows}

    def fetch_missing_candidates(self, limit: int) -> tuple[list[int], int]:
        """Return eligible albums from one cached page, rotating only after it is exhausted."""
        if self.missing_cache_time == 0:
            self.refresh_missing_cache()

        on_cooldown = self.cooldown_ids()
        candidates = [
            album_id for album_id in self.missing_cache
            if album_id not in on_cooldown
        ][:limit]
        cache_age = time.time() - self.missing_cache_time

        # Keep a page until its albums have been attempted or are already on cooldown.
        # This avoids throwing away most of a 500-item page every six hours.
        if (
            not candidates
            and self.missing_cache_time > 0
            and cache_age >= MISSING_REFRESH_MINUTES * 60
        ):
            if self.missing_total > 0:
                page = max(1, int(self.get_kv("missing_page") or "1"))
                page_count = max(1, (self.missing_total + MISSING_PAGE_SIZE - 1) // MISSING_PAGE_SIZE)
                next_page = page + 1 if page < page_count else 1
                self.set_kv("missing_page", str(next_page))
                self.set_kv("missing_page_seen", "[]")
                log.info("Missing page %d exhausted; rotating to page %d/%d", page, next_page, page_count)
            else:
                # An empty library still needs an occasional refresh to discover new gaps.
                log.info("No missing albums reported; refreshing the missing list after the idle interval")

            self.missing_cache = []
            self.missing_cache_time = 0.0
            # Fetch at most one new page in this cycle, even if it contains only cooldown albums.
            self.refresh_missing_cache()
            on_cooldown = self.cooldown_ids()
            candidates = [
                album_id for album_id in self.missing_cache
                if album_id not in on_cooldown
            ][:limit]

        log.info(
            "Candidate scan: eligible=%d/%d | cached_page=%d | on cooldown=%d | total_missing=%d",
            len(candidates), limit, len(self.missing_cache), len(on_cooldown), self.missing_total,
        )
        return candidates, self.missing_total

    def searches_last_24h(self) -> int:
        cutoff = self.now() - 86400
        row = self.db.execute(
            "SELECT COUNT(*) AS count FROM search_events WHERE searched_at >= ?",
            (cutoff,),
        ).fetchone()
        return int(row["count"])

    def seconds_until_daily_capacity(self) -> int:
        """Return seconds until at least one rolling-24h search slot opens."""
        cutoff = self.now() - 86400
        row = self.db.execute(
            "SELECT MIN(searched_at) AS oldest FROM search_events WHERE searched_at >= ?",
            (cutoff,),
        ).fetchone()
        oldest = row["oldest"]
        if oldest is None:
            return 0
        return max(1, int(oldest) + 86400 - self.now() + 1)

    def recently_searched(self, album_id: int) -> bool:
        if ALBUM_COOLDOWN_HOURS <= 0:
            return False
        cutoff = self.now() - int(ALBUM_COOLDOWN_HOURS * 3600)
        row = self.db.execute(
            "SELECT 1 FROM album_state WHERE album_id = ? AND last_search >= ?",
            (album_id, cutoff),
        ).fetchone()
        return row is not None

    def record_search_start(self, album_ids: list[int], command_id: int) -> None:
        now = self.now()
        for album_id in album_ids:
            self.db.execute(
                "INSERT INTO search_events(album_id, searched_at, command_id, result) VALUES (?, ?, ?, ?)",
                (album_id, now, command_id, "started"),
            )
            self.db.execute(
                """
                INSERT INTO album_state(album_id, last_search, search_count, last_result, last_success)
                VALUES (?, ?, 1, ?, NULL)
                ON CONFLICT(album_id) DO UPDATE SET
                    last_search=excluded.last_search,
                    search_count=album_state.search_count + 1,
                    last_result=excluded.last_result
                """,
                (album_id, now, "started"),
            )
        self.db.commit()
        # Persist IDs attempted on this page so a restart or cooldown expiry cannot loop back
        # to the same albums before the page has been consumed.
        page_seen = {int(value) for value in json.loads(self.get_kv("missing_page_seen") or "[]")}
        page_seen.update(int(album_id) for album_id in album_ids)
        self.set_kv("missing_page_seen", json.dumps(sorted(page_seen)))
        if self.missing_cache:
            attempted = set(album_ids)
            self.missing_cache = [album_id for album_id in self.missing_cache if album_id not in attempted]

    def record_search_result(self, album_ids: list[int], command_id: int, result: str, success: bool) -> None:
        now = self.now()
        self.db.executemany(
            "UPDATE search_events SET result = ? WHERE command_id = ? AND album_id = ?",
            [(result, command_id, album_id) for album_id in album_ids],
        )
        for album_id in album_ids:
            self.db.execute(
                """
                UPDATE album_state
                SET last_result = ?, last_success = CASE WHEN ? THEN ? ELSE last_success END
                WHERE album_id = ?
                """,
                (result, int(success), now, album_id),
            )
        self.db.commit()

    @staticmethod
    def parse_download_count(result: str) -> int:
        match = re.search(r"(\d+)\s+reports?\s+downloaded", result, re.IGNORECASE)
        return int(match.group(1)) if match else 0

    def refresh_lifetime_track_stats(self) -> None:
        """Count imported track events for albums the scheduler has searched."""
        cursor = self.get_kv("lifetime_history_cursor")
        if cursor is None:
            # Start counting from feature activation; don't misattribute older imports.
            cursor = str(self.now() - 1)
            self.set_kv("lifetime_history_cursor", cursor)
        since_ts = float(cursor)
        try:
            events = self.recent_history(since_ts)
        except requests.RequestException as exc:
            log.warning("Could not refresh lifetime imported-track count: %s", exc)
            return

        latest = since_ts
        for event in events:
            event_ts = self.parse_iso(event.get("date"))
            latest = max(latest, event_ts)
            if str(event.get("eventType", "")).lower() != "trackfileimported":
                continue
            data = event.get("data") or {}
            album_value = data.get("albumId", event.get("albumId"))
            try:
                album_id = int(album_value)
            except (TypeError, ValueError):
                continue
            if not self.db.execute(
                "SELECT 1 FROM album_state WHERE album_id = ? LIMIT 1", (album_id,)
            ).fetchone():
                continue
            event_id = event.get("id")
            if event_id is None:
                event_id = "|".join((
                    str(event.get("date", "")), str(album_id),
                    str(data.get("trackFileId", "")), str(data.get("path", "")),
                ))
            self.db.execute(
                "INSERT OR IGNORE INTO imported_track_events(event_id, imported_at, album_id) VALUES (?, ?, ?)",
                (str(event_id), int(event_ts or self.now()), album_id),
            )
        if events:
            self.db.commit()
            self.set_kv("lifetime_history_cursor", str(latest + 0.001))

    def lifetime_stats(self) -> dict[str, int | float]:
        searched = int(self.db.execute(
            "SELECT COUNT(DISTINCT album_id) FROM search_events"
        ).fetchone()[0])
        batches = self.db.execute(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN downloads_found > 0 THEN 1 ELSE 0 END) AS hits "
            "FROM search_batches WHERE completed = 1"
        ).fetchone()
        total_batches = int(batches["total"] or 0)
        hit_batches = int(batches["hits"] or 0)
        tracks = int(self.db.execute(
            "SELECT COUNT(*) FROM imported_track_events"
        ).fetchone()[0])
        return {
            "lifetime_albums_searched": searched,
            "lifetime_completed_batches": total_batches,
            "lifetime_hit_batches": hit_batches,
            "lifetime_hit_rate": round(hit_batches * 100.0 / total_batches, 1) if total_batches else 0.0,
            "lifetime_tracks_downloaded": tracks,
        }

    def run_search(self, album_ids: list[int]) -> tuple[bool, int]:
        payload = {"name": "AlbumSearch", "albumIds": album_ids}

        try:
            self.disable_other_indexers()
            if MANAGE_DEEZER_AUTOMATIC_SEARCH:
                self.set_deezer_automatic_search(True)

            data = self.post_json("command", payload)
            command_id = int(data["id"])
            self.record_search_start(album_ids, command_id)
            self.db.execute(
                "INSERT OR REPLACE INTO search_batches(command_id, searched_at, album_count, completed, downloads_found) VALUES (?, ?, ?, 0, 0)",
                (command_id, self.now(), len(album_ids)),
            )
            self.db.commit()

            log.info("Started Lidarr AlbumSearch command %s for %d album(s): %s", command_id, len(album_ids), album_ids)

            deadline = time.time() + COMMAND_TIMEOUT_SECONDS
            final: dict[str, Any] | None = None
            while time.time() < deadline:
                final = self.get_json(f"command/{command_id}")
                status = str(final.get("status", "")).lower()
                if status in {"completed", "failed", "canceled", "cancelled"}:
                    break
                time.sleep(2)

            if final is None:
                result = "command polling returned no result"
                success = False
            else:
                status = str(final.get("status", "unknown")).lower()
                result = final.get("message") or final.get("result") or status
                success = status == "completed"
                if time.time() >= deadline and status not in {"completed", "failed", "canceled", "cancelled"}:
                    result = f"command timeout after {COMMAND_TIMEOUT_SECONDS}s"
                    success = False

            result = str(result)
            download_count = self.parse_download_count(result) if success else 0
            self.record_search_result(album_ids, command_id, result, success)
            self.db.execute(
                "UPDATE search_batches SET completed = ?, downloads_found = ? WHERE command_id = ?",
                (int(success), self.parse_download_count(result) if success else 0, command_id),
            )
            self.db.commit()

            if success:
                log.info("Completed AlbumSearch command %s: %s | downloads initiated: %d", command_id, result, download_count)
            else:
                log.error("AlbumSearch command %s did not complete successfully: %s", command_id, result)
            return success, download_count
        finally:
            self.reset_indexers("after search chunk")

    @staticmethod
    def deezer_match(e: dict[str, Any]) -> bool:
        """True if a history event came from the Deezer indexer (or the optional Deezer client)."""
        data = e.get("data") or {}
        if str(data.get("indexer", "")).lower() == DEEZER_INDEXER_NAME.lower():
            return True
        if not DEEZER_DOWNLOAD_CLIENT_NAME:
            return False
        # Lidarr uses "downloadClientName" on some events and "downloadClient" on others.
        clients = {str(data.get(k, "")).lower() for k in ("downloadClientName", "downloadClient")}
        return DEEZER_DOWNLOAD_CLIENT_NAME.lower() in clients

    @staticmethod
    def local_now() -> datetime:
        return datetime.now().astimezone()

    def seconds_until_next_day(self) -> int:
        now = self.local_now()
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return max(1, int((tomorrow - now).total_seconds()))

    def todays_search_cap(self) -> int:
        """Today's randomly chosen search cap (kept for the whole local day)."""
        today = self.local_now().strftime("%Y-%m-%d")
        stored_day = self.get_kv("search_cap_day")
        stored_value = self.get_kv("search_cap_value")
        if stored_day == today and stored_value is not None:
            value = int(stored_value)
            if MIN_SEARCHES_PER_DAY <= value <= MAX_SEARCHES_PER_DAY:
                return value

        value = random.randint(MIN_SEARCHES_PER_DAY, MAX_SEARCHES_PER_DAY)
        self.set_kv("search_cap_day", today)
        self.set_kv("search_cap_value", str(value))
        log.info(
            "New daily search cap for %s: %d (random from %d-%d)",
            today, value, MIN_SEARCHES_PER_DAY, MAX_SEARCHES_PER_DAY,
        )
        return value

    def searches_today(self) -> int:
        """Albums searched since local midnight or the most recent manual counter reset."""
        day_start = int(self.local_now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        reset_at = int(self.get_kv("search_count_reset_at") or "0")
        day_start = max(day_start, reset_at)
        row = self.db.execute(
            "SELECT COUNT(*) AS count FROM search_events WHERE searched_at >= ?",
            (day_start,),
        ).fetchone()
        return int(row["count"])

    def todays_grab_cap(self) -> int | None:
        """Today's randomly chosen grab cap, or None when the cap is disabled."""
        if MAX_DOWNLOADS_PER_DAY <= 0:
            return None

        today = self.local_now().strftime("%Y-%m-%d")
        stored_day = self.get_kv("grab_cap_day")
        stored_value = self.get_kv("grab_cap_value")
        if stored_day == today and stored_value is not None:
            value = int(stored_value)
            if MIN_DOWNLOADS_PER_DAY <= value <= MAX_DOWNLOADS_PER_DAY:
                return value

        value = random.randint(MIN_DOWNLOADS_PER_DAY, MAX_DOWNLOADS_PER_DAY)
        self.set_kv("grab_cap_day", today)
        self.set_kv("grab_cap_value", str(value))
        log.info(
            "New daily grab cap for %s: %d (random from %d-%d)",
            today, value, MIN_DOWNLOADS_PER_DAY, MAX_DOWNLOADS_PER_DAY,
        )
        return value

    def deezer_grabs_today(self) -> int:
        """Count Deezer grabs since local midnight or the most recent manual counter reset."""
        day_start = self.local_now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        reset_at = int(self.get_kv("grab_count_reset_at") or "0")
        day_start = max(day_start, reset_at)
        events = self.recent_history(day_start)
        return sum(
            1 for e in events
            if str(e.get("eventType", "")).lower() == "grabbed" and self.deezer_match(e)
        )

    def recent_history(self, since_ts: float) -> list[dict[str, Any]]:
        """Return Lidarr history events newer than since_ts."""
        iso = datetime.fromtimestamp(since_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            data = self.get_json("history/since", params={"date": iso})
            return data if isinstance(data, list) else []
        except requests.RequestException as exc:
            log.warning("history/since request failed (%s); falling back to paged history", exc)

        data = self.get_json(
            "history",
            params={"page": 1, "pageSize": 500, "sortKey": "date", "sortDirection": "descending"},
        )
        return [e for e in data.get("records", []) if self.parse_iso(e.get("date")) >= since_ts]

    def start_pause(self, reason: str) -> int:
        seconds = int(FAIL_PAUSE_HOURS * 3600)
        until = self.now() + seconds
        self.set_kv("paused_until", str(until))
        # After the pause, only count failures that happen from then on.
        self.set_kv("failure_ignore_before", str(until))
        log.error("SAFETY PAUSE for %.1fh: %s", FAIL_PAUSE_HOURS, reason)
        log.error("Check Lidarr (Activity > History/Queue, System > Status) and your Deezer ARL before the pause ends.")
        return seconds

    def safety_check(self) -> int:
        """Return seconds to pause, or 0 when it is safe to search."""
        if not SAFETY_CHECK_ENABLED:
            return 0

        now = self.now()
        paused_until = int(self.get_kv("paused_until") or 0)
        if paused_until > now:
            remaining = paused_until - now
            log.warning("Safety pause active, %dm remaining", remaining // 60)
            return remaining

        # 1. Lidarr's own health report (download client or indexer errors)
        try:
            health = self.get_json("health")
        except requests.RequestException as exc:
            log.warning("Could not read Lidarr health: %s", exc)
            health = []
        names = [n.lower() for n in (DEEZER_INDEXER_NAME, DEEZER_DOWNLOAD_CLIENT_NAME) if n]
        problems = []
        for h in health:
            if str(h.get("type", "")).lower() != "error":
                continue
            if not any(k in str(h.get("source", "")).lower() for k in ("downloadclient", "indexer")):
                continue
            message = str(h.get("message", h.get("source", "?")))
            if any(n in message.lower() for n in names):
                problems.append(message)
            else:
                log.warning("Lidarr health error not attributed to Deezer (ignored): %s", message)
        if problems:
            return self.start_pause("Lidarr health error: " + "; ".join(problems))

        # 2. Recent grab vs. failure history
        ignore_before = int(self.get_kv("failure_ignore_before") or 0)
        since = max(now - int(FAIL_LOOKBACK_HOURS * 3600), ignore_before)
        # Fetch extra history so a failure can be matched to a grab made before the window.
        events = self.recent_history(now - int((FAIL_LOOKBACK_HOURS + 72) * 3600))

        def etype(e: dict[str, Any]) -> str:
            return str(e.get("eventType", "")).lower()

        direct_match = self.deezer_match

        grabs = [e for e in events if etype(e) == "grabbed"]
        can_attribute = any("indexer" in (e.get("data") or {}) for e in grabs)
        deezer_ids = {e.get("downloadId") for e in events if e.get("downloadId") and direct_match(e)}

        def is_ours(e: dict[str, Any]) -> bool:
            if not can_attribute:
                return True
            return direct_match(e) or (e.get("downloadId") in deezer_ids)

        in_window = [e for e in events if self.parse_iso(e.get("date")) >= since]
        grabbed = sum(1 for e in in_window if etype(e) == "grabbed" and is_ours(e))

        def manual(e: dict[str, Any]) -> bool:
            # Failures the user marked by hand say nothing about whether the ARL works.
            return "manually marked" in str((e.get("data") or {}).get("message", "")).lower()

        failed = sum(1 for e in in_window if etype(e) == "downloadfailed" and is_ours(e) and not manual(e))
        other_failed = sum(
            1 for e in in_window
            if etype(e) == "downloadfailed" and (not is_ours(e) or manual(e))
        )
        ratio = failed / max(grabbed, 1)

        if not can_attribute and grabs:
            log.warning("History events carry no indexer info; counting ALL grabs/failures, not just Deezer")
        if other_failed:
            log.info("Ignoring %d failed download(s) from other indexers/clients or marked failed manually", other_failed)

        tripped = failed >= FAIL_MIN_FAILURES and ratio >= FAIL_RATIO
        if not tripped:
            log.info(
                "Safety check OK window=%.0fh: grabbed=%d failed=%d (pause at >=%d failures and >=%.0f%% of grabs)",
                FAIL_LOOKBACK_HOURS, grabbed, failed, FAIL_MIN_FAILURES, FAIL_RATIO * 100,
            )

        if tripped:
            return self.start_pause(
                f"{failed} failed download(s) out of {grabbed} grab(s) in the last "
                f"{FAIL_LOOKBACK_HOURS:.0f}h; the Deezer ARL may have expired"
            )
        return 0

    def run_once(self) -> int:
        """Run one small chunk and return how long to wait before the next chunk."""
        try:
            self.refresh_lifetime_track_stats()
        except Exception:
            log.exception("Could not update lifetime statistics")
        pause = self.safety_check()
        if pause > 0:
            return pause

        grab_cap = self.todays_grab_cap()
        grabs_today = self.deezer_grabs_today() if grab_cap is not None else 0
        if grab_cap is not None and grabs_today >= grab_cap:
            # Resume shortly after local midnight, with some jitter so it isn't clockwork.
            wait = self.seconds_until_next_day() + random.randint(300, 3600)
            log.info(
                "Daily grab cap reached: %d/%d | pausing for %dh %dm",
                grabs_today, grab_cap, wait // 3600, (wait % 3600) // 60,
            )
            return wait

        search_cap = self.todays_search_cap()
        used_today = self.searches_today()
        remaining_daily = max(0, search_cap - used_today)

        if remaining_daily <= 0:
            wait = self.seconds_until_next_day() + random.randint(300, 3600)
            log.info(
                "Daily search cap reached: %d/%d | pausing for %dh %dm",
                used_today, search_cap, wait // 3600, (wait % 3600) // 60,
            )
            return wait

        chunk_size = min(BATCH_SIZE, remaining_daily)
        if grab_cap is not None:
            # An album yields at most one grab, so this keeps a chunk from overshooting the cap.
            chunk_size = min(chunk_size, grab_cap - grabs_today)
        candidates, total_missing = self.fetch_missing_candidates(chunk_size)

        if total_missing == 0:
            log.info("No missing albums found.")
            return IDLE_RETRY_SECONDS

        if not candidates:
            log.info(
                "Missing albums: %d | no albums outside the %sh cooldown | searches today: %d/%d",
                total_missing, ALBUM_COOLDOWN_HOURS, used_today, search_cap,
            )
            return IDLE_RETRY_SECONDS

        log.info(
            "Starting search chunk: %d album(s) | missing=%d | searches today=%d/%d | grabs today=%s",
            len(candidates), total_missing, used_today, search_cap,
            f"{grabs_today}/{grab_cap}" if grab_cap is not None else "uncapped",
        )

        success, download_count = self.run_search(candidates)
        new_used = used_today + len(candidates)

        log.info(
            "Chunk complete: searches=%d | searches today=%d/%d | downloads initiated=%d",
            len(candidates), new_used, search_cap, download_count,
        )

        if not success:
            log.warning("Search chunk did not complete successfully; using the normal random delay before retrying")

        if new_used >= search_cap:
            wait = self.seconds_until_next_day() + random.randint(300, 3600)
            log.info(
                "Daily search cap now full: %d/%d | pausing for %dh %dm",
                new_used, search_cap, wait // 3600, (wait % 3600) // 60,
            )
            return wait

        delay = random.randint(MIN_CHUNK_DELAY_SECONDS, MAX_CHUNK_DELAY_SECONDS)
        log.info(
            "Next search chunk scheduled in %dm %ds (random range %dm-%dm)",
            delay // 60, delay % 60,
            MIN_CHUNK_DELAY_SECONDS // 60, MAX_CHUNK_DELAY_SECONDS // 60,
        )
        return delay

    def run_forever(self) -> None:
        log.info("Lidarr Deezer Scheduler starting")
        log.info("Lidarr URL: %s", LIDARR_URL)
        log.info("Deezer-only searches: manage_other_indexers=%s excluded=%s",
                 MANAGE_OTHER_INDEXERS, sorted(OTHER_INDEXERS_EXCLUDE) or "none")
        log.info(
            "Order=%s | safety check=%s (pause %.0fh at >=%d failures and >=%.0f%% of grabs in %.0fh)",
            SEARCH_ORDER, SAFETY_CHECK_ENABLED, FAIL_PAUSE_HOURS, FAIL_MIN_FAILURES,
            FAIL_RATIO * 100, FAIL_LOOKBACK_HOURS,
        )
        log.info(
            "Daily grab cap: %s",
            f"random {MIN_DOWNLOADS_PER_DAY}-{MAX_DOWNLOADS_PER_DAY} per local day"
            if MAX_DOWNLOADS_PER_DAY > 0 else "disabled",
        )
        log.info(
            "Config: batch=%d daily searches=random %d-%d random chunk delay=%d-%ds cooldown=%sh idle retry=%ds manage_deezer_automatic_search=%s",
            BATCH_SIZE, MIN_SEARCHES_PER_DAY, MAX_SEARCHES_PER_DAY,
            MIN_CHUNK_DELAY_SECONDS, MAX_CHUNK_DELAY_SECONDS,
            ALBUM_COOLDOWN_HOURS, IDLE_RETRY_SECONDS,
            MANAGE_DEEZER_AUTOMATIC_SEARCH,
        )

        self.reset_indexers("at startup")

        while True:
            try:
                delay = self.run_once()
            except requests.RequestException as exc:
                log.error("Lidarr API request failed: %s", exc)
                # Indexer changes are always restored in run_search's finally block. Errors
                # before a search do not need another set of API calls just to reset them.
                # Avoid hammering Lidarr every few minutes when a heavy request fails.
                delay = max(IDLE_RETRY_SECONDS, 1800)
            except Exception:
                log.exception("Unexpected scheduler error")
                # run_search restores indexers in its finally block if a search was underway.
                delay = IDLE_RETRY_SECONDS

            log.info("Scheduler sleeping for %d seconds", delay)
            if self.counter_reset_event.wait(delay):
                self.counter_reset_event.clear()
                log.info("Daily cap counters were reset; resuming scheduler early")


if __name__ == "__main__":
    Scheduler().run_forever()
