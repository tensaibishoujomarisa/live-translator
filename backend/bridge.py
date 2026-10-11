"""与 Electron/终端之间的消息通道。

协议：JSON Lines 写到 stdout（每行一个 JSON 对象）。
Electron 主进程按行解析后通过 IPC 转发给字幕窗；日志走 stderr。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from typing import Any


def _now_ms() -> float:
    return time.time() * 1000.0


class Bridge:
    """线程安全的事件发射器。"""

    def __init__(self, mode: str = "jsonl", stream=None, echo: bool = False):
        # mode: jsonl(给 Electron 用) | text(人看的调试输出) | none
        self.mode = mode
        self._stream = stream or sys.stdout
        self._lock = threading.Lock()
        self._echo = echo
        self._t0 = _now_ms()
        self._subs: list = []

    # ---------- 订阅（给本地 HTTP/SSE 转发用） ----------
    def subscribe(self, maxsize: int = 256):
        import queue as _q

        q: "_q.Queue[dict]" = _q.Queue(maxsize=maxsize)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q) -> None:
        with self._lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    def _fanout(self, msg: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except Exception:
                pass

    # ---------- 基础 ----------
    def emit(self, kind: str, **payload: Any) -> None:
        msg = {"t": round(_now_ms() - self._t0, 1), "kind": kind}
        msg.update(payload)
        self._write(msg)

    def _write(self, msg: dict) -> None:
        line = None
        self._fanout(msg)
        with self._lock:
            try:
                if self.mode == "jsonl":
                    line = json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
                    self._stream.write(line + "\n")
                elif self.mode == "text":
                    line = self._format_text(msg)
                    if line:
                        self._stream.write(line + "\n")
                self._stream.flush()
            except Exception:
                pass
        if line and self._echo:
            try:
                sys.stderr.write(line + "\n")
                sys.stderr.flush()
            except Exception:
                pass

    @staticmethod
    def _format_text(msg: dict) -> str:
        kind = msg.get("kind")
        ts = f"[{(msg.get('t') or 0) / 1000:8.2f}]"
        if kind == "status":
            return f"{ts} * {msg.get('stage', '')}: {msg.get('message', '')}"
        if kind == "final":
            return f"{ts} 日 {msg.get('text', '')}"
        if kind == "translation":
            return f"{ts} 中 {msg.get('text', '')}"
        if kind == "partial":
            return f"{ts} ... {msg.get('text', '')}"
        if kind == "error":
            return f"{ts} x {msg.get('where', '')}: {msg.get('message', '')}"
        if kind == "warn":
            return f"{ts} ! {msg.get('message', '')}"
        if kind == "stats":
            body = {k: v for k, v in msg.items() if k not in ("kind", "t")}
            return f"{ts} ~ {body}"
        if kind == "segment":
            return f"{ts} _ {msg.get('duration_ms')}ms rms={msg.get('rms')}"
        return f"{ts} ? {msg}"

    # ---------- 语义化快捷方法 ----------
    def status(self, stage: str, message: str, **extra: Any) -> None:
        self.emit("status", stage=stage, message=message, **extra)

    def warn(self, message: str, **extra: Any) -> None:
        self.emit("warn", message=message, **extra)

    def error(self, where: str, message: str, **extra: Any) -> None:
        self.emit("error", where=where, message=message, **extra)

    def partial(self, seg_id: int, text: str, **extra: Any) -> None:
        self.emit("partial", id=seg_id, text=text, **extra)

    def final(self, seg_id: int, text: str, **extra: Any) -> None:
        self.emit("final", id=seg_id, text=text, **extra)

    def translation(self, seg_id: int, text: str, **extra: Any) -> None:
        self.emit("translation", id=seg_id, text=text, **extra)

    def level(self, rms: float, peak: float, speaking: bool) -> None:
        self.emit("level", rms=round(rms, 5), peak=round(peak, 5), speaking=speaking)

    def stats(self, **payload: Any) -> None:
        self.emit("stats", **payload)

    def ready(self, **payload: Any) -> None:
        self.emit("ready", **payload)
