import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

import requests


LIDARR_URL = os.environ["LIDARR_URL"].rstrip("/")
LIDARR_API_KEY = os.environ["LIDARR_API_KEY"]
SEARCH_INTERVAL_SECONDS = int(os.getenv("SEARCH_INTERVAL_SECONDS", "30"))
BATCH_SIZE = max(1, int(os.getenv("BATCH_SIZE", "5")))
MAX_SEARCHES_PER_RUN = max(1, int(os.getenv("MAX_SEARCHES_PER_RUN", "20")))
MAX_SEARCHES_PER_DAY = max(1, int(os.getenv("MAX_SEARCHES_PER_DAY", "100")))
ALBUM_COOLDOWN_HOURS = max(0, float(os.getenv("ALBUM_COOLDOWN_HOURS", "72")))
MISSING_PAGE_SIZE = max(50, int(os.getenv("MISSING_PAGE_SIZE", "500")))
COMMAND_TIMEOUT_SECONDS = max(60, int(os.getenv("COMMAND_TIMEOUT_SECONDS", "600")))
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

    def fetch_missing_albums(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        page = 1

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
            page_records = data.get("records", [])
            records.extend(page_records)

            total = int(data.get("totalRecords", len(records)))
            if not page_records or len(records) >= total or len(page_records) < MISSING_PAGE_SIZE:
                break
            page += 1

        # De-duplicate defensively in case Lidarr returns the same album more than once.
        unique: dict[int, dict[str, Any]] = {}
        for record in records:
            album_id = record.get("id")
            if album_id is None:
                album_id = record.get("albumId")
            if album_id is not None:
                unique[int(album_id)] = record

        return list(unique.values())

    def searches_last_24h(self) -> int:
        cutoff = self.now() - 86400
        row = self.db.execute(
            "SELECT COUNT(*) AS count FROM search_events WHERE searched_at >= ?",
            (cutoff,),
        ).fetchone()
        return int(row["count"])

    def recently_searched(self, album_id: int) -> bool:
        if ALBUM_COOLDOWN_HOURS <= 0:
            return False
        cutoff = self.now() - int(ALBUM_COOLDOWN_HOURS * 3600)
        row = self.db.execute(
            "SELECT 1 FROM album_state WHERE album_id = ? AND last_search >= ?",
            (album_id, cutoff),
        ).fetchone()
        return row is not None

    def select_candidates(self, albums: list[dict[str, Any]]) -> list[int]:
        candidates: list[int] = []
        for album in albums:
            album_id = album.get("id", album.get("albumId"))
            if album_id is None:
                continue
            album_id = int(album_id)
            if not self.recently_searched(album_id):
                candidates.append(album_id)
            if len(candidates) >= MAX_SEARCHES_PER_RUN:
                break
        return candidates

    def record_search_start(self, album_ids: list[int], command_id: int) -> None:
        now = self.now()
        for album_id in album_ids:
            self.db.execute(
                """
                INSERT INTO search_events(album_id, searched_at, command_id, result)
                VALUES (?, ?, ?, ?)
                """,
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
            """
            UPDATE search_events
            SET result = ?
            WHERE command_id = ? AND album_id = ?
            """,
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

    def run_search(self, album_ids: list[int]) -> bool:
        payload = {"name": "AlbumSearch", "albumIds": album_ids}
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

        self.record_search_result(album_ids, command_id, str(result), success)
        if success:
            log.info("Completed AlbumSearch command %s: %s", command_id, result)
        else:
            log.error("AlbumSearch command %s did not complete successfully: %s", command_id, result)
        return success

    def run_once(self) -> None:
        used_today = self.searches_last_24h()
        remaining_daily = max(0, MAX_SEARCHES_PER_DAY - used_today)
        if remaining_daily <= 0:
            log.info("Rolling 24h search limit reached: %d/%d", used_today, MAX_SEARCHES_PER_DAY)
            return

        missing = self.fetch_missing_albums()
        if not missing:
            log.info("No missing albums found.")
            return

        candidates = self.select_candidates(missing)
        candidates = candidates[:remaining_daily]
        if not candidates:
            log.info("No missing albums are outside the %sh cooldown.", ALBUM_COOLDOWN_HOURS)
            return

        log.info(
            "Missing albums: %d | eligible now: %d | rolling 24h searches: %d/%d",
            len(missing), len(candidates), used_today, MAX_SEARCHES_PER_DAY,
        )

        for offset in range(0, len(candidates), BATCH_SIZE):
            batch = candidates[offset : offset + BATCH_SIZE]
            self.run_search(batch)
            if offset + BATCH_SIZE < len(candidates):
                log.info("Waiting %ss before next batch", SEARCH_INTERVAL_SECONDS)
                time.sleep(SEARCH_INTERVAL_SECONDS)

    def run_forever(self) -> None:
        log.info("Lidarr Deezer Scheduler starting")
        log.info("Lidarr URL: %s", LIDARR_URL)
        log.info(
            "Config: interval=%ss batch=%d max/run=%d max/24h=%d cooldown=%sh",
            SEARCH_INTERVAL_SECONDS,
            BATCH_SIZE,
            MAX_SEARCHES_PER_RUN,
            MAX_SEARCHES_PER_DAY,
            ALBUM_COOLDOWN_HOURS,
        )

        while True:
            try:
                self.run_once()
            except requests.RequestException as exc:
                log.error("Lidarr API request failed: %s", exc)
            except Exception:
                log.exception("Unexpected scheduler error")

            time.sleep(SEARCH_INTERVAL_SECONDS)


if __name__ == "__main__":
    Scheduler().run_forever()
