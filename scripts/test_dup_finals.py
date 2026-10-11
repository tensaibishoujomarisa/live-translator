"""复现/排查「同一句字幕出现两遍」——「字幕作成者 字幕作成者」那种。

要区分两种完全不同的原因：
  A. 主播真的说了两遍（或音频里就是重复的）→ 正常，不用改
  B. 同一段音频被送进识别两次        → bug，要加去重

判据：连续两条定稿如果 **文本完全相同、时长接近、且 id 相邻**，
就高度可疑是 B。这个脚本把流水线跑起来，打印相邻定稿的对比。

    python -m scripts.test_dup_finals
    python -m scripts.test_dup_finals --asr-text "字幕作成者。字幕作成者。"   # 注入模式
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from backend.bridge import Bridge  # noqa: E402
from backend.config import Config  # noqa: E402
from backend.pipeline import Pipeline  # noqa: E402
from backend.util import setup_logging  # noqa: E402


def analyse(events: list[dict]) -> int:
    finals = [e for e in events if e.get("kind") == "final"]
    print("--- 所有定稿 ---")
    for e in finals:
        print(f"  id={e.get('id'):<5} dur={e.get('duration_ms')}ms  "
              f"识别={e.get('compute_ms')}ms  {e.get('text')}")

    print("\n--- 相邻定稿对比 ---")
    suspicious = 0
    for a, b in zip(finals, finals[1:]):
        same_text = (a.get("text") or "") == (b.get("text") or "")
        same_dur = abs((a.get("duration_ms") or 0) - (b.get("duration_ms") or 0)) < 200
        near_id = abs((b.get("id") or 0) - (a.get("id") or 0)) == 1
        gap = (b.get("t") or 0) - (a.get("t") or 0)
        flag = ""
        if same_text and same_dur and near_id and gap < 4000:
            flag = "  <== 可疑：文本与时长都一样，像是同一段音频跑了两遍"
            suspicious += 1
        elif same_text:
            flag = "  (文本相同，但时长/时间不同，可能主播真的说了两遍)"
        print(f"  id {a.get('id')} -> {b.get('id')}  间隔 {gap:.0f}ms  "
              f"文本相同={same_text} 时长相近={same_dur}{flag}")

    print(f"\n可疑重复：{suspicious} 处")
    return suspicious


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asr-text", default="", help="注入识别结果，跳过音频")
    ap.add_argument("--repeat-audio", type=int, default=1, help="把测试音频重复播放几遍")
    ap.add_argument("--provider", default="off", choices=["off", "local", "llm"])
    ap.add_argument("--seconds", type=float, default=0.0, help="不播放，只采集这么久")
    args = ap.parse_args()

    setup_logging("warning", None)
    cfg = Config.load().with_overrides({"translate": {"provider": args.provider}})

    events: list[dict] = []
    lock = threading.Lock()

    class Collect(Bridge):
        def _write(self, msg: dict) -> None:
            with lock:
                events.append(msg)

    print("=" * 70)
    print(" 重复定稿排查")
    print("=" * 70)

    bridge = Collect(mode="none")
    pl = Pipeline(cfg, bridge)
    pl.build()

    if args.asr_text:
        # 注入模式：直接往识别队列塞「同一段音频」两次，看会不会出两条一样的定稿
        print(f"注入模式：把同一段音频（文本={args.asr_text}）投递两次")
        from backend.asr_engine import AsrEngine

        fake = np.zeros(16000 * 2, dtype=np.float32)
        # 造一段有声音的音频，避免被 no_speech 过滤
        t = np.arange(fake.size) / 16000.0
        fake = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)

        eng: AsrEngine = pl.asr
        eng.ensure_loaded()
        orig = eng.transcribe

        def fake_transcribe(audio, **kw):
            r = orig(audio, **kw)
            r.text = args.asr_text
            r.raw = args.asr_text
            r.ok = True
            r.reason = ""
            r.sentences = [args.asr_text]
            return r

        eng.transcribe = fake_transcribe
        threading.Thread(target=pl._asr_worker, daemon=True).start()
        for _ in range(2):
            pl._enqueue_segment({"id": pl.counters["segments"] + 1, "audio": fake,
                                 "duration_ms": 2000, "speech_ms": 2000, "rms": 0.2,
                                 "reason": "test"})
            time.sleep(0.2)
        time.sleep(3.0)
        pl._stop.set()
    else:
        pl.start()
        time.sleep(1.0)
        wav = ROOT / "testdata" / "ja_sample16k.wav"
        if not wav.exists():
            from scripts.test_loopback import ensure_sample

            wav = ensure_sample(wav)
        import wave

        with wave.open(str(wav), "rb") as w:
            audio = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
        if args.seconds:
            print(f"只采集 {args.seconds}s，不播放（用来观察环境音）")
            time.sleep(args.seconds)
        else:
            try:
                import sounddevice as sd

                for i in range(max(1, args.repeat_audio)):
                    sd.play(audio * 0.85, samplerate=16000)
                    sd.wait()
                    print(f"  已播放第 {i + 1}/{args.repeat_audio} 遍")
                    time.sleep(0.3)
            except Exception as exc:
                print(f"[warn] 播放失败：{exc}")
            time.sleep(3.0)
        pl.stop()

    with lock:
        got = list(events)

    suspicious = analyse(got)

    from collections import Counter

    print("\n事件统计：", dict(Counter(e.get("kind") for e in got)))
    print()
    if suspicious:
        print("结论：发现可疑重复 ✘ —— 同一段音频很可能被识别了两次")
        return 1
    print("结论：没有发现可疑重复 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
