"""把项目打包成「解压即用」的便携发行版。

产物结构（用户在任意位置解压后双击 启动.bat 即可）：

    jp-live-translator-0.1.1-portable/
    ├── 启动字幕.bat            一键启动
    ├── 自检.bat                出问题先跑这个
    ├── runtime/
    │   ├── python/             嵌入式 Python + 全部依赖（CPU 版 torch）
    │   └── ui/                 Electron 应用（含运行时）
    ├── models/                 Whisper 模型（可选，见 --with-whisper）
    ├── config.yaml
    ├── glossary.yaml
    ├── README.md / LICENSE / CHANGELOG.md
    └── 使用说明.txt

前置：
    1. python -m scripts.build_env           建好 runtime/python 的依赖环境
    2. python -m scripts.setup_whisper       下好识别模型（--with-whisper 时需要）
    3. cd ui && npm install                  装好 Electron

用法：
    python -m scripts.build_release                     # 轻量版（不含 Whisper 模型）
    python -m scripts.build_release --with-whisper      # 完全离线版（+1.5GB）
    python -m scripts.build_release --name my-build     # 自定义产物目录名
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
DIST = ROOT / "dist"

# 开发环境里的依赖目录（build_env 建的）
ENV_SITE = BUILD / "env" / "Lib" / "site-packages"
ENV_SCRIPTS = BUILD / "env" / "Scripts"

# 从 site-packages 里排除的东西（发行版不需要，能省不少体积）
SITE_EXCLUDE = {
    "pip", "setuptools", "wheel", "pkg_resources",
    "__pycache__", "_distutils_hack", "distutils-precedence.pth",
}

# 需要拷进去的顶层目录/文件模式（其余的一律不要）
COPY_DIRS = [
    "backend",
    "scripts",
]
COPY_FILES = [
    "config.yaml",
    "glossary.yaml",
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "requirements.txt",
]


def log(msg: str) -> None:
    print(msg, flush=True)


def mb(p: Path) -> float:
    if not p.exists():
        return 0.0
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1024 / 1024


def copy_tree(src: Path, dst: Path, ignore=None, exts_to_skip=None) -> int:
    """拷贝目录树，返回拷贝的文件数。"""
    n = 0
    for root, dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        if ignore and ignore(rel, dirs):
            dirs[:] = []
            continue
        if exts_to_skip:
            files = [f for f in files if not f.endswith(exts_to_skip)]
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for f in files:
            try:
                shutil.copy2(Path(root) / f, dst / rel / f)
                n += 1
            except Exception:
                pass
    return n


def launcher_bat(target_dir: Path, bat_name: str, py_rel: str, args: str, title: str,
                 note: str = "") -> Path:
    """生成一个纯 ASCII 的启动 bat（中文会让 cmd 解析错位，必须 ASCII）。"""
    content = f"""@echo off
chcp 65001 >nul
cd /d "%~dp0"
title {title}

set "PY=%~dp0{py_rel}"
if not exist "%PY%" (
  echo [ERROR] Bundled Python not found:
  echo         %PY%
  echo         The package seems incomplete. Re-extract the archive.
  pause
  exit /b 1
)

rem Keep model cache inside this folder so nothing is written to the system drive
set "HF_HOME=%~dp0models"
set "HUGGINGFACE_HUB_CACHE=%~dp0models"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"
rem Some launchers set this; it makes Electron run as plain Node and everything breaks
set "ELECTRON_RUN_AS_NODE="
{note}
"%PY%" {args}
"""
    p = target_dir / bat_name
    # 必须是 ASCII + 不带 BOM（UTF-8 BOM 会让 @echo off 解析失败）
    p.write_bytes(content.replace("\n", "\r\n").encode("ascii"))
    return p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="", help="产物目录名（默认带版本号）")
    ap.add_argument("--with-whisper", action="store_true",
                    help="把 Whisper 模型也打进去（完全离线，+约1.5GB）")
    ap.add_argument("--no-electron", action="store_true", help="不打包 Electron 界面（只出后端）")
    ap.add_argument("--no-zip", action="store_true", help="只生成目录，不压缩")
    ap.add_argument("--source", default=str(BUILD / "env"), help="依赖环境目录")
    args = ap.parse_args()

    version = "0.1.1"
    try:
        import re

        txt = (ROOT / "backend" / "__init__.py").read_text(encoding="utf-8")
        m = re.search(r'__version__\s*=\s*"([^"]+)"', txt)
        if m:
            version = m.group(1)
    except Exception:
        pass

    name = args.name or f"jp-live-translator-{version}-portable"
    out = DIST / name

    log("=" * 72)
    log(f" 打包便携发行版：{name}")
    log("=" * 72)
    t_all = time.time()

    # ---------- 0. 前置检查 ----------
    env_site = Path(args.source) / "Lib" / "site-packages"
    env_py = Path(args.source) / "Scripts" / "python.exe"
    log(f"\n[0/6] 前置检查")
    problems = []
    if not env_py.exists():
        problems.append(f"缺少依赖环境：{env_py}（先跑 python -m scripts.build_env）")
    if not env_site.exists():
        problems.append(f"缺少 site-packages：{env_site}")
    if not (ROOT / "ui" / "node_modules" / "electron" / "dist" / "electron.exe").exists() \
            and not args.no_electron:
        problems.append("缺少 Electron（先跑 cd ui && npm install）")
    whisper_dir = ROOT / "models" / "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo"
    if args.with_whisper and not whisper_dir.exists():
        problems.append(f"缺少 Whisper 模型：{whisper_dir}（先跑 python -m scripts.setup_whisper）")
    local_mt = ROOT / "models" / "hf-shun89__opus-mt-ja-zh"
    if not (local_mt / "config.json").exists():
        log(f"      [warn] 没有本地翻译模型，发行版将只能用在线 LLM 翻译")
    if problems:
        for p in problems:
            log(f"      [FAIL] {p}")
        return 2
    log("      检查通过")

    if out.exists():
        log(f"\n      清理旧产物：{out}")
        shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)

    # ---------- 1. 代码 ----------
    log(f"\n[1/6] 拷贝后端代码与配置")
    n = 0
    for d in COPY_DIRS:
        src = ROOT / d
        if src.exists():
            n += copy_tree(src, out / d,
                           ignore=lambda rel, dirs:
                               "__pycache__" in dirs and (dirs.remove("__pycache__") or False))
    for f in COPY_FILES:
        src = ROOT / f
        if src.exists():
            shutil.copy2(src, out / f)
            n += 1
    # 测试音频不放（首次跑测试会自动下载）
    log(f"      共 {n} 个文件")

    # ---------- 2. 嵌入式 Python + 依赖 ----------
    log(f"\n[2/6] 组装嵌入式 Python 运行时")
    rt = out / "runtime"
    py_dir = rt / "python"
    py_dir.mkdir(parents=True, exist_ok=True)

    # 拷解释器本体（Scripts 里的 exe 没用，改为直接 python.exe -m）
    src_root = Path(args.source)
    for item in ("python.exe", "pythonw.exe", "python3.dll", "python312.dll",
                 "vcruntime140.dll", "vcruntime140_1.dll"):
        s = src_root / "Scripts" / item
        if not s.exists():
            s = src_root / item
        if s.exists():
            shutil.copy2(s, py_dir / item)

    # site-packages
    log(f"      拷贝 site-packages（这一步最慢）")
    sp_dst = py_dir / "Lib" / "site-packages"
    t0 = time.time()
    copied = 0
    for item in env_site.iterdir():
        if item.name in SITE_EXCLUDE:
            continue
        if item.is_dir():
            copied += copy_tree(item, sp_dst / item.name)
        else:
            try:
                shutil.copy2(item, sp_dst / item.name)
                copied += 1
            except Exception:
                pass
    log(f"      {copied} 个文件（{time.time() - t0:.0f}s）")

    # _pth：让嵌入式解释器找到 Lib/site-packages 与项目根
    (py_dir / "python._pth").write_text(
        "python312.zip\r\n"
        ".\r\n"
        "Lib\r\n"
        "Lib\\site-packages\r\n"
        "..\\..\r\n"          # 让 backend / scripts 可被 import
        "import site\r\n",
        encoding="ascii",
    )
    log(f"      Python 运行时 {mb(py_dir):,.0f} MB")

    # ---------- 3. 模型 ----------
    log(f"\n[3/6] 组装模型")
    models_dst = out / "models"
    models_dst.mkdir(parents=True, exist_ok=True)
    if (local_mt / "config.json").exists():
        copy_tree(local_mt, models_dst / local_mt.name)
        log(f"      本地翻译模型 {mb(models_dst / local_mt.name):,.0f} MB")
    if args.with_whisper:
        copy_tree(whisper_dir, models_dst / whisper_dir.name)
        log(f"      Whisper 识别模型 {mb(models_dst / whisper_dir.name):,.0f} MB")
    else:
        log(f"      跳过 Whisper 模型（首次启动会自动下载）")

    # ---------- 4. Electron ----------
    if not args.no_electron:
        log(f"\n[4/6] 打包 Electron 界面")
        ui_src = ROOT / "ui"
        ui_dst = rt / "ui"
        ui_dst.mkdir(parents=True, exist_ok=True)

        # app 代码（不含 node_modules，Electron 运行时单独拷）
        for item in ("main.js", "preload.js", "package.json", "ui-test.js", "ui-test-preload.js"):
            s = ui_src / item
            if s.exists():
                shutil.copy2(s, ui_dst / item)
        copy_tree(ui_src / "renderer", ui_dst / "renderer")

        # Electron 运行时
        el_src = ui_src / "node_modules" / "electron" / "dist"
        el_dst = ui_dst / "node_modules" / "electron" / "dist"
        log(f"      拷贝 Electron 运行时（{mb(el_src):,.0f} MB）")
        t0 = time.time()
        copy_tree(el_src, el_dst)
        # electron 包自身的 package.json（main.js 里 require('electron') 需要）
        for item in ("package.json", "index.js", "path.txt"):
            s = ui_src / "node_modules" / "electron" / item
            if s.exists():
                shutil.copy2(s, el_dst.parent / item)
        log(f"      完成（{time.time() - t0:.0f}s）")
    else:
        log(f"\n[4/6] 跳过 Electron")

    # ---------- 5. 启动脚本 ----------
    log(f"\n[5/6] 生成启动脚本")
    py_rel = "runtime\\python\\python.exe"
    launcher_bat(out, "_selfcheck.bat", py_rel, '-m backend.main --doctor',
                 "JP Live Translator - Self Check")
    launcher_bat(out, "_console.bat", py_rel, '-m backend.main --pretty',
                 "JP Live Translator - Console")
    if not args.no_electron:
        # Electron 入口。注意后端是由 Electron 自己拉起来的：
        #   * 它知道准确路径（runtime/python/python.exe，见 ui/main.js 的 resolvePython）
        #   * 它会清掉 ELECTRON_RUN_AS_NODE，也能把设置面板里的参数当命令行传下去
        # 所以这里只负责把 Electron 启动起来。
        launcher_bat(
            out, "_start.bat", py_rel,
            "-c \"import faster_whisper\"",
            "JP Live Translator",
            note=(
                'rem 先确认自带的 Python 依赖完好（有问题就早点报出来，别等窗口开一半）\n'
                'if errorlevel 1 (\n'
                '  echo [ERROR] Bundled Python is broken. Re-extract the archive.\n'
                '  pause\n'
                '  exit /b 1\n'
                ')\n'
                'set "ELECTRON=%~dp0runtime\\ui\\node_modules\\electron\\dist\\electron.exe"\n'
                'if not exist "%ELECTRON%" (\n'
                '  echo [ERROR] Electron runtime missing. Re-extract the archive.\n'
                '  pause\n'
                '  exit /b 1\n'
                ')\n'
                'start "" "%ELECTRON%" "%~dp0runtime\\ui"\n'
                'exit /b 0'
            ),
        )
    log("      已生成 _start.bat / _selfcheck.bat / _console.bat")

    # ---------- 6. 说明与统计 ----------
    log(f"\n[6/6] 生成说明文件")
    (out / "使用说明.txt").write_text(
        _readme_txt(version, with_whisper=args.with_whisper),
        encoding="utf-8",
    )
    (out / "manifest.json").write_text(
        json.dumps({
            "name": name,
            "version": version,
            "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "with_whisper": args.with_whisper,
            "with_electron": not args.no_electron,
            "python": "bundled",
            "size_mb": round(mb(out), 1),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    total = mb(out)
    log("\n" + "=" * 72)
    log(f" 打包完成：{out}")
    log(f" 体积：{total:,.0f} MB   耗时 {time.time() - t_all:.0f}s")
    log("=" * 72)
    for sub in sorted(out.iterdir(), key=lambda p: -mb(p) if p.is_dir() else 0):
        size = mb(sub) if sub.is_dir() else sub.stat().st_size / 1024 / 1024
        log(f"   {sub.name:<34} {size:>9,.1f} MB")

    if not args.no_zip:
        log(f"\n压缩中（这一步比较久）…")
        t0 = time.time()
        archive = DIST / name
        shutil.make_archive(str(archive), "zip", root_dir=str(DIST), base_dir=name)
        zpath = archive.with_suffix(".zip")
        log(f"  {zpath.name}  {zpath.stat().st_size / 1024 / 1024:,.0f} MB"
            f"（{time.time() - t0:.0f}s）")
    return 0


def _readme_txt(version: str, with_whisper: bool) -> str:
    whisper_note = (
        "识别模型已经打包在里面，完全离线可用。"
        if with_whisper else
        "首次启动会自动下载识别模型（约 1.5GB，需要一个能上外网的网络）；\n"
        "  下完就再也不用联网了。下载慢的话可以设代理：\n"
        "      set HTTPS_PROXY=http://127.0.0.1:7890\n"
        "  然后再启动。"
    )
    return f"""日语直播实时翻译字幕 v{version}  —— 便携版
================================================

怎么用
------
1. 把整个文件夹解压到任意位置（路径不要有奇怪字符，最好别有空格）
2. 双击  _start.bat
3. 等约 10 秒，屏幕底部会出现一条半透明字幕栏
4. 把直播打开让它出声，字幕就会自动出现

{whisper_note}

快捷键
------
Ctrl+Shift+H        显示 / 隐藏字幕栏
Ctrl+Shift+P        暂停 / 恢复识别（接电话时用）
Ctrl+Shift+M        鼠标穿透 —— 开了之后能直接点字幕栏下面的直播间
Ctrl+Shift+↑ / ↓    字号增减

回看之前的翻译
--------------
鼠标移到字幕上，用滚轮往上翻即可（默认保留最近 300 条）。
往上翻的时候字幕不会自动跳到最新，右下角会出现「↓ 最新」按钮；
点它或者滚回底部就恢复实时跟随。想看更多历史就把窗口拉大。

翻译怎么配
----------
字幕栏上点 ⚙ 打开设置：

  在线大模型（推荐，质量最好）
      方式选「在线大模型」，填接口地址、模型名、API Key。
      DeepSeek:  https://api.deepseek.com   deepseek-chat
      也可以把 Key 放环境变量：setx JP_LIVE_TRANSLATOR_API_KEY "sk-..."

  本地翻译（免费、离线，已打包在 models/ 里）
      方式选「本地 NMT」。质量不如大模型，但完全免费且断网可用。

  关闭翻译
      方式选「关闭」，只看日文。

出问题了
--------
双击  _selfcheck.bat ，它会逐项检查环境、音频设备、翻译配置。
纯文字模式（字幕打在命令行里，方便确认到底哪一步不对）：_console.bat

最常见的两个问题：
  * 采不到声音 —— 确认直播真的在出声；如果用了 Voicemeeter 之类的虚拟声卡，
    在设置里把「采集设备」改成你实际听的那个。
  * 中文是空的 —— 翻译没配好。要么填 API Key 用在线翻译，
    要么把方式切成「本地 NMT」。

提示：字幕栏不置顶的话，全屏看直播会被挡住。点控制栏的 📌 打开置顶。
"""


if __name__ == "__main__":
    sys.exit(main())
