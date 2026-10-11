"""验证 SSE 的「粘性状态补发」：后端已经 ready 之后再连接，也应该立刻收到 ready。

    python -m scripts.test_sse_replay
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \u2714 {name}")
    else:
        FAIL += 1
        print(f"  \u2718 {name}" + (f"  ({detail})" if detail else ""))


def main() -> int:
    print("=" * 66)
    print(" SSE 粘性状态补发验证")
    print("=" * 66)

    # 用随机端口，避免连到用户正在跑的实例（它会占住 8765）
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "backend.main", "--serve", "--port", "0",
         "--provider", "off", "--model", "tiny"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", bufsize=1, cwd=str(ROOT),
    )
    stdout_lines: list[str] = []

    def reader() -> None:
        assert proc.stdout is not None
        for ln in proc.stdout:
            stdout_lines.append(ln.strip())

    threading.Thread(target=reader, daemon=True).start()

    port = None
    t0 = time.time()
    while time.time() - t0 < 180:
        for ln in list(stdout_lines):
            try:
                m = json.loads(ln)
            except Exception:
                continue
            if m.get("kind") == "server":
                port = m.get("port")
        if port:
            break
        time.sleep(0.3)
    if not port:
        print("[FAIL] 没拿到端口；输出尾部：", stdout_lines[-4:])
        proc.kill()
        return 3
    print(f"SSE 端口 {port}")

    # 关键：故意等到后端 ready 之后（而不是之前）才连接
    print("等待后端完成启动（ready 已经发过了）…")
    t0 = time.time()
    while time.time() - t0 < 180:
        if any('"ready"' in ln for ln in list(stdout_lines)):
            break
        time.sleep(0.3)
    ready_in_stdout = any('"ready"' in ln for ln in list(stdout_lines))
    check("后端已发出过 ready（stdout 可见）", ready_in_stdout)
    # 把 stdout 里 ready 那条完整打出来，确认它长什么样
    for ln in list(stdout_lines):
        if '"ready"' in ln:
            print(f"  stdout 里的 ready：{ln[:220]}")
            break
    print("再等 2 秒，确保 ready 早就过去了…")
    time.sleep(2.0)

    # 顺便问一下后端的 /config，确认服务确实是同一进程
    # 问一下后端：粘性状态里到底攒了什么
    def sticky() -> str:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/debug/sticky", timeout=5) as r:
                return r.read().decode()
        except Exception as exc:
            return f"(失败 {type(exc).__name__}: {exc})"

    print(f"  /debug/sticky -> {sticky()}")
    print("再等 2 秒，确保 ready 早就过去了…")
    time.sleep(2.0)
    print(f"  /debug/sticky -> {sticky()}")

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
            print(f"  /health -> {r.read().decode()}")
    except Exception as exc:
        print(f"  /health 失败：{exc}")

    print("\n现在才连接 /events，看看能不能补发：")
    got: list[dict] = []
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/events", timeout=25) as r:
            deadline = time.time() + 12
            for raw in r:
                s = raw.decode("utf-8", "ignore").strip()
                if s.startswith("data:"):
                    m = json.loads(s[5:])
                    got.append(m)
                    print(f"    +{time.time() - t0:5.1f}s  {m.get('kind')}"
                          + (f" / {m.get('stage')}" if m.get("stage") else ""))
                if time.time() > deadline or len(got) > 12:
                    break
    except Exception as exc:
        print(f"  [warn] 读取中断：{type(exc).__name__}: {exc}")

    kinds = [m.get("kind") for m in got]
    print(f"\n  收到 {len(got)} 条，种类：{sorted(set(kinds))}")
    print(f"  前 6 条明细：{[(m.get('kind'), m.get('stage')) for m in got[:6]]}")
    check("连接后立刻收到事件（不是空流）", len(got) > 0, f"{len(got)} 条")
    check("补发了 ready", "ready" in kinds, str(kinds))
    check("补发了 status", "status" in kinds, str(kinds))

    try:
        if proc.stdin:
            proc.stdin.write("quit\n")
            proc.stdin.flush()
        proc.wait(timeout=30)
    except Exception:
        proc.kill()

    print("\n" + "=" * 66)
    print(f" 通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 66)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
