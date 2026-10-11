"""完整流水线播放测试：把日语从扬声器放出来，走一遍真实的采集→切句→识别→翻译。

和 test_loopback 的区别：那个是「录制 + 离线分段」，这个跑的是**真正的后台线程流水线**
（AudioCapture → Segmenter → asr-worker / partial-worker → mt-worker），
所以能验证 partial 是否按时出现、事件顺序对不对、队列会不会积压。

    python -m scripts.test_pipeline
    python -m scripts.test_pipeline --provider llm      # 有 API Key 时连翻译一起验证
    python -m scripts.test_pipeline --wav other.wav
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
import wave
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from backend.bridge import Bridge  # noqa: E402
from backend.config import Config  # noqa: E402
from backend.util import rms, setup_logging  # noqa: E402


def load_wav16k(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise SystemExit(f"只支持 16bit wav，收到 width={width}")
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    if rate != 16000:
        from backend.util import resample_linear

        data = resample_linear(data, rate, 16000)
    return data


def ensure_sample(path: Path) -> Path:
    """测试音频不进仓库，缺了就调用 test_loopback 里的自动下载。"""
    if path.exists():
        return path
    from scripts.test_loopback import ensure_sample as _ensure

    return _ensure(path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", default=str(ROOT / "testdata" / "ja_sample16k.wav"))
    ap.add_argument("--repeat", type=int, default=2, help="重复播放几遍（模拟连续讲话）")
    ap.add_argument("--volume", type=float, default=0.85)
    ap.add_argument("--device", default="")
    ap.add_argument("--provider", default="off", choices=["off", "llm", "local"])
    ap.add_argument("--model", default="large-v3-turbo")
    args = ap.parse_args()

    setup_logging("info", None)

    wav = ensure_sample(Path(args.wav))
    audio = load_wav16k(wav)
    one = audio.size / 16000.0

    events: list[dict] = []
    lock = threading.Lock()

    class Collect(Bridge):
        def _write(self, msg: dict) -> None:
            with lock:
                events.append(msg)

    bridge = Collect(mode="none")
    cfg = Config.load().with_overrides(
        {
            "translate": {"provider": args.provider},
            "asr": {"model": args.model},
            "audio": {"device": args.device},
        }
    )

    from backend.pipeline import Pipeline

    pl = Pipeline(cfg, bridge)

    print("=" * 70)
    print(" 完整流水线播放测试")
    print("=" * 70)
    print(f"音频：{wav.name}  {one:.2f}s  播放 {args.repeat} 遍  翻译后端：{args.provider}")
    print(f"采集设备：{args.device or '（默认输出设备）'}")
    print()

    pl.build()
    pl.start()
    time.sleep(1.0)

    t_play0 = time.time()
    try:
        import sounddevice as sd

        for i in range(args.repeat):
            sd.play(audio * args.volume, samplerate=16000)
            sd.wait()
            time.sleep(0.35)  # 句间短暂停顿，模拟自然换气
            print(f"  已播放第 {i + 1}/{args.repeat} 遍")
    except Exception as exc:
        print(f"[warn] 播放失败：{type(exc).__name__}: {exc}")

    # 等 VAD 收尾 + 识别 + 翻译
    time.sleep(2.5)
    t_end = time.time()
    pl.stop()

    with lock:
        got = list(events)

    counts = Counter(e.get("kind") for e in got)
    print("\n事件统计：", dict(counts))

    finals = [e for e in got if e.get("kind") == "final"]
    partials = [e for e in got if e.get("kind") == "partial"]
    trans = [e for e in got if e.get("kind") == "translation"]
    drops = [e for e in got if e.get("kind") == "asr_drop"]
    errs = [e for e in got if e.get("kind") == "error"]

    print("\n--- 时间线（相对播放开始）---")
    for e in got:
        k = e.get("kind")
        if k in ("level", "stats", "segment"):
            continue
        rel = (e.get("t", 0) / 1000.0)
        if k == "partial":
            print(f"{rel:7.2f}s  … {e.get('text', '')[:70]}")
        elif k == "final":
            print(f"{rel:7.2f}s  日 {e.get('text', '')}")
            if e.get("compute_ms") is not None:
                print(f"            （识别 {e.get('compute_ms')}ms, 实时率 x{e.get('rtf')}）")
        elif k == "translation":
            print(f"{rel:7.2f}s  中 {e.get('text', '')}   [{e.get('latency_ms')}ms]")
        elif k == "asr_drop":
            print(f"{rel:7.2f}s  [丢弃] {e.get('reason')} ({e.get('audio_s')}s)")
        elif k in ("status", "ready", "warn", "error"):
            print(f"{rel:7.2f}s  · {e.get('stage', k)}: {e.get('message', '')}")

    print("\n--- 判定 ---")
    ok = True
    if not finals:
        print("✘ 没有定稿字幕：检查音量、采集设备、VAD 阈值")
        ok = False
    else:
        print(f"✔ 定稿字幕 {len(finals)} 条")
    if partials:
        print(f"✔ partial 推测 {len(partials)} 次（做到边说边出字幕）")
    else:
        print("· partial 一次都没触发（句子太短或 partial_interval_ms 太大，不一定是问题）")
    if args.provider != "off":
        if trans:
            print(f"✔ 译文 {len(trans)} 条")
        else:
            print("✘ 开启了翻译但没产出译文：检查 API Key / 本地模型")
            ok = False
    if errs:
        print(f"✘ 报错 {len(errs)} 条")
        for e in errs[:3]:
            print("   ", e.get("where"), e.get("message"))
        ok = False
    if drops:
        print(f"· 幻觉过滤丢弃 {len(drops)} 段（正常现象，说明过滤在工作）")

    print(f"\n统计：{pl.counters}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
