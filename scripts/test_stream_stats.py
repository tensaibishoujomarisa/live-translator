"""验证「直播数据采集 + 自动调参」。

关注点：
  * 语速/字数算得对不对（标点不能算进语速）
  * 样本不足时不能乱调
  * 快/慢语速给出的阈值方向要对（快→阈值小）
  * 电平低时给增益
  * 落盘/重载/窗口/坏数据容错

    python -m scripts.test_stream_stats
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.stream_stats import StatsStore, _char_count  # noqa: E402

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
    tmp = Path(tempfile.mkdtemp(prefix="jplt_stats_"))
    path = tmp / "stats.jsonl"

    print("=" * 70)
    print(" 直播数据采集 / 自动调参验证")
    print("=" * 70)

    # ---------- 1. 字数统计 ----------
    print("\n[1] 字数统计（标点不该算进语速）")
    check("纯假名", _char_count("こんにちは") == 5, str(_char_count("こんにちは")))
    check("带标点", _char_count("こんにちは、元気？") == 7,
          str(_char_count("こんにちは、元気？")))
    check("带空格和英文", _char_count("FF14 やってる") == 8,
          str(_char_count("FF14 やってる")))
    check("空字符串", _char_count("") == 0)
    check("纯标点", _char_count("！！？？…") == 0, str(_char_count("！！？？…")))

    # ---------- 2. 空数据 ----------
    print("\n[2] 空数据不应该给出建议")
    st = StatsStore(path)
    p = st.profile()
    check("segments 为 0", p["segments"] == 0, str(p["segments"]))
    rec = st.recommend()
    check("样本不足时不覆盖参数", rec["overrides"] == {}, str(rec["overrides"]))
    check("有说明原因", any("样本不足" in n for n in rec["notes"]), str(rec["notes"]))

    # ---------- 3. 语速计算 ----------
    print("\n[3] 语速计算")
    st.add(audio_s=2.0, text="あいうえおかきくけこ", speech_ms=2000)   # 10 字 / 2s
    r = st.records()[-1]
    check("cps = 5.0", abs(r.cps - 5.0) < 0.01, str(r.cps))
    check("chars = 10", r.chars == 10, str(r.chars))
    st.add(audio_s=3.0, text="あ" * 21, speech_ms=2000)   # 21 字 / 2s 说话
    r2 = st.records()[-1]
    check("用说话时长算语速（不计静音）", abs(r2.cps - 10.5) < 0.05, str(r2.cps))

    # ---------- 4. 快语速 → 阈值调短 ----------
    print("\n[4] 快语速")
    p2 = tmp / "fast.jsonl"
    fast = StatsStore(p2)
    for _ in range(30):
        fast.add(audio_s=3.0, text="あ" * 21, speech_ms=2800, rms=0.05)
    rec2 = fast.recommend()
    p = rec2["profile"]
    check("识别出快语速（>=6.5）", p["cps_median"] >= 6.5, str(p["cps_median"]))
    check("断句阈值调短到 300ms",
          rec2["overrides"].get("vad.min_silence_ms") == 300,
          str(rec2["overrides"]))
    check("局部刷新加快", rec2["overrides"].get("asr.partial_interval_ms") == 450,
          str(rec2["overrides"]))
    check("电平正常时不加增益", "audio.gain" not in rec2["overrides"],
          str(rec2["overrides"]))

    # ---------- 5. 慢语速 → 阈值加长 ----------
    print("\n[5] 慢语速")
    slow = StatsStore(tmp / "slow.jsonl")
    for _ in range(30):
        slow.add(audio_s=4.0, text="あ" * 12, speech_ms=3800, rms=0.05)  # 12/3.8 ≈ 3.2
    rec3 = slow.recommend()
    check("断句阈值加长到 460ms",
          rec3["overrides"].get("vad.min_silence_ms") == 460,
          str(rec3["overrides"]))

    # ---------- 6. 电平低 → 增益 ----------
    print("\n[6] 电平低")
    quiet = StatsStore(tmp / "quiet.jsonl")
    for _ in range(30):
        quiet.add(audio_s=2.5, text="あ" * 12, speech_ms=2400, rms=0.004)
    rec4 = quiet.recommend()
    check("给出增益 3.0", rec4["overrides"].get("audio.gain") == 3.0,
          str(rec4["overrides"]))
    check("说明里提到电平", any("电平" in n for n in rec4["notes"]),
          str(rec4["notes"]))

    # ---------- 7. 落盘 / 重载 ----------
    print("\n[7] 落盘与重载")
    check("文件已生成", p2.exists())
    if p2.exists():
        lines = [x for x in p2.read_text(encoding="utf-8").splitlines() if x.strip()]
        check("行数 = 记录数", len(lines) == 30, f"{len(lines)} 行")
        check("每行都是合法 JSON", all(_ok(x) for x in lines))
    reloaded = StatsStore(p2)
    check("重载后条数一致", len(reloaded.records()) == 30,
          str(len(reloaded.records())))
    check("重载后仍能给出同样的建议",
          reloaded.recommend()["overrides"].get("vad.min_silence_ms") == 300,
          str(reloaded.recommend()["overrides"]))

    # ---------- 8. 窗口限制 ----------
    print("\n[8] 窗口限制")
    w = StatsStore(tmp / "win.jsonl", window=20)
    for i in range(50):
        w.add(audio_s=1.0, text="あ" * (i % 5 + 1), speech_ms=1000)
    check("内存里最多 window 条", len(w.records()) == 20, str(len(w.records())))

    # ---------- 9. 坏数据容错 ----------
    print("\n[9] 坏数据容错")
    bad = tmp / "bad.jsonl"
    bad.write_text(
        json.dumps({"audio_s": 2, "chars": 10, "cps": 5, "t": 1}) + "\n"
        + "这不是 JSON\n"
        + '{"audio_s": "x"}\n'
        + json.dumps({"audio_s": 1.5, "chars": 6, "cps": 4, "t": 2, "unknown_field": 1}) + "\n",
        encoding="utf-8")
    b = StatsStore(bad)
    check("跳过坏行、保留好行", len(b.records()) == 2, str(len(b.records())))

    # ---------- 10. 清空 ----------
    print("\n[10] 清空")
    n = reloaded.clear()
    check("清空返回条数", n == 30, str(n))
    check("内存已清空", len(reloaded.records()) == 0)
    check("文件已删除", not p2.exists())

    import shutil

    shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 70)
    print(f" 通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


def _ok(line: str) -> bool:
    try:
        json.loads(line)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(main())
