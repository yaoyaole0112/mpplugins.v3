"""Exercise the production approval listener through a temporary private Serve route."""

import argparse
from contextlib import closing
from hashlib import sha256
from http.client import HTTPSConnection
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import threading
from types import ModuleType
import sys
from uuid import uuid4


PATH = "/resourceflow-smoke/v1/rpc"
GROUP = "private-approval-smoke"
CLIENTS = ("a" * 32, "b" * 32)


def serve(tokens, directory):
    # Load the coordinator directly, not the MoviePilot plugin lifecycle.
    package = ModuleType("resourceflow")
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    sys.modules["resourceflow"] = package
    from resourceflow.shared_coordinator import CoordinatorStore
    from resourceflow.shared_http import ApprovalServer
    from resourceflow.shared_models import ApprovalPeer
    from resourceflow.shared_protocol import RULE_DEFAULTS
    store = CoordinatorStore(directory / "smoke.sqlite3", GROUP, ["resolution", "effects"], 5, RULE_DEFAULTS)
    peers = [ApprovalPeer(instance_id=instance, sender=index + 1, token_hash=sha256(tokens[index].encode()).hexdigest())
             for index, instance in enumerate(CLIENTS)]
    listener = ApprovalServer(directory / "private" / "rpc.sock", store, peers)
    print(store.authority()["authority_id"], flush=True)
    try:
        threading.Event().wait()
    finally:
        listener.stop()


def bridge(sock, container, ip):
    with sock, subprocess.Popen(
        ["docker", "exec", "-i", container, "tailscale", "nc", ip, "443"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    ) as proc:
        try:
            while True:
                ready, _, _ = select.select([sock, proc.stdout], [], [], 10)
                if not ready:
                    break
                for stream in ready:
                    data = os.read(stream.fileno(), 65536)
                    if not data:
                        return
                    if stream is sock:
                        proc.stdin.write(data)
                        proc.stdin.flush()
                    else:
                        sock.sendall(data)
        except OSError:
            pass
        finally:
            proc.terminate()


def smoke(tokens, host, connect_via, authority):
    def call(op, client=0, rid=None, auth=None, expected=200, instance=None, path=PATH, **fields):
        auth = tokens[client] if auth is None else auth
        body = dict(v=1, group=GROUP, instance=instance or CLIENTS[client], label=f"smoke-{client}", op=op, **fields)
        payload = json.dumps(dict(request_id=rid or uuid4().hex, authority_id=authority, body=body))
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer " + auth
        worker = None
        with closing(HTTPSConnection(host, timeout=8)) as conn:
            if connect_via:
                def connect(*args, **kwargs):
                    nonlocal worker
                    with socket.socket() as listener:
                        listener.bind(("127.0.0.1", 0))
                        listener.listen()
                        client_socket = socket.create_connection(listener.getsockname(), timeout=8)
                        server_socket, _ = listener.accept()
                    client_socket.settimeout(8)
                    worker = threading.Thread(target=bridge, args=(server_socket, *connect_via))
                    worker.start()
                    return client_socket
                conn._create_connection = connect
            try:
                conn.request("POST", path, payload, headers)
                reply = conn.getresponse()
                raw = reply.read()
                result = json.loads(raw) if reply.getheader("Content-Type") == "application/json" else {"ok": False}
                assert reply.status == expected, (op, reply.status, result)
                return result
            finally:
                conn.close()
                if worker:
                    worker.join()

    assert not call("policy", auth="", expected=401)["ok"]
    assert not call("policy", auth="z" * 43, expected=401)["ok"]
    assert not call("policy", instance=CLIENTS[1], expected=403)["ok"]
    assert not call("policy", path="/api/v1/user", expected=404)["ok"]
    print("PASS unauthorized credentials, wrong instance and MoviePilot admin route rejected")
    policy = call("policy")
    scope = dict(authority_id=policy["authority_id"], revision=policy["policy"]["revision"])
    cursor = call("sync_records", client=1, cursor=0, **scope)["cursor"]

    def reserve(client, episode, rid=None):
        files = [dict(id=f"{episode:024x}", title="Smoke fixture", media_type="tv", tmdb_id=900000001,
                      season=1, episodes=[episode], specs={"resolution": "1080p", "effects": "SDR"})]
        result = call("reserve", client=client, rid=rid, revision=scope["revision"], pack_policy="partial", files=files)
        assert result["ok"], result
        return result

    def update(client, claim, state, absent=False):
        return call("update", client=client, updates=[dict(claim=claim, state=state)], confirmed_absent=absent)

    rid = uuid4().hex
    approval = reserve(0, 1, rid)
    claim = approval["approved"][0]["claim"]
    assert reserve(0, 1, rid) == approval
    assert reserve(1, 1)["rejected"][0]["state"] == "reserved"
    assert not update(1, claim, "completed")["ok"]
    for state in ("queued", "completed"):
        assert update(0, claim, state)["ok"]
    assert reserve(1, 1)["rejected"][0]["state"] == "completed"
    print("PASS reserve, idempotent retry, cross-client dedup, queue and complete")
    claim_2 = reserve(0, 2)["approved"][0]["claim"]
    assert not update(0, claim_2, "released")["ok"]
    assert update(0, claim_2, "released", absent=True)["ok"]
    claim_3 = reserve(1, 2)["approved"][0]["claim"]
    assert claim_3 != claim_2
    delta = call("sync_records", client=1, cursor=cursor, **scope)
    assert not delta["reset"] and not delta["more"]
    assert {item["id"]: item["record"]["state"] for item in delta["changes"]} == {
        claim: "completed", claim_2: "released", claim_3: "reserved",
    }
    end = call("sync_records", client=1, cursor=delta["cursor"], **scope)
    assert not end["changes"] and end["cursor"] == delta["cursor"]
    print("PASS verified release, reapproval, incremental synchronization and no-change cursor")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("serve", "check"))
    parser.add_argument("--host", help="Tailnet HTTPS hostname for check mode")
    parser.add_argument("--via", nargs=2, metavar=("CONTAINER", "TAILSCALE_IP"))
    parser.add_argument("--authority")
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    tokens = os.environ["RF_SMOKE_TOKENS"].split(",")
    if len(tokens) != 2 or any(len(token) != 43 or not token.isascii() for token in tokens):
        parser.error("RF_SMOKE_TOKENS must contain two fresh 43-character credentials")
    if args.mode == "serve":
        if not args.directory:
            parser.error("--directory is required in serve mode")
        serve(tokens, args.directory)
    else:
        if not args.host or not args.authority:
            parser.error("--host and --authority are required in check mode")
        smoke(tokens, args.host, args.via, args.authority)
