"""本地 HTTP 服务：给字幕窗 / 第三方（OBS 浏览器源、网页）提供事件流。

  GET  /health           健康检查
  GET  /events           SSE 事件流（同一份 JSON 协议）
  GET  /config           当前配置（脱敏）
  POST /tuning           运行时调整参数
  POST /command          控制：pause/resume/quit/switch-provider
"""
from __future__ import annotations

import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .bridge import Bridge
from .util import get_logger

log = get_logger("jplt.server")


class _State:
    def __init__(self) -> None:
        self.cfg_snapshot: dict = {}
        self.apply_tuning: Callable[[dict], None] | None = None
        self.command: Callable[[str, dict], dict] | None = None
        # 「粘性状态」：ready / status 这类只发一次的事件，新连接进来要补发一遍，
        # 否则晚连上的客户端（OBS、网页、断线重连）永远等不到 ready。
        self.sticky: list[dict] = []
        self.sticky_lock = threading.Lock()
        self.max_sticky = 30

    def remember(self, msg: dict) -> None:
        kind = msg.get("kind")
        if kind not in ("ready", "status", "warn", "error", "server"):
            return
        with self.sticky_lock:
            # status 只保留最新的同 stage 一条，避免堆积一堆过程消息
            if kind == "status":
                stage = msg.get("stage")
                self.sticky = [m for m in self.sticky
                               if not (m.get("kind") == "status" and m.get("stage") == stage)]
            elif kind in ("ready", "server"):
                self.sticky = [m for m in self.sticky if m.get("kind") != kind]
            self.sticky.append(msg)
            if len(self.sticky) > self.max_sticky:
                self.sticky = self.sticky[-self.max_sticky:]

    def snapshot(self) -> list[dict]:
        with self.sticky_lock:
            return list(self.sticky)


def make_handler(bridge: Bridge, state: _State):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "jp-live-translator"

        # 静默默认日志，避免刷屏
        def log_message(self, fmt, *args):  # noqa: ANN001
            log.debug("%s - %s", self.address_string(), fmt % args)

        # ---------- helpers ----------
        def _cors(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")

        def _json(self, obj: Any, code: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0:
                    return {}
                return json.loads(self.rfile.read(n).decode("utf-8")) or {}
            except Exception:
                return {}

        # ---------- routes ----------
        def do_OPTIONS(self):  # noqa: N802
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/health":
                return self._json({"ok": True, "service": "jp-live-translator"})
            if path == "/config":
                return self._json(state.cfg_snapshot)
            if path == "/debug/sticky":
                # 诊断用：看看「粘性状态」里到底攒了什么
                snap = state.snapshot()
                return self._json({
                    "count": len(snap),
                    "items": [{"kind": m.get("kind"), "stage": m.get("stage")} for m in snap],
                })
            if path == "/events":
                return self._sse()
            return self._json({"error": "not found", "path": path}, 404)

        def do_POST(self):  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            payload = self._read_json()
            if path == "/tuning":
                if state.apply_tuning:
                    try:
                        state.apply_tuning(payload)
                    except Exception as exc:
                        return self._json({"ok": False, "error": str(exc)}, 400)
                return self._json({"ok": True})
            if path == "/command":
                name = str(payload.get("name") or "").strip()
                if not name or not state.command:
                    return self._json({"ok": False, "error": "缺少 name"}, 400)
                try:
                    res = state.command(name, payload) or {}
                except Exception as exc:
                    return self._json({"ok": False, "error": str(exc)}, 400)
                return self._json({"ok": True, **res})
            return self._json({"error": "not found", "path": path}, 404)

        # ---------- SSE ----------
        def _sse(self) -> None:
            q = bridge.subscribe()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self._cors()
            self.end_headers()
            try:
                self.wfile.write(b": connected\n\n")
                # 先把已有的状态补发一遍，让晚连的客户端也能立刻知道当前状态
                for msg in state.snapshot():
                    data = json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
                    self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
                while True:
                    try:
                        msg = q.get(timeout=15.0)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    data = json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
                    self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            except Exception as exc:
                log.debug("SSE 断开: %s", exc)
            finally:
                bridge.unsubscribe(q)
                self.close_connection = True

    return Handler


class EventServer:
    def __init__(self, bridge: Bridge, host: str = "127.0.0.1", port: int = 8765):
        self.bridge = bridge
        self.host = host
        self.port = int(port)
        self.state = _State()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._sticky_thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> int:
        handler = make_handler(self.bridge, self.state)
        try:
            self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        except OSError:
            # 端口占用则自动换一个
            self._httpd = ThreadingHTTPServer((self.host, 0), handler)
        self._httpd.daemon_threads = True
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="http", daemon=True)
        self._thread.start()
        # 后台线程持续记录「粘性状态」，供后连的客户端补发
        self._sticky_thread = threading.Thread(target=self._sticky_worker, name="sticky", daemon=True)
        self._sticky_thread.start()
        log.info("事件服务 http://%s:%d/events", self.host, self.port)
        return self.port

    def _sticky_worker(self) -> None:
        q = self.bridge.subscribe(maxsize=512)
        try:
            while not self._stop.is_set():
                try:
                    msg = q.get(timeout=0.5)
                except queue.Empty:
                    continue
                self.state.remember(msg)
        except Exception:
            pass
        finally:
            self.bridge.unsubscribe(q)

    def stop(self) -> None:
        self._stop.set()
        if self._httpd:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
