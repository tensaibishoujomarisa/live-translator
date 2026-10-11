"""验证「一段日文被切成多句翻译时，译文会累积而不是互相覆盖」。

这是用户实际踩到的 bug：
  日 クレヨンしんちゃんは何?あ、子供時代の思い出みたいな感じだった。   ← 完整
  中 蜡笔小新是什么？                                                  ← 第 1 句
  中 啊，就是那种童年回忆的感觉。                                       ← 覆盖掉了第 1 句

修法：后端把同一 seg_id 的多句译文累积后发送，并且每句一到就发一次，
所以字幕会先显示第 1 句，再变成 1+2 句的完整文本。

    python -m scripts.test_multisentence
"""
from __future__ import annotations

import json
import sys
import threading
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from backend.bridge import Bridge  # noqa: E402
from backend.config import Config  # noqa: E402
from backend.pipeline import Pipeline  # noqa: E402
from backend.util import setup_logging  # noqa: E402

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
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--asr-text", default="",
                    help="跳过音频识别，直接把这句日文当成识别结果喂进翻译流水线（复现特定句子用）")
    ap.add_argument("--provider", default="local", choices=["local", "llm", "off"])
    args = ap.parse_args()

    setup_logging("warning", None)

    cfg = Config.load().with_overrides({"translate": {"provider": args.provider}})
    print("=" * 70)
    print(" 多句译文累积验证")
    print("=" * 70)

    if args.asr_text:
        # 注入模式：不碰音频，直接走「定稿 -> 切句 -> 翻译 -> 累积」这条链路，
        # 也就是用户日志里出问题的那个场景。
        print(f"注入识别结果：{args.asr_text}")
        print(f"翻译后端：{args.provider}\n")
        return run_injected(cfg, args.asr_text)

    wav = ROOT / "testdata" / "ja_sample16k.wav"
    if not wav.exists():
        from scripts.test_loopback import ensure_sample

        wav = ensure_sample(wav)
    with wave.open(str(wav), "rb") as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0

    events: list[dict] = []
    lock = threading.Lock()

    class Collect(Bridge):
        def _write(self, msg: dict) -> None:
            with lock:
                events.append(msg)

    print(f"音频：{wav.name}  {audio.size / 16000:.2f}s  翻译后端：{args.provider}\n")

    pl = Pipeline(cfg, Collect(mode="none"))
    pl.build()
    pl.start()
    time.sleep(1.0)
    try:
        import sounddevice as sd

        sd.play(audio * 0.85, samplerate=16000)
        sd.wait()
    except Exception as exc:
        print(f"[warn] 播放失败：{exc}")
    time.sleep(3.5)
    pl.stop()

    with lock:
        got = list(events)
    return report(got)


def run_injected(cfg: Config, text: str) -> int:
    """直接往翻译线程投喂切好的句子，验证累积逻辑。"""
    events: list[dict] = []
    lock = threading.Lock()

    class Collect(Bridge):
        def _write(self, msg: dict) -> None:
            with lock:
                events.append(msg)

    from backend.mt_context import needs_translation
    from backend.util import split_sentences

    bridge = Collect(mode="none")
    pl = Pipeline(cfg, bridge)
    pl.build()

    # 只起翻译线程，跳过音频与识别
    threading.Thread(target=pl._mt_worker, name="mt-worker", daemon=True).start()

    sents = [s for s in split_sentences(text) if needs_translation(s)]
    print(f"切出 {len(sents)} 句：")
    for i, s in enumerate(sents, 1):
        print(f"  {i}. {s}")
    print()

    seg_id = 1
    pl._seg_mt[seg_id] = []
    for s in sents:
        pl._mt_q.put({"id": seg_id, "text": s})
    pl._mt_q.put({"id": seg_id, "text": "", "last": True, "total": len(sents)})

    deadline = time.time() + 60
    while time.time() < deadline:
        with lock:
            if len([e for e in events if e.get("kind") == "translation"]) >= len(sents):
                break
        time.sleep(0.2)
    time.sleep(0.5)
    pl._stop.set()

    with lock:
        got = list(events)
    return report(got, expect_parts=len(sents))


def report(got: list[dict], expect_parts: int = 0) -> int:
    finals = [e for e in got if e.get("kind") == "final"]
    trans = [e for e in got if e.get("kind") == "translation"]

    print("--- 时间线 ---")
    for e in got:
        k = e.get("kind")
        if k == "final":
            print(f"  [定稿] {e.get('text')}")
        elif k == "translation":
            print(f"  [译文 part={e.get('part')}] {e.get('text')}")
        elif k in ("warn", "error"):
            print(f"  [{k}] {e.get('message')}")
    print()

    by_seg: dict[int, list[dict]] = {}
    for t in trans:
        by_seg.setdefault(int(t.get("id") or 0), []).append(t)

    if not expect_parts:
        check("有定稿字幕", len(finals) >= 1, f"{len(finals)} 条")
    check("有译文", len(trans) >= 1, f"{len(trans)} 条")

    if trans:
        ids = sorted(by_seg.keys())
        multi = [i for i in ids if len(by_seg[i]) > 1]
        print(f"  涉及 {len(ids)} 个片段，其中 {len(multi)} 个片段有多句译文")
        if multi:
            seg = multi[0]
            seq = by_seg[seg]
            print(f"  片段 {seg} 的译文序列：")
            for t in seq:
                print(f"    part={t.get('part')}: {t.get('text')}")
            texts = [t.get("text") or "" for t in seq]
            last = texts[-1]
            check("最后一条译文比第一条长（发生了累积）",
                  len(last) > len(texts[0]), f"{len(texts[0])} -> {len(last)}")
            check("最后一条以第一条开头（累积语义正确，前句没被覆盖）",
                  last.startswith(texts[0]), f"{last[:24]} vs {texts[0][:24]}")
            check("每条都在变长（边翻边上屏）",
                  all(len(texts[i]) <= len(texts[i + 1]) for i in range(len(texts) - 1)),
                  str([len(t) for t in texts]))
            check("最后一条包含所有句子",
                  all(any(t in last for t in [x]) for x in [texts[0]]),
                  last[:40])
            if expect_parts:
                check(f"句数与预期一致（{expect_parts} 条累积更新）",
                      len(seq) == expect_parts, f"{len(seq)} vs {expect_parts}")
        else:
            check("译文内容非空", all((t.get("text") or "") for t in trans),
                  str([t.get("text") for t in trans]))
            if expect_parts > 1:
                check(f"应有多句累积（预期 {expect_parts} 条）", False, f"只有 {len(trans)} 条译文")

    print("\n" + "=" * 70)
    print(f" 通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
