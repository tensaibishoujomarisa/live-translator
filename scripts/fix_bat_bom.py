"""去掉 .bat 文件开头的 UTF-8 BOM。

为什么必须去：cmd 对 BOM 的处理不干净，某些系统区域设置下会把
`@echo off` 解析成 `∩╗┐@echo off`，于是报
    '@echo' is not recognized as an internal or external command
而文件其余部分是纯 ASCII 时，BOM 是唯一的非 ASCII 内容，纯属添乱。

    python -m scripts.fix_bat_bom
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOM = b"\xef\xbb\xbf"


def main() -> int:
    fixed = []
    for p in sorted(ROOT.glob("*.bat")):
        data = p.read_bytes()
        if data.startswith(BOM):
            p.write_bytes(data[len(BOM):])
            fixed.append(p.name)
    if fixed:
        print(f"已去掉 BOM（{len(fixed)} 个文件）：")
        for f in fixed:
            print(f"  {f}")
    else:
        print("没有文件带 BOM ✔")

    # 复查
    remain = [p.name for p in sorted(ROOT.glob("*.bat"))
              if any(b > 127 for b in p.read_bytes())]
    if remain:
        print(f"\n仍有非 ASCII 内容的文件：{remain}")
        print("跑 python -m scripts.check_bat_ascii 看具体行")
        return 1
    print("所有 .bat 现在都是纯 ASCII ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
