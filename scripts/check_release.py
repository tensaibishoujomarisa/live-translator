"""洁净室检查：从 git 暂存区导出一份「新克隆者会拿到的东西」，验证能不能跑。

模拟 `git clone` 之后的状态，检查：
  1. 有没有误提交敏感文件（API Key、模型、虚拟环境、依赖目录）
  2. 中文文件名在归档里是否完好（乱了的话克隆者会拿到乱码文件名）
  3. 仓库体积是否合理
  4. 必要文件是否齐全（bat 脚本、config、README、requirements）

    python -m scripts.check_release
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 绝对不该出现在仓库里的东西
FORBIDDEN = [
    "ui-settings.json", "config.local.yaml", ".env", "secrets.json",
    ".venv", "venv", "models", "node_modules", "ui/node_modules",
    ".cache", "logs", "__pycache__",
]
# 必须存在的文件
REQUIRED = [
    "README.md", "LICENSE", "CONTRIBUTING.md", "requirements.txt",
    "config.yaml", "glossary.yaml", ".gitignore",
    "backend/main.py", "backend/pipeline.py", "backend/audio_in.py",
    "backend/vad.py", "backend/asr_engine.py", "backend/translator.py",
    "ui/main.js", "ui/preload.js", "ui/renderer/index.html",
    "ui/renderer/renderer.js", "ui/renderer/style.css",
    "scripts/setup_whisper.py", "scripts/setup_local_mt.py",
]

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \u2714 {name}")
    else:
        FAIL += 1
        print(f"  \u2718 {name}" + (f"  ({detail})" if detail else ""))


def git(*args: str) -> str:
    r = subprocess.run(["git", "-c", "core.quotepath=false", *args],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{(r.stderr or '').strip()}")
    return r.stdout or ""


def main() -> int:
    print("=" * 66)
    print(" 发布前洁净室检查")
    print("=" * 66)

    if not (ROOT / ".git").exists():
        print("[error] 不是 git 仓库")
        return 2

    # 1) 暂存区内容
    staged = [f for f in git("ls-files").splitlines() if f.strip()]
    print(f"\n[1] 仓库包含 {len(staged)} 个文件")
    check("文件数合理（不是把整个工作区塞进去了）", 20 < len(staged) < 200, str(len(staged)))

    # 2) 敏感文件
    print("\n[2] 敏感文件检查")
    leaked = []
    for f in staged:
        norm = f.replace("\\", "/")
        for bad in FORBIDDEN:
            if norm == bad or norm.startswith(bad + "/"):
                leaked.append(norm)
    check("没有密钥/模型/环境/依赖目录被提交", not leaked, ", ".join(leaked[:5]))

    # 3) 内容里有没有残留密钥
    print("\n[3] 内容密钥扫描")
    hits = []
    for f in staged:
        p = ROOT / f
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for marker in ("sk-", "ghp_", "AKIA"):
            idx = 0
            while True:
                idx = text.find(marker, idx)
                if idx < 0:
                    break
                tail = text[idx + len(marker):idx + len(marker) + 24]
                # 只看像真钥匙的（连续 20+ 位字母数字）
                body = "".join(c for c in tail if c.isalnum())
                if len(body) >= 20:
                    hits.append(f"{f}: {marker}…")
                    break
                idx += len(marker)
    check("源码里没有硬编码的密钥", not hits, "; ".join(hits[:5]))

    # 4) 归档 + 中文文件名
    print("\n[4] 归档与文件名编码")
    try:
        tree = git("write-tree").strip()
        tmp = Path(tempfile.mkdtemp(prefix="jplt_release_"))
        tar_path = tmp / "repo.tar"
        with open(tar_path, "wb") as fh:
            r = subprocess.run(["git", "archive", "--format=tar", tree],
                               cwd=ROOT, stdout=fh, stderr=subprocess.PIPE)
            if r.returncode != 0:
                raise RuntimeError(r.stderr.decode("utf-8", "ignore"))
        size_mb = tar_path.stat().st_size / 1024 / 1024
        check(f"归档体积合理（{size_mb:.2f} MB < 5MB）", size_mb < 5, f"{size_mb:.2f} MB")

        dest = tmp / "x"
        dest.mkdir()
        with tarfile.open(tar_path) as tf:
            names = tf.getnames()
            tf.extractall(dest)
        nonascii = [n for n in names if any(ord(c) > 127 for c in n)]
        broken = [n for n in nonascii if "\ufffd" in n]
        check(f"中文文件名在归档里完好（{len(nonascii)} 个）", not broken, str(broken))
        for n in nonascii:
            print(f"      {n}")

        # 解出来的东西再检查一遍
        extracted = {p.name for p in dest.iterdir()}
        bats = sorted(n for n in extracted if n.lower().endswith(".bat"))
        check(f"解出 {len(bats)} 个一键脚本", len(bats) >= 5, str(bats))
        for b in bats:
            print(f"      {b}")

        # 5) 必要文件
        print("\n[5] 必要文件")
        missing = [f for f in REQUIRED if not (dest / f).exists()]
        check("必要文件齐全", not missing, ", ".join(missing))

        # 6) bat 只见 ASCII
        print("\n[6] 批处理文件纯 ASCII 检查")
        bad_bat = []
        for b in bats:
            data = (dest / b).read_bytes()
            try:
                data.decode("ascii")
            except UnicodeDecodeError:
                bad_bat.append(b)
        check("所有 .bat 都是纯 ASCII（中文会让 cmd 解析错位）", not bad_bat, ", ".join(bad_bat))

        shutil.rmtree(tmp, ignore_errors=True)
    except Exception as exc:
        check("归档检查", False, f"{type(exc).__name__}: {exc}")

    print("\n" + "=" * 66)
    print(f" 通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 66)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
