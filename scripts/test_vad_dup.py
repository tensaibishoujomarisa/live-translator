"""定位重复定稿的来源：是 VAD 重复切出了同一段音频吗？

用合成语音（带静音间隔的调制音）直接喂 Segmenter，检查它吐出的片段有没有重叠/重复：
  - 相邻片段的音频是否高度相似
  - 片段之间是否有内容重叠（上一段的尾巴 == 下一段的开头）

    python -m scripts.test_vad_dup
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from backend.util import rms  # noqa: E402
from backend.vad import Segmenter, create_vad  # noqa: E402

SR = 16000


def speech(seconds: float, freq: float = 220.0, amp: float = 0.3) -> np.ndarray:
    """合成一段"像语音"的音频：基频 + 谐波 + 包络起伏。"""
    n = int(SR * seconds)
    t = np.arange(n) / SR
    x = np.zeros(n, dtype=np.float32)
    for k, w in ((1, 1.0), (2, 0.5), (3, 0.3), (5, 0.2)):
        x += (w * np.sin(2 * np.pi * freq * k * t)).astype(np.float32)
    env = 0.6 + 0.4 * np.sin(2 * np.pi * 3.5 * t)
    x = (x * env / 2.0 * amp).astype(np.float32)
    return x


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(SR * seconds), dtype=np.float32)


def spec_fingerprint(x: np.ndarray) -> tuple:
    """粗指纹：长度、rms、以及分段的零交叉率，用来判断两段音频是不是同一份。"""
    if x.size == 0:
        return (0, 0.0, 0.0)
    zc = float(np.mean(np.abs(np.diff(np.sign(x))) > 0))
    return (x.size, round(rms(x), 5), round(zc, 4))


def main() -> int:
    print("=" * 70)
    print(" VAD 重复切分排查")
    print("=" * 70)

    vad = create_vad("", models_dir=ROOT / "models")
    print(f"VAD 后端：{vad.name}\n")

    seg = Segmenter(
        vad, sample_rate=SR, threshold=0.5,
        min_speech_ms=320, min_silence_ms=380, max_speech_s=11.0,
        pad_ms=120, min_rms=0.0025,
    )

    # 造 4 段语音，中间夹静音；再模拟「同一段音频被喂两次」
    print("[场景 1] 4 段语音 + 句间静音，一次性喂入")
    audio = np.concatenate([
        speech(1.2, 220), silence(0.7),
        speech(1.0, 260), silence(0.7),
        speech(1.5, 300), silence(0.7),
        speech(0.9, 340), silence(0.7),
    ])
    out = seg.push(audio)
    print(f"  分出 {len(out)} 段（预期 4）")
    for s in out:
        print(f"    id={s['id']} {s['duration_ms']}ms  fp={spec_fingerprint(s['audio'])}")

    # 场景 2：把同一段音频连着喂两次（模拟采集重复）
    print("\n[场景 2] 同一段音频被连续喂两次")
    vad2 = create_vad("", models_dir=ROOT / "models")
    seg2 = Segmenter(vad2, sample_rate=SR, threshold=0.5, min_speech_ms=320,
                     min_silence_ms=380, max_speech_s=11.0, pad_ms=120, min_rms=0.0025)
    one = np.concatenate([speech(1.3, 240), silence(0.7)])
    out2 = seg2.push(np.concatenate([one, one]))
    print(f"  分出 {len(out2)} 段（预期 2，因为音频里确实是两遍一样的内容）")
    for s in out2:
        print(f"    id={s['id']} {s['duration_ms']}ms  fp={spec_fingerprint(s['audio'])}")

    # 场景 3：小块喂入（模拟真实采集流），看会不会因为分块而重复
    print("\n[场景 3] 按 32ms 小块喂入（模拟真实采集）")
    vad3 = create_vad("", models_dir=ROOT / "models")
    seg3 = Segmenter(vad3, sample_rate=SR, threshold=0.5, min_speech_ms=320,
                     min_silence_ms=380, max_speech_s=11.0, pad_ms=120, min_rms=0.0025)
    stream = np.concatenate([
        speech(1.2, 220), silence(0.8),
        speech(1.4, 280), silence(0.8),
        speech(1.0, 320), silence(0.8),
    ])
    block = int(SR * 0.032)
    out3 = []
    for i in range(0, stream.size, block):
        out3.extend(seg3.push(stream[i:i + block]))
    tail = seg3.flush()
    if tail:
        out3.append(tail)
    print(f"  分出 {len(out3)} 段（预期 3）")
    for s in out3:
        print(f"    id={s['id']} {s['duration_ms']}ms  fp={spec_fingerprint(s['audio'])}")

    # 判定：任何场景里出现两条指纹相同的片段都算可疑
    print("\n[判定]")
    bad = 0
    for name, seq, expect in (("场景1", out, 4), ("场景2", out2, 2), ("场景3", out3, 3)):
        fps = [spec_fingerprint(s["audio"]) for s in seq]
        dup = len(fps) != len(set(fps))
        id_dup = len({s["id"] for s in seq}) != len(seq)
        flag = ""
        if dup:
            flag += " 指纹重复!"
            bad += 1
        if id_dup:
            flag += " id 重复!"
            bad += 1
        if len(seq) != expect:
            flag += f" 数量不符(得{len(seq)}期{expect})"
        print(f"  {name}: {len(seq)} 段{flag or ' 正常'}")

    print()
    if bad:
        print("结论：VAD 会产出重复/异常的片段 ✘")
        return 1
    print("结论：VAD 分段正常 ✔（重复定稿不是 VAD 造成的）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
