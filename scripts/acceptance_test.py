"""端到端验收测试：--serve / SSE / stdin 命令 / HTTP 命令 全链路。

比 test_pipeline 更"整机"：真的把后端当子进程拉起来，
走 HTTP SSE 订阅事件，走 stdin 和 HTTP 两种方式发控制命令。

    python -m scripts.acceptance_test
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import wave
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402


def free_port() -> int:
    """挑一个空闲端口。

    必须这样做：用户可能正开着一个字幕程序占着配置里的 8765，
    如果这里用固定端口，后端会自动换端口，而我们却去连 8765 —— 就接到了别人的进程上，
    测试结果完全不可信（这个坑真踩过）。
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def post(port: int, path: str, obj: dict) -> str:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        return resp.read().decode("utf-8")


def main() -> int:
    wav = ROOT / "testdata" / "ja_sample16k.wav"
    if not wav.exists():
        from scripts.test_loopback import ensure_sample

        wav = ensure_sample(wav)
    with wave.open(str(wav), "rb") as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0

    cmd_path = Path(tempfile.gettempdir()) / "jplt_acceptance_cmds.txt"
    cmd_path.write_text("", encoding="utf-8")

    print("=" * 70)
    print(" 验收测试：后端进程 + SSE + 控制命令")
    print("=" * 70)

    # stdin 用管道（不是文件）：文件当 stdin 时 EOF 会立刻到来，根本来不及发命令
    want_port = free_port()
    print(f"（本次使用端口 {want_port}，避开你可能正在运行的实例）")
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "backend.main", "--serve", "--port", str(want_port),
         "--stdin-commands", "--provider", "local"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", bufsize=1, cwd=str(ROOT),
    )
    stdout_lines: list[str] = []

    def send(cmd: str) -> None:
        if proc.stdin and proc.stdin.writable:
            proc.stdin.write(cmd + "\n")
            proc.stdin.flush()

    def reader() -> None:
        assert proc.stdout is not None
        for ln in proc.stdout:
            stdout_lines.append(ln.strip())

    threading.Thread(target=reader, daemon=True).start()

    # 1) 等后端报出 SSE 端口
    port = None
    t0 = time.time()
    while time.time() - t0 < 150:
        for ln in list(stdout_lines):
            try:
                m = json.loads(ln)
            except Exception:
                continue
            if m.get("kind") == "server":
                port = m.get("port")
        if port:
            break
        time.sleep(0.4)
    if not port:
        print("[FAIL] 没等到 SSE 端口")
        print("       最后几行：", stdout_lines[-5:])
        proc.kill()
        return 3
    if port != want_port:
        print(f"[warn] 后端实际用了 {port}（请求的是 {want_port}）")
    print(f"[ OK ] SSE 服务端口 {port}")

    # 确认连的是「我们自己拉起来的这个进程」，而不是别人的实例
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/debug/sticky", timeout=5) as r:
            r.read()
        print("[ OK ] 确认是本进程（/debug/sticky 存在）")
    except Exception as exc:
        print(f"[FAIL] /debug/sticky 不可用，可能连到了别的进程：{exc}")
        proc.kill()
        return 4

    # 2) health
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=6) as r:
        print("[ OK ] /health ->", r.read().decode("utf-8"))

    # 3) 订阅 SSE
    events: list[dict] = []

    def sse() -> None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/events", timeout=120) as r:
                for raw in r:
                    s = raw.decode("utf-8", "ignore").strip()
                    if s.startswith("data:"):
                        events.append(json.loads(s[5:]))
        except Exception:
            pass

    threading.Thread(target=sse, daemon=True).start()

    t0 = time.time()
    while time.time() - t0 < 150 and not any(m.get("kind") == "ready" for m in events):
        time.sleep(0.5)
    ready = any(m.get("kind") == "ready" for m in events)
    print(f"[{' OK ' if ready else 'FAIL'}] 后端 ready 事件")

    # 4) 播放日语，看字幕
    try:
        import sounddevice as sd

        sd.play(audio * 0.85, samplerate=16000)
        sd.wait()
        time.sleep(4.0)
    except Exception as exc:
        print(f"[warn] 播放失败：{type(exc).__name__}: {exc}")

    # 5) stdin 命令
    for c in ("status", "pause", "resume"):
        send(c)
        time.sleep(0.9)
    print("[ OK ] 已通过 stdin 发送 status / pause / resume")

    # 6) HTTP 命令
    print("[ OK ] POST /command toggle-pause ->", post(port, "/command", {"name": "toggle-pause"}))
    print("[ OK ] POST /tuning              ->", post(port, "/tuning", {"vad": {"threshold": 0.45}}))
    print("[ OK ] POST /command switch-prov ->", post(port, "/command", {"name": "switch-provider", "provider": "off"}))

    # 7) 优雅退出
    send("quit")
    try:
        proc.wait(timeout=30)
        print("[ OK ] quit 命令让后端正常退出")
    except Exception:
        proc.kill()
        print("[warn] quit 后 30s 没退出，已强杀")

    time.sleep(0.5)
    counts = Counter(m.get("kind") for m in events)
    print("\nSSE 事件统计：", dict(counts))
    print("\n--- 字幕时间线 ---")
    for m in events:
        k = m.get("kind")
        if k == "partial":
            print(f"  … {m.get('text', '')[:60]}")
        elif k == "final":
            print(f"  日 {m.get('text', '')}")
        elif k == "translation":
            print(f"  中 {m.get('text', '')}   [{m.get('latency_ms')}ms]")
        elif k in ("status", "warn", "error"):
            print(f"  · [{k}] {m.get('stage', '')} {m.get('message', '')}")

    print("\n--- 判定 ---")
    ok = True
    checks = [
        ("SSE 端口", bool(port)),
        ("ready 事件", ready),
        ("定稿字幕", counts.get("final", 0) > 0),
        ("译文", counts.get("translation", 0) > 0),
        ("无错误事件", counts.get("error", 0) == 0),
    ]
    for name, good in checks:
        print(f"  {'✔' if good else '✘'} {name}")
        ok = ok and good
    print("\n" + ("验收通过 ✔" if ok else "验收未通过 ✘"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
