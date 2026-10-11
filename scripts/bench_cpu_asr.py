"""在 CPU 上基准测试几个 Whisper 模型，用来决定「便携 CPU 版」该默认带哪个。

背景：发行版不带 CUDA 运行库时只能 CPU 推理。
large-v3-turbo 在 GPU 上是 x0.05 实时率，CPU 上可能慢到跟不上语速 ——
跟不上就意味着字幕一直滞后、队列积压、丢句。
这个脚本实测每个候选模型的实时率和识别质量，给出可用的最小模型。

    python -m scripts.bench_cpu_asr
    python -m scripts.bench_cpu_asr --models tiny base small
"""
from __future__ import annotations

import argparse
import sys
import time
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

warnings.filterwarnings("ignore")

import numpy as np  # noqa: E402

CANDIDATES = ["tiny", "base", "small", "medium", "large-v3-turbo"]


def load_sample() -> np.ndarray:
    import wave

    wav = ROOT / "testdata" / "ja_sample16k.wav"
    if not wav.exists():
        from scripts.test_loopback import ensure_sample

        wav = ensure_sample(wav)
    with wave.open(str(wav), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=CANDIDATES)
    ap.add_argument("--compute", default="int8", help="CPU 计算精度")
    ap.add_argument("--threads", type=int, default=0, help="限制 CPU 线程数（0=不限制）")
    ap.add_argument("--device", default="cpu")
    args = ap.parse_args()

    audio = load_sample()
    dur = audio.size / 16000.0
    print("=" * 74)
    print(" CPU 识别基准测试")
    print("=" * 74)
    print(f"测试音频：{dur:.2f}s   device={args.device}  compute={args.compute}")
    if args.threads:
        import os

        os.environ["OMP_NUM_THREADS"] = str(args.threads)
        print(f"线程数限制：{args.threads}")
    print()

    from faster_whisper import WhisperModel

    rows = []
    for name in args.models:
        try:
            t0 = time.time()
            m = WhisperModel(name, device=args.device, compute_type=args.compute,
                             download_root=str(ROOT / "models"))
            load_s = time.time() - t0
        except Exception as exc:
            print(f"[{name:<16}] 加载失败：{type(exc).__name__}: {exc}")
            continue

        # 预热
        list(m.transcribe(audio[:8000], language="ja", beam_size=1)[0])

        t0 = time.time()
        segs, info = m.transcribe(audio, language="ja", beam_size=1,
                                  condition_on_previous_text=False, vad_filter=False)
        seg_list = list(segs)
        elapsed = time.time() - t0
        text = "".join(s.text for s in seg_list).strip()
        rtf = elapsed / dur

        verdict = "✔ 可实时" if rtf < 0.5 else ("△ 勉强" if rtf < 0.9 else "✘ 跟不上语速")
        rows.append((name, load_s, elapsed, rtf, verdict, text))
        print(f"[{name:<16}] 加载 {load_s:5.1f}s  识别 {elapsed:5.2f}s  "
              f"实时率 x{rtf:.3f}  {verdict}")
        print(f"                   {text[:66]}")

    print("\n" + "=" * 74)
    print(" 结论")
    print("=" * 74)
    ok = [r for r in rows if r[3] < 0.5]
    if ok:
        best = min(ok, key=lambda r: r[3])
        print(f"  能在 CPU 上实时的最小/最快模型：{best[0]}（x{best[3]:.3f}）")
    else:
        print("  没有模型能在 CPU 上实时跑（都需要 GPU，或需要更小的模型）")
    print("  参考：实时率 < 0.5 表示识别速度是语速的 2 倍以上，留足余量")
    print("        0.5~0.9 勉强能用但连续讲话会积压；> 0.9 会持续滞后")
    return 0


if __name__ == "__main__":
    sys.exit(main())
