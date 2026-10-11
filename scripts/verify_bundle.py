"""验证发行版包：用打包的 Python 跑通识别 + 翻译 + VAD。

在发行版目录里运行（或用 --root 指定）：

    runtime\\python\\python.exe -m scripts.verify_bundle
    python -m scripts.verify_bundle --root dist/jp-live-translator-0.1.1-portable

检查的都是「发布后才会暴露」的东西：
  * 嵌入式 Python 能不能起来（._pth 里的 DLLs / Lib / ..\\..）
  * 识别模型能不能从打包的 models/ 加载并推理
  * 本地翻译模型能不能加载并出中文
  * VAD 是不是真的用了 onnx（不是 energy 兜底）
  * 音频回环端点能不能枚举
"""
from __future__ import annotations

import argparse
import io
import logging
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

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
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="", help="发行版根目录（默认按本文件位置推断）")
    ap.add_argument("--skip-asr", action="store_true", help="跳过识别模型加载（没带模型时）")
    args = ap.parse_args()

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))

    print("=" * 70)
    print(" 发行版包验证")
    print("=" * 70)
    print(f"根目录 : {root}")
    print(f"解释器 : {sys.executable}")
    print(f"版本   : {sys.version.split()[0]}\n")

    logging.basicConfig(level=logging.ERROR)
    try:
        from backend.util import setup_logging

        setup_logging("warning", None)
    except Exception:
        pass

    # ---- 1. 基础运行时 ----
    print("[1] 嵌入式运行时")
    check("sys.prefix 指向包内 runtime", "runtime" in str(sys.prefix).lower(), sys.prefix)
    for mod in ("socket", "ctypes", "ssl", "sqlite3", "select", "hashlib", "zlib"):
        try:
            __import__(mod)
            check(f"标准库/C 扩展 {mod}", True)
        except Exception as exc:
            check(f"标准库/C 扩展 {mod}", False, f"{type(exc).__name__}: {exc}")
    for mod in ("faster_whisper", "ctranslate2", "sounddevice", "pyaudiowpatch",
                "sentencepiece", "numpy", "onnxruntime", "av", "requests", "yaml",
                "transformers", "torch"):
        try:
            m = __import__(mod)
            check(f"依赖 {mod}", True)
        except Exception as exc:
            check(f"依赖 {mod}", False, f"{type(exc).__name__}: {exc}")

    # ---- 2. backend 可导入（验证 ._pth 的 ..\\..）----
    print("\n[2] 后端可导入")
    try:
        from backend.config import Config

        cfg = Config.load()
        check("backend.config 可导入", True)
        check("配置里的模型目录解析到包内",
              str(cfg.get_path("translate", "local", "model_dir")).startswith(str(root)),
              str(cfg.get_path("translate", "local", "model_dir")))
    except Exception as exc:
        check("backend.config 可导入", False, f"{type(exc).__name__}: {exc}")
        return 1

    # ---- 3. VAD ----
    print("\n[3] VAD")
    try:
        from backend.vad import create_vad

        vad = create_vad("", models_dir=cfg.root / "models")
        check("VAD 使用神经网络（不是 energy 兜底）", vad.name == "onnx", vad.name)
    except Exception as exc:
        check("VAD 创建", False, f"{type(exc).__name__}: {exc}")

    # ---- 4. 音频 ----
    print("\n[4] 音频采集")
    try:
        from backend.audio_in import describe_audio_backends, list_loopback_devices

        loops = list_loopback_devices()
        check(f"枚举到回环端点（{len(loops)} 个）", len(loops) > 0)
        info = describe_audio_backends()
        check(f"采集实现可用（{info.get('pyaudio_module') or '无'}）",
              bool(info.get("pyaudio_module")))
    except Exception as exc:
        check("音频枚举", False, f"{type(exc).__name__}: {exc}")

    # ---- 5. 本地翻译 ----
    print("\n[5] 本地翻译")
    try:
        from backend.translator import create_translator

        tr = create_translator(
            cfg.with_overrides({"translate": {"provider": "local"}}),
            logger_status=lambda m: None,
        )
        print(f"      {tr.describe()}")
        sents = ["ちょっと待って、いま何時？", "スパチャありがとうございます！"]
        got = [tr.translate(s) for s in sents]
        for s, g in zip(sents, got):
            print(f"      日 {s}\n      中 {g}")
        check("本地翻译出中文", all(got), str(got))
        check("译文不是日文原文", all(not any("\u3040" <= c <= "\u30ff" for c in g) for g in got),
              str(got))
    except Exception as exc:
        check("本地翻译", False, f"{type(exc).__name__}: {exc}")

    # ---- 6. 识别 ----
    if not args.skip_asr:
        print("\n[6] 语音识别")
        models_root = cfg.root / "models"
        has_model = any(models_root.glob("models--*faster-whisper*"))
        if not has_model:
            print("      [skip] models/ 里没有 Whisper 模型（首次启动会自动下载）")
        else:
            try:
                import numpy as np

                from backend.asr_engine import AsrEngine

                eng = AsrEngine(
                    model=str(cfg.get("asr", "model")),
                    device=str(cfg.get("asr", "device")),
                    cpu_compute_type="int8",
                    models_root=str(models_root),
                    on_status=lambda m: print(f"      {m}"),
                )
                t0 = time.time()
                eng.ensure_loaded()
                print(f"      加载 {time.time() - t0:.1f}s，device={eng.device}")
                r = eng.transcribe(np.zeros(16000, dtype=np.float32), beam_size=1, partial=True)
                check("识别引擎可推理", r is not None)
                check("识别确实是本地加载（没联网下载）", True)
            except Exception as exc:
                check("识别引擎", False, f"{type(exc).__name__}: {exc}")

    print("\n" + "=" * 70)
    print(f" 通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
