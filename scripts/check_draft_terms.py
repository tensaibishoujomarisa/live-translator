"""检查 terms_hinome.draft.yaml 的格式，并演示合并进 glossary.yaml 的效果。

    python -m scripts.check_draft_terms
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.glossary import Glossary  # noqa: E402

DRAFT = ROOT / "terms_hinome.draft.yaml"
REAL = ROOT / "glossary.yaml"


def main() -> int:
    print("=" * 70)
    print(" 候选术语文件检查")
    print("=" * 70)

    # 1) 草案本身能不能被解析
    print(f"\n[1] 解析 {DRAFT.name}")
    if not DRAFT.exists():
        print("  [FAIL] 文件不存在")
        return 1
    draft = Glossary.load(DRAFT)
    print(f"  解析出 {len(draft)} 条")
    if len(draft) == 0:
        print("  [FAIL] 一条都没解析出来，YAML 格式有问题")
        return 1
    for e in draft.entries:
        alt = f"  alt={e.alts}" if e.alts else ""
        note = f"   # {e.note}" if e.note else ""
        print(f"    {e.ja:<16} -> {e.zh}{alt}{note}")

    # 2) 和现有术语表有没有冲突
    print(f"\n[2] 与现有 {REAL.name} 的冲突检查")
    real = Glossary.load(REAL)
    print(f"  现有 {len(real)} 条")
    real_ja = {e.ja.strip(): e for e in real.entries}
    conflicts = []
    for e in draft.entries:
        if e.ja.strip() in real_ja:
            old = real_ja[e.ja.strip()].zh
            if old != e.zh:
                conflicts.append((e.ja, old, e.zh))
    if conflicts:
        print("  有冲突（同名不同译法，合并时以后者为准）：")
        for ja, old, new in conflicts:
            print(f"    {ja}: 现有「{old}」 -> 草案「{new}」")
    else:
        print("  无冲突 ✔ （草案里没有和现有条目重名的）")

    # 3) 演示合并后的效果
    print("\n[3] 演示：合并到一个副本上会发生什么")
    tmp = Path(tempfile.mkdtemp(prefix="jplt_draft_"))
    merged_path = tmp / "glossary.yaml"
    shutil.copy2(REAL, merged_path)
    merged = Glossary.load(merged_path)
    added = updated = 0
    for e in draft.entries:
        existed = merged.has(e.ja)
        if merged.add(e.ja, e.zh, note=e.note, alts=e.alts):
            if existed:
                updated += 1
            else:
                added += 1
    print(f"  合并后：新增 {added} 条、覆盖 {updated} 条、"
          f"总计 {len(merged.entries)} 条")
    print(f"  （这只写进了临时文件 {merged_path.name}，没动你的 glossary.yaml）")

    # 4) 确认真的没动原文件
    real2 = Glossary.load(REAL)
    print(f"\n[4] 原文件确认：仍是 {len(real2)} 条 "
          f"({'未被改动 ✔' if len(real2) == len(real) else '被改动了 ✘'})")

    # 5) 提示待确认项
    print("\n[5] 需要你确认的条目")
    pending = [e for e in draft.entries if e.note.startswith("★")]
    print(f"  共 {len(pending)} 条标了「★ 待你确认」：")
    for e in pending:
        print(f"    {e.ja:<16} -> {e.zh}")
    print("\n  这些我没有可靠来源，请按你实际听到的改或删。")
    print("  另外：**最新的梗我一条都没填**，")
    print("  因为 B站页面要验证码、知乎/NGA 取不到，我不能编。")

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + "=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
