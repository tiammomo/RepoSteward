"""A bounded loopback-only HTTP adapter; no file server or mutation routes."""

from __future__ import annotations

import hmac
import json
import secrets
import socket
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from threading import BoundedSemaphore
from urllib.parse import parse_qs, urlsplit

from .config import AppConfig
from .external_tasks import TaskConflict
from .projects import ProjectError
from .workbench import Workbench

MAX_RESPONSE_BYTES = 400_000
SESSION_SECONDS = 12 * 60 * 60
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}
ROUTES = {
    "/api/overview": ("overview", (), ()),
    "/api/projects": ("projects", (), ()),
    "/api/workspace": ("workspace", ("project_id", "binding_id"), ("focus",)),
    "/api/code": ("code", ("project_id", "binding_id", "evidence_id"), ("start_line",)),
    "/api/tasks": ("tasks", ("project_id",), ()),
    "/api/task": ("task", ("project_id", "run_id"), ()),
    "/api/review": ("review", ("project_id", "run_id"), ()),
    "/api/settings": ("settings", (), ()),
}


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 8

    def __init__(self, app: Workbench, *, port: int = 0):
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        self.app = app
        self.session = secrets.token_urlsafe(32)
        self.expires = time.monotonic() + SESSION_SECONDS
        self.slots = BoundedSemaphore(4)
        self.assets = {
            route: (files("reposteward").joinpath("web", name).read_bytes(), mime)
            for route, (name, mime) in ASSETS.items()
        }
        super().__init__(("127.0.0.1", port), Handler)
        self.authority = f"127.0.0.1:{self.server_port}"
        self.origin = "http://" + self.authority

    @property
    def url(self) -> str:
        return self.origin + "/#session=" + self.session

    def get_request(self) -> tuple[socket.socket, tuple]:
        request, address = super().get_request()
        request.settimeout(5)
        return request, address

    def verify_request(self, request, client_address) -> bool:
        return client_address[0] == "127.0.0.1"

    def process_request(self, request, client_address) -> None:
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address) -> None:
        # Neither arbitrary requests, repository data nor local session keys go to logs.
        pass


class Handler(BaseHTTPRequestHandler):
    server: LocalServer
    server_version = "RepoSteward"
    sys_version = ""

    def log_message(self, format, *args) -> None:
        pass

    def _send(self, status: int, body: bytes, mime: str = "application/json") -> None:
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'none'",
        )
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: int, code: str, message: str) -> None:
        self._send(
            status, json.dumps({"error": {"code": code, "message": message}}).encode()
        )

    def send_error(self, code, message=None, explain=None) -> None:
        self._error(code, "invalid_request", "请求不可用。请使用工作台提供的入口。")

    def do_GET(self) -> None:
        try:
            self._get()
        except TaskConflict:
            self._error(409, "changed", "配置或工作区已变化，请核对后重新打开工作台。")
        except ProjectError:
            self._error(
                409,
                "binding_or_policy",
                "项目绑定或策略不可用，请在 CLI 检查关联与配置。",
            )
        except KeyError:
            self._error(
                404, "not_found", "所选项目、工作区或任务不存在，或不属于当前范围。"
            )
        except ValueError:
            self._error(
                400,
                "invalid_request",
                "请求超出读取边界，请检查选择或使用 CLI 查看完整内容。",
            )
        except (OSError, RuntimeError, sqlite3.Error):
            self._error(
                503,
                "unavailable",
                "本地数据暂不可读。请检查设置诊断；其他页面仍可使用。",
            )

    def _get(self) -> None:
        if (
            self.headers.get_all("Host") != [self.server.authority]
            or self.headers.get_all("Origin") not in (None, [self.server.origin])
            or self.headers.get("Sec-Fetch-Site", "none") not in {"none", "same-origin"}
        ):
            self._error(403, "origin", "仅接受当前本地工作台的请求。")
            return
        if (
            len(self.path) > 4096
            or len(str(self.headers)) > 16_000
            or self.headers.get_all("Transfer-Encoding")
            or self.headers.get_all("Content-Length") not in (None, ["0"])
        ):
            self._error(400, "invalid_request", "请求超出读取边界。")
            return
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or parsed.fragment or "%" in parsed.path:
            raise ValueError("invalid route")
        if parsed.path in self.server.assets and not parsed.query:
            body, mime = self.server.assets[parsed.path]
            self._send(200, body, mime)
            return
        authorization = self.headers.get_all("Authorization") or []
        if (
            len(authorization) != 1
            or not hmac.compare_digest(
                authorization[0].encode(), ("Bearer " + self.server.session).encode()
            )
            or time.monotonic() >= self.server.expires
        ):
            self._error(401, "session", "请用终端打印的本地会话链接重新打开工作台。")
            return
        route = ROUTES.get(parsed.path)
        if route is None:
            self._error(404, "not_found", "没有这个读取入口。")
            return
        method, required, optional = route
        query = parse_qs(
            parsed.query, strict_parsing=True, keep_blank_values=True, max_num_fields=6
        )
        if (
            not set(required).issubset(query)
            or not set(query).issubset((*required, *optional))
            or any(len(value) != 1 or len(value[0]) > 2000 for value in query.values())
        ):
            raise ValueError("invalid query")
        self.server.app.check_configuration()
        result = getattr(self.server.app, method)(**{k: v[0] for k, v in query.items()})
        self.server.app.check_configuration()
        body = json.dumps(result, ensure_ascii=False).encode()
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("read result exceeds response limit")
        self._send(200, body)

    def do_POST(self) -> None:
        self._error(405, "read_only", "工作台仅提供读取；请通过已有 CLI 执行明确操作。")

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST
    do_OPTIONS = do_POST
    do_HEAD = do_POST


def serve(config: AppConfig, *, port: int = 0) -> None:
    with LocalServer(Workbench(config), port=port) as server:
        print(f"RepoSteward 本地工作台：{server.url}", flush=True)
        print("仅本机、只读。链接在本次进程中有效；按 Ctrl+C 停止。", flush=True)
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
