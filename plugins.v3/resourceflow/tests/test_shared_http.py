from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from resourceflow.shared_coordinator import CoordinatorStore
from resourceflow.shared_http import ApprovalServer, HTTPSApprovalRPC, RPC_PATH, _UnixHTTPConnection
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
        with patch.object(self.plugin, "_https_shared_exchange", side_effect=SharedError("offline")), \
                patch("resourceflow.shared_plugin.ed2k_account") as account, self.assertRaises(SharedError) as failed:
            self.plugin._shared_exchange(uuid4().hex, {"op": "policy"})
        self.assertFalse(getattr(failed.exception, "definitive", False))
        account.assert_not_called()
        self.assertIn("shared_retry_at_https", [call.args[0] for call in self.plugin.save_data.call_args_list])

    def test_migrating_does_not_use_telegram_backoff_and_keeps_instance(self):
        p = self.plugin
        original_instance = p._shared_instance_id
        p._shared_retry_at = 10**10
        p._shared_claims = {"r": {"request_id": "r", "phase": "queued", "approved": {"f": "claim"}}}
        with patch.object(p, "_https_shared_exchange", side_effect=[
            {"authority_id": self.authority, "authority_version": 2},
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
        route = digest([config.bot_username, config.group])[:16]
        original = CoordinatorStore(Path(directory.name) / f"coordinator-{route}.sqlite3",
                                    config.group, ["resolution"], 5, RULE_DEFAULTS)
        authority = original.authority()
        body = dict(v=1, group=config.group, instance=p._shared_instance_id, label="NAS",
                    op="policy")
        original.handle(11, uuid4().hex, body)
        token = self.https.https_token
        p._shared_settings = SharedSettingsPayload(enabled=True, host_coordinator=True,
            bot_username=config.bot_username, group=config.group, transport="https",
            https_listener=True, telegram_listener=False, https_token=token, https_peers=[
                ApprovalPeer(sender=11, instance_id=p._shared_instance_id,
                             token_hash=sha256(token.encode()).hexdigest())
            ])
        with patch.object(p, "_sync_shared_history"), patch("resourceflow.shared_plugin.BotWorker") as bot:
            p._start_shared_worker()
            result = p._shared_exchange(uuid4().hex, {"op": "policy"})
        bot.assert_not_called()
        self.assertEqual(result["authority_id"], authority["authority_id"])
        self.assertEqual(result["user_id"], 11)
        self.assertNotIn("https_token", p.get_shared_status().data)
