"""验证「勾了专有名词 → 真正写进术语表」这条链路。

之前这里是个**假承诺**：界面提示说"会建议加进术语表"，
但程序从来不写 glossary.yaml，永远不会真的生效。这个测试就是防止它退回去。

关键断言：
  * 写入后 glossary.yaml 真的多了那条
  * 译法变了要覆盖旧条目，而不是新增重复项
  * 写入后翻译器的 prompt 缓存要失效（否则新术语不生效）
  * 重复写入同一条不产生重复行
  * 日语里的 `:` `#` 等字符不会把 YAML 写坏

    python -m scripts.test_glossary_write
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.glossary import Glossary  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \u2714 {name}")
    else:
        FAIL += 1
        print(f"  \u2718 {name}" + (f"  ({detail})" if detail else ""))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="jplt_gloss_"))
    gl_path = tmp / "glossary.yaml"

    # 起始文件：一条普通术语 + 一条带注释的
    gl_path.write_text(
        "# 我自己的注释，重写后条目内容要保留\n"
        "entries:\n"
        "  - ja: ライブ配信\n"
        "    zh: 直播\n",
        encoding="utf-8",
    )

    print("=" * 70)
    print(" 术语表写入验证")
    print("=" * 70)
    print(f"术语表：{gl_path}\n")

    # ---------- 1. 加载与基本写入 ----------
    print("[1] 基本写入")
    g = Glossary.load(gl_path)
    check("载入 1 条", len(g) == 1, str(len(g)))
    check("记住了文件路径", g.path == gl_path, str(g.path))
    check("has() 能查已存在的", g.has("ライブ配信") is True)
    check("has() 对不存在的返回 False", g.has("ぺこら") is False)

    changed = g.add("ぺこら", "佩克拉", note="主播名")
    check("新增返回 True（有变化）", changed is True)
    text = gl_path.read_text(encoding="utf-8")
    check("文件里出现新条目", "ぺこら" in text and "佩克拉" in text, text[-120:])
    check("原有条目还在", "ライブ配信" in text)
    check("文件头说明还在", "术语表" in text)
    check("写了备份 .bak", gl_path.with_suffix(".yaml.bak").exists())

    # 重新加载确认能读回来
    g2 = Glossary.load(gl_path)
    check("重载后 2 条", len(g2) == 2, str(len(g2)))
    entry = next((e for e in g2.entries if e.ja == "ぺこら"), None)
    check("译法正确", entry is not None and entry.zh == "佩克拉", str(entry))
    check("备注保留了", entry is not None and entry.note == "主播名", str(entry))

    # ---------- 2. 幂等与覆盖 ----------
    print("\n[2] 幂等与覆盖")
    check("完全相同再写返回 False", g2.add("ぺこら", "佩克拉") is False)
    check("重复写没有增加条目", len(Glossary.load(gl_path)) == 2,
          str(len(Glossary.load(gl_path))))

    changed2 = g2.add("ぺこら", "佩可拉")
    check("改译法返回 True", changed2 is True)
    check("译法被覆盖（不是新增重复项）", len(Glossary.load(gl_path)) == 2,
          str(len(Glossary.load(gl_path))))
    entry2 = next(e for e in Glossary.load(gl_path).entries if e.ja == "ぺこら")
    check("覆盖后的值正确", entry2.zh == "佩可拉", entry2.zh)

    # ---------- 3. YAML 安全 ----------
    print("\n[3] 特殊字符不能写坏 YAML")
    tricky = [
        ("テスト: コロン", "测试：冒号"),
        ("ハッシュ#タグ", "井号"),
        ("ブラケット[1]", "方括号"),
        ('クォート"二重"', "引号"),
    ]
    for ja, zh in tricky:
        try:
            g2.add(ja, zh)
        except Exception as exc:
            check(f"写入 {ja!r} 不抛异常", False, f"{type(exc).__name__}: {exc}")
    try:
        g3 = Glossary.load(gl_path)
        ok = all(g3.has(ja) for ja, _ in tricky)
        check("含特殊字符的条目都能读回来", ok,
              str([e.ja for e in g3.entries]))
        vals = {e.ja: e.zh for e in g3.entries}
        check("译法没被 YAML 解析搞坏",
              all(vals.get(ja) == zh for ja, zh in tricky),
              str({k: vals.get(k) for k, _ in tricky}))
    except Exception as exc:
        check("重载含特殊字符的术语表", False, f"{type(exc).__name__}: {exc}")

    # ---------- 4. 通过 Pipeline 走一遍（勾专有名词 → 自动写术语表） ----------
    print("\n[4] Pipeline：勾「专有名词」自动写进术语表")
    from backend.bridge import Bridge
    from backend.config import Config
    from backend.pipeline import Pipeline

    corr_path = tmp / "corrections.jsonl"
    cfg = Config.load().with_overrides({
        # 用本地翻译（离线、不需要 Key），这样拿到的是真实翻译器，
        # 顺便验证本地后端也能走完整条链路
        "translate": {"provider": "local", "local": {"model_dir": str(ROOT / "models" / "hf-shun89__opus-mt-ja-zh")}},
        "glossary": {"enabled": True, "path": str(gl_path)},
        "learning": {"enabled": True, "path": str(corr_path)},
    })

    events: list[dict] = []

    class Collect(Bridge):
        def _write(self, msg: dict) -> None:
            events.append(msg)

    pl = Pipeline(cfg, Collect(mode="none"))
    pl.build()
    tr = pl.translator
    check("翻译器带上了 glossary", getattr(tr, "glossary", None) is not None)
    check("翻译器带上了修正库", getattr(tr, "learning", None) is not None)

    # 先制造一次 prompt 缓存（模拟已经翻过几句）
    if hasattr(tr, "_glossary_block"):
        tr._glossary_block = "（缓存内容）"
    before_count = len(tr.glossary)

    res = pl.add_correction(ja="みこ", right="小巫女", wrong="米可", scope="term")
    check("add_correction 返回成功", res.get("ok") is True, str(res))
    check("回执里带上 promoted 数", "promoted" in res, str(res))
    check("术语表条目数 +1", len(tr.glossary) == before_count + 1,
          f"{before_count} -> {len(tr.glossary)}")
    check("术语表文件里真的有了", Glossary.load(gl_path).has("みこ"))
    check("prompt 缓存被清掉（新术语才会生效）",
          tr._glossary_block is None, repr(getattr(tr, "_glossary_block", "?")))

    kinds = [e.get("kind") for e in events]
    check("发了 corrected 事件", "corrected" in kinds, str(kinds))
    check("给了写进术语表的提示",
          any("写进术语表" in (e.get("message") or "") for e in events),
          str([e.get("message") for e in events if e.get("kind") == "status"]))

    # 不勾专有名词的普通修正不该进术语表
    before2 = len(tr.glossary)
    pl.add_correction(ja="今日はいい天気ですね", right="今天天气真好",
                      wrong="今天天气不错", scope="sentence")
    check("整句修正不进术语表", len(tr.glossary) == before2,
          f"{before2} -> {len(tr.glossary)}")

    # 过长的"术语"要跳过（术语表只放词/短语）
    long_ja = "これはとても長い文章なので用語表には入れないでほしいのです"
    before3 = len(tr.glossary)
    pl.add_correction(ja=long_ja, right="超长句子不该进术语表", scope="term")
    check("过长的术语被跳过", len(tr.glossary) == before3,
          f"{before3} -> {len(tr.glossary)}")

    # ---------- 5. 批量提升 ----------
    print("\n[5] 批量提升「反复出现」的候选")
    pl2_corr = tmp / "c2.jsonl"
    cfg2 = Config.load().with_overrides({
        "translate": {"provider": "local", "local": {"model_dir": str(ROOT / "models" / "hf-shun89__opus-mt-ja-zh")}},
        "glossary": {"enabled": True, "path": str(tmp / "g2.yaml")},
        "learning": {"enabled": True, "path": str(pl2_corr)},
    })
    pl2 = Pipeline(cfg2, Collect(mode="none"))
    pl2.build()

    # 同一个名字散落在多句里
    pl2.add_correction(ja="ぺこら", right="佩克拉", wrong="佩科拉")
    pl2.add_correction(ja="ぺこらの歌枠", right="佩克拉的唱歌回", wrong="佩科拉的歌回")
    pl2.add_correction(ja="ぺこらは今日配信しない", right="佩克拉今天不直播")
    res2 = pl2.promote_terms()
    check("批量提升成功", res2.get("ok") is True, str(res2))
    check("至少写入 1 条", (res2.get("added", 0) + res2.get("updated", 0)) >= 1, str(res2))
    g_final = Glossary.load(tmp / "g2.yaml")
    check("ぺこら 进了术语表", g_final.has("ぺこら"), str([e.ja for e in g_final.entries]))
    check("长整句没进术语表",
          not any(len(e.ja) > 12 for e in g_final.entries),
          str([e.ja for e in g_final.entries]))
    # 再提升一次应该全部跳过
    res3 = pl2.promote_terms()
    check("重复提升不产生新增", res3.get("added", 0) == 0, str(res3))

    import shutil

    shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 70)
    print(f" 通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
