from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import Mock
import uuid

from resourceflow.shared_cache import (
    ACTIVE_MAX_AGE, COMPLETED_MAX_AGE, HIGH_RESOLUTION_WAIT, SYNC_MAX_PARTS, SharedRecordCache,
)
from resourceflow.shared_coordinator import CoordinatorStore
from resourceflow.shared_protocol import RULE_DEFAULTS, SharedError, canonical, encode
import test_shared_arbitration as arbitration
from test_shared_arbitration import file_row


class SharedRecordCacheTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = CoordinatorStore(self.root / "coordinator.sqlite3", "resources", ["resolution"], 5, RULE_DEFAULTS)
        self.cache = SharedRecordCache(self.root / "cache.sqlite3")
        self.instance = "a" * 32
        self.scope = "example-scope"
        self.now = time.time()

    def send(self, op, instance=None, sender=11, **body):
        return self.store.handle(sender, uuid.uuid4().hex, {
            "v": 1, "group": self.store.group, "instance": instance or self.instance, "label": "NAS-A", "op": op,
            "revision": self.store.policy["revision"], **body,
        })

    def reserve(self, *files, **kwargs):
        reply = self.send("reserve", files=list(files), pack_policy=self.store.policy["rules"]["pack_policy"], **kwargs)
        self.assertTrue(reply["ok"], reply)
        return reply

    def sync(self, **kwargs):
        cursor = self.cache.progress(self.scope)["cursor"]
        response = self.send("sync_records", cursor=cursor, authority_id=self.store.authority()["authority_id"], **kwargs)
        self.assertTrue(response["ok"], response)
        self.assertLessEqual(len(encode(response, "f" * 32, response=True)), SYNC_MAX_PARTS)
        self.cache.apply(self.scope, cursor, response, self.now)
        return response

    def match(self, *files, age=0):
        return self.cache.filter(self.scope, list(files), self.store.policy, self.now + age)

    def test_existing_cache_and_authority_records_match_canonical_specs(self):
        self.store = CoordinatorStore(self.store.path, "resources", ["resolution", "effects"], 5, RULE_DEFAULTS)
        claim = self.reserve(file_row(resolution="2160p", effects="HDR"))["approved"][0]["claim"]
        legacy_specs = {"resolution": "4K", "effects": "HDR10+"}
        with self.store.connect() as db:
            db.execute("UPDATE claims SET specs=? WHERE id=?", (canonical(legacy_specs), claim))
        snapshot = self.send("sync_records", cursor=0, authority_id=self.store.authority()["authority_id"])
        self.assertEqual(snapshot["changes"][0]["record"]["specs"], {"resolution": "2160p", "effects": "HDR"})
        snapshot["changes"][0]["record"]["specs"] = legacy_specs
        self.cache.apply(self.scope, 0, snapshot, self.now)

        candidate = file_row(resolution="2160p", effects="HDR Vivid")
        self.assertTrue(self.match(candidate)[1])
        self.assertEqual(self.cache.search(self.scope, "2160p", "", 1, 20, "")["total"], 1)
        self.assertFalse(self.reserve(candidate, instance="b" * 32, sender=22)["approved"])
        distinct = file_row(resolution="4k", effects="Dolby Vision Profile 8")
        self.assertFalse(self.match(distinct)[1])
        self.assertTrue(self.reserve(distinct, instance="b" * 32, sender=22)["approved"])
        self.assertIn({"resolution": "2160p", "effects": "DV"}, [row["specs"] for row in self.store.recent()])

    def test_snapshot_resume_and_filter_identical_to_authority(self):
        for start in range(1, 202, 50):
            self.reserve(*(dict(file_row(number, number), title=uuid.uuid4().hex * 3)
                           for number in range(start, start + 50)))
        first = self.sync()
        self.assertTrue(first["more"])
        self.assertFalse(self.match(file_row())[1])
        self.cache = SharedRecordCache(self.cache.path)
        self.assertEqual(self.cache.progress(self.scope)["cursor"], first["cursor"])
        while self.sync()["more"]:
            pass
        self.assertEqual(self.cache.search(self.scope, "", "reserved", 1, 20, "tv")["total"], 250)
        candidates = [file_row(1, 1, effects="HDR"), file_row(2, 2, resolution="2160p"), file_row(3, 999)]
        available, blocked = self.match(*candidates)
        self.assertEqual((available, blocked), self.store.filter_duplicates(candidates))
        self.assertEqual([item[0]["episodes"] for item in blocked], [[1]])
        self.assertEqual(self.sync()["changes"], [])

    def test_release_and_delete_remove_cached_block(self):
        claim = self.reserve(file_row())["approved"][0]["claim"]
        self.sync()
        self.assertTrue(self.match(file_row())[1])
        self.assertTrue(self.send("update", updates=[{"claim": claim, "state": "released"}], confirmed_absent=True)["ok"])
        released = self.sync()
        self.assertEqual(released["changes"][0]["record"]["state"], "released")
        self.assertFalse(self.match(file_row())[1])
        self.assertEqual(self.cache.search(self.scope, "", "released", 1, 20, "")["total"], 1)
        with self.store.connect() as db:
            db.execute("DELETE FROM claims WHERE id=?", (claim,))
        self.assertIsNone(self.sync()["changes"][0]["record"])
        self.assertEqual(self.cache.search(self.scope, "", "released", 1, 20, "")["total"], 0)
        self.assertEqual(len(self.reserve(file_row())["approved"]), 1)

    def test_completed_2160p_mirror_blocks_lower_resolution_per_episode(self):
        self.store = CoordinatorStore(self.store.path, "resources", ["effects"], 5, RULE_DEFAULTS)
        high = file_row(resolution="4K", effects="SDR")
        claim = self.reserve(high)["approved"][0]["claim"]
        self.send("update", updates=[{"claim": claim, "state": "queued"}])
        self.send("update", updates=[{"claim": claim, "state": "completed"}])
        self.sync()

        blocked = self.match(file_row(2, 1, resolution="720p", effects="HDR"))[1]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0][1]["_match_reason"], "已有2160p/4K高分辨率资源")
        self.assertFalse(self.match(file_row(3, 2, resolution="720p", effects="HDR"))[1])

    def test_active_2160p_mirror_holds_lower_only_while_fresh_and_not_released(self):
        claim = self.reserve(file_row(resolution="4K"))["approved"][0]["claim"]
        low = file_row(2, 1, resolution="720p")
        unrelated = file_row(3, 2, resolution="720p")
        for state in ("reserved", "queued"):
            self.sync()
            available, blocked = self.match(low, unrelated)
            self.assertEqual((available, blocked), self.store.filter_duplicates([low, unrelated]))
            self.assertEqual(available, [unrelated])
            self.assertEqual(blocked[0][1]["state"], state)
            self.assertEqual(blocked[0][1]["_match_reason"], HIGH_RESOLUTION_WAIT)
            self.assertEqual(self.match(low, age=ACTIVE_MAX_AGE), ([low], []))
            if state == "reserved":
                self.assertTrue(self.send("update", updates=[{"claim": claim, "state": "queued"}])["ok"])
        self.assertTrue(self.send("update", confirmed_absent=True,
                                  updates=[{"claim": claim, "state": "released"}])["ok"])
        self.sync()
        self.assertEqual(self.match(low), ([low], []))

    def test_mirror_display_time_order_and_search_by_reused_localized_name(self):
        old, new = [row["claim"] for row in self.reserve(dict(file_row(), media_title="剧集中文名"),
                                                          dict(file_row(2, 2), media_title=""))["approved"]]
        with self.store.connect() as db:
            db.execute("UPDATE claims SET created=10,updated=90 WHERE id=?", (old,))
            db.execute("UPDATE claims SET created=20,updated=30 WHERE id=?", (new,))
        self.sync()
        first = self.cache.search(self.scope, "剧集中文名", "", 1, 1, "tv")["items"][0]
        second = self.cache.search(self.scope, "剧集中文名", "", 2, 1, "tv")["items"][0]
        self.assertEqual((first["id"], second["id"]), (new, old))
        self.assertEqual(first["media_title"], "剧集中文名")
        self.assertEqual(self.cache.media_titles("other-scope"), {})

    def test_policy_mapping_syncs_and_matches_existing_mirror_and_coordinator_history(self):
        original = {
            **file_row(), "tmdb_id": 336181, "media_title": "This Is 炫茂路",
            "title": "This Is Hyun Moo Road S01E01 1080p",
            "specs": {"resolution": "1080p", "effects": "SDR"},
        }
        claim = self.reserve(original)["approved"][0]["claim"]
        self.assertTrue(self.send("update", updates=[{"claim": claim, "state": "queued"}])["ok"])
        self.assertTrue(self.send("update", updates=[{"claim": claim, "state": "completed"}])["ok"])

        self.store = CoordinatorStore(
            self.store.path, "resources", ["resolution"], 5, RULE_DEFAULTS,
            {"TMDB 336181": 336292}, {},
        )
        self.sync()
        duplicate = {
            **file_row(2, 1), "tmdb_id": 336292, "media_title": "This Is 炫茂路",
            "title": "This Is Hyun Moo Road S01E01 1080p",
            "specs": {"resolution": "1080p", "effects": "SDR"},
        }
        available, blocked = self.match(duplicate)
        self.assertEqual(available, [])
        self.assertEqual(blocked[0][1]["id"], claim)
        self.assertEqual(self.reserve(duplicate)["rejected"][0]["claim"], claim)

    def test_mapping_chains_apply_to_series_but_not_movies(self):
        aliases = {"TMDB 336181": 336292, "TMDB 336292": 336293}
        self.store = CoordinatorStore(
            self.store.path, "resources", ["resolution"], 5, RULE_DEFAULTS, aliases, {},
        )
        original = {**file_row(), "tmdb_id": 336181}
        claim = self.reserve(original)["approved"][0]["claim"]
        canonical_series = {**file_row(2), "tmdb_id": 336293}
        result = self.reserve(canonical_series, instance="b" * 32, sender=22)
        self.assertEqual(result["rejected"][0]["claim"], claim)

        original_movie = {
            **file_row(3), "media_type": "movie", "tmdb_id": 336181,
            "season": None, "episodes": [],
        }
        canonical_movie = {
            **original_movie, "id": f"{4:024x}", "tmdb_id": 336293,
        }
        self.assertEqual(len(self.reserve(original_movie)["approved"]), 1)
        self.sync()
        available, blocked = self.match(canonical_series, canonical_movie)
        self.assertEqual([row["id"] for row in available], [canonical_movie["id"]])
        self.assertEqual([row[0]["id"] for row in blocked], [canonical_series["id"]])

        self.assertEqual(len(self.reserve(
            canonical_movie, instance="c" * 32, sender=33,
        )["approved"]), 1)

    def test_mirror_displays_chinese_name_from_original_title_without_changing_dedup(self):
        candidate = dict(file_row(), tmdb_id=338938, media_title="Miss You",
                         title="后会友期.I.Miss.You.S01E01.2026.2160p.MGTV.WEB-DL.H265.AAC-ADWeb.mkv")
        self.reserve(candidate)
        self.sync()
        row, = self.cache.search(self.scope, "后会友期", "", 1, 20, "tv")["items"]
        self.assertEqual((row["media_title"], row["tmdb_id"]), ("后会友期", 338938))
        self.assertTrue(self.match(candidate)[1])
        self.assertEqual(self.cache.media_titles(self.scope), {})

    def test_mirror_owner_filter_happens_before_paging_and_returns_all_owners(self):
        self.reserve(file_row())
        second = self.reserve(file_row(2, 2), instance="b" * 32, sender=22)
        with self.store.connect() as db:
            db.execute("UPDATE claims SET label='NAS-B' WHERE id=?", (second["approved"][0]["claim"],))
        self.sync()
        result = self.cache.search(self.scope, "", "", 1, 20, "", owner="NAS-B")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["label"], "NAS-B")
        self.assertEqual(result["owners"], ["NAS-A", "NAS-B"])

    def test_site_name_is_stored_synced_searchable_and_optional_for_old_records(self):
        self.reserve(file_row(), site_name="憨憨")
        self.reserve(file_row(2, 2))
        self.sync()
        result = self.cache.search(self.scope, "憨憨", "", 1, 20, "tv")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["site_name"], "憨憨")
        self.assertEqual(self.store.search_records(query="S01E02")["items"][0]["site_name"], "")

    def test_expiry_and_scope_fail_open_only_to_reserve(self):
        completed, active = [row["claim"] for row in self.reserve(file_row(), file_row(2, 2))["approved"]]
        self.send("update", updates=[{"claim": completed, "state": "completed"}, {"claim": active, "state": "queued"}])
        self.sync()
        self.assertEqual(len(self.match(file_row(), file_row(2, 2))[1]), 2)
        self.assertEqual([row[1]["state"] for row in self.match(file_row(), file_row(2, 2), age=ACTIVE_MAX_AGE)[1]],
                         ["completed"])
        self.assertFalse(self.match(file_row(), age=COMPLETED_MAX_AGE)[1])
        self.assertFalse(self.cache.filter("different-scope", [file_row()], self.store.policy, self.now)[1])
        self.assertFalse(self.reserve(file_row(), instance="b" * 32, sender=22)["approved"])

    def test_scope_reset_and_malformed_page_are_atomic(self):
        self.reserve(file_row())
        first = self.sync()
        with self.assertRaises(SharedError):
            self.cache.apply(self.scope, first["cursor"], {**first, "reset": True, "cursor": first["cursor"] + 1}, self.now)
        self.assertEqual(self.cache.progress(self.scope)["cursor"], first["cursor"])
        self.assertTrue(self.match(file_row())[1])
        result = self.sync(revision="outdated")
        self.assertTrue(result["reset"])
        self.assertTrue(self.match(file_row())[1])

    def test_both_clients_can_miss_but_only_one_can_reserve(self):
        self.sync()
        other_cache = SharedRecordCache(self.root / "another-cache.sqlite3")
        result = self.send("sync_records", cursor=0, authority_id=self.store.authority()["authority_id"])
        other_cache.apply(self.scope, 0, result, self.now)
        barrier = threading.Barrier(2)

        def attempt(cache, instance, sender):
            self.assertFalse(cache.filter(self.scope, [file_row()], self.store.policy, self.now)[1])
            barrier.wait(timeout=5)
            return bool(self.reserve(file_row(), instance=instance, sender=sender)["approved"])

        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = [pool.submit(attempt, cache, instance, sender) for cache, instance, sender in
                       [(self.cache, "b" * 32, 22), (other_cache, "c" * 32, 33)]]
            self.assertEqual(sum(future.result() for future in replies), 1)

    def client(self):
        fixture = arbitration.AdmissionTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        plugin = fixture.plugin
        plugin._shared_instance_id = "b" * 32
        plugin._shared_cache = self.cache
        plugin._shared_probe = {**self.store.authority(), "policy": self.store.policy}
        plugin._shared_cache_activity = time.time()
        plugin._channel_dedup_pack_policy = self.store.policy["rules"]["pack_policy"]
        for candidate in plugin._shared_candidates.return_value[0]:
            candidate["media_title"] = "测试剧集"

        def exchange(request_id, body):
            reply = self.store.handle(22, request_id, {"v": 1, "group": "resources", "instance": "b" * 32, **body})
            if not reply["ok"]:
                raise SharedError(reply["error"])
            return reply

        plugin._shared_exchange = Mock(side_effect=exchange)
        plugin._sync_shared_record_cache()
        plugin._shared_exchange.reset_mock()
        return fixture, plugin

    def test_partial_pack_only_sends_missing_file_and_list_uses_local_cache(self):
        self.reserve(file_row())
        fixture, plugin = self.client()
        files = fixture.downloader.get_files.return_value
        files[0]["priority"], files[1]["priority"] = 0, 1
        page = plugin.get_shared_records(kind="records", query="NAS-A")
        self.assertEqual(page.data["total"], 1)
        plugin._shared_exchange.assert_not_called()
        self.assertEqual(fixture.add(), "a" * 40)
        requests = [call.args[1] for call in plugin._shared_exchange.call_args_list]
        self.assertEqual([row["op"] for row in requests], ["reserve", "update"])
        self.assertEqual([row["episodes"] for row in requests[0]["files"]], [[2]])
        self.assertEqual(plugin._add_local_prechecked_torrent.call_args.args[6]["accepted"], {"Show.S01E02.mkv"})
        fixture.downloader.start_torrents.assert_called_once()

    def test_client_waits_for_4k_release_then_retries_same_pack(self):
        high = self.reserve(file_row(resolution="2160p"))["approved"][0]["claim"]
        fixture, plugin = self.client()
        self.assertIsNone(fixture.add())
        plugin._shared_exchange.assert_not_called()
        plugin._add_local_prechecked_torrent.assert_not_called()

        self.assertTrue(self.send("update", confirmed_absent=True,
                                  updates=[{"claim": high, "state": "released"}])["ok"])
        plugin._shared_cache_next_sync = 0
        plugin._sync_shared_record_cache()
        plugin._shared_exchange.reset_mock()
        files = fixture.downloader.get_files.return_value
        files[0]["priority"], files[1]["priority"] = 1, 1
        self.assertEqual(fixture.add(), "a" * 40)
        requests = [call.args[1] for call in plugin._shared_exchange.call_args_list]
        self.assertEqual([row["op"] for row in requests], ["reserve", "update"])
        self.assertEqual([row["episodes"] for row in requests[0]["files"]], [[1], [2]])
        fixture.downloader.start_torrents.assert_called_once()

    def test_cache_hits_send_nothing_and_failed_sync_falls_back(self):
        self.reserve(file_row(), file_row(2, 2))
        fixture, plugin = self.client()
        self.assertIsNone(fixture.add())
        plugin._shared_exchange.assert_not_called()
        exchange = plugin._shared_exchange.side_effect

        def offline_sync(request_id, body):
            if body["op"] == "sync_records":
                raise SharedError("offline")
            return exchange(request_id, body)

        plugin._shared_exchange.side_effect = offline_sync
        plugin._shared_cache_next_sync = 0
        plugin._sync_shared_record_cache()
        self.assertIsNone(fixture.add())
        self.assertEqual([call.args[1]["op"] for call in plugin._shared_exchange.call_args_list],
                         ["sync_records", "reserve"])
        plugin._add_local_prechecked_torrent.assert_not_called()

    def test_coordinator_local_lookup_uses_authority_instead_of_mirror(self):
        fixture, plugin = self.client()
        # Both claims were added after the client's last mirror sync.
        self.reserve(file_row(), file_row(2, 2))
        plugin._shared_settings = plugin._shared_settings.model_copy(update={"host_coordinator": True})
        plugin._shared_store = self.store
        self.assertIsNone(fixture.add())
        plugin._shared_exchange.assert_not_called()
        plugin._add_local_prechecked_torrent.assert_not_called()

    def test_old_coordinator_does_not_receive_sync_requests(self):
        _, plugin = self.client()
        plugin._shared_probe.pop("record_sync_version")
        plugin._shared_cache_next_sync = 0
        plugin._sync_shared_record_cache()
        plugin._shared_exchange.assert_not_called()
