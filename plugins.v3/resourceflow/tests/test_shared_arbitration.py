import asyncio
import base64
import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid
import zlib

from resourceflow.shared_coordinator import BotWorker, CoordinatorStore
from resourceflow.shared_models import SharedSettingsPayload
from resourceflow.shared_plugin import SharedResourceMixin
from resourceflow.shared_protocol import MAX_BYTES, SharedError, decode, encode, parse_part, policy
from resourceflow.telegram_rpc import TelegramRPC
from resourceflow import ResourceFlow
from test_channel_integration import make_torrent


def file_row(number=1, episode=1, **specs):
    return {"id": f"{number:024x}", "title": "Show", "media_type": "tv", "tmdb_id": 123,
            "season": 1, "episodes": [episode], "specs": {"resolution": "1080p", **specs}}


class ProtocolTests(unittest.TestCase):
    def test_round_trip_multipart_and_out_of_order_assembly(self):
        body = {"random": [uuid.uuid4().hex for _ in range(500)]}
        parts = encode(body, "a" * 32)
        self.assertGreater(len(parts), 1)
        parsed = sorted([parse_part(part) for part in reversed(parts)], key=lambda row: row[1])
        self.assertEqual(decode([row[3] for row in parsed]), body)

    def test_bounded_decompression_and_invalid_envelopes(self):
        bomb = base64.urlsafe_b64encode(zlib.compress(b" " * (MAX_BYTES + 1))).decode()
        with self.assertRaises(SharedError):
            decode([bomb])
        for text in ("RFQ1 bad 1/1 eA==", f"RFQ1 {'a'*32} 0/1 eA==", f"RFA1 {'a'*32} 1/1 eA=="):
            with self.subTest(text=text), self.assertRaises(SharedError):
                parse_part(text)
        with self.assertRaises(SharedError):
            encode({"large": "a" * MAX_BYTES}, "a" * 32)
        for raw in (b'{"value":NaN}', b'{"value":Infinity}', b'{"value":1e400}',
                    b'{"value":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}'):
            with self.assertRaises(SharedError):
                decode([base64.urlsafe_b64encode(zlib.compress(raw)).decode()])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "claims.sqlite3"
        self.store = CoordinatorStore(self.path, "resources", ["resolution"], 5)

    def request(self, files=None, instance="a" * 32, **extra):
        return {"v": 1, "group": "resources", "instance": instance, "label": instance[:2], "op": "reserve",
                "revision": self.store.policy["revision"], "pack_policy": "partial",
                "files": files or [file_row()], **extra}

    def send(self, body, sender=11, rid=None):
        return self.store.handle(sender, rid or uuid.uuid4().hex, body)

    def test_concurrent_clients_only_one_wins_and_reopen_preserves_claim(self):
        barrier = threading.Barrier(2)

        def reserve(index):
            barrier.wait()
            return self.send(self.request(instance=str(index) * 32), sender=index)

        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(reserve, (1, 2)))
        self.assertEqual(sum(len(row["approved"]) for row in replies), 1)
        self.assertEqual(sum(len(row["rejected"]) for row in replies), 1)
        restored = CoordinatorStore(self.path, "resources", ["resolution"], 5)
        self.assertEqual(len(restored.recent()), 1)
        self.assertTrue(restored.has_unfinished())

    def test_selected_resolution_only_ignores_unknown_or_different_other_fields(self):
        self.assertTrue(self.send(self.request())["approved"])
        body = self.request([file_row(codec="H.265", effects="HDR10", fps=60)], instance="b" * 32)
        self.assertFalse(self.send(body, sender=22)["approved"])
        body["files"][0]["specs"]["resolution"] = "2160p"
        self.assertTrue(self.send(body, sender=22)["approved"])

    def test_all_selected_fields_are_required_and_must_match(self):
        self.store = CoordinatorStore(self.path, "resources", ["resolution", "effects"], 5)
        self.assertFalse(self.send(self.request())["ok"])
        self.assertTrue(self.send(self.request([file_row(effects="SDR")]))["approved"])
        self.assertTrue(self.send(self.request([file_row(effects="HDR10")]))["approved"])
        self.assertFalse(self.send(self.request([file_row(effects="SDR")]))["approved"])

    def test_partial_pack_and_exclude_pack_are_atomic(self):
        self.send(self.request())
        rows = [file_row(), file_row(2, 2)]
        rejected = self.send(self.request(rows, pack_policy="exclude"))
        self.assertTrue(rejected["pack_rejected"])
        self.assertFalse(rejected["approved"])
        self.assertEqual(len(self.store.recent()), 1)
        partial = self.send(self.request(rows))
        self.assertEqual([row["id"] for row in partial["approved"]], [rows[1]["id"]])

    def test_movie_tv_and_season_episode_namespaces_are_separate(self):
        self.send(self.request())
        for changed in ({"season": 2}, {"episodes": [2]}, {"tmdb_id": 124},
                        {"media_type": "movie", "season": None, "episodes": []}):
            self.assertTrue(self.send(self.request([{**file_row(), **changed}]))["approved"])

    def test_multi_episode_file_with_any_overlap_is_excluded(self):
        self.send(self.request())
        self.assertFalse(self.send(self.request([{**file_row(), "episodes": [1, 2]}]))["approved"])

    def test_mixed_seasons_and_titles_in_one_batch_do_not_conflict(self):
        rows = [file_row(), {**file_row(2), "season": 2}, {**file_row(3), "tmdb_id": 124},
                {**file_row(4), "media_type": "movie", "season": None, "episodes": []}]
        self.assertEqual(len(self.send(self.request(rows))["approved"]), 4)

    def test_idempotent_replay_survives_restart_and_rejects_changed_body(self):
        rid, body = uuid.uuid4().hex, self.request()
        reply = self.send(body, rid=rid)
        self.store = CoordinatorStore(self.path, "resources", ["resolution"], 5)
        self.assertEqual(self.send(body, rid=rid), reply)
        with self.assertRaises(SharedError):
            self.send({**body, "label": "changed"}, rid=rid)
        self.assertEqual(len(self.store.recent()), 1)

    def test_owner_binding_and_release_confirmation(self):
        claim = self.send(self.request())["approved"][0]["claim"]
        body = self.request(op="update", updates=[{"claim": claim, "state": "released"}])
        self.assertFalse(self.send(body, sender=22)["ok"])
        self.assertFalse(self.send(body)["ok"])
        self.assertTrue(self.send({**body, "confirmed_absent": True})["ok"])
        self.assertTrue(self.send(self.request())["approved"])

    def test_completion_is_permanent_and_bad_batch_cannot_partially_update(self):
        reply = self.send(self.request([file_row(), file_row(2, 2)]))
        claim = reply["approved"][0]["claim"]
        body = self.request(op="update", updates=[{"claim": claim, "state": "completed"},
                                                   {"claim": "missing", "state": "queued"}])
        self.assertFalse(self.send(body)["ok"])
        self.assertEqual({row["state"] for row in self.store.recent()}, {"reserved"})
        body["updates"] = body["updates"][:1]
        self.assertTrue(self.send(body)["ok"])
        body["updates"][0]["state"] = "released"
        body["confirmed_absent"] = True
        self.assertFalse(self.send(body)["ok"])

    def test_fragment_replay_and_authorized_private_chat_only(self):
        message = encode(self.request(), "c" * 32)[0]
        updates = [
            {"update_id": 1, "message": {"from": {"id": 99}, "chat": {"id": 99, "type": "private"}, "text": message}},
            {"update_id": 2, "message": {"from": {"id": 11}, "chat": {"id": -1, "type": "group"}, "text": message}},
            {"update_id": 3, "message": {"from": {"id": 11}, "chat": {"id": 11, "type": "private"}, "text": message}},
        ]
        call = Mock(side_effect=lambda method, payload: updates if method == "getUpdates" else True)
        worker = BotWorker(self.store, "fake", "testbot", [11], call=call)
        worker.poll_once()
        self.assertEqual(sum(item.args[0] == "sendMessage" for item in call.call_args_list), 1)
        self.assertEqual(self.store.meta("offset"), 4)
        self.assertEqual(len(self.store.recent()), 1)

    def test_failed_reply_does_not_advance_cursor_and_retry_uses_same_claim(self):
        message = encode(self.request(), "d" * 32)[0]
        update = {"update_id": 1, "message": {"from": {"id": 11},
                    "chat": {"id": 11, "type": "private"}, "text": message}}

        def call(method, payload):
            if method == "getUpdates":
                return [update]
            raise SharedError("network")

        worker = BotWorker(self.store, "fake", "testbot", [11], call=call)
        with self.assertRaises(SharedError):
            worker.poll_once()
        self.assertEqual(self.store.meta("offset", 0), 0)
        saved = self.store.recent()[0]["id"]
        worker.custom_call = lambda method, payload: [update] if method == "getUpdates" else True
        worker.poll_once()
        self.assertEqual(self.store.recent()[0]["id"], saved)
        self.assertEqual(self.store.meta("offset"), 2)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.plugin = SharedResourceMixin()
        p = self.plugin
        p._shared_settings = SharedSettingsPayload(enabled=True, bot_username="@testbot")
        p._shared_stopping = False
        p._shared_lock, p._shared_rpc_lock = threading.RLock(), threading.Lock()
        p._shared_claims, p._inflight_entries = {}, {}
        p._inflight_lock = threading.RLock()
        p._inflight_key = lambda downloader, torrent_hash: f"{downloader}:{torrent_hash}"
        p._channel_dedup_attributes, p._channel_dedup_size_tolerance_pct = ["resolution"], 5
        p._channel_dedup_pack_policy = "partial"
        p._shared_candidates = Mock(return_value=([{**file_row(), "path": "Show.S01E01.mkv"},
                                                  {**file_row(2, 2), "path": "Show.S01E02.mkv"}], []))
        p.save_data = Mock()
        p._save_inflight_entries = Mock()
        p._shared_task_ready = Mock(return_value=True)
        p._add_local_prechecked_torrent = Mock(return_value="a" * 40)
        self.downloader = Mock()
        self.downloader.get_files.return_value = [
            {"index": 0, "name": "Show.S01E01.mkv", "size": 10, "progress": 0, "priority": 1},
            {"index": 1, "name": "Show.S01E02.mkv", "size": 10, "progress": 0, "priority": 0},
        ]
        helper = Mock()
        helper.get_service.return_value = SimpleNamespace(instance=self.downloader)
        helper.is_downloader.return_value = True
        patcher = patch("resourceflow.shared_plugin.DownloaderHelper", return_value=helper)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.task = SimpleNamespace(id="t", downloader="QB", enabled=True, spec_dedup_enabled=True, name="test")
        p._task_configs = {"t": self.task}
        self.events = []

        def exchange(rid, body):
            self.events.append(body["op"])
            if body["op"] == "reserve":
                return {"ok": True, "approved": [{"id": file_row()["id"], "claim": "b" * 32}],
                        "rejected": [{"id": file_row(2)["id"], "reason": "duplicate"}],
                        "policy": policy(["resolution"], 5), "pack_rejected": False}
            return {"ok": True, "updated": len(body["updates"])}

        p._shared_exchange = Mock(side_effect=exchange)
        p._add_local_prechecked_torrent.side_effect = lambda *args: self.events.append("add") or "a" * 40
        self.downloader.start_torrents.side_effect = lambda value: self.events.append("start") or True

    def add(self):
        return self.plugin._add_prechecked_torrent(b"torrent", None, self.task, self.downloader, Mock(),
                                                   "a" * 40, None, "Show")

    def entry(self):
        return next(iter(self.plugin._shared_claims.values()))

    def test_atomic_approval_then_paused_subset_then_queue_ack_then_start(self):
        self.assertEqual(self.add(), "a" * 40)
        self.assertEqual(self.events, ["reserve", "add", "update", "start"])
        selection = self.plugin._add_local_prechecked_torrent.call_args.args[6]
        self.assertEqual(selection["accepted"], {"Show.S01E01.mkv"})
        self.assertTrue(selection["hold_paused"])
        self.assertEqual(selection["shared_tag"], self.plugin._shared_tag(self.entry()))
        self.assertEqual(self.entry()["phase"], "queued")

    def test_lost_reservation_reply_never_adds_and_persists_request(self):
        self.plugin._shared_exchange.side_effect = SharedError("timeout")
        self.assertIsNone(self.add())
        self.plugin._add_local_prechecked_torrent.assert_not_called()
        self.downloader.start_torrents.assert_not_called()
        self.assertEqual(self.entry()["phase"], "requesting")
        self.assertTrue(self.entry()["body"]["files"])

    def test_lost_queued_confirmation_preserves_paused_ownership_and_reservation(self):
        exchange = self.plugin._shared_exchange.side_effect

        def fail(rid, body):
            if body["op"] == "update":
                raise SharedError("timeout")
            return exchange(rid, body)

        self.plugin._shared_exchange.side_effect = fail
        self.assertEqual(self.add(), "a" * 40)
        self.downloader.start_torrents.assert_not_called()
        self.assertEqual(self.entry()["phase"], "awaiting_start")
        self.assertIn("action", self.entry())

    def test_failed_add_never_releases_early(self):
        self.plugin._add_local_prechecked_torrent.side_effect = None
        self.plugin._add_local_prechecked_torrent.return_value = None
        self.assertIsNone(self.add())
        self.assertEqual(self.events, ["reserve"])
        self.assertEqual(self.entry()["phase"], "uncertain")

    def test_task_off_bypasses_shared_transport(self):
        self.task.spec_dedup_enabled = False
        self.assertEqual(self.add(), "a" * 40)
        self.plugin._shared_exchange.assert_not_called()
        self.assertFalse(self.plugin._shared_claims)

    def test_task_changed_after_approval_does_not_add(self):
        self.plugin._shared_task_ready.return_value = False
        self.assertIsNone(self.add())
        self.plugin._add_local_prechecked_torrent.assert_not_called()
        self.assertEqual(self.entry()["phase"], "reserved")

    def test_duplicate_same_hash_is_not_submitted_twice(self):
        self.add()
        self.assertIsNone(self.add())
        self.assertEqual(self.plugin._add_local_prechecked_torrent.call_count, 1)

    def test_ambiguous_coordinator_reply_is_rejected(self):
        reply = {"approved": [], "rejected": [], "policy": policy(["resolution"], 5)}
        self.plugin._shared_exchange.side_effect = None
        self.plugin._shared_exchange.return_value = reply
        self.assertIsNone(self.add())
        self.plugin._add_local_prechecked_torrent.assert_not_called()

    def reconcile(self, torrents=None, files=None):
        helper = Mock()
        helper.get_service.return_value = SimpleNamespace(instance=self.downloader)
        helper.is_downloader.return_value = True
        self.downloader.get_torrents.return_value = (torrents or [], None)
        self.downloader.get_files.return_value = files
        with patch("resourceflow.shared_plugin.DownloaderHelper", return_value=helper):
            self.plugin._reconcile_shared_entry(self.entry())

    def test_uncertain_add_needs_grace_and_two_absence_checks_before_release(self):
        self.plugin._add_local_prechecked_torrent.side_effect = lambda *args: None
        self.add()
        self.reconcile()
        self.assertEqual(self.entry()["phase"], "uncertain")
        self.entry()["created"] = time.time() - 400
        self.reconcile()
        self.assertEqual(self.entry()["phase"], "uncertain")
        self.reconcile()
        self.assertEqual(self.entry()["phase"], "released")
        self.assertTrue(self.plugin._shared_exchange.call_args.args[1]["confirmed_absent"])

    def test_recovery_refuses_unowned_task_and_unapproved_extra_files(self):
        self.add()
        with self.assertRaises(SharedError):
            self.reconcile(torrents=[{"tags": "somebody-else"}])
        snapshot = [{"index": 0, "name": "Show.S01E01.mkv", "size": 10, "progress": 0.5, "priority": 1},
                    {"index": 1, "name": "unapproved.mkv", "size": 10, "progress": 0, "priority": 1}]
        with self.assertRaises(SharedError):
            self.reconcile(torrents=[{"tags": self.plugin._shared_tag(self.entry())}], files=snapshot)

    def test_completion_report_and_no_release_of_completed_claim(self):
        self.add()
        snapshot = [{"index": 0, "name": "Show.S01E01.mkv", "size": 10, "progress": 1, "priority": 1}]
        self.reconcile(torrents=[{"tags": self.plugin._shared_tag(self.entry())}], files=snapshot)
        self.assertEqual(self.entry()["phase"], "completed")
        self.assertEqual(self.plugin._shared_exchange.call_args.args[1]["updates"][0]["state"], "completed")

    def test_observed_completion_survives_downloader_deletion(self):
        self.add()
        self.entry()["observed_completed"] = [file_row()["id"]]
        self.reconcile()
        self.assertEqual(self.entry()["phase"], "completed")
        self.downloader.get_torrents.assert_not_called()

    def test_pending_completion_action_is_replayed_before_absence_release(self):
        self.add()
        body = {"op": "update", "updates": [{"claim": "b" * 32, "state": "completed"}], "confirmed_absent": False}
        self.entry()["action"] = {"id": "c" * 32, "body": body}
        self.reconcile()
        self.assertEqual(self.entry()["phase"], "completed")
        self.assertNotIn("action", self.entry())
        self.downloader.get_torrents.assert_not_called()

    def test_crash_during_add_revalidates_selection_before_queue_and_start(self):
        self.add()
        self.entry()["phase"] = "adding"
        self.events.clear()
        self.plugin._select_recent_episode_files = Mock(return_value=True)
        snapshot = [{"index": 0, "name": "Show.S01E01.mkv", "size": 10, "progress": 0.5, "priority": 1}]
        self.reconcile(torrents=[{"tags": self.plugin._shared_tag(self.entry())}], files=snapshot)
        self.plugin._select_recent_episode_files.assert_called_once()
        self.assertEqual(self.events, ["update", "start"])
        self.assertEqual(self.entry()["phase"], "queued")


class TransportTests(unittest.TestCase):
    def account(self, reply_ok=True):
        rid = "a" * 32
        peer = SimpleNamespace(bot=True, username="testbot", id=99)
        client = SimpleNamespace(connect=AsyncMock(), is_user_authorized=AsyncMock(return_value=True),
                                 get_me=AsyncMock(return_value=SimpleNamespace(bot=False)),
                                 get_entity=AsyncMock(return_value=peer),
                                 send_message=AsyncMock(return_value=SimpleNamespace(id=10)))
        replies = [SimpleNamespace(id=11, out=False, sender_id=88, message="irrelevant")]
        replies += [SimpleNamespace(id=12 + index, out=False, sender_id=99, message=text)
                    for index, text in enumerate(encode({"ok": reply_ok, "error": "denied", "answer": 42}, rid, True))]

        async def messages(target, **kwargs):
            self.assertIs(target, peer)
            for row in replies:
                yield row

        client.iter_messages = messages
        account = SimpleNamespace(_credentials=Mock(return_value={}), _client=Mock(return_value=client),
                                  _disconnect=AsyncMock(), locked=False)

        @contextmanager
        def exclusive(**kwargs):
            account.locked = True
            try:
                yield
            finally:
                account.locked = False

        account.exclusive = exclusive
        return account, client

    def test_only_designated_bot_private_conversation_is_read_and_session_released(self):
        account, client = self.account()
        with patch("resourceflow.telegram_rpc.asyncio.sleep", new=AsyncMock()):
            self.assertEqual(TelegramRPC(account, "@testbot").exchange("a" * 32, {"op": "policy"})["answer"], 42)
        self.assertFalse(account.locked)
        account._disconnect.assert_awaited_once_with(client)

    def test_definitive_denial_and_transport_failure_are_distinguishable(self):
        account, client = self.account(False)
        with patch("resourceflow.telegram_rpc.asyncio.sleep", new=AsyncMock()), self.assertRaises(SharedError) as caught:
            TelegramRPC(account, "@testbot").exchange("a" * 32, {})
        self.assertTrue(caught.exception.definitive)
        client.connect.side_effect = RuntimeError("must-not-leak-secret")
        with self.assertRaises(SharedError) as caught:
            TelegramRPC(account, "@testbot").exchange("a" * 32, {})
        self.assertFalse(getattr(caught.exception, "definitive", False))
        self.assertNotIn("secret", str(caught.exception))
        self.assertFalse(account.locked)


class PluginSetupTests(unittest.TestCase):
    def setUp(self):
        self.plugin = ResourceFlow.__new__(ResourceFlow)
        self.plugin.get_data = Mock(return_value=None)
        self.plugin.save_data = Mock()
        self.plugin.update_config = Mock()
        self.plugin.init_plugin({})

    def test_independent_plugin_identity_and_persisted_instance_id(self):
        self.assertEqual(self.plugin.plugin_name, "资源刷流")
        self.assertEqual(self.plugin.plugin_config_prefix, "resourceflow_")
        config = self.plugin.update_config.call_args.args[0]
        self.assertEqual(config["shared"]["instance_id"], self.plugin._shared_instance_id)
        self.assertFalse(self.plugin._channel_dedup_enabled)
        self.assertEqual(self.plugin._channel_dedup_channels, [])
        self.assertEqual(self.plugin._shared_claims, {})

    def test_http_json_settings_routes_preserve_pydantic_body_contract(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.post("/shared/settings")(self.plugin.update_shared_settings)
        app.post("/rules")(self.plugin.update_channel_dedup_settings)
        self.plugin._refresh_scheduler = Mock()
        with TestClient(app) as client:
            response = client.post("/shared/settings", json={"enabled": True, "bot_username": "@testbot"})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["success"])
            self.assertNotIn("bot_token", response.json()["data"])
            response = client.post("/rules", json={"attributes": ["resolution"], "enabled": False})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["success"])
            self.assertEqual(client.post("/rules", json={"attributes": ["not-a-field"]}).status_code, 422)

    def test_real_candidate_parser_requires_only_selected_specs(self):
        p = self.plugin
        p._channel_dedup_attributes = ["resolution"]
        task = SimpleNamespace(spec_dedup_enabled=True, name="test")
        path = "Show.2026.(tmdbid=123).S02E05.1080p.mkv"
        files, rejected = p._shared_candidates(make_torrent([(path, 123456)]), "Show", task, None)
        self.assertFalse(rejected)
        self.assertEqual((files[0]["tmdb_id"], files[0]["season"], files[0]["episodes"]), (123, 2, [5]))
        self.assertEqual(files[0]["specs"]["resolution"], "1080p")
        p._channel_dedup_attributes = ["resolution", "fps"]
        files, rejected = p._shared_candidates(make_torrent([(path, 123456)]), "Show", task, None)
        self.assertFalse(files)
        self.assertEqual(rejected, [path])

    def test_host_cannot_abandon_remote_claims_or_change_their_policy(self):
        from resourceflow.models import ChannelDedupSettingsPayload

        p = self.plugin
        with TemporaryDirectory() as directory:
            store = CoordinatorStore(Path(directory) / "host.sqlite3", "resources", ["resolution"], 5)
            reply = store.handle(11, uuid.uuid4().hex, {
                "v": 1, "group": "resources", "instance": "a" * 32, "op": "reserve",
                "revision": store.policy["revision"], "pack_policy": "partial", "files": [file_row()]})
            self.assertTrue(reply["approved"])
            p._shared_store = store
            p._shared_settings = SharedSettingsPayload(enabled=True, host_coordinator=True,
                bot_username="@testbot", bot_token="12345:" + "x" * 30, allowed_users=[11])
            p._shared_worker = SimpleNamespace(stop=Mock())
            p._start_shared_worker = Mock()
            response = p.update_shared_settings(SharedSettingsPayload(enabled=True, bot_username="@testbot"))
            self.assertFalse(response.success)
            self.assertTrue(p._shared_settings.host_coordinator)
            p._shared_store = store
            p._channel_dedup_attributes = ["resolution"]
            response = p.update_channel_dedup_settings(ChannelDedupSettingsPayload(attributes=["codec"]))
            self.assertFalse(response.success)
            self.assertEqual(p._channel_dedup_attributes, ["resolution"])


if __name__ == "__main__":
    unittest.main()
