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
        instance.db.execute("CREATE TABLE album_state (album_id INTEGER PRIMARY KEY, last_search INTEGER)")
        instance.db.commit()
        instance.missing_cache = []
        instance.missing_total = 0
        instance.missing_cache_time = 0.0
        return instance

    def test_fetches_one_page_and_persists_rotating_cursor(self):
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
        self.assertEqual(instance.get_kv("missing_page"), "3")
        self.assertGreater(instance.missing_cache_time, 0)

    def test_wraps_cursor_after_last_page(self):
        instance = self.make_scheduler()
        instance.set_kv("missing_page", "3")
        instance.get_json = Mock(return_value={"totalRecords": 5, "records": [{"id": 5, "artistId": 1}]})

        with patch.object(scheduler, "MISSING_PAGE_SIZE", 2), patch.object(scheduler, "SEARCH_ORDER", "random"):
            instance.refresh_missing_cache()

        self.assertEqual(instance.get_kv("missing_page"), "1")
        self.assertEqual(instance.missing_cache, [5])
        instance.get_json.assert_called_once()


if __name__ == "__main__":
    unittest.main()
