from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from resourceflow.shared_coordinator import CoordinatorStore
from resourceflow.shared_http import ApprovalBusy, ApprovalServer, HTTPSApprovalRPC, RPC_PATH, _UnixHTTPConnection
from resourceflow.shared_models import ApprovalPeer, SharedSettingsPayload
from resourceflow.shared_protocol import MAX_BYTES, RULE_DEFAULTS, SharedError
import test_shared_arbitration as arbitration

file_row = arbitration.file_row


class PrivateApprovalTest(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.socket = Path(directory.name) / "rpc.sock"
        self.store = CoordinatorStore(Path(directory.name) / "old-ledger.sqlite3", "resources",
                                      ["resolution"], 5, RULE_DEFAULTS)
        self.authority = self.store.authority()["authority_id"]
        self.tokens = ("A" * 43, "B" * 43)
        self.peers = [ApprovalPeer(instance_id=label * 32, sender=sender, token_hash=sha256(token.encode()).hexdigest())
                      for label, sender, token in zip("ab", (11, 22), self.tokens)]
        self.server = ApprovalServer(self.socket, self.store, self.peers)
        self.addCleanup(self.server.stop)

    def body(self, i, op, **fields):
        return dict(v=1, group="resources", instance=self.peers[i].instance_id,
                    label=f"NAS {i}", op=op, **fields)

    def call(self, i, op, rid=None, **fields):
        return HTTPSApprovalRPC("", self.tokens[i], self.authority, local_socket=self.socket).exchange(
            rid or uuid4().hex, self.body(i, op, **fields))

    def test_old_reservation_survives_transport_change(self):
        rid = uuid4().hex
        reserve = {"revision": self.store.policy["revision"], "files": [file_row()], "pack_policy": "partial"}
        approved = self.store.handle(11, rid, self.body(0, "reserve", **reserve))
        claim = approved["approved"][0]["claim"]
        self.assertEqual(self.call(0, "reserve", rid, **reserve), approved)
        self.assertEqual(self.call(1, "reserve", **reserve)["rejected"][0]["state"], "reserved")
        with self.assertRaises(SharedError):
            self.call(1, "update", updates=[dict(claim=claim, state="completed")])
        with self.assertRaises(SharedError):
            self.call(0, "update", updates=[dict(claim=claim, state="released")])
        self.assertEqual(self.call(0, "inspect", claims=[dict(
            claim=claim, request_id=rid, file_id=file_row()["id"])])["claims"][0]["state"], "reserved")
        cursor = self.call(1, "sync_records", cursor=0, authority_id=self.authority,
                           revision=self.store.policy["revision"])["cursor"]
        for state in ("queued", "completed"):
            self.assertTrue(self.call(0, "update", updates=[dict(claim=claim, state=state)])["ok"])
        delta = self.call(1, "sync_records", cursor=cursor, authority_id=self.authority,
                          revision=self.store.policy["revision"])
        self.assertEqual(delta["changes"][-1]["record"]["state"], "completed")
        self.assertEqual(self.call(1, "reserve", **reserve)["rejected"][0]["state"], "completed")
        self.assertEqual(self.store.recent()[0]["label"], "NAS 0")

    def test_wrong_credential_instance_and_ledger_rejected_before_mutation(self):
        for token, authority, i, message in (
            ("C" * 43, self.authority, 0, "未获授权"),
            (self.tokens[1], self.authority, 0, "实例 ID 不匹配"),
            (self.tokens[0], "c" * 32, 0, "账本 ID 不匹配"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(SharedError, message):
                HTTPSApprovalRPC("", token, authority, local_socket=self.socket).exchange(
                    uuid4().hex, self.body(i, "policy"))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM owners").fetchone()[0], 0)

    def test_socket_has_no_admin_route_and_rejects_large_body(self):
        for method, path, headers, status in (
            ("GET", "/api/v1/system/setting", {}, 501),
            ("POST", "/api/v1/system/setting", {}, 404),
            ("POST", RPC_PATH, {"Authorization": f"Bearer {self.tokens[0]}",
                                "Content-Type": "application/json", "Content-Length": str(MAX_BYTES + 1)}, 413),
        ):
            conn = _UnixHTTPConnection(self.socket, 2)
            try:
                conn.request(method, path, headers=headers)
                self.assertEqual(conn.getresponse().status, status)
            finally:
                conn.close()
        self.assertEqual(self.socket.stat().st_mode & 0o777, 0o600)

    def test_existing_account_mapping_cannot_be_reassigned(self):
        self.assertTrue(self.call(0, "policy")["ok"])
        with self.assertRaisesRegex(SharedError, "个人账号 ID"):
            self.store.verify_peer_owners([self.peers[0].model_copy(update={"sender": 22})])

    def test_slow_approval_does_not_block_another_approval(self):
        entered, resume = Event(), Event()
        handle = self.store.handle

        def gated(sender, request_id, body, **kwargs):
            if sender == 11:
                entered.set()
                if not resume.wait(5):
                    raise RuntimeError("timed out waiting for test")
            return handle(sender, request_id, body, **kwargs)

        with patch.object(self.store, "handle", side_effect=gated), ThreadPoolExecutor(2) as pool:
            slow = pool.submit(self.call, 0, "policy")
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(pool.submit(self.call, 1, "policy").result(timeout=2)["user_id"], 22)
            finally:
                resume.set()
            self.assertTrue(slow.result(timeout=2)["ok"])

    def test_locked_ledger_returns_busy_without_committing_then_same_request_can_retry(self):
        rid = uuid4().hex
        reserve = {"revision": self.store.policy["revision"], "files": [file_row()], "pack_policy": "partial"}
        with self.store.connect() as db, patch("resourceflow.shared_http.LEDGER_WAIT", 0.05):
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ApprovalBusy, "审批繁忙（reserve）") as failed:
                self.call(0, "reserve", rid, **reserve)
            self.assertFalse(getattr(failed.exception, "definitive", False))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM claims").fetchone()[0], 0)
        approved = self.call(0, "reserve", rid, **reserve)
        self.assertEqual(len(approved["approved"]), 1)
        self.assertEqual(self.call(0, "reserve", rid, **reserve), approved)

    def test_parallel_reservations_still_create_only_one_claim(self):
        reserve = {"revision": self.store.policy["revision"], "files": [file_row()], "pack_policy": "partial"}
        with ThreadPoolExecutor(2) as pool:
            calls = [pool.submit(self.call, index, "reserve", **reserve) for index in range(2)]
            outcomes = [result.result(timeout=5) for result in calls]
        self.assertEqual(sorted(len(outcome["approved"]) for outcome in outcomes), [0, 1])
        self.assertEqual(len(self.store.recent()), 1)

    def test_saturated_listener_returns_busy_without_starting_extra_worker(self):
        socket = self.socket.with_name("limited.sock")
        with patch("resourceflow.shared_http.MAX_HANDLERS", 1):
            limited = ApprovalServer(socket, self.store, self.peers)
        self.addCleanup(limited.stop)
        entered, resume = Event(), Event()
        handle = self.store.handle

        def gated(*args, **kwargs):
            entered.set()
            if not resume.wait(5):
                raise RuntimeError("timed out waiting for test")
            return handle(*args, **kwargs)

        def policy(i):
            return HTTPSApprovalRPC("", self.tokens[i], self.authority, local_socket=socket).exchange(
                uuid4().hex, self.body(i, "policy"))

        with patch.object(self.store, "handle", side_effect=gated) as dispatch, ThreadPoolExecutor(1) as pool:
            first = pool.submit(policy, 0)
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaises(ApprovalBusy):
                    policy(1)
                dispatch.assert_called_once()
            finally:
                resume.set()
            self.assertTrue(first.result(timeout=2)["ok"])

    def test_local_response_timeout_does_not_point_to_dns_or_certificates(self):
        with patch("resourceflow.shared_http._UnixHTTPConnection") as connection:
            connection.return_value.getresponse.side_effect = TimeoutError
            with self.assertRaisesRegex(SharedError, "本机私网审批等待响应超时（policy）"):
                self.call(0, "policy")

    def test_https_client_does_not_follow_redirects(self):
        with patch("resourceflow.shared_http.HTTPSConnection") as connection:
            connection.return_value.getresponse.return_value.status = 302
            with self.assertRaisesRegex(SharedError, "302"):
                HTTPSApprovalRPC("approval.example.ts.net", self.tokens[0], self.authority).exchange(
                    uuid4().hex, self.body(0, "policy"))
            connection.assert_called_once_with("approval.example.ts.net", port=443, timeout=10)
            connection.return_value.request.assert_called_once()


class PluginHTTPSMigrationTest(TestCase):
    def setUp(self):
        fixture = arbitration.PluginSetupTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.plugin = fixture.plugin
        self.plugin._refresh_scheduler = Mock()
        self.previous = SharedSettingsPayload(enabled=True, bot_username="@testbot")
        self.plugin._shared_settings = self.previous
        self.authority = "a" * 32
        self.https = SharedSettingsPayload(enabled=True, bot_username="@testbot", transport="https",
                                          https_host="approval.example.ts.net", https_token="A" * 43,
                                          https_authority_id=self.authority)

    def test_https_failure_cannot_post_to_bot_or_mark_request_denied(self):
        self.plugin._shared_settings = self.https
        with patch.object(self.plugin, "_https_shared_exchange", side_effect=SharedError("offline")) as exchange, \
                self.assertRaises(SharedError) as failed:
            self.plugin._shared_exchange(uuid4().hex, {"op": "policy"})
        self.assertFalse(getattr(failed.exception, "definitive", False))
        exchange.assert_called_once()
        self.assertIn("shared_retry_at_https", [call.args[0] for call in self.plugin.save_data.call_args_list])

    def test_coordinator_busy_does_not_back_off_other_requests(self):
        with patch.object(self.plugin, "_https_shared_exchange", side_effect=[
            ApprovalBusy("协调端审批繁忙"), {"ok": True},
        ]) as exchange:
            with self.assertRaises(ApprovalBusy):
                self.plugin._shared_exchange(uuid4().hex, {"op": "policy"})
            self.assertEqual(self.plugin._shared_retry_at, 0)
            self.assertTrue(self.plugin._shared_exchange(uuid4().hex, {"op": "policy"})["ok"])
        self.assertEqual(exchange.call_count, 2)

    def test_migrating_does_not_use_telegram_backoff_and_keeps_instance(self):
        p = self.plugin
        original_instance = p._shared_instance_id
        p.get_data.side_effect = {"shared_retry_at": 10**10}.get
        p._load_shared_backoff()
        self.assertEqual(p._shared_retry_at, 0)
        p._shared_claims = {"r": {"request_id": "r", "phase": "queued", "approved": {"f": "claim"}}}
        with patch.object(p, "_https_shared_exchange", side_effect=[
            {"authority_id": self.authority, "authority_version": 3},
            {"claims": [{"claim": "claim", "state": "queued"}]},
        ]):
            # Status rendering is covered by ordinary plugin tests.
            with patch.object(p, "get_shared_status", return_value=Mock(success=True)):
                self.assertTrue(p.update_shared_settings(self.https).success)
        self.assertEqual(p._shared_instance_id, original_instance)
        self.assertEqual(p._shared_claims["r"]["approved"], {"f": "claim"})
        self.assertEqual(p._shared_retry_at, 0)

    def test_wrong_authority_keeps_original_transport(self):
        p = self.plugin
        p._shared_probe = {"authority_id": "c" * 32}
        with patch.object(p, "_https_shared_exchange") as exchange:
            self.assertFalse(p.update_shared_settings(self.https).success)
        exchange.assert_not_called()
        self.assertIs(p._shared_settings, self.previous)

    def test_https_only_coordinator_opens_existing_ledger_without_starting_bot(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        p = self.plugin
        self.addCleanup(p._stop_shared)
        p.get_data_path = Mock(return_value=directory.name)
        p._channel_dedup_attributes = ["resolution"]
        config = p._shared_settings
        from resourceflow.shared_protocol import digest
        old_bot = "@TestBot"
        route = digest([old_bot, config.group])[:16]
        original = CoordinatorStore(Path(directory.name) / f"coordinator-{route}.sqlite3",
                                    config.group, ["resolution"], 5, RULE_DEFAULTS)
        authority = original.authority()
        body = dict(v=1, group=config.group, instance=p._shared_instance_id, label="NAS",
                    op="policy")
        original.handle(11, uuid4().hex, body)
        token = self.https.https_token
        old_config = {"shared": dict(enabled=True, host_coordinator=True,
            bot_username=old_bot, bot_token="unused", group=config.group, transport="https",
            https_listener=True, telegram_listener=True, https_token=token, instance_id=p._shared_instance_id,
            https_peers=[dict(sender=11, instance_id=p._shared_instance_id,
                              token_hash=sha256(token.encode()).hexdigest())])}
        with patch.object(p, "_sync_shared_history"):
            p._init_shared(old_config)
            result = p._shared_exchange(uuid4().hex, {"op": "policy"})
        self.assertEqual(result["authority_id"], authority["authority_id"])
        self.assertEqual(result["user_id"], 11)
        self.assertNotIn("https_token", p.get_shared_status().data)
        self.assertNotIn("legacy_namespace", p.get_shared_status().data)
        self.assertNotIn("bot_token", p._shared_config())
        self.assertEqual(p._shared_config()["legacy_namespace"], old_bot)

    def test_retired_telegram_configuration_starts_without_any_network_fallback(self):
        p = self.plugin
        p._shared_settings = SharedSettingsPayload(
            enabled=True, transport="telegram", bot_username="@testbot", bot_token="old-token")
        with patch("resourceflow.shared_plugin.HTTPSApprovalRPC") as rpc:
            with self.assertRaisesRegex(SharedError, "填写本机私网审批密钥"):
                p._shared_exchange(uuid4().hex, {"op": "policy"})
            rpc.assert_not_called()
