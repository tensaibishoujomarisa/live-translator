"""下载语音活动检测（VAD）模型 —— 让断句质量上一个台阶。

不装也能跑（会退化成能量检测），但直播快速口语下能量检测会切得很碎，
装上 Silero VAD 后「哪里是一句话」的判断准确得多。

用法：
    python -m scripts.setup_vad
    python -m scripts.setup_vad --source hf        # 走 HuggingFace
    python -m scripts.setup_vad --source gh        # 走 GitHub
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"

SOURCES = {
    "hf": [
        "https://huggingface.co/deepghs/silero-vad-onnx/resolve/main/silero_vad.onnx",
        "https://huggingface.co/onnx-community/silero-vad/resolve/main/onnx/model.onnx",
    ],
    "gh": [
        "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx",
        "https://raw.githubusercontent.com/snakers4/silero-vad/master/src/silero_vad/data/silero_vad.onnx",
    ],
}

COMMON_PROXY_PORTS = [7890, 7897, 10809, 10808, 20171, 2080, 1080, 8888, 9910]


def ensure_proxy() -> None:
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        if os.environ.get(key):
            print(f"[proxy] 使用 {key}")
            return
    import socket

    for port in COMMON_PROXY_PORTS:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.4)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                url = f"http://127.0.0.1:{port}"
                os.environ["HTTPS_PROXY"] = url
                os.environ["HTTP_PROXY"] = url
                print(f"[proxy] 自动探测到 {url}")
                return


def try_pip_package() -> bool:
    """最省事的路径：pip 包自带 onnx 模型。"""
    try:
        import subprocess

        print("[1/2] 尝试 pip 安装 silero-vad（自带 ONNX 模型）…")
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--no-cache-dir", "-q", "silero-vad"],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if r.returncode != 0:
            print(f"      pip 安装失败：{(r.stderr or '')[-300:]}")
            return False
        import silero_vad  # type: ignore

        base = Path(silero_vad.__file__).parent
        hits = sorted(base.rglob("silero_vad*.onnx"))
        if not hits:
            print("      pip 包装上了，但里面没有 onnx 模型")
            return False
        dest = MODELS_DIR / "silero_vad.onnx"
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(hits[0], dest)
        print(f"      ✔ 已复制到 {dest}  ({dest.stat().st_size / 1024:.0f} KB)")
        return True
    except Exception as exc:
        print(f"      pip 路径不可用：{type(exc).__name__}: {exc}")
        return False


def download(urls: list[str], dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    for url in urls:
        try:
            print(f"      尝试 {url}")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f)
            size = tmp.stat().st_size
            if size < 50_000:
                print(f"      文件太小（{size} B），可能不是模型，换下一个源")
                tmp.unlink(missing_ok=True)
                continue
            tmp.replace(dest)
            print(f"      ✔ 下载完成 {dest}  ({size / 1024:.0f} KB)")
            return True
        except Exception as exc:
            print(f"      失败：{type(exc).__name__}: {exc}")
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
    return False


def verify(path: Path) -> bool:
    try:
        import onnxruntime as ort

        sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        names = [i.name for i in sess.get_inputs()]
        shapes = {i.name: i.shape for i in sess.get_inputs()}
        print(f"[2/2] 校验模型：输入 {names} {shapes}")
        if "input" not in names:
            print("      [WARN] 输入名不是 'input'，可能是另一种导出格式")
        return True
    except Exception as exc:
        print(f"[2/2] 校验失败：{type(exc).__name__}: {exc}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="auto", choices=["auto", "pip", "hf", "gh"])
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    dest = MODELS_DIR / "silero_vad.onnx"
    if dest.exists() and not args.force:
        print(f"已存在：{dest}")
        verify(dest)
        return 0

    print("=" * 60)
    print(" 下载 Silero VAD 模型（约 2MB）")
    print("=" * 60)
    ensure_proxy()

    ok = False
    if args.source in ("auto", "pip"):
        ok = try_pip_package()
    if not ok and args.source in ("auto", "hf"):
        print("[1/2] 从 HuggingFace 下载…")
        ok = download(SOURCES["hf"], dest)
    if not ok and args.source in ("auto", "gh"):
        print("[1/2] 从 GitHub 下载…")
        ok = download(SOURCES["gh"], dest)

    if not ok:
        print("\n下载失败。不装也能用（自动退回能量 VAD），只是断句会差一些。")
        print("可以手动把 silero_vad.onnx 放到： " + str(dest))
        return 1

    verify(dest)
    print("\n完成 ✔  重启软件后 VAD 会自动使用 onnx 后端")
    return 0


if __name__ == "__main__":
    sys.exit(main())
