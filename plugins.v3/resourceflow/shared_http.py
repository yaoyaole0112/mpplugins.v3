"""ResourceFlow's dedicated approval RPC. TLS terminates at private Tailscale Serve."""

import fcntl
from hashlib import sha256
from hmac import compare_digest
from http.client import HTTPConnection, HTTPSConnection, HTTPException
from http.server import BaseHTTPRequestHandler
import os
from pathlib import Path
import re
import socket
from socketserver import ThreadingMixIn, UnixStreamServer
import sqlite3
import ssl
import threading
import time

from app.sdk.logging import logger

from .shared_protocol import ID_PATTERN, MAX_BYTES, SharedError, canonical, decode_json


RPC_PATH = "/resourceflow-approval/v1/rpc"
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
HOST_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.ts\.net")
OPS = frozenset({"policy", "reserve", "records", "sync_records", "inspect", "update", "import_completed"})
SOCKET_SUBDIRECTORY = "approval-private"
SOCKET_NAME = "rpc.sock"
CLIENT_TIMEOUT = 10
LEDGER_WAIT = 3
MAX_HANDLERS = 8
BUSY_RESPONSE = b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


class ApprovalBusy(SharedError):
    """The coordinator answered, but could not process this request yet."""


def approval_socket(data_path):
    return Path(data_path) / SOCKET_SUBDIRECTORY / SOCKET_NAME


class ApprovalHandler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def log_message(self, *_):
        pass

    def address_string(self):
        return "local"

    def send_error(self, code, *_, **__):
        self.reply(code)

    def reply(self, status, value=None):
        payload = canonical(value if value is not None else {"ok": False}).encode()
        if len(payload) > MAX_BYTES:
            status, payload = 500, b'{"ok":false}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        if self.path != RPC_PATH:
            return self.reply(404)
        auth = self.headers.get_all("Authorization", [])
        token = auth[0][7:] if len(auth) == 1 and auth[0].startswith("Bearer ") else ""
        if not TOKEN_PATTERN.fullmatch(token):
            return self.reply(401)
        credential = sha256(token.encode("ascii")).hexdigest()
        # Match the credential first: never trust a sender or instance from the request.
        owner = None
        for peer in self.server.peers:
            if compare_digest(peer.token_hash, credential):
                owner = peer
        if owner is None:
            return self.reply(401)
        lengths = self.headers.get_all("Content-Length", [])
        if (self.headers.get("Transfer-Encoding") is not None or len(lengths) != 1
                or not re.fullmatch(r"[1-9][0-9]{0,5}", lengths[0])
                or self.headers.get_content_type() != "application/json"):
            return self.reply(400)
        length = int(lengths[0])
        if length > MAX_BYTES:
            return self.reply(413)
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                return self.reply(400)
            envelope = decode_json(raw)
            request_id, body = envelope.get("request_id"), envelope.get("body")
            if (not isinstance(request_id, str) or not ID_PATTERN.fullmatch(request_id)
                    or not isinstance(body, dict) or not isinstance(body.get("op"), str)
                    or body["op"] not in OPS):
                return self.reply(400)
            if body.get("instance") != owner.instance_id:
                return self.reply(403)
            return self.dispatch(owner, request_id, body, envelope.get("authority_id"))
        except (BrokenPipeError, ConnectionResetError):
            return
        except SharedError:
            return self.reply(400)


    def dispatch(self, owner, request_id, body, authority_id):
        started = time.monotonic()
        op = body["op"]
        store = self.server.store
        try:
            if authority_id != store.authority(timeout=LEDGER_WAIT)["authority_id"]:
                return self.reply(409)
            return self.reply(200, store.handle(owner.sender, request_id, body, timeout=LEDGER_WAIT))
        except sqlite3.OperationalError as error:
            if (getattr(error, "sqlite_errorcode", 0) & 0xff) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                return self.reply(503)
            logger.warning(f"私网审批账本操作异常：{op} · {type(error).__name__}")
            return self.reply(500)
        except (OSError, SharedError):
            raise
        except Exception as error:
            logger.warning(f"私网审批请求处理异常：{op} · {type(error).__name__}")
            return self.reply(500)
        finally:
            elapsed = time.monotonic() - started
            if elapsed >= 2:
                logger.warning(f"私网审批请求耗时：{op} · {request_id} · {elapsed:.2f} 秒")


class _PrivateServer(ThreadingMixIn, UnixStreamServer):
    request_queue_size = 16
    # Use HTTP parsing without exposing an IP listener.
    server_name = "resourceflow"
    server_port = 0

    def __init__(self, *args):
        self._slots = threading.BoundedSemaphore(MAX_HANDLERS)
        super().__init__(*args)

    def process_request(self, request, client_address):
        # Do not grow the worker pool or block the accept loop under load.
        if not self._slots.acquire(blocking=False):
            try:
                request.setblocking(False)
                request.sendall(BUSY_RESPONSE)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        pass


class ApprovalServer:
    def __init__(self, path, store, peers):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = os.open(self.path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.server = None
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.path.unlink(missing_ok=True)
            self.server = _PrivateServer(str(self.path), ApprovalHandler)
            self.path.chmod(0o600)
            self.server.store, self.server.peers = store, tuple(peers)
            self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.2},
                                           name="ResourceFlowPrivateApproval", daemon=True)
            self.thread.start()
        except BaseException:
            if self.server is not None:
                self.server.server_close()
                self.path.unlink(missing_ok=True)
            os.close(self.lock)
            raise

    def stop(self):
        try:
            self.server.shutdown()
            self.thread.join()
            self.server.server_close()
            self.path.unlink(missing_ok=True)
        finally:
            os.close(self.lock)


class _UnixHTTPConnection(HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class HTTPSApprovalRPC:
    def __init__(self, host, token, authority_id, *, local_socket=None):
        if not TOKEN_PATTERN.fullmatch(token or "") or not ID_PATTERN.fullmatch(authority_id or ""):
            raise SharedError("私网审批密钥或协调端账本 ID 未配置")
        if local_socket is None and not HOST_PATTERN.fullmatch(host or ""):
            raise SharedError("私网审批必须使用 Tailscale HTTPS 域名")
        self.host, self.token, self.authority_id, self.local_socket = host, token, authority_id, local_socket

    def exchange(self, request_id, body):
        payload = canonical({"request_id": request_id, "authority_id": self.authority_id, "body": body}).encode()
        if len(payload) > MAX_BYTES:
            raise SharedError("私网审批申请批次过大")
        conn = (_UnixHTTPConnection(self.local_socket, timeout=CLIENT_TIMEOUT) if self.local_socket else
                HTTPSConnection(self.host, port=443, timeout=CLIENT_TIMEOUT))
        op = body.get("op")
        operation = f"（{op}）" if isinstance(op, str) and op in OPS else ""
        endpoint = "本机私网审批" if self.local_socket else "Tailscale 私网审批"
        phase = "连接或发送"
        try:
            conn.request("POST", RPC_PATH, payload, {
                "Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
            })
            phase = "等待响应"
            response = conn.getresponse()
            if response.status == 503:
                raise ApprovalBusy(f"协调端审批繁忙{operation}，本次暂缓")
            if response.status != 200:
                message = {401: "设备密钥未获授权", 403: "设备密钥与实例 ID 不匹配",
                           409: "协调端账本 ID 不匹配", 404: "未部署私网审批路径"}.get(response.status)
                raise SharedError(f"私网审批未确认：{message or f'HTTP {response.status}'}")
            result = decode_json(response.read(MAX_BYTES + 1))
        except TimeoutError:
            raise SharedError(f"{endpoint}{phase}超时{operation}，本次暂缓") from None
        except socket.gaierror:
            raise SharedError(f"{endpoint}域名解析失败{operation}") from None
        except ssl.SSLError:
            raise SharedError(f"{endpoint} TLS 握手或证书校验失败{operation}") from None
        except (OSError, HTTPException):
            raise SharedError(f"{endpoint}{phase}失败{operation}，暂未收到审批确认") from None
        finally:
            conn.close()
        if result.get("ok") is False:
            error = SharedError(str(result.get("error", "协调端拒绝申请"))[:240])
            error.definitive = True
            raise error
        if result.get("ok") is not True or result.get("authority_id") != self.authority_id:
            raise SharedError("私网审批回执未确认或账本 ID 不匹配")
        return result
