"""验证「越用越准」：改过的译法，以后真的会被自动参考。

这是这个功能唯一有意义的验收标准 —— 光把修正存进文件不算数，
必须证明下一次翻译**确实带上了这条修正**。

    python -m scripts.test_learning
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import Config  # noqa: E402
from backend.learning import CorrectionStore  # noqa: E402
from backend.translator import create_translator  # noqa: E402
from scripts.mock_llm_server import MockLlmServer  # noqa: E402

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
    tmp = Path(tempfile.mkdtemp(prefix="jplt_learn_"))
    store_path = tmp / "corrections.jsonl"

    print("=" * 72)
    print(" 「越用越准」验证")
    print("=" * 72)
    print(f"修正库：{store_path}\n")

    # ---------- 1. 存储层 ----------
    print("[1] 修正存储")
    st = CorrectionStore(store_path, max_examples=4)
    check("初始为空", len(st) == 0)
    st.add(ja="ぺこら", right="佩克拉", wrong="佩科拉", scope="term")
    st.add(ja="ちょっと待って、いま何時？", right="等一下，现在几点？",
           wrong="等一下,现在几点了?")
    check("写入两条", len(st) == 2)
    check("落盘了", store_path.exists() and store_path.stat().st_size > 0)

    # 同一条重复改，应覆盖而不是堆叠（同一句日语以最后一次改法为准）
    st.add(ja="ぺこら", right="佩可拉", wrong="佩科拉", scope="term")
    check("同一原文重复修改只留最新", len(st) == 2, str(len(st)))
    got = [c.right for c in st.all() if c.ja == "ぺこら"]
    check("保留的是最新值", got == ["佩可拉"], str(got))

    # 重新加载（模拟重启）
    st2 = CorrectionStore(store_path, max_examples=4)
    check("重载后条数一致", len(st2) == 2, str(len(st2)))
    check("重载后内容正确",
          any(c.ja == "ぺこら" and c.right == "佩可拉" for c in st2.all()))

    # 完全相同的修正不应产生新条目
    st2.add(ja="ぺこら", right="佩可拉")
    check("相同修正不重复写入", len(st2) == 2, str(len(st2)))

    # 覆盖写后文件里不该残留旧版本（否则文件越滚越大、看着也乱）
    raw = store_path.read_text(encoding="utf-8")
    check("旧版本已从文件里清掉", "佩克拉" not in raw and raw.count("ぺこら") == 1,
          f"ぺこら 出现 {raw.count('ぺこら')} 次")

    # ---------- 2. 相关度挑选 ----------
    print("\n[2] 相关度挑选（只挑跟当前句子有关的）")
    p2 = tmp / "rel.jsonl"
    st3 = CorrectionStore(p2, max_examples=2)
    st3.add(ja="ホロライブの配信", right="hololive 的直播")
    st3.add(ja="全く関係ない長い文章です", right="完全无关的长句子")
    ex = st3.examples_for("ホロライブの配信を見てる")
    check("挑到了相关的", any("ホロライブ" in c.ja for c in ex), str([c.ja for c in ex]))
    check("挑的数量受限（max_examples=2）", len(ex) <= 2, str(len(ex)))
    check("无关的那条被排除", not any("全く関係ない" in c.ja for c in ex),
          str([c.ja for c in ex]))

    # ---------- 3. 注入 LLM prompt ----------
    print("\n[3] 注入到 LLM 翻译请求（关键）")
    # 单独一份库，避免受前面用例影响
    p3 = tmp / "inject.jsonl"
    st_inj = CorrectionStore(p3, max_examples=4)
    st_inj.add(ja="ぺこら", right="佩克拉", wrong="佩科拉", scope="term")

    with MockLlmServer() as srv:
        cfg = Config.load().with_overrides({
            "translate": {
                "provider": "llm",
                "llm": {
                    "base_url": srv.base_url, "api_key": "test-key",
                    "model": "mock-model", "retry": 0,
                },
            },
            "learning": {"enabled": True, "path": str(p3), "max_examples": 4},
        })

        tr = create_translator(cfg, logger_status=lambda m: None)
        check("翻译器带上了修正库", getattr(tr, "learning", None) is not None)

        tr.translate("ぺこらはかわいい")
        prompt = srv.state.last_user_prompt()
        check("请求里出现修正段落", "用户以前的修正" in prompt, prompt[-200:])
        check("请求里出现「佩克拉」", "佩克拉" in prompt, prompt[-200:])
        check("请求里标注了原来的错译", "佩科拉" in prompt, prompt[-200:])

        # 无关的修正不该硬塞进去
        srv.state.requests.clear()
        tr.translate("今日はいい天気ですね")
        p2txt = srv.state.last_user_prompt()
        check("无关句子不会硬塞不相关的修正",
              "佩克拉" not in p2txt, p2txt[-160:])

        # 关掉功能就不该注入
        cfg_off = cfg.with_overrides({"learning": {"enabled": False}})
        tr_off = create_translator(cfg_off, logger_status=lambda m: None)
        srv.state.requests.clear()
        tr_off.translate("ぺこらはかわいい")
        p3txt = srv.state.last_user_prompt()
        check("关掉后不注入修正", "用户以前的修正" not in p3txt)

    # ---------- 4. 本地 NMT 的后处理替换 ----------
    print("\n[4] 本地 NMT：直接替换用户改过的错译")
    st4 = CorrectionStore(tmp / "c2.jsonl", max_examples=4)
    st4.add(ja="ぺこら", right="佩克拉", wrong="佩科拉")
    cfg_local = Config.load().with_overrides({
        "translate": {"provider": "local"},
        "learning": {"enabled": True, "path": str(tmp / "c2.jsonl")},
    })
    trl = create_translator(cfg_local, logger_status=lambda m: None)
    if getattr(trl, "learning", None) is None:
        check("本地翻译也能挂上修正库", False, "learning 为空")
    else:
        # 直接测后处理：模拟模型输出了错译
        replaced = trl.learning.apply("佩科拉真的很可爱")
        check("错译被替换成用户改法", replaced == "佩克拉真的很可爱", replaced)
        untouched = trl.learning.apply("完全没提那个名字")
        check("没命中就不动", untouched == "完全没提那个名字", untouched)

    # ---------- 5. 术语建议 ----------
    print("\n[5] 反复出现的短短语 → 建议加术语表")
    p5 = tmp / "terms.jsonl"
    st5 = CorrectionStore(p5, max_examples=4)
    # 同一个人名在不同句子里反复出现（这才是真实场景）
    st5.add(ja="ぺこら", right="佩克拉", wrong="佩科拉")
    st5.add(ja="ぺこらの歌枠", right="佩克拉的唱歌回", wrong="佩科拉的歌回")
    st5.add(ja="ぺこらは今日配信しない", right="佩克拉今天不直播", wrong="佩科拉今天不直播")
    st5.add(ja="今日は長い一日でしたね", right="今天真是漫长的一天")
    sug = st5.suggest_terms(min_hits=2)
    names = [c.ja for c in sug]
    check("反复出现的短短语被选为术语候选", "ぺこら" in names, str(names))
    check("长句子不被当作术语", not any(len(c.ja) > 12 for c in sug), str(names))

    # ---------- 6. 统计与删除 ----------
    print("\n[6] 统计与删除")
    s = st5.stats()
    check("统计有总数", s.get("total") == 4, str(s))
    check("统计有术语候选数", s.get("suggest_terms", 0) >= 1, str(s))
    removed = st5.remove("ぺこらの歌枠")
    check("按原文删除生效", removed == 1 and len(st5) == 3, f"removed={removed} left={len(st5)}")

    import shutil

    shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 72)
    print(f" 通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 72)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
