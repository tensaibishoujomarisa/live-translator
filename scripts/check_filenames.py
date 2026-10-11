"""检查 git 里存的文件名编码是否正确（中文名会不会变乱码）。

克隆到别的机器后如果文件名是乱码，说明仓库里存的不是 UTF-8 —— 必须在发布前发现。

    python -m scripts.check_filenames
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    print("=" * 66)
    print(" 文件名编码检查")
    print("=" * 66)

    # 1) 工作区真实文件名（Python 用 UTF-8 读，显示的才是真名）
    print("\n[工作区] 顶层文件：")
    bad = []
    for p in sorted(ROOT.iterdir()):
        if p.name.startswith("."):
            continue
        name = p.name
        flag = ""
        try:
            name.encode("utf-8").decode("utf-8")
        except Exception:
            flag = "  <-- 编码异常"
            bad.append(name)
        if any("\ufffd" in c or "\uFFFD" == c for c in name):
            flag = "  <-- 含替换字符（乱码）"
            bad.append(name)
        print(f"  {name}{flag}")

    # 2) git 索引里的名字（git 存储的是字节，需要按 UTF-8 解）
    print("\n[git 索引] 所有文件（按 UTF-8 解码）：")
    try:
        out = subprocess.run(
            ["git", "-c", "core.quotepath=false", "ls-files"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        )
        files = [f for f in (out.stdout or "").splitlines() if f.strip()]
        non_ascii = [f for f in files if any(ord(c) > 127 for c in f)]
        print(f"  共 {len(files)} 个文件，其中 {len(non_ascii)} 个含非 ASCII 字符：")
        for f in non_ascii:
            print(f"    {f}")
            if "\ufffd" in f:
                bad.append(f)
    except Exception as exc:
        print(f"  [warn] 读取 git 索引失败：{exc}")

    # 3) 本地文件系统的编码（Windows 上可能是 UTF-16/UTF-8，取决于系统区域设置）
    print("\n[文件系统] 用 os.listdir 读取（走系统编码）：")
    try:
        names = os.listdir(ROOT)
        bats = [n for n in names if n.lower().endswith(".bat")]
        for n in sorted(bats):
            ok = "中文名正常" if any(ord(c) > 127 for c in n) and "\ufffd" not in n else "?"
            print(f"  {n}   ({ok})")
    except Exception as exc:
        print(f"  [warn] {exc}")

    print("\n" + "=" * 66)
    if bad:
        print(f" 发现 {len(bad)} 个可能有编码问题的名字：")
        for b in bad:
            print(f"   - {b}")
        print(" 建议：把中文文件名改成 ASCII（例如 01_install.bat），或确认 git 存的是 UTF-8。")
        return 1
    print(" 文件名编码正常 ✔（中文名在 UTF-8 下可读）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
