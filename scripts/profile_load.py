"""量一下发行版里识别模型加载到底慢在哪。

现象：单独跑 verify_bundle 时加载 small 只要 ~2s，
但通过 Electron 启动后端时日志显示「模型就绪，用时 61.5s」。
61.5s 这种接近整分钟的数字很可疑（像是某个超时），得查清楚，
否则用户每次启动都要等一分钟。

    runtime\\python\\python.exe -m scripts.profile_load
"""
from __future__ import annotations

import logging
import os
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="  %(levelname)s %(name)s: %(message)s")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def t(label: str, fn):
    t0 = time.time()
    try:
        r = fn()
        print(f"  {label:<44} {time.time() - t0:6.2f}s")
        return r
    except Exception as exc:
        print(f"  {label:<44} {time.time() - t0:6.2f}s  [FAIL] {type(exc).__name__}: {exc}")
        return None


def main() -> int:
    print("=" * 70)
    print(" 识别模型加载耗时剖析")
    print("=" * 70)
    print(f"根目录   : {ROOT}")
    print(f"解释器   : {sys.executable}")
    print(f"HF_HOME  : {os.environ.get('HF_HOME', '(未设置)')}")
    print(f"HF_HUB_OFFLINE : {os.environ.get('HF_HUB_OFFLINE', '(未设置)')}")
    print()

    from backend.config import Config

    cfg = Config.load()
    models_root = cfg.root / "models"
    model_name = str(cfg.get("asr", "model"))
    print(f"配置模型 : {model_name}   device={cfg.get('asr', 'device')}")
    print(f"模型目录 : {models_root}")
    print()

    print("[1] 导入阶段")
    t("import faster_whisper", lambda: __import__("faster_whisper"))
    t("import ctranslate2", lambda: __import__("ctranslate2"))

    print("\n[2] CUDA 检测（auto 会走这一步）")
    from backend.cuda_setup import prepare_cuda_dlls, preload_torch

    t("preload_torch (import torch)", preload_torch)
    rep = t("prepare_cuda_dlls", prepare_cuda_dlls)
    if isinstance(rep, dict):
        print(f"      ok={rep.get('ok')} loaded={len(rep.get('loaded', []))} "
              f"missing={rep.get('missing', [])[:3]}")

    print("\n[3] 构造 WhisperModel（关键：看 device 选了什么）")
    from faster_whisper import WhisperModel

    holder = {}

    def build():
        holder["m"] = WhisperModel(model_name, device="cpu", compute_type="int8",
                                   download_root=str(models_root))
        return holder["m"]

    t("WhisperModel(cpu/int8)", build)

    print("\n[4] 试推理")

    import numpy as np

    m = holder.get("m")

    def infer():
        return list(m.transcribe(np.zeros(8000, dtype=np.float32), language="ja",
                                 beam_size=1, vad_filter=False,
                                 condition_on_previous_text=False,
                                 without_timestamps=True)[0])

    t("transcribe 0.5s 静音", infer)

    print("\n[5] 逐项排除：是不是 ctranslate2 在探测 CUDA")
    def probe_gpu():
        import ctranslate2

        try:
            tr = ctranslate2.Translator(str(ROOT / "models" / "nonexistent"))
            del tr
        except Exception:
            pass

    t("ctranslate2.Translator 构造失败路径", probe_gpu)

    print("\n[6] 纯加载对比：直接指定模型目录（跳过 huggingface 解析）")
    snap = None
    for p in models_root.glob("models--*small*/snapshots/*"):
        if p.is_dir():
            snap = p
            break
    if snap:
        print(f"      快照目录：{snap}")
        t("WhisperModel(本地目录)", lambda: WhisperModel(str(snap), device="cpu",
                                                         compute_type="int8"))
    else:
        print("      没找到快照目录")

    print("\n" + "=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
