"""让 CTranslate2 能找到 CUDA 运行库。

背景：pip 装的 ctranslate2 不会自动加载 nvidia-cublas-cu12 / nvidia-cudnn-cu12 里的 DLL，
而 torch 自带的 cuDNN/cuBLAS 也不在默认搜索路径上，于是报
    RuntimeError: Library cublas64_12.dll is not found or cannot be loaded

这里在进程启动早期（import ctranslate2 之前）把这些 DLL 目录塞进搜索路径并显式预加载，
用户机器上只要有任意一套 CUDA 12 运行库（pip 装的 nvidia-* 或 torch 自带）就能用 GPU。
"""
from __future__ import annotations

import ctypes
import glob
import os
import sys
from pathlib import Path

from .util import get_logger

log = get_logger("jplt.cuda")

_done = False
_report: dict = {"ok": False, "dirs": [], "loaded": [], "missing": [], "note": ""}

# 顺序有讲究：cudnn 依赖 cublas，先加载 cublas
_DLL_ORDER = [
    "cublasLt64_12.dll",
    "cublas64_12.dll",
    "cudnn64_9.dll",
    "cudnn_ops64_9.dll",
    "cudnn_cnn64_9.dll",
    "cudnn_engines_precompiled64_9.dll",
    "cudnn_engines_runtime_compiled64_9.dll",
    "cudnn_heuristic64_9.dll",
    "cudnn_adv64_9.dll",
    "cudnn_graph64_9.dll",
]


def _candidate_dirs() -> list[Path]:
    dirs: list[Path] = []

    # 1) pip 安装的 nvidia-* 包
    #
    # 注意：nvidia 是**命名空间包**（PEP 420，没有 __init__.py），
    # 所以 `nvidia.__file__` 是 None。原来直接 Path(nvidia.__file__).parent
    # 会抛 TypeError 并被 except 吞掉 —— 表现为「装了 nvidia-cublas-cu12
    # 但检测不到 CUDA」，实测踩过。必须改用 __path__。
    try:
        import nvidia  # type: ignore

        roots: list[Path] = []
        f = getattr(nvidia, "__file__", None)
        if f:
            roots.append(Path(f).parent)
        for p in (getattr(nvidia, "__path__", None) or []):
            roots.append(Path(p))
        for root in roots:
            for sub in root.rglob("bin"):
                if sub.is_dir():
                    dirs.append(sub)
            for sub in root.rglob("lib"):
                if sub.is_dir():
                    dirs.append(sub)
    except Exception:
        pass

    # 2) torch 自带的 CUDA 库（环境里常已经有）
    try:
        import torch  # noqa: F401

        torch_lib = Path(torch.__file__).parent / "lib"
        if torch_lib.is_dir():
            dirs.append(torch_lib)
    except Exception:
        pass

    # 3) 系统 CUDA
    for env in ("CUDA_PATH", "CUDA_HOME"):
        v = os.environ.get(env)
        if v:
            dirs.append(Path(v) / "bin")
    for base in (r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA",):
        p = Path(base)
        if p.is_dir():
            for sub in sorted(p.glob("v12*"), reverse=True):
                dirs.append(sub / "bin")

    # 去重且只保留真实存在的目录
    seen: set[str] = set()
    out: list[Path] = []
    for d in dirs:
        try:
            key = str(d.resolve()).lower()
        except Exception:
            continue
        if key in seen or not d.is_dir():
            continue
        seen.add(key)
        out.append(d)
    return out


def prepare_cuda_dlls(verbose: bool = False) -> dict:
    """把 CUDA DLL 加进搜索路径并预加载。可重复调用，只生效一次。"""
    global _done
    if _done:
        return _report
    _done = True

    if sys.platform != "win32":
        _report.update(ok=True, note="非 Windows，跳过")
        return _report

    dirs = _candidate_dirs()
    _report["dirs"] = [str(d) for d in dirs]
    if not dirs:
        _report["note"] = "没有找到任何 CUDA 运行库目录"
        return _report

    # Windows 8+ 默认不从 PATH 之外加载依赖 DLL，需要显式加目录
    for d in dirs:
        try:
            os.add_dll_directory(str(d))
        except Exception:
            pass
        os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")

    found_any = False
    for name in _DLL_ORDER:
        path = None
        for d in dirs:
            cand = d / name
            if cand.exists():
                path = cand
                break
        if path is None:
            # cudnn 9 的 DLL 名字随小版本变，用通配兜底
            for d in dirs:
                hits = glob.glob(str(d / name.replace("64_9", "*64*9*")))
                if hits:
                    path = Path(hits[0])
                    break
        if path is None:
            _report["missing"].append(name)
            continue
        try:
            ctypes.WinDLL(str(path))
            _report["loaded"].append(path.name)
            found_any = True
        except Exception as exc:
            _report["missing"].append(f"{name}({type(exc).__name__})")

    _report["ok"] = found_any and "cublas64_12.dll" in " ".join(_report["loaded"])
    if _report["ok"]:
        _report["note"] = f"已加载 {len(_report['loaded'])} 个 CUDA DLL"
    else:
        _report["note"] = "找不到 cublas64_12.dll，GPU 推理不可用"
    if verbose:
        log.info("CUDA 准备：%s", _report["note"])
    return _report


def cuda_report() -> dict:
    return dict(_report)


def preload_torch() -> bool:
    """在加载 CUDA DLL 之前先 import torch，能让 torch 的 DLL 目录进入搜索路径。

    有些环境下 torch 的 cudnn 会提供 CTranslate2 需要的符号，此时不必额外装 nvidia-*。
    """
    try:
        import torch  # noqa: F401

        return True
    except Exception:
        return False
