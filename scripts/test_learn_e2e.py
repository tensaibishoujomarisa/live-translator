"""端到端验证修正链路：真的起后端进程，通过 stdin 发修正，确认落盘与统计。

和 test_learning 的分工：
  * test_learning     —— 库层面的逻辑（存储/相关度/注入/替换），纯内存 + mock LLM
  * test_learn_e2e    —— **协议层面**：Electron 走的就是这条 stdin 命令，
                          所以这里必须真的起一个后端进程来验

    python -m scripts.test_learn_e2e
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
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
    tmp = Path(tempfile.mkdtemp(prefix="jplt_e2e_"))
    corr = tmp / "corrections.jsonl"
    gloss = tmp / "glossary.yaml"
    # 术语表也给个临时文件：测试发的是 scope=term，会触发「自动写进术语表」，
    # 不隔离的话会改到用户真实的 glossary.yaml（真踩过这个坑）
    gloss.write_text("entries:\n  - ja: 既存\n    zh: 已有条目\n", encoding="utf-8")
    real_gloss = ROOT / "glossary.yaml"
    real_gloss_before = real_gloss.read_bytes() if real_gloss.exists() else None
    real_corr = ROOT / "data" / "corrections.jsonl"
    real_existed = real_corr.exists()

    print("=" * 70)
    print(" 修正链路端到端验证（Electron 走的就是这条命令）")
    print("=" * 70)
    print(f"测试修正库：{corr}")
    print(f"测试术语表：{gloss}\n")

    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "backend.main", "--stdin-commands",
         "--provider", "local", "--serve", "--port", "0",
         "--learning-path", str(corr), "--glossary-path", str(gloss),
         "--model", "tiny"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", bufsize=1, cwd=str(ROOT),
    )
    lines: list[str] = []
    threading.Thread(target=lambda: [lines.append(x.strip()) for x in proc.stdout],
                     daemon=True).start()

    def wait_for(marker: str, timeout: float = 150.0) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if any(marker in x for x in list(lines)):
                return True
            time.sleep(0.3)
        return False

    def send(cmd: str) -> None:
        if proc.stdin:
            proc.stdin.write(cmd + "\n")
            proc.stdin.flush()

    try:
        check("后端启动到 ready", wait_for('"kind":"ready"'))

        # 1) 记一条修正（Electron 发的就是这个格式）
        send("correct-json " + json.dumps(
            {"ja": "ぺこら", "wrong": "佩科拉", "right": "佩克拉", "scope": "term"},
            ensure_ascii=False))
        time.sleep(1.5)
        joined = "\n".join(lines)
        check("收到 corrected 回执", '"kind":"corrected"' in joined,
              joined[-200:])
        check("回执里带了统计", '"stats"' in joined)
        # scope=term 会被自动提升进术语表，提示文案随之变化，
        # 所以这里匹配共同前缀而不是整句
        check("给了界面提示", "已记住" in joined, joined[-200:])

        # 2) 文件真的写到了指定路径
        check("修正写进指定文件", corr.exists())
        if corr.exists():
            raw = corr.read_text(encoding="utf-8").strip()
            obj = json.loads(raw.splitlines()[-1])
            check("内容正确", obj.get("ja") == "ぺこら" and obj.get("right") == "佩克拉",
                  str(obj)[:120])
            check("记录了 scope", obj.get("scope") == "term", str(obj.get("scope")))

        # 3) 查询统计（注意：统计必须走**事件流**，不能 print 到 stdout，
        #    否则会污染 JSONL 协议、让按行解析的客户端报错）
        lines.clear()
        send("learn")
        time.sleep(1.5)
        joined2 = "\n".join(lines)
        check("learn 通过事件流返回（没污染 stdout）",
              '"kind":"learn_stats"' in joined2, joined2[-260:])
        check("统计显示 1 条",
              '"total":1' in joined2.replace(" ", ""), joined2[-260:])
        check("统计标记已启用",
              '"enabled":true' in joined2.replace(" ", ""), joined2[-260:])
        # 每一行都必须是合法 JSON，否则协议被污染了
        bad = []
        for ln in list(lines):
            ln = ln.strip()
            if not ln:
                continue
            try:
                json.loads(ln)
            except Exception:
                bad.append(ln[:60])
        check("stdout 每一行都是合法 JSON", not bad, str(bad[:3]))

        # 4) 重复记同一条不应增加
        lines.clear()
        send("correct-json " + json.dumps(
            {"ja": "ぺこら", "wrong": "佩科拉", "right": "佩克拉酱"},
            ensure_ascii=False))
        send("learn")
        time.sleep(1.5)
        joined3 = "\n".join(lines)
        check("同一原文再改仍只有 1 条", '"total":1' in joined3.replace(" ", ""),
              joined3[-260:])
        if corr.exists():
            n = len([x for x in corr.read_text(encoding="utf-8").splitlines() if x.strip()])
            check("文件里也没堆旧版本", n == 1, f"{n} 行")

        # 5) 空内容要被拒绝
        lines.clear()
        send("correct-json " + json.dumps({"ja": "x", "right": ""}, ensure_ascii=False))
        time.sleep(1.2)
        joined4 = "\n".join(lines)
        check("空的修正被拒绝", '"kind":"corrected"' not in joined4, joined4[-160:])

        send("quit")
        try:
            proc.wait(timeout=30)
        except Exception:
            proc.kill()
    finally:
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass
        # 确认没碰到真实数据
        if not real_existed and real_corr.exists():
            check("没有污染真实修正库", False, str(real_corr))
            try:
                real_corr.unlink()
            except Exception:
                pass
        else:
            check("没有污染真实修正库", True)

        # 术语表最容易出事：scope=term 会自动写入。
        # 之前没隔离路径，测试真的往用户的 glossary.yaml 里写过条目。
        if real_gloss_before is not None:
            now = real_gloss.read_bytes() if real_gloss.exists() else b""
            check("没有污染真实术语表", now == real_gloss_before,
                  "glossary.yaml 被改动了")
        else:
            check("没有污染真实术语表", not real_gloss.exists())

    import shutil

    shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 70)
    print(f" 通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
