"""回环采集端到端测试：把一段日语从扬声器放出来，看能不能被采集并识别。

用法（必须在有声卡的机器上，且系统音量不能为 0）：
    python -m scripts.test_loopback
    python -m scripts.test_loopback --wav testdata/ja_sample16k.wav --volume 0.8

流程：
    1. 打开指定的回环端点开始采集
    2. 用 sounddevice 从默认扬声器播放测试音频
    3. 采集到的音频过一遍 VAD 分段
    4. 每段送 faster-whisper 识别
    5. 打印识别结果和各项耗时，用来判断端到端是否通
"""
from __future__ import annotations

import argparse
import shutil
import sys
import threading
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from backend.audio_in import AudioCapture, list_loopback_devices  # noqa: E402
from backend.util import get_logger, rms, setup_logging  # noqa: E402
from backend.vad import Segmenter, create_vad  # noqa: E402

log = get_logger("jplt.test")


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
    """测试音频不在仓库里（1MB 的 flac 没必要带），缺了就自动下一段。

    用 Common Voice 日语样本，约 440KB，7 秒左右，适合验证整条链路。
    """
    if path.exists():
        return path
    dest_dir = path.parent
    dest_dir.mkdir(parents=True, exist_ok=True)
    src = dest_dir / "ja_sample.flac"
    url = ("https://huggingface.co/datasets/japanese-asr/ja_asr.common_voice_8_0"
           "/resolve/main/sample.flac")

    if not src.exists():
        print(f"测试音频不存在，尝试下载：{url}")
        try:
            import urllib.request

            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=90) as r, open(src, "wb") as f:
                shutil.copyfileobj(r, f)
            if src.stat().st_size < 10_000:
                raise RuntimeError(f"文件太小（{src.stat().st_size} B），可能被重定向")
            print(f"  已下载 {src.name}（{src.stat().st_size / 1024:.0f} KB）")
        except Exception as exc:
            print(f"[error] 下载失败：{type(exc).__name__}: {exc}")
            print("        可以自己放一段 16k 单声道 wav 到 testdata/ja_sample16k.wav，")
            print("        或用 --wav 指定其它文件。")
            raise SystemExit(2) from exc

    # flac -> 16k mono wav
    try:
        import av
        import numpy as np
    except ImportError as exc:
        print(f"[error] 需要 pyav 来转换音频：{exc}（pip install av）")
        raise SystemExit(2) from exc

    print(f"转换 {src.name} -> {path.name}")
    container = av.open(str(src))
    stream = container.streams.audio[0]
    frames = [f.to_ndarray() for f in container.decode(stream)]
    data = np.concatenate(frames, axis=1) if frames[0].ndim == 2 else np.concatenate(frames)
    mono = data.mean(axis=0) if data.ndim == 2 else data
    if data.dtype.kind in "iu":
        mono = mono.astype(np.float32) / np.iinfo(data.dtype).max
    from backend.util import resample_linear

    x = resample_linear(mono.astype(np.float32), stream.rate, 16000)
    peak = float(np.abs(x).max()) or 1.0
    x = (x / peak * 0.7).astype(np.float32)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
    print(f"  已生成 {path.name}（{x.size / 16000:.2f}s）")
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", default=str(ROOT / "testdata" / "ja_sample16k.wav"))
    ap.add_argument("--device", default="", help="回环端点 id/名称，留空=默认输出设备")
    ap.add_argument("--volume", type=float, default=0.75)
    ap.add_argument("--gain", type=float, default=1.0)
    ap.add_argument("--model", default="large-v3-turbo")
    ap.add_argument("--device-asr", default="cuda")
    ap.add_argument("--skip-play", action="store_true", help="只采集不播放（用来测环境噪音）")
    args = ap.parse_args()

    setup_logging("info", None)

    wav = ensure_sample(Path(args.wav))
    audio = load_wav16k(wav)
    play_s = audio.size / 16000.0

    print("=" * 66)
    print(" 回环采集端到端测试")
    print("=" * 66)
    print(f"测试音频：{wav.name}  {play_s:.2f}s  rms={rms(audio):.4f}")

    loops = list_loopback_devices()
    print(f"\n可用回环端点 {len(loops)} 个：")
    for d in loops[:12]:
        print(f"  [{d['id']:>8}] {d['name'][:52]:<52} {'← 默认' if d.get('is_default') else ''}")
    if not loops:
        print("[FAIL] 没有回环端点，先装 PyAudioWPatch")
        return 3

    # ---- 启动采集 ----
    captured: list[np.ndarray] = []
    lock = threading.Lock()

    def on_chunk(c: np.ndarray) -> None:
        with lock:
            captured.append(c.copy())

    cap = AudioCapture(
        device=args.device,
        target_rate=16000,
        gain=args.gain,
        on_chunk=on_chunk,
        on_status=lambda m: print(f"  [audio] {m}"),
    )
    cap.start()
    time.sleep(1.2)
    print(f"\n开始采集：impl={cap._impl}  device={cap._device_info.get('name', '?')}")

    # ---- 播放 ----
    played_ok = False
    if not args.skip_play:
        try:
            import sounddevice as sd

            print(f"从默认扬声器播放（音量 {args.volume}）…")
            sd.play(audio * args.volume, samplerate=16000)
            played_ok = True
        except Exception as exc:
            print(f"  [warn] 播放失败：{type(exc).__name__}: {exc}")

    # 播放 + 尾巴留白，让 VAD 能收尾
    time.sleep(play_s + 2.0 if played_ok else 5.0)
    cap.stop()

    with lock:
        got = list(captured)
    if not got:
        print("\n[FAIL] 一个音频块都没采到")
        print(f"       错误：{cap.error or '(无)'}")
        return 4

    rec = np.concatenate(got)
    print(f"\n采集到 {rec.size / 16000:.2f}s 音频，整体 rms={rms(rec):.5f}")
    if rms(rec) < 0.001:
        print("[WARN] 电平极低。可能原因：系统音量为 0、采集端点选错了、或默认输出设备没在放声音。")
        print("       试试 --device <上表中的 id>，例如 --device pa:70")

    # ---- VAD 分段 ----
    print("\n跑 VAD 分段…")
    vad = create_vad("", models_dir=ROOT / "models")
    seg = Segmenter(vad, sample_rate=16000, threshold=0.5, min_silence_ms=380, max_speech_s=11.0)
    segments = seg.push(rec)
    tail = seg.flush()
    if tail:
        segments.append(tail)
    print(f"VAD 后端={vad.name}  分出 {len(segments)} 段")
    for i, s in enumerate(segments, 1):
        print(f"  段{i}: {s['duration_ms']}ms  speech={s['speech_ms']}ms  rms={s['rms']:.4f}  原因={s['reason']}")

    if not segments:
        print("[FAIL] VAD 没分出任何语音段")
        return 5

    # ---- 识别 ----
    from backend.asr_engine import AsrEngine

    eng = AsrEngine(
        model=args.model, device=args.device_asr,
        models_root=str(ROOT / "models"),
        on_status=lambda m: print(f"  [asr] {m}"),
    )
    eng.ensure_loaded()
    print("\n识别结果：")
    print("-" * 66)
    total_audio = 0.0
    total_compute = 0.0
    n_ok = 0
    for i, s in enumerate(segments, 1):
        res = eng.transcribe(s["audio"], beam_size=3)
        total_audio += res.audio_s
        total_compute += res.compute_s
        flag = "OK " if res.ok else f"丢弃({res.reason})"
        print(f"[{flag}] ({res.audio_s:.2f}s → {res.compute_s * 1000:.0f}ms)")
        if res.ok:
            n_ok += 1
            print(f"        日文：{res.text}")
        elif res.raw:
            print(f"        原文：{res.raw[:90]}")
    print("-" * 66)

    if total_audio > 0:
        print(f"共识别 {total_audio:.2f}s 音频，耗时 {total_compute:.2f}s，"
              f"实时率 x{total_compute / total_audio:.3f}")
    print()
    if n_ok:
        print(f"结论：端到端打通 ✔  （{n_ok}/{len(segments)} 段识别出内容）")
        return 0
    print("结论：采集到了音频，但没识别出内容 ✘ —— 检查音量、gain，或换 --device")
    return 6


if __name__ == "__main__":
    sys.exit(main())
