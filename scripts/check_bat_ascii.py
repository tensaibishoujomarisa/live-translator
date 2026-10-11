"""列出 .bat 文件里所有非 ASCII 行（cmd 按系统代码页解析时会出问题）。

    python -m scripts.check_bat_ascii
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    bad_files = 0
    for p in sorted(ROOT.glob("*.bat")):
        data = p.read_bytes()
        lines = data.replace(b"\r\n", b"\n").split(b"\n")
        bad = [(i, ln) for i, ln in enumerate(lines, 1) if any(b > 127 for b in ln)]
        if bad:
            bad_files += 1
            print(f"{p.name}  （{len(bad)} 行含非 ASCII）")
            for i, ln in bad:
                print(f"   {i:>3}: {ln.decode('utf-8', 'replace')}")
            print()
    if not bad_files:
        print("所有 .bat 都是纯 ASCII ✔")
        return 0
    print(f"共 {bad_files} 个文件需要处理")
    return 1


if __name__ == "__main__":
    sys.exit(main())
