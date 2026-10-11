"""一次性下载 Whisper 识别模型（开发期步骤，之后运行完全离线）。

用法：
    python -m scripts.setup_whisper                    # 默认 large-v3-turbo（约 1.6GB）
    python -m scripts.setup_whisper --model small      # 笔记本/无显卡用这个小得多的
    python -m scripts.setup_whisper --list

模型会下到项目下的 models/ 目录，不会动 C 盘。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"

PRESETS = [
    ("large-v3-turbo", "约 1.6GB", "推荐。速度快、日语准，8GB 显存即可 float16"),
    ("large-v3", "约 3.1GB", "最准但最慢，显存 10GB+"),
    ("medium", "约 1.5GB", "无显卡时可用（int8 CPU）"),
    ("small", "约 480MB", "低配兜底，日语错误明显变多"),
    ("distil-large-v3", "约 1.5GB", "英文为优化目标，日语不推荐"),
]

COMMON_PROXY_PORTS = [7890, 7897, 10809, 10808, 20171, 2080, 1080, 8888, 9910]


def ensure_proxy() -> None:
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        if os.environ.get(key):
            print(f"[proxy] 使用 {key}={os.environ[key]}")
            return
    import socket

    for port in COMMON_PROXY_PORTS:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.4)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                url = f"http://127.0.0.1:{port}"
                os.environ["HTTPS_PROXY"] = url
                os.environ["HTTP_PROXY"] = url
                print(f"[proxy] 自动探测到本机代理 {url}（仅本次下载使用）")
                return
    print("[proxy] 未检测到代理，直连下载（国内网络可能较慢，可用 HF_ENDPOINT=https://hf-mirror.com）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="large-v3-turbo")
    ap.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    ap.add_argument("--compute-type", default=None)
    ap.add_argument("--mirror", action="store_true", help="使用 hf-mirror.com 镜像下载")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        print("可选识别模型：")
        for name, size, note in PRESETS:
            print(f"  {name:<18} {size:<10} {note}")
        return 0

    if args.mirror and not os.environ.get("HF_ENDPOINT"):
        os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
        print("[mirror] HF_ENDPOINT=https://hf-mirror.com")

    ensure_proxy()

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(MODELS_DIR))
    os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(MODELS_DIR))

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("缺少 faster-whisper。先运行：pip install -r requirements.txt")
        return 2

    compute = args.compute_type or ("float16" if args.device == "cuda" else "int8")
    print(f"[1/2] 下载并加载 {args.model}（{args.device}/{compute}）…")
    print(f"      缓存目录：{MODELS_DIR}")
    try:
        model = WhisperModel(
            args.model, device=args.device, compute_type=compute, download_root=str(MODELS_DIR)
        )
    except Exception as exc:
        print(f"[error] 加载失败：{type(exc).__name__}: {exc}")
        if "cublas" in str(exc).lower() or "cudnn" in str(exc).lower():
            print("\n缺少 CUDA 运行库，二选一：")
            print("  A. 装运行时库： pip install nvidia-cublas-cu12 nvidia-cudnn-cu12")
            print("  B. 用 CPU 跑： python -m scripts.setup_whisper --device cpu")
        return 3

    print("[2/2] 预热推理（把 CUDA kernel 编译缓存起来，之后首句不用等）…")
    import numpy as np

    list(model.transcribe(np.zeros(16000, dtype=np.float32), language="ja", beam_size=1)[0])

    total = sum(f.stat().st_size for f in MODELS_DIR.rglob("*") if f.is_file())
    print(f"\n完成 ✔  模型已就绪，models/ 当前占用 {total / 1024**3:.2f} GB")
    print("现在可以直接启动： python -m backend.main --pretty")
    return 0


if __name__ == "__main__":
    sys.exit(main())
