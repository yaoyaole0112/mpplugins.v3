import copy
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from resourceflow import ResourceFlow
from resourceflow.record_search import matches_owner, matches_record, matches_media_type, record_page
from resourceflow.shared_coordinator import CoordinatorStore


class RecordSearchTests(unittest.TestCase):
    def setUp(self):
        self.row = {
            "title": "The.Show.1080p.mkv", "media_title": "玫瑰战争", "media_type": "tv",
            "tmdb_id": 123, "season": 1, "episodes": [5, 6], "label": "边城旧事",
            "specs": {"resolution": "1080p", "effects": "SDR"}, "state": "completed",
        }

    def test_titles_identity_specs_and_owner(self):
        for query in ("玫瑰 1080P", "the.show", "TMDB 123 S01E06", "边城旧事 SDR", "已完成", "ＴＨＥ"):
            with self.subTest(query=query):
                self.assertTrue(matches_record(self.row, query))
        for query in ("S01E07", "2160p", "玫瑰 HDR"):
            self.assertFalse(matches_record(self.row, query))

    def test_directed_owner_and_media_type_filters(self):
        request = {"download_owners": ["设备A"], "files": [self.row]}
        self.assertTrue(matches_owner(request, "设备A"))
        self.assertTrue(matches_owner(request, "设备a"))
        self.assertFalse(matches_owner(request, "设备B"))
        self.assertTrue(matches_media_type(request, "tv"))
        self.assertFalse(matches_media_type(request, "movie"))

    def test_nested_files_and_rejections(self):
        row = {"files": [self.row], "rejected": [{"owner": "朋友", "reason": "已存在"}],
               "task_name": "Test Site", "downloader": "QB"}
        self.assertTrue(matches_record(row, "玫瑰 S01E05 QB 朋友"))
        self.assertTrue(matches_record(row, "test site"))

    def test_private_payloads_not_searched_and_input_not_changed(self):
        row = {**self.row, "body": {"secret": "dont-find-me"}, "hash": "dont-find-me",
               "private_url": "dont-find-me", "specs": {**self.row["specs"], "private_url": "dont-find-me"}}
        original = copy.deepcopy(row)
        self.assertFalse(matches_record(row, "dont-find-me"))
        self.assertEqual(row, original)

    def test_literal_special_characters_not_sql_or_regex(self):
        for query in ("%", "_", ".*", "' OR 1=1 --"):
            self.assertFalse(matches_record(self.row, query))
        self.assertTrue(matches_record({"title": "100% literal_[x].mkv"}, "literal_[x]"))

    def test_empty_missing_optional_fields(self):
        self.assertTrue(matches_record({}, None))
        self.assertFalse(matches_record({"specs": None, "episodes": None}, "missing"))

    def test_legacy_invalid_identity_fields_do_not_break_search(self):
        for fields in ({"season": ""}, {"season": 1, "episodes": ["unknown"]}, {"specs": []}):
            self.assertTrue(matches_record({"title": "Legacy", **fields}, "legacy"))
        self.assertFalse(matches_record({}, "剧集"))
        self.assertTrue(matches_record({"rejected": [{"reason": "同规格已占位或已完成"}]}, "资源已完成/已下载"))

    def test_paging_bounds_and_empty_page(self):
        self.assertEqual(record_page([], 99)["page"], 1)
        self.assertEqual(len(record_page(list(range(130)), 0, 999)["items"]), 100)
        self.assertEqual(record_page(list(range(21)), 2)["items"], [20])


class ApprovalSearchTests(unittest.TestCase):
    def setUp(self):
        self.plugin = ResourceFlow.__new__(ResourceFlow)
        self.plugin._shared_lock = threading.RLock()
        self.plugin._shared_claims = {}
        self.plugin._task_configs = {"test": SimpleNamespace(name="站点")}
        self.plugin._shared_store = None
        self.plugin._inflight_lock = threading.RLock()
        self.plugin._resource_ledger = {}
        self.plugin._identity_pending = {}
        self.plugin.save_data = Mock()
        for index in range(125):
            row = {"request_id": str(index), "title": f"Show {index}", "phase": "queued",
                   "task_id": "test", "updated": index, "body": {"url": "private-passkey"},
                   "files": [{"media_type": "tv", "media_title": "玫瑰战争", "tmdb_id": 123,
                              "season": 1, "episodes": [index + 1], "specs": {"resolution": "1080p"}}]}
            self.plugin._shared_claims[str(index)] = row
            self.plugin._resource_ledger[str(index)] = {
                **row["files"][0], "title": row["title"], "remembered_at": index}
            self.plugin._identity_pending[str(index)] = {**row["files"][0], "title": row["title"]}

    def test_requests_search_before_paging_beyond_recent_100(self):
        response = self.plugin.get_shared_records(query="S01E01")
        self.assertTrue(response.success)
        self.assertEqual(response.data["total"], 1)
        self.assertEqual(response.data["items"][0]["request_id"], "0")
        self.assertNotIn("private-passkey", str(response.data))

    def test_request_phase_filter_and_paging(self):
        self.plugin._shared_claims["0"]["phase"] = "rejected"
        result = self.plugin.get_shared_records(status="queued", page=2).data
        self.assertEqual(result["total"], 124)
        self.assertEqual(len(result["items"]), 20)
        self.assertEqual(self.plugin.get_shared_records(status="rejected").data["total"], 1)
        self.assertEqual(self.plugin.get_shared_records(query="private-passkey").data["total"], 0)

    def test_download_owner_and_media_type_filters_apply_before_paging(self):
        for index in range(125):
            row = self.plugin._shared_claims[str(index)]
            row["approved"] = {"approved": "claim"} if index % 2 else {}
            row["rejected"] = ([{"id": "duplicate", "owner": "设备B", "reason": "同规格已占位或已完成",
                                 "state": "queued"}] if index % 2 == 0 else [])
            row["files"][0]["media_type"] = "movie" if index == 1 else "tv"
        self.plugin._shared_settings = SimpleNamespace(instance_label="设备A", enabled=False)
        result = self.plugin.get_shared_records(owner="设备B", media_type="tv", page=1, page_size=10).data
        self.assertEqual(result["total"], 63)
        self.assertTrue(all(row["files"][0]["media_type"] == "tv" for row in result["items"]))
        own = self.plugin.get_shared_records(owner="设备A", media_type="movie").data
        self.assertEqual(own["total"], 1)
        self.assertEqual(own["items"][0]["request_id"], "1")
        self.assertIn("设备A", own["owners"])

    def test_filtered_label_and_reasons_searchable_with_legacy_alias(self):
        self.plugin._shared_claims["0"].update(phase="rejected", error="规格数值无效", rejected=[{"id": "legacy"}])
        self.plugin._shared_claims["1"].update(phase="rejected", rejected=[
            {"id": "file", "reason": "同规格已占位或已完成", "state": "completed", "owner": "边城旧事"}])
        for query, request_id in (("已过滤 规格数值无效", "0"), ("已排除 资源已下载 边城旧事", "1")):
            result = self.plugin.get_shared_records(query=query).data
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["items"][0]["request_id"], request_id)
        status = self.plugin.get_shared_records(query="规格数值无效").data["items"][0]
        self.assertEqual(status["error"], "规格数值无效")

    def test_requests_created_sort_across_pages_even_after_old_updates(self):
        for index, created, updated in ((0, 10, 80), (1, 20, 30), (2, 20, 40)):
            self.plugin._shared_claims[str(index)].update(created=created, updated=updated)
        pages = [self.plugin.get_shared_records(page=page, page_size=1).data for page in (1, 2, 3)]
        self.assertEqual([page["items"][0]["request_id"] for page in pages], ["1", "2", "0"])

    def test_request_names_reused_by_tmdb_and_untranslated_titles_fall_back(self):
        unlocalized = self.plugin._shared_claims["0"]["files"][0]
        unlocalized.pop("media_title")
        self.assertEqual(self.plugin.get_shared_records(query="S01E01 玫瑰").data["items"][0]["files"][0]["media_title"],
                         "玫瑰战争")
        unlocalized.update(tmdb_id=340280, path="Gordon.S01E07.1080p.mkv")
        self.assertEqual(self.plugin.get_shared_records(query="Gordon").data["items"][0]["files"][0]["media_title"],
                         "Gordon")
        self.assertNotIn("media_title", unlocalized)

    def test_shared_display_page_uses_gordon_fallback_without_mutating_records(self):
        page = {"items": [{"title": "Gordon S01 1080p NF WEB-DL DDP 5.1 Atmos H.264-CHDWEB", "media_title": ""}]}
        display = self.plugin._shared_record_display_page(page, "records")
        self.assertEqual(display["items"][0]["media_title"], "Gordon")
        self.assertEqual(page["items"][0]["media_title"], "")

    def test_requests_identity_task_and_specs_combined(self):
        result = self.plugin.get_shared_records(query="站点 玫瑰 S01E05 1080p").data
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["task_name"], "站点")

    def test_non_coordinator_empty_and_invalid_kind(self):
        self.assertEqual(self.plugin.get_shared_records(kind="records").data["items"], [])
        self.assertFalse(self.plugin.get_shared_records(kind="private").success)

    def test_ledger_and_pending_search_before_paging(self):
        for state in ("ledger", "pending"):
            result = self.plugin.get_resource_ledger(state=state, query="玫瑰 S01E01", page_size=20).data
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["items"][0]["title"], "Show 0")

    def test_media_type_filter_on_both_tabs(self):
        self.plugin._resource_ledger["movie"] = {"title": "Movie", "media_type": "movie"}
        self.plugin._identity_pending["movie"] = {"title": "Unknown Movie", "media_type": "movie"}
        for state in ("ledger", "pending"):
            self.assertEqual(self.plugin.get_resource_ledger(state=state, media_type="tv").data["total"], 125)
            self.assertEqual(self.plugin.get_resource_ledger(state=state, media_type="movie").data["total"], 1)
            self.assertEqual(self.plugin.get_resource_ledger(state=state, query="missing").data["total"], 0)

    def test_legacy_titles_enriched_from_verified_claims_without_changing_ledger_or_identity(self):
        self.plugin.chain = SimpleNamespace(recognize_media=Mock(side_effect=AssertionError("No recognition calls")))
        for state, records in (("ledger", self.plugin._resource_ledger), ("pending", self.plugin._identity_pending)):
            for row in records.values():
                row.pop("media_title")
            original = copy.deepcopy(records)
            result = self.plugin.get_resource_ledger(state=state, query="玫瑰 S01E01").data
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["items"][0]["media_title"], "玫瑰战争")
            self.assertEqual(records, original)
        self.plugin.chain.recognize_media.assert_not_called()
        self.plugin.save_data.assert_not_called()

    def test_titles_never_cross_movie_tv_identity_and_missing_id_stays_pending(self):
        self.plugin._shared_media_title_cache = {("movie", 123): "电影标题"}
        self.plugin._shared_claims = {}
        self.plugin._identity_pending = {"unknown": {"path": "Unknown.S01E01.mkv", "media_type": "tv",
                                                    "season": 1, "episodes": [1]}}
        for row in self.plugin._resource_ledger.values():
            row.pop("media_title")
        self.assertEqual(self.plugin.get_resource_ledger().data["items"][0]["media_title"], "")
        result = self.plugin.get_resource_ledger(state="pending").data["items"][0]
        self.assertEqual(result["media_title"], "")
        self.assertNotIn("tmdb_id", result)

    def test_pending_chinese_filename_is_only_display_hint(self):
        self.plugin._identity_pending = {"unknown": {"path": "测试剧集.S01E05.1080p.mkv", "media_type": "tv",
                                                    "season": 1, "episodes": [5]}}
        result = self.plugin.get_resource_ledger(state="pending").data["items"][0]
        self.assertEqual(result["media_title"], "测试剧集")
        self.assertNotIn("tmdb_id", result)
        self.assertNotIn("media_title", self.plugin._identity_pending["unknown"])

    def test_localized_store_titles_and_existing_title_priority(self):
        self.plugin._shared_claims = {}
        self.plugin._shared_store = SimpleNamespace(media_titles=lambda: {("tv", 123): "共享中文剧名"})
        self.plugin._resource_ledger["0"]["media_title"] = "记录自身中文名"
        self.plugin._resource_ledger["1"].pop("media_title")
        self.assertEqual(self.plugin.get_resource_ledger(query="记录自身中文名").data["total"], 1)
        self.assertEqual(self.plugin.get_resource_ledger(query="共享中文剧名").data["total"], 1)


class StoreSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = CoordinatorStore(Path(self.temp.name) / "store.sqlite3", "resources", ["resolution"], 5)
        with self.store.connect() as db:
            db.executemany(
                "INSERT INTO claims VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(str(index), "resources" if index < 125 else "other", 11, "instance-secret", "边城旧事",
                  "request-secret", "file", "tv", 123, 1, f"[{index + 1}]",
                  '{"resolution":"1080p","effects":"HDR Vivid"}', f"Show {index}", "玫瑰战争",
                  "queued" if index else "completed", index, index)
                 for index in range(126)])

    def test_store_search_beyond_recent_100_and_private_projection(self):
        self.assertEqual(len(self.store.recent()), 100)
        result = self.store.search_records(query="玫瑰 S01E01")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["id"], "0")
        self.assertEqual(result["items"][0]["specs"]["effects"], "HDR")
        for secret in ("instance-secret", "request-secret"):
            self.assertEqual(self.store.search_records(query=secret)["total"], 0)
            self.assertNotIn(secret, str(result))

    def test_store_status_filter_pagination_and_group_isolation(self):
        self.assertEqual(self.store.search_records(status="queued")["total"], 124)
        self.assertEqual(self.store.search_records(status="completed")["total"], 1)
        self.assertEqual(self.store.search_records(query="S01E126")["total"], 0)
        page = self.store.search_records(page=2, page_size=20)
        self.assertEqual(page["total"], 125)
        self.assertEqual(len(page["items"]), 20)
        self.assertEqual(page["items"][0]["id"], "104")
        self.assertEqual(self.store.search_records(query="' OR 1=1 --")["total"], 0)
        self.assertEqual(self.store.search_records(query="边城旧事 1080P")["total"], 125)

    def test_store_owner_filter_is_applied_before_pagination_and_returns_all_owners(self):
        with self.store.connect() as db:
            db.execute("UPDATE claims SET label='设备A' WHERE id='0'")
            db.execute("UPDATE claims SET label='设备B' WHERE id='1'")
        result = self.store.search_records(owner="设备B", page=1, page_size=1)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["id"], "1")
        self.assertEqual(result["owners"], ["设备A", "设备B", "边城旧事"])

    def test_media_titles_are_group_scoped_and_latest_wins(self):
        with self.store.connect() as db:
            db.execute("UPDATE claims SET media_title='其他共享组' WHERE group_id='other'")
            db.execute("UPDATE claims SET media_title='最新中文名称' WHERE id='124'")
        self.assertEqual(self.store.media_titles(), {("tv", 123): "最新中文名称"})

    def test_sort_on_download_time_and_search_reused_chinese_title(self):
        with self.store.connect() as db:
            db.execute("UPDATE claims SET updated=10000,media_title='' WHERE id='123'")
            db.execute("UPDATE claims SET media_title='' WHERE id='124'")
        first = self.store.search_records(query="玫瑰 1080p", page=1, page_size=1)["items"][0]
        second = self.store.search_records(query="玫瑰 1080p", page=2, page_size=1)["items"][0]
        self.assertEqual((first["id"], second["id"]), ("124", "123"))
        self.assertEqual(first["media_title"], "玫瑰战争")


if __name__ == "__main__":
    unittest.main()
