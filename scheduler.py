import logging
import os
import random
import re
import sqlite3
import time
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

    def fetch_missing_candidates(self, limit: int) -> tuple[list[int], int]:
        """Page through missing albums until enough eligible candidates are found."""
        candidates: list[int] = []
        seen: set[int] = set()
        page = 1
        total = 0
        records_scanned = 0

        while len(candidates) < limit:
            data = self.get_json(
                "wanted/missing",
                params={
                    "page": page,
                    "pageSize": MISSING_PAGE_SIZE,
                    "sortKey": "title",
                    "sortDirection": "ascending",
                },
            )
            page_records = data.get("records", [])
            total = int(data.get("totalRecords", total))
            records_scanned += len(page_records)

            if not page_records:
                break

            for record in page_records:
                album_id = record.get("id")
                if album_id is None:
                    album_id = record.get("albumId")
                if album_id is None:
                    continue

                album_id = int(album_id)
                if album_id in seen:
                    continue
                seen.add(album_id)

                if not self.recently_searched(album_id):
                    candidates.append(album_id)
                    if len(candidates) >= limit:
                        break

            log.info(
                "Missing scan: page=%d records_scanned=%d eligible=%d/%d total_missing=%d",
                page,
                records_scanned,
                len(candidates),
                limit,
                total,
            )

            if len(candidates) >= limit:
                break
            if len(page_records) < MISSING_PAGE_SIZE:
                break
            if records_scanned >= total:
                break
            page += 1

        return candidates, total

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

    def run_once(self) -> int:
        """Run one small chunk and return how long to wait before the next chunk."""
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
            "Starting random search chunk: %d album(s) | missing=%d | rolling 24h searches=%d/%d",
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
