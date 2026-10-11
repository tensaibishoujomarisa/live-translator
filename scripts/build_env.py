"""为发行版准备一套「干净、自包含、可打包」的依赖环境。

为什么要单独建：开发用的 `.venv` 是 `--system-site-packages` 建的，
torch/transformers/numpy 都继承自系统 Python（torch 占 4.2GB，且是 CUDA 版）。
发行版需要：
  * 全部依赖都在同一个 site-packages 里（拷贝即可用）
  * torch 用 **CPU 版** —— 本地 NMT 本来就跑 CPU，CUDA 版多出 4GB 纯属浪费

    python -m scripts.build_env                # 建到 build/portable-py
    python -m scripts.build_env --force        # 重装
    python -m scripts.build_env --skip-torch   # 只装非 torch 部分（调试用）
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
ENV_DIR = BUILD / "env"

# 运行期真正需要的包（发行版就带这些）
RUNTIME_PACKAGES = [
    # 语音识别
    "faster-whisper",
    # 音频采集
    "sounddevice",
    "PyAudioWPatch",
    # VAD 与数值
    "onnxruntime",
    # VAD 模型本体（自带 silero_vad.onnx）。
    # 注意：这个包必须显式装上 —— 少了它 VAD 会静默退化成能量检测，
    # 断句明显变碎，而且只在日志里留一行 warning，很容易漏掉。
    "silero-vad",
    "numpy",
    "scipy",
    # 翻译：LLM 调用 + 本地 NMT
    "requests",
    "ctranslate2",
    "sentencepiece",
    "transformers",
    # 配置与术语表
    "PyYAML",
    # 音频格式（测试脚本用；faster-whisper 也依赖它读音频）
    "av",
]

# GPU 推理（faster-whisper 走 CUDA）需要的运行库。
# 不带也能跑，只是识别回退 CPU（慢 10 倍以上）。放进单独的可选包。
CUDA_PACKAGES = ["nvidia-cublas-cu12", "nvidia-cudnn-cu12"]

COMMON_PROXY_PORTS = [7890, 7897, 10809, 10808, 20171, 2080, 1080, 8888, 9910]


def log(msg: str) -> None:
    print(msg, flush=True)


def ensure_proxy() -> str:
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        v = os.environ.get(key)
        if v:
            log(f"[proxy] 使用 {key}={v}")
            return v
    import socket

    for port in COMMON_PROXY_PORTS:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.4)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                url = f"http://127.0.0.1:{port}"
                os.environ["HTTPS_PROXY"] = url
                os.environ["HTTP_PROXY"] = url
                log(f"[proxy] 自动探测到 {url}")
                return url
    log("[proxy] 未检测到代理，直连")
    return ""


def dir_size_mb(p: Path) -> float:
    if not p.exists():
        return 0.0
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1024 / 1024


def run(cmd: list[str], **kw) -> int:
    log("  $ " + " ".join(cmd[:6]) + (" …" if len(cmd) > 6 else ""))
    return subprocess.call(cmd, **kw)


def install_torch_cpu_direct(py: Path) -> int:
    """直接从 pytorch 的 CPU 索引把 wheel 下下来再本地安装。

    `pip install --index-url` 在走代理时容易在依赖解析/续传阶段卡死或超时
    （实测 Read timed out）。先把 wheel 单独抓下来再 `pip install <file>` 稳得多。
    """
    import re
    import urllib.request

    py_tag = "cp312"
    try:
        out = subprocess.run(
            [str(py), "-c",
             "import sys;print('cp%d%d' % sys.version_info[:2])"],
            capture_output=True, text=True,
        )
        if (out.stdout or "").strip():
            py_tag = out.stdout.strip()
    except Exception:
        pass

    index = "https://download.pytorch.org/whl/cpu/torch/"
    log(f"      查询可用 wheel（{py_tag} win_amd64）：{index}")
    try:
        req = urllib.request.Request(index, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            html = r.read().decode("utf-8", "ignore")
    except Exception as exc:
        log(f"      查询失败：{type(exc).__name__}: {exc}")
        return 1

    pat = re.compile(r'href="[^"]*/(torch-[^"/]*?%s[^"/]*?win_amd64\.whl)"' % re.escape(py_tag))
    names = pat.findall(html)
    # 排除带 cuda 标记的（CPU 索引里一般没有，但保险）
    names = [n for n in names if "cuda" not in n and "+cpu" not in n]
    if not names:
        log("      没找到匹配的 wheel")
        return 1

    def vkey(fname: str):
        m = re.search(r"torch-([0-9]+(?:\.[0-9]+)*)", fname)
        return tuple(int(x) for x in (m.group(1).split(".") if m else ["0"]))

    fname = sorted(names, key=vkey)[-1]
    url = index + fname
    dest = BUILD / "wheels" / fname
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"      下载 {fname}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=600) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f, length=1024 * 256)
        log(f"      已下载 {dest.stat().st_size / 1024 / 1024:,.1f} MB")
    except Exception as exc:
        log(f"      下载失败：{type(exc).__name__}: {exc}")
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass
        return 1

    return run([str(py), "-m", "pip", "install", "--no-input", "--progress-bar", "off",
                "--disable-pip-version-check", str(dest)])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="已存在也重建")
    ap.add_argument("--skip-torch", action="store_true", help="不装 torch（调试用）")
    ap.add_argument("--cuda", action="store_true", help="装 CUDA 运行库（+700MB，让识别能用 GPU）")
    ap.add_argument("--source", default=str(ENV_DIR), help="环境目录（默认 build/env）")
    ap.add_argument("--python", default=sys.executable, help="用哪个解释器建环境")
    args = ap.parse_args()

    env_dir = Path(args.source).resolve()

    log("=" * 70)
    log(" 构建发行版依赖环境（干净、自包含）")
    log("=" * 70)
    log(f"目标：{env_dir}")
    log(f"解释器：{args.python}")

    if env_dir.exists():
        if not args.force:
            log(f"\n[skip] 已存在：{env_dir}（加 --force 重建）")
            return 0
        log(f"\n清理旧环境…")
        shutil.rmtree(env_dir, ignore_errors=True)

    env_dir.parent.mkdir(parents=True, exist_ok=True)
    t_all = time.time()

    # 1) 建干净 venv（不加 --system-site-packages，保证自包含）
    log(f"\n[1/4] 创建干净虚拟环境")
    t0 = time.time()
    rc = run([args.python, "-m", "venv", str(env_dir)])
    if rc != 0:
        log("[error] venv 创建失败")
        return 2
    py = env_dir / "Scripts" / "python.exe"
    if not py.exists():
        log(f"[error] 没找到 {py}")
        return 2
    log(f"      完成（{time.time() - t0:.1f}s）")

    ensure_proxy()
    base = [str(py), "-m", "pip", "install", "--no-input", "--progress-bar", "off",
            "--disable-pip-version-check",
            # CPU 版 torch 约 200MB，走代理时默认 15s 超时不够，放宽
            "--timeout", "300", "--retries", "8"]

    # 2) 先装 CPU 版 torch —— 必须在 transformers 之前，
    #    否则 transformers 可能把 CUDA 版 torch 一起拖进来（多 4GB）
    if not args.skip_torch:
        log(f"\n[2/4] 安装 CPU 版 torch（比 CUDA 版省约 4GB）")
        t0 = time.time()
        rc = run(base + ["torch", "--index-url", "https://download.pytorch.org/whl/cpu"])
        if rc != 0:
            log("      [warn] 索引方式失败，改为直接下载 wheel 再本地安装")
            rc = install_torch_cpu_direct(py)
        if rc != 0:
            log("      [warn] 仍失败，退回默认源（会装上 CUDA 版，体积大很多）")
            rc = run(base + ["torch"])
            if rc != 0:
                log("[error] torch 安装失败")
                return 3
        log(f"      完成（{time.time() - t0:.1f}s）")
    else:
        log(f"\n[2/4] 跳过 torch（--skip-torch）")

    # 3) 运行期依赖
    log(f"\n[3/4] 安装运行期依赖（{len(RUNTIME_PACKAGES)} 个）")
    t0 = time.time()
    rc = run(base + RUNTIME_PACKAGES)
    if rc != 0:
        log("[error] 依赖安装失败")
        return 4
    log(f"      完成（{time.time() - t0:.1f}s）")

    # 4) 可选：CUDA 运行库
    if args.cuda:
        log(f"\n[4/4] 安装 CUDA 运行库（让识别走 GPU，约 +700MB）")
        rc = run(base + CUDA_PACKAGES)
        if rc != 0:
            log("      [warn] CUDA 运行库安装失败，发行版将只能用 CPU 识别")
        else:
            log("      完成")
    else:
        log(f"\n[4/4] 跳过 CUDA 运行库（发行版识别将用 CPU；加 --cuda 可带上 GPU 支持）")

    # 汇报
    sp = env_dir / "Lib" / "site-packages"
    log("\n" + "=" * 70)
    log(" 结果")
    log("=" * 70)
    total = dir_size_mb(env_dir)
    log(f"  环境总体积：{total:,.0f} MB")
    tops = []
    if sp.exists():
        for d in sp.iterdir():
            if d.is_dir() and not d.name.endswith(("dist-info", "__pycache__")):
                tops.append((dir_size_mb(d), d.name))
    for mb, name in sorted(tops, reverse=True)[:12]:
        log(f"    {name:<28} {mb:8,.1f} MB")

    # 自检：真的能 import 吗
    log("\n  验证导入：")
    check = (
        "import importlib,sys\n"
        "mods = ['faster_whisper','ctranslate2','sounddevice','pyaudiowpatch',"
        "'sentencepiece','numpy','scipy','requests','yaml','onnxruntime','av','transformers','torch']\n"
        "bad = []\n"
        "for m in mods:\n"
        "    try:\n"
        "        importlib.import_module(m)\n"
        "        print('    OK  ', m)\n"
        "    except Exception as e:\n"
        "        bad.append((m, type(e).__name__, str(e)[:60]))\n"
        "        print('    FAIL', m, type(e).__name__, str(e)[:60])\n"
        "sys.exit(1 if bad else 0)\n"
    )
    rc = subprocess.call([str(py), "-c", check])
    log(f"\n  总耗时 {time.time() - t_all:.0f}s")
    if rc != 0:
        log("  [warn] 有依赖导入失败，见上面 FAIL 行")
    else:
        log("  全部依赖就绪 ✔")
    return rc


if __name__ == "__main__":
    sys.exit(main())
