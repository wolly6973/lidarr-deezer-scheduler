import json
import sqlite3
import unittest
from unittest.mock import Mock, patch

import scheduler


class MissingCacheRefreshTests(unittest.TestCase):
    def make_scheduler(self):
        instance = scheduler.Scheduler.__new__(scheduler.Scheduler)
        instance.db = sqlite3.connect(":memory:")
        instance.db.row_factory = sqlite3.Row
        instance.db.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)")
        instance.db.execute("""
            CREATE TABLE album_state (
                album_id INTEGER PRIMARY KEY,
                last_search INTEGER,
                search_count INTEGER NOT NULL DEFAULT 0,
                last_result TEXT,
                last_success INTEGER
            )
        """)
        instance.db.execute("""
            CREATE TABLE search_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                album_id INTEGER NOT NULL,
                searched_at INTEGER NOT NULL,
                command_id INTEGER,
                result TEXT
            )
        """)
        instance.db.commit()
        instance.missing_cache = []
        instance.missing_total = 0
        instance.missing_cache_time = 0.0
        return instance

    def test_fetches_one_page_and_keeps_current_cursor(self):
        instance = self.make_scheduler()
        instance.set_kv("missing_page", "2")
        instance.get_json = Mock(return_value={
            "totalRecords": 5,
            "records": [
                {"id": 3, "artistId": 10, "artist": {"id": 10, "added": "2024-01-01T00:00:00Z"}},
                {"id": 4, "artistId": 20, "artist": {"id": 20, "added": "2025-01-01T00:00:00Z"}},
            ],
        })

        with patch.object(scheduler, "MISSING_PAGE_SIZE", 2), patch.object(scheduler, "SEARCH_ORDER", "newest_artist"):
            instance.refresh_missing_cache()

        instance.get_json.assert_called_once_with(
            "wanted/missing",
            params={"page": 2, "pageSize": 2, "sortKey": "title", "sortDirection": "ascending"},
            timeout=90,
        )
        self.assertEqual(instance.missing_cache, [4, 3])
        self.assertEqual(instance.missing_total, 5)
        self.assertEqual(instance.get_kv("missing_page"), "2")
        self.assertGreater(instance.missing_cache_time, 0)

    def test_keeps_cursor_on_page_until_page_is_consumed(self):
        instance = self.make_scheduler()
        instance.set_kv("missing_page", "3")
        instance.get_json = Mock(return_value={"totalRecords": 5, "records": [{"id": 5, "artistId": 1}]})

        with patch.object(scheduler, "MISSING_PAGE_SIZE", 2), patch.object(scheduler, "SEARCH_ORDER", "random"):
            instance.refresh_missing_cache()

        self.assertEqual(instance.get_kv("missing_page"), "3")
        self.assertEqual(instance.missing_cache, [5])
        instance.get_json.assert_called_once()

    def test_advances_only_after_cached_page_is_exhausted(self):
        instance = self.make_scheduler()
        instance.set_kv("missing_page", "2")
        now = scheduler.time.time()
        instance.db.executemany(
            "INSERT INTO album_state(album_id, last_search) VALUES (?, ?)",
            [(3, int(now)), (4, int(now))],
        )
        instance.db.commit()
        instance.missing_cache = [3, 4]
        instance.missing_total = 5
        instance.missing_cache_time = now - 360 * 60 - 10
        instance.get_json = Mock(return_value={
            "totalRecords": 5,
            "records": [{"id": 5, "artistId": 30, "artist": {"id": 30, "added": "2025-01-01T00:00:00Z"}}],
        })

        with patch.object(scheduler, "MISSING_PAGE_SIZE", 2), patch.object(scheduler, "MISSING_REFRESH_MINUTES", 360), patch.object(scheduler, "SEARCH_ORDER", "newest_artist"):
            candidates, total = instance.fetch_missing_candidates(1)

        self.assertEqual(candidates, [5])
        self.assertEqual(total, 5)
        self.assertEqual(instance.get_kv("missing_page"), "3")
        instance.get_json.assert_called_once_with(
            "wanted/missing",
            params={"page": 3, "pageSize": 2, "sortKey": "title", "sortDirection": "ascending"},
            timeout=90,
        )
 
    def test_search_start_persists_page_progress_and_removes_attempted_ids(self):
        instance = self.make_scheduler()
        instance.missing_cache = [3, 4, 5]
        instance.set_kv("missing_page_seen", "[]")

        instance.record_search_start([3, 4], 123)

        self.assertEqual(instance.missing_cache, [5])
        self.assertEqual(json.loads(instance.get_kv("missing_page_seen")), [3, 4])


if __name__ == "__main__":
    unittest.main()
