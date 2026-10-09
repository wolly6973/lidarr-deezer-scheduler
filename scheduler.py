import logging
import os
import random
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

import requests


LIDARR_URL = os.environ["LIDARR_URL"].rstrip("/")
LIDARR_API_KEY = os.environ["LIDARR_API_KEY"]
BATCH_SIZE = max(1, int(os.getenv("BATCH_SIZE", "5")))
MAX_SEARCHES_PER_DAY = max(1, int(os.getenv("MAX_SEARCHES_PER_DAY", "100")))
MIN_CHUNK_DELAY_SECONDS = max(60, int(os.getenv("MIN_CHUNK_DELAY_SECONDS", "900")))
MAX_CHUNK_DELAY_SECONDS = max(MIN_CHUNK_DELAY_SECONDS, int(os.getenv("MAX_CHUNK_DELAY_SECONDS", "2700")))
IDLE_RETRY_SECONDS = max(60, int(os.getenv("IDLE_RETRY_SECONDS", "300")))
ALBUM_COOLDOWN_HOURS = max(0, float(os.getenv("ALBUM_COOLDOWN_HOURS", "72")))
MISSING_PAGE_SIZE = max(50, int(os.getenv("MISSING_PAGE_SIZE", "500")))
COMMAND_TIMEOUT_SECONDS = max(60, int(os.getenv("COMMAND_TIMEOUT_SECONDS", "600")))
MANAGE_DEEZER_AUTOMATIC_SEARCH = os.getenv("MANAGE_DEEZER_AUTOMATIC_SEARCH", "true").lower() in {"1", "true", "yes", "on"}
DEEZER_INDEXER_NAME = os.getenv("DEEZER_INDEXER_NAME", "Deezer")
DB_PATH = os.getenv("DB_PATH", "/data/scheduler.db")

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


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
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
            """
        )
        self.db.commit()

    @staticmethod
    def now() -> int:
        return int(time.time())

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{LIDARR_URL}/api/v1/{path.lstrip('/')}"
        response = self.session.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

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
        """Load all missing albums and order them according to SEARCH_ORDER."""
        added_by_artist: dict[int, float] = {}
        if SEARCH_ORDER == "newest_artist":
            for artist in self.get_json("artist"):
                if artist.get("id") is not None:
                    added_by_artist[int(artist["id"])] = self.parse_iso(artist.get("added"))

        entries: list[tuple[int, int]] = []  # (album_id, artist_id)
        seen: set[int] = set()
        page = 1
        total = 0
        while True:
            data = self.get_json(
                "wanted/missing",
                params={
                    "page": page,
                    "pageSize": MISSING_PAGE_SIZE,
                    "sortKey": "title",
                    "sortDirection": "ascending",
                },
            )
            records = data.get("records", [])
            total = int(data.get("totalRecords", total))
            for record in records:
                album_id = record.get("id")
                if album_id is None:
                    album_id = record.get("albumId")
                if album_id is None:
                    continue
                album_id = int(album_id)
                if album_id in seen:
                    continue
                seen.add(album_id)
                artist_id = record.get("artistId")
                if artist_id is None:
                    artist_id = (record.get("artist") or {}).get("id", 0)
                entries.append((album_id, int(artist_id or 0)))

            if not records or len(records) < MISSING_PAGE_SIZE or len(seen) >= total:
                break
            page += 1

        if SEARCH_ORDER == "random":
            random.shuffle(entries)
        else:
            entries.sort(key=lambda e: (-added_by_artist.get(e[1], 0.0), e[1], e[0]))

        self.missing_cache = [album_id for album_id, _ in entries]
        self.missing_total = total
        self.missing_cache_time = time.time()
        log.info(
            "Missing list refreshed: %d album(s) cached, order=%s, total_missing=%d",
            len(self.missing_cache), SEARCH_ORDER, total,
        )

    def cooldown_ids(self) -> set[int]:
        if ALBUM_COOLDOWN_HOURS <= 0:
            return set()
        cutoff = self.now() - int(ALBUM_COOLDOWN_HOURS * 3600)
        rows = self.db.execute("SELECT album_id FROM album_state WHERE last_search >= ?", (cutoff,))
        return {int(row[0]) for row in rows}

    def fetch_missing_candidates(self, limit: int) -> tuple[list[int], int]:
        """Return the next eligible albums from the cached, ordered missing list."""
        cache_age = time.time() - self.missing_cache_time
        if not self.missing_cache or cache_age > MISSING_REFRESH_MINUTES * 60:
            self.refresh_missing_cache()

        on_cooldown = self.cooldown_ids()
        candidates: list[int] = []
        for album_id in self.missing_cache:
            if album_id in on_cooldown:
                continue
            candidates.append(album_id)
            if len(candidates) >= limit:
                break

        log.info(
            "Candidate scan: eligible=%d/%d | cached=%d | on cooldown=%d | total_missing=%d",
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

    def run_search(self, album_ids: list[int]) -> tuple[bool, int]:
        payload = {"name": "AlbumSearch", "albumIds": album_ids}

        if MANAGE_DEEZER_AUTOMATIC_SEARCH:
            self.set_deezer_automatic_search(True)

        try:
            data = self.post_json("command", payload)
            command_id = int(data["id"])
            self.record_search_start(album_ids, command_id)

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

            if success:
                log.info("Completed AlbumSearch command %s: %s | downloads initiated: %d", command_id, result, download_count)
            else:
                log.error("AlbumSearch command %s did not complete successfully: %s", command_id, result)
            return success, download_count
        finally:
            if MANAGE_DEEZER_AUTOMATIC_SEARCH:
                try:
                    self.set_deezer_automatic_search(False)
                except Exception:
                    log.exception("Failed to disable Deezer Automatic Search after search chunk")

    def recent_history(self, since_ts: float) -> list[dict[str, Any]]:
        """Return Lidarr history events newer than since_ts."""
        iso = datetime.fromtimestamp(since_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            data = self.get_json("history/since", params={"date": iso})
            return data if isinstance(data, list) else []
        except requests.HTTPError as exc:
            log.warning("history/since unavailable (%s); falling back to paged history", exc)

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
        problems = [
            h for h in health
            if str(h.get("type", "")).lower() == "error"
            and any(k in str(h.get("source", "")).lower() for k in ("downloadclient", "indexer"))
        ]
        if problems:
            detail = "; ".join(str(h.get("message", h.get("source", "?"))) for h in problems)
            return self.start_pause(f"Lidarr health error: {detail}")

        # 2. Recent grab vs. failure history
        ignore_before = int(self.get_kv("failure_ignore_before") or 0)
        since = max(now - int(FAIL_LOOKBACK_HOURS * 3600), ignore_before)
        events = self.recent_history(since)
        grabbed = sum(1 for e in events if str(e.get("eventType", "")).lower() == "grabbed")
        failed = sum(1 for e in events if str(e.get("eventType", "")).lower() == "downloadfailed")
        ratio = failed / max(grabbed, 1)

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
        pause = self.safety_check()
        if pause > 0:
            return pause

        used_today = self.searches_last_24h()
        remaining_daily = max(0, MAX_SEARCHES_PER_DAY - used_today)

        if remaining_daily <= 0:
            wait = self.seconds_until_daily_capacity()
            log.info(
                "Rolling 24h search limit reached: %d/%d | pausing for %dm %ds",
                used_today, MAX_SEARCHES_PER_DAY, wait // 60, wait % 60,
            )
            return wait

        chunk_size = min(BATCH_SIZE, remaining_daily)
        candidates, total_missing = self.fetch_missing_candidates(chunk_size)

        if total_missing == 0:
            log.info("No missing albums found.")
            return IDLE_RETRY_SECONDS

        if not candidates:
            log.info(
                "Missing albums: %d | no albums outside the %sh cooldown | rolling 24h searches: %d/%d",
                total_missing, ALBUM_COOLDOWN_HOURS, used_today, MAX_SEARCHES_PER_DAY,
            )
            return IDLE_RETRY_SECONDS

        log.info(
            "Starting search chunk: %d album(s) | missing=%d | rolling 24h searches=%d/%d",
            len(candidates), total_missing, used_today, MAX_SEARCHES_PER_DAY,
        )

        success, download_count = self.run_search(candidates)
        new_used = used_today + len(candidates)

        log.info(
            "Chunk complete: searches=%d | rolling 24h searches=%d/%d | downloads initiated=%d",
            len(candidates), new_used, MAX_SEARCHES_PER_DAY, download_count,
        )

        if not success:
            log.warning("Search chunk did not complete successfully; using the normal random delay before retrying")

        if new_used >= MAX_SEARCHES_PER_DAY:
            wait = self.seconds_until_daily_capacity()
            log.info(
                "Rolling 24h search limit now full: %d/%d | pausing for %dm %ds",
                new_used, MAX_SEARCHES_PER_DAY, wait // 60, wait % 60,
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
        log.info(
            "Order=%s | safety check=%s (pause %.0fh at >=%d failures and >=%.0f%% of grabs in %.0fh)",
            SEARCH_ORDER, SAFETY_CHECK_ENABLED, FAIL_PAUSE_HOURS, FAIL_MIN_FAILURES,
            FAIL_RATIO * 100, FAIL_LOOKBACK_HOURS,
        )
        log.info(
            "Config: batch=%d max searches/24h=%d random chunk delay=%d-%ds cooldown=%sh idle retry=%ds manage_deezer_automatic_search=%s",
            BATCH_SIZE, MAX_SEARCHES_PER_DAY,
            MIN_CHUNK_DELAY_SECONDS, MAX_CHUNK_DELAY_SECONDS,
            ALBUM_COOLDOWN_HOURS, IDLE_RETRY_SECONDS,
            MANAGE_DEEZER_AUTOMATIC_SEARCH,
        )

        if MANAGE_DEEZER_AUTOMATIC_SEARCH:
            try:
                self.set_deezer_automatic_search(False)
            except Exception:
                log.exception("Failed to establish safe starting state for Deezer Automatic Search")

        while True:
            try:
                delay = self.run_once()
            except requests.RequestException as exc:
                log.error("Lidarr API request failed: %s", exc)
                if MANAGE_DEEZER_AUTOMATIC_SEARCH:
                    try:
                        self.set_deezer_automatic_search(False)
                    except Exception:
                        log.exception("Failed to disable Deezer Automatic Search after API error")
                delay = IDLE_RETRY_SECONDS
            except Exception:
                log.exception("Unexpected scheduler error")
                if MANAGE_DEEZER_AUTOMATIC_SEARCH:
                    try:
                        self.set_deezer_automatic_search(False)
                    except Exception:
                        log.exception("Failed to disable Deezer Automatic Search after scheduler error")
                delay = IDLE_RETRY_SECONDS

            log.info("Scheduler sleeping for %d seconds", delay)
            time.sleep(delay)


if __name__ == "__main__":
    Scheduler().run_forever()
