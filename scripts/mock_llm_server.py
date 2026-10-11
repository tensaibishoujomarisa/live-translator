"""最小 OpenAI 兼容 mock 服务，用于在没有真实 API Key 的情况下验证 LLM 翻译链路。

它做的事：
  * 实现 POST /chat/completions（普通 JSON 和 SSE 流式两种）
  * 把收到的请求记录下来，供测试断言「上下文/术语表/系统提示有没有正确注入」
  * 可配置返回固定译文、或模拟 401 / 500 / 超时，用来验证错误处理

只监听 127.0.0.1，测试完即关，不碰外网。
"""
from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class MockLlmState:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.mode = "ok"            # ok | unauthorized | server_error | timeout | empty
        self.delay = 0.0
        # 默认吐中文（真实 LLM 就该这样）。做成可替换的，方便测「原文回显」过滤器。
        self.reply_template = "〔译〕这是模拟的中文译文"
        self.lock = threading.Lock()

    def record(self, body: dict, path: str) -> None:
        with self.lock:
            self.requests.append({"path": path, "body": body, "at": time.time()})

    def last_user_prompt(self) -> str:
        for r in reversed(self.requests):
            for m in reversed(r["body"].get("messages") or []):
                if m.get("role") == "user":
                    return str(m.get("content") or "")
        return ""

    def last_system_prompt(self) -> str:
        for r in reversed(self.requests):
            for m in r["body"].get("messages") or []:
                if m.get("role") == "system":
                    return str(m.get("content") or "")
        return ""

    def extract_source(self, body: dict) -> str:
        """从 user prompt 里把「日：xxx」那一行抠出来，模拟真实翻译。"""
        for m in reversed(body.get("messages") or []):
            if m.get("role") != "user":
                continue
            text = str(m.get("content") or "")
            hit = re.search(r"【本句】\s*\n?\s*日：(.*?)(?:\n|$)", text)
            if hit:
                return hit.group(1).strip()
        return ""


def make_handler(state: MockLlmState):
    class Handler(BaseHTTPRequestHandler):
        # 用 HTTP/1.0 语义：响应结束即关连接，避免客户端痴等
        protocol_version = "HTTP/1.0"
        server_version = "mock-llm"

        def log_message(self, *args):  # 静默
            pass

        def _send_json(self, obj: Any, code: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path.rstrip("/") in ("/v1/models", "/models"):
                return self._send_json({"object": "list", "data": [{"id": "mock-model"}]})
            return self._send_json({"error": "not found"}, 404)

        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8") if n else "{}"
            try:
                body = json.loads(raw)
            except Exception:
                body = {}
            state.record(body, self.path)

            auth = self.headers.get("Authorization") or ""
            if state.mode == "unauthorized" or not auth.startswith("Bearer "):
                return self._send_json(
                    {"error": {"message": "Invalid API key", "type": "invalid_request_error"}}, 401
                )
            if state.mode == "server_error":
                return self._send_json({"error": {"message": "boom"}}, 500)
            if state.mode == "timeout":
                time.sleep(30)
                return self._send_json({"error": {"message": "too slow"}}, 504)
            if state.delay:
                time.sleep(state.delay)

            src = state.extract_source(body)
            try:
                text = state.reply_template.format(src=src)
            except (KeyError, IndexError):
                text = state.reply_template

            if not body.get("stream"):
                if state.mode == "empty":
                    return self._send_json({"choices": []})
                return self._send_json(
                    {
                        "id": "chatcmpl-mock",
                        "object": "chat.completion",
                        "model": body.get("model", "mock"),
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": text},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 10, "completion_tokens": len(text)},
                    }
                )

            # ---- SSE 流式 ----
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            # 关键：不发 Content-Length，就必须声明 Connection: close，
            # 否则客户端会一直等「还有没有更多数据」，直到读超时。
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.flush()
            except Exception:
                pass

            def chunk(piece: str) -> None:
                payload = {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
                }
                self.wfile.write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()

            try:
                # 切成小块，模拟逐字生成
                for i in range(0, len(text), 2):
                    chunk(text[i:i + 2])
                    time.sleep(0.01)
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    return Handler


class MockLlmServer:
    def __init__(self, port: int = 0):
        self.state = MockLlmState()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(self.state))
        self._httpd.daemon_threads = True
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def stop(self) -> None:
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception:
            pass

    def __enter__(self) -> "MockLlmServer":
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
