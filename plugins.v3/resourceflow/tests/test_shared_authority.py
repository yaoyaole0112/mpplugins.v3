from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from resourceflow import ResourceFlow
from resourceflow.shared_coordinator import CoordinatorStore
from resourceflow.shared_history import completion_rows
from resourceflow.shared_models import SharedSettingsPayload
from resourceflow.shared_protocol import RULE_DEFAULTS, SharedError


def evidence(episode=21, **changes):
    return {
        "media_type": "tv", "tmdb_id": 279155, "season": 1, "episodes": [episode],
        "media_title": "谷雨", "path": f"Show.S01E{episode}.2160p.HDR.mkv",
        "specs": {"resolution": "2160p", "effects": "HDR", "fps": 25, "size": 1000},
        "evidence_source": "completed", "remembered_at": time.time() - 86400, **changes,
    }


class SharedAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "authority.sqlite3"
        self.store = CoordinatorStore(self.path, "resources", ["resolution", "effects"], 5, RULE_DEFAULTS)
        self.instance = "a" * 32

    def send(self, op, sender=11, instance=None, **extra):
        result = self.store.handle(sender, uuid.uuid4().hex, {
            "v": 1, "group": "resources", "instance": instance or self.instance,
            "label": "设备A" if sender == 11 else "设备B", "op": op,
            "revision": self.store.policy["revision"], "pack_policy": "partial", **extra,
        })
        return result

    def import_rows(self, records=None):
        rows, _ = completion_rows(records or [evidence()], self.store.policy["attributes"])
        return self.send("import_completed", files=rows), rows

    @staticmethod
    def media_file(tmdb_id, file_id, media_title="This Is 炫茂路"):
        return {
            "id": file_id, "title": "This Is Hyun Moo Road S01E10 1080p WEB-DL H264",
            "media_type": "tv", "tmdb_id": tmdb_id, "season": 1, "episodes": [10],
            "media_title": media_title, "specs": {"resolution": "1080p", "effects": "SDR"},
        }

    def test_tmdb_identity_mapping_blocks_duplicate_id_and_matches_existing_history(self):
        first = self.media_file(336181, "1" * 24)
        result = self.send("reserve", files=[first])
        claim = result["approved"][0]["claim"]
        self.send("update", updates=[{"claim": claim, "state": "queued"}])
        self.send("update", updates=[{"claim": claim, "state": "completed"}])

        # The mapping is added after the first completed record; no raw history IDs are rewritten.
        self.store = CoordinatorStore(
            self.path, "resources", ["resolution", "effects"], 5, RULE_DEFAULTS,
            {"TMDB 336181": 336292}, {},
        )
        second = self.media_file(336292, "2" * 24)
        duplicate = self.send("reserve", 22, "b" * 32, files=[second])
        self.assertEqual(duplicate["approved"], [])
        self.assertEqual(duplicate["rejected"][0]["claim"], claim)
        self.assertEqual(duplicate["rejected"][0]["state"], "completed")
        self.assertEqual(next(row for row in self.store.recent() if row["id"] == claim)["tmdb_id"], 336181)

    def test_same_title_different_ids_are_not_merged_without_an_explicit_binding(self):
        first = self.media_file(336181, "1" * 24)
        second = self.media_file(336292, "2" * 24)
        self.assertEqual(len(self.send("reserve", files=[first])["approved"]), 1)
        self.assertEqual(len(self.send("reserve", 22, "b" * 32, files=[second])["approved"]), 1)

    def test_existing_name_binding_is_applied_to_shared_resource_identity(self):
        self.store = CoordinatorStore(
            self.path, "resources", ["resolution", "effects"], 5, RULE_DEFAULTS,
            {"This Is 炫茂路": 336181}, {},
        )
        first = self.media_file(336181, "1" * 24)
        second = self.media_file(336292, "2" * 24)
        claim = self.send("reserve", files=[first])["approved"][0]["claim"]
        duplicate = self.send("reserve", 22, "b" * 32, files=[second])
        self.assertEqual(duplicate["approved"], [])
        self.assertEqual(duplicate["rejected"][0]["claim"], claim)

    def test_history_is_idempotent_and_blocks_peer_with_original_link(self):
        first, rows = self.import_rows()
        self.assertTrue(first["ok"])
        second = self.send("import_completed", files=rows)
        self.assertEqual(first["imported"], second["imported"])
        self.assertEqual(len(self.store.recent()), 1)
        peer = self.send("reserve", 22, "b" * 32, files=rows)
        self.assertEqual(peer["approved"], [])
        self.assertEqual(peer["rejected"][0]["claim"], first["imported"][0]["claim"])
        self.assertEqual(peer["rejected"][0]["state"], "completed")
        self.assertEqual(peer["rejected"][0]["owner"], "设备A")

    def test_normalizer_does_not_promote_channel_unverified_or_missing_selected_specs(self):
        rows, skipped = completion_rows([
            evidence(evidence_source="channel"), evidence(tmdb_id=None),
            evidence(identity_invalidated=True), evidence(specs={"resolution": "2160p"}),
        ], ["resolution", "effects"])
        self.assertEqual(rows, [])
        self.assertEqual(skipped, 3)

    def test_unselected_fields_do_not_prevent_import_and_multi_episode_files_split(self):
        rows, skipped = completion_rows([
            evidence(episodes=[21, 22], specs={"resolution": "2160p", "effects": None, "fps": None}),
        ], ["resolution"])
        self.assertEqual(skipped, 0)
        self.assertEqual([row["episodes"] for row in rows], [[21], [22]])
        self.assertEqual(rows[0]["specs"], {"resolution": "2160p"})

    def test_unselected_resolution_is_preserved_for_2160p_preference(self):
        rows, skipped = completion_rows([
            evidence(specs={"resolution": "4K", "effects": "HDR", "fps": 24, "size": 1000}),
        ], ["effects"])
        self.assertEqual(skipped, 0)
        self.assertEqual(rows[0]["specs"], {"effects": "HDR", "resolution": "2160p"})

    def test_imported_2160p_history_blocks_lower_quality_peer_when_resolution_is_unselected(self):
        self.store = CoordinatorStore(self.path, "resources", ["effects"], 5, RULE_DEFAULTS)
        rows, _ = completion_rows([
            evidence(specs={"resolution": "4K", "effects": "HDR", "fps": 24, "size": 1000}),
        ], ["effects"])
        imported = self.send("import_completed", files=rows)
        self.assertTrue(imported["ok"], imported)
        lower = {"id": "b" * 24, "title": "Show", "media_type": "tv", "tmdb_id": 279155,
                 "season": 1, "episodes": [21], "specs": {"resolution": "720p", "effects": "SDR"}}
        result = self.send("reserve", 22, "b" * 32, files=[lower])
        self.assertEqual(result["approved"], [])
        self.assertEqual(result["rejected"][0]["reason"], "已有2160p/4K高分辨率资源")

    def test_invalid_import_batch_rolls_back_all_evidence(self):
        _, rows = self.import_rows()
        bad = {**rows[0], "id": "f" * 24, "completed_at": time.time() + 600}
        result = self.send("import_completed", files=[{**rows[0], "id": "e" * 24, "episodes": [22]}, bad])
        self.assertFalse(result["ok"])
        self.assertEqual(len(self.store.recent()), 1)

    def test_legacy_completion_does_not_release_or_reassign_an_existing_queue(self):
        _, rows = self.import_rows([evidence(episodes=[22])])
        candidate = {**rows[0], "id": "e" * 24, "episodes": [21]}
        approved = self.send("reserve", 22, "b" * 32, files=[candidate])
        claim = approved["approved"][0]["claim"]
        self.import_rows()
        result = self.send("update", 22, "b" * 32, updates=[{"claim": claim, "state": "queued"}])
        self.assertFalse(result["ok"])
        self.assertIn("启动前", result["error"])
        self.assertEqual(next(row for row in self.store.recent() if row["id"] == claim)["state"], "reserved")

    def test_multi_episode_start_is_blocked_if_any_episode_was_completed(self):
        rows, _ = completion_rows([evidence()], ["resolution", "effects"])
        candidate = {**rows[0], "episodes": [21, 22]}
        approved = self.send("reserve", 22, "b" * 32, files=[candidate])
        self.import_rows()
        result = self.send("update", 22, "b" * 32, updates=[
            {"claim": approved["approved"][0]["claim"], "state": "queued"}])
        self.assertFalse(result["ok"])
        self.assertIn("启动前", result["error"])

    def test_multi_episode_original_claim_restores_as_one_complete_physical_file(self):
        record = evidence(episodes=[21, 22], original_claim="d" * 32,
                          original_request="e" * 32, original_file="f" * 24)
        result, rows = self.import_rows([record])
        self.assertEqual(len(rows), 1)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.store.recent()[0]["episodes"], [21, 22])
        self.assertEqual(self.store.recent()[0]["id"], "d" * 32)
        self.assertEqual(self.send("reserve", 22, "b" * 32, files=rows)["approved"], [])

    def test_partial_proof_cannot_mark_a_multi_episode_original_claim_complete(self):
        rows, _ = completion_rows([evidence()], ["resolution", "effects"])
        claim = self.send("reserve", files=[{**rows[0], "episodes": [21, 22]}])["approved"][0]["claim"]
        with self.store.connect() as db:
            linked = dict(db.execute("SELECT * FROM claims WHERE id=?", (claim,)).fetchone())
        result, _ = self.import_rows([evidence(original_claim=claim, original_request=linked["request_id"],
                                              original_file=linked["file_id"])])
        self.assertFalse(result["ok"])
        self.assertEqual(self.store.recent()[0]["state"], "reserved")

    def test_local_seed_is_bound_to_telegram_owner_on_first_policy_request(self):
        rows, _ = completion_rows([evidence()], ["resolution", "effects"])
        result = self.store.seed_completed(self.instance, "设备A", rows)
        self.assertTrue(result["ok"])
        self.send("policy")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT sender FROM claims").fetchone()[0], 11)
        denied = self.send("policy", 22)
        self.assertFalse(denied["ok"])

    def test_inspection_does_not_expose_other_instance_state(self):
        rows, _ = completion_rows([evidence()], ["resolution", "effects"])
        approved = self.send("reserve", files=rows)
        claim = approved["approved"][0]["claim"]
        with self.store.connect() as db:
            linked = dict(db.execute("SELECT * FROM claims WHERE id=?", (claim,)).fetchone())
        result = self.send("inspect", 22, "b" * 32, claims=[{
            "claim": claim, "request_id": linked["request_id"], "file_id": linked["file_id"]}])
        self.assertEqual(result["claims"][0]["state"], "missing")

    def test_released_original_claim_is_not_resurrected_by_completion_import(self):
        rows, _ = completion_rows([evidence()], ["resolution", "effects"])
        claim = self.send("reserve", files=rows)["approved"][0]["claim"]
        with self.store.connect() as db:
            linked = dict(db.execute("SELECT * FROM claims WHERE id=?", (claim,)).fetchone())
            db.execute("UPDATE claims SET state='released' WHERE id=?", (claim,))
        result, _ = self.import_rows([evidence(original_claim=claim, original_request=linked["request_id"],
                                              original_file=linked["file_id"])])
        self.assertFalse(result["ok"])
        self.assertEqual(self.store.recent()[0]["state"], "released")

    def test_partial_pack_has_claims_only_for_approved_files(self):
        _, rows = self.import_rows()
        files = [rows[0], {**rows[0], "id": "e" * 24, "episodes": [22]}]
        reply = self.send("reserve", 22, "b" * 32, files=files)
        self.assertEqual(len(reply["approved"]), 1)
        self.assertEqual(len(reply["rejected"]), 1)
        self.assertEqual(reply["rejected"][0]["claim"], self.store.recent()[-1]["id"])
        self.assertEqual(len(self.store.recent()), 2)

    def test_completion_older_than_72_hours_is_retained_and_blocks(self):
        _, rows = self.import_rows([evidence(remembered_at=time.time() - 90 * 86400)])
        reopened = CoordinatorStore(self.path, "resources", ["resolution", "effects"], 5, RULE_DEFAULTS)
        self.assertEqual(reopened.recent()[0]["state"], "completed")
        self.assertEqual(self.send("reserve", 22, "b" * 32, files=rows)["approved"], [])

    def test_authority_id_survives_reopen_but_not_a_new_database(self):
        same = CoordinatorStore(self.path, "resources", ["resolution"], 5)
        other = CoordinatorStore(Path(self.temp.name) / "other.sqlite3", "resources", ["resolution"], 5)
        self.assertEqual(same.authority(), self.store.authority())
        self.assertNotEqual(other.authority()["authority_id"], self.store.authority()["authority_id"])

    def test_record_query_is_group_scoped_and_supports_completed_view(self):
        self.import_rows()
        self.import_rows([evidence(media_type="movie", tmdb_id=123, season=None, episodes=[])])
        page = self.send("records", 22, "b" * 32, status="completed", media_type="tv", query="谷雨 S01E21")
        self.assertEqual(page["records"]["total"], 1)
        self.assertIn("request_id", page["records"]["items"][0])
        self.assertNotIn("sender", page["records"]["items"][0])
        isolated = CoordinatorStore(self.path, "other", ["resolution", "effects"], 5, RULE_DEFAULTS)
        self.assertEqual(isolated.search_records()["total"], 0)

    def make_plugin(self):
        p = ResourceFlow.__new__(ResourceFlow)
        p.get_data, p.save_data, p.update_config = Mock(return_value=None), Mock(), Mock()
        p.chain = SimpleNamespace(recognize_media=Mock(return_value=None))
        p.init_plugin({})
        p._shared_settings = SharedSettingsPayload(enabled=True, bot_username="@testbot", instance_label="设备A")
        p._shared_instance_id = self.instance
        p._channel_dedup_attributes = ["resolution", "effects"]
        p._shared_probe = {"policy": self.store.policy, **self.store.authority()}

        def exchange(request_id, body):
            result = self.store.handle(11, request_id, {
                "v": 1, "group": "resources", "instance": self.instance, "label": "设备A", **body,
            })
            if not result["ok"]:
                raise SharedError(result["error"])
            return result
        p._shared_exchange = Mock(side_effect=exchange)
        return p

    def test_local_history_is_synced_before_peer_admission_and_retries_do_not_duplicate(self):
        p = self.make_plugin()
        p._resource_ledger = {"old": evidence()}
        p._sync_shared_policy()
        self.assertEqual(p._shared_history["pending"], 0)
        calls = p._shared_exchange.call_count
        p._sync_shared_history()
        self.assertEqual(p._shared_exchange.call_count, calls)
        self.assertEqual(len(self.store.recent()), 1)

    def test_observed_file_completion_has_a_stable_import_identity(self):
        p = self.make_plugin()
        p._shared_claims = {"c" * 32: {
            "request_id": "c" * 32, "phase": "queued", "approved": {"d" * 24: "e" * 32},
            "files": [{**evidence(), "id": "d" * 24}], "observed_completed": ["d" * 24],
            "created": time.time() - 100, "updated": time.time() - 50,
        }}
        p._sync_shared_history()
        count = p._shared_exchange.call_count
        p._shared_claims["c" * 32]["updated"] = time.time()
        p._sync_shared_history()
        self.assertEqual(p._shared_exchange.call_count, count)
        self.assertEqual(len(self.store.recent()), 1)

    def test_backfills_linked_record_once_from_own_torrent_history(self):
        p = self.make_plugin()
        request_id, file_id, claim = "c" * 32, "d" * 24, "e" * 32
        p._shared_claims = {request_id: {
            "request_id": request_id, "phase": "completed", "approved": {file_id: claim},
            "files": [{**evidence(), "id": file_id}], "done": [file_id],
            "created": time.time() - 100, "updated": time.time() - 50,
            "task_id": "site-task", "hash": "f" * 40,
        }}
        p._get_task_data = Mock(return_value={})
        p._sync_shared_history()
        before = self.store.recent()[0]
        self.assertEqual(before["site_name"], "")
        original_ids = set(p._shared_history["acknowledged"])

        p._get_task_data.side_effect = lambda task, kind: (
            {"f" * 40: {"site_name": "  观众  "}} if (task, kind) == ("site-task", "archived") else {}
        )
        p._shared_probe.pop("site_metadata_version")
        requests = p._shared_exchange.call_count
        p._sync_shared_history()
        self.assertEqual(p._shared_exchange.call_count, requests)
        p._shared_probe.update(self.store.authority())
        p._sync_shared_history()
        self.assertEqual(self.store.recent(), [{**before, "site_name": "观众"}])
        self.assertEqual(set(p._shared_history["acknowledged"]), original_ids)
        self.assertEqual(p._shared_exchange.call_count, requests + 1)
        p._sync_shared_history()
        self.assertEqual(p._shared_exchange.call_count, requests + 1)

    def test_history_of_another_instance_cannot_fill_existing_blank_site(self):
        _, rows = self.import_rows()
        peer = self.send("import_completed", 22, "b" * 32, files=[{**rows[0], "site_name": "观众"}])
        self.assertTrue(peer["ok"])
        self.assertEqual(len(self.store.recent()), 1)
        self.assertEqual(self.store.recent()[0]["site_name"], "")

    def test_coordinator_imports_history_before_starting_https_listener(self):
        p = self.make_plugin()
        p._shared_settings = p._shared_settings.model_copy(update={
            "host_coordinator": True, "https_listener": True, "group": "resources"})
        p.get_data_path = Mock(return_value=Path(self.temp.name))
        p._resource_ledger = {"old": evidence()}
        with patch.object(p, "_start_shared_http") as listener:
            def ready():
                self.assertEqual(p._shared_store.recent()[0]["state"], "completed")
            listener.side_effect = ready
            p._start_shared_worker()
            listener.assert_called_once()
        self.assertEqual(p._shared_error, "")

    def test_history_sync_failure_cannot_reach_downloader_add(self):
        p = self.make_plugin()
        p._resource_ledger = {"old": evidence()}
        p._shared_exchange.side_effect = SharedError("offline")
        p._add_local_prechecked_torrent = Mock()
        task = SimpleNamespace(spec_dedup_enabled=True, name="test", downloader="QB")
        self.assertIsNone(p._add_prechecked_torrent(b"torrent", None, task, Mock(), Mock(), "a" * 40, None, "Show"))
        p._add_local_prechecked_torrent.assert_not_called()

    def test_missing_active_claim_is_reported_without_reapproval(self):
        p = self.make_plugin()
        p._shared_claims = {"c" * 32: {
            "request_id": "c" * 32, "phase": "queued", "approved": {"d" * 24: "e" * 32}, "files": [],
        }}
        with self.assertRaisesRegex(SharedError, "关联异常"):
            p._audit_shared_links(force=True)
        self.assertIn("缺失", p._shared_claims["c" * 32]["integrity_error"])
        self.assertEqual(self.store.recent(), [])
        self.assertFalse(any(call.args[1]["op"] == "reserve" for call in p._shared_exchange.call_args_list))

    def test_confirmed_completion_repairs_missing_original_link_without_new_approval(self):
        p = self.make_plugin()
        record = evidence()
        file_id, claim, request = "d" * 24, "e" * 32, "c" * 32
        p._shared_claims = {request: {
            "request_id": request, "phase": "completed", "approved": {file_id: claim},
            "files": [{**record, "id": file_id}], "done": [file_id],
            "created": record["remembered_at"], "updated": record["remembered_at"],
        }}
        p._sync_shared_history()
        p._audit_shared_links(force=True)
        self.assertEqual(self.store.recent()[0]["id"], claim)
        with self.store.connect() as db:
            db.execute("DELETE FROM claims")
        with self.assertRaises(SharedError):
            p._audit_shared_links(force=True)
        p._sync_shared_history()
        p._audit_shared_links(force=True)
        self.assertEqual(self.store.recent()[0]["id"], claim)
        self.assertEqual(p._shared_integrity_error, "")

    def test_shared_completed_view_reads_authority_not_local_archive(self):
        p = self.make_plugin()
        p._resource_ledger = {"unrelated": evidence(tmdb_id=999)}
        self.import_rows()
        response = p.get_resource_ledger(query="谷雨", media_type="tv")
        self.assertTrue(response.success)
        self.assertTrue(response.data["shared_authority"])
        self.assertEqual(response.data["items"][0]["tmdb_id"], 279155)
        self.assertEqual(response.data["total"], 1)

    def test_shared_mode_bypasses_local_inflight_and_ledger_matching(self):
        p = self.make_plugin()
        p._inflight_entries = {"old": {"records": [evidence()]}}
        p._channel_dedup_file_selection = Mock(side_effect=AssertionError("Local archive must not arbitrate"))
        task = SimpleNamespace(spec_dedup_enabled=True)
        with p._inflight_download_scope(b"torrent", Mock(), task, "a" * 40, None) as result:
            self.assertFalse(result["blocked"])
        p._channel_dedup_file_selection.assert_not_called()
        self.assertEqual(list(p._inflight_entries), ["old"])

    def test_old_coordinator_is_rejected_instead_of_silently_using_local_history(self):
        p = self.make_plugin()
        p._shared_exchange.return_value = {"ok": True, "policy": self.store.policy}
        p._shared_exchange.side_effect = None
        with self.assertRaisesRegex(SharedError, "先将协调端更新"):
            p._sync_shared_policy()
        self.assertFalse(p.test_shared_connection().success)

    def test_database_replacement_during_admission_requires_resync(self):
        p = self.make_plugin()
        with patch.object(p, "_https_shared_exchange") as rpc:
            rpc.return_value = {
                "ok": True, "authority_version": 2, "authority_id": "f" * 32,
                "approved": [], "rejected": [],
            }
            with self.assertRaisesRegex(SharedError, "资源库已变更"):
                ResourceFlow._shared_exchange(p, uuid.uuid4().hex, {"op": "reserve"})
        self.assertIsNone(p._shared_probe)
        self.assertIn("等待历史同步", p._shared_integrity_error)

    def test_capture_inherited_completion_uses_outbox_not_a_second_ledger(self):
        p = self.make_plugin()
        task = SimpleNamespace(downloader="QB")
        p._capture_shared_completed(task, "a" * 40, [evidence()])
        self.assertEqual(p._resource_ledger, {})
        self.assertEqual(len(p._shared_completed_outbox), 1)
        p._sync_shared_history()
        self.assertEqual(self.store.recent()[0]["state"], "completed")


if __name__ == "__main__":
    unittest.main()
