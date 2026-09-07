"""CLI-compatible lifecycle for the FastAPI loopback workbench."""

from __future__ import annotations

import socket
from threading import Event

import uvicorn

from .config import AppConfig
from .web_api.app import LocalSession, create_app
from .web_api.assets import load_assets
from .workbench import Workbench


class LocalServer:
    """Own the bound socket and session; retain the existing local lifecycle API."""

    def __init__(self, app: Workbench, *, port: int = 0):
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        self.app = app
        self.assets, _ = load_assets()
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.socket.bind(("127.0.0.1", port))
            self.socket.listen(8)
        except BaseException:
            self.socket.close()
            raise
        self.server_address = self.socket.getsockname()
        self.server_port = self.server_address[1]
        self.authority = f"127.0.0.1:{self.server_port}"
        self.capability = LocalSession(self.authority)
        self.origin = self.capability.origin
        self.session = self.capability.token
        self.stopped = Event()
        self.stopped.set()
        self.asgi = create_app(app, session=self.capability)
        self.server = uvicorn.Server(
            uvicorn.Config(
                self.asgi,
                host="127.0.0.1",
                port=self.server_port,
                workers=1,
                proxy_headers=False,
                access_log=False,
                log_config=None,
                log_level="critical",
                server_header=False,
                timeout_keep_alive=1,
                timeout_graceful_shutdown=5,
                h11_max_incomplete_event_size=16_384,
            )
        )

    @property
    def expires(self) -> float:
        return self.capability.expires

    @expires.setter
    def expires(self, value: float) -> None:
        self.capability.expires = value

    @property
    def url(self) -> str:
        return self.origin + "/#session=" + self.session

    def verify_request(self, request, client_address) -> bool:
        return client_address[0] == "127.0.0.1"

    def serve_forever(self, *, poll_interval: float = 0.2) -> None:
        self.stopped.clear()
        try:
            self.server.run(sockets=[self.socket])
        finally:
            self.stopped.set()

    def shutdown(self) -> None:
        self.server.should_exit = True
        if not self.stopped.wait(timeout=10):
            raise RuntimeError("local server did not complete shutdown")

    def server_close(self) -> None:
        self.shutdown()
        self.socket.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.shutdown()
        self.server_close()


def serve(config: AppConfig, *, port: int = 0) -> None:
    with LocalServer(Workbench(config), port=port) as server:
        print(f"RepoSteward 本地工作台：{server.url}", flush=True)
        print("仅本机、只读。链接在本次进程中有效；按 Ctrl+C 停止。", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
