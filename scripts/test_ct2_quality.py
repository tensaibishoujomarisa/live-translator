"""对比 CTranslate2 的几种分词喂法，找出哪种能正确跑 Marian 日中翻译。

动机：如果 CT2 能用，发行版就能省掉 ~3GB 的 torch。
Marian 用独立的 source.spm / target.spm，而 CT2 转换时可能生成了
shared_vocabulary.json —— 词表对不上的话解码就会退化。
这个脚本把候选喂法各跑一遍，用「与 transformers 基准的重合度」和
「有没有退化重复」来判断哪种对。

    python -m scripts.test_ct2_quality
"""
from __future__ import annotations

import sys
import time
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

warnings.filterwarnings("ignore")

SENTENCES = [
    "ちょっと待って、いま何時？",
    "森永のおいしい牛乳は濃い青色に牛乳瓶をあしらったデザインのパック牛乳である。",
    "スパチャありがとうございます！めっちゃ嬉しいです。",
    "うわ、これ詰んだわ。ワンチャンいけると思ったのに。",
    "えーと、今日はちょっとゲームやっていこうかなと思ってます。",
]


def degenerate(text: str) -> bool:
    if not text or len(text) < 40:
        return False
    for unit in (1, 2, 3, 4):
        tail = text[-unit:]
        if text.count(tail) > len(text) / (unit * 2):
            return True
    return len(set(text)) < max(4, len(text) // 30)


def jaccard(a: str, b: str) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / max(1, len(sa | sb))


def main() -> int:
    import ctranslate2
    import sentencepiece as spm

    hf_dir = ROOT / "models" / "hf-shun89__opus-mt-ja-zh"
    ct2_dir = ROOT / "models" / "opus-mt-ja-zh-ct2"
    if not (hf_dir / "config.json").exists():
        print(f"[error] 缺少 {hf_dir}，先跑 python -m scripts.setup_local_mt")
        return 2

    print("=" * 72)
    print(" CTranslate2 分词喂法对比（目标：判断能否替掉 transformers/torch）")
    print("=" * 72)

    # ---------- 基准：transformers ----------
    from transformers import MarianMTModel, MarianTokenizer

    tok = MarianTokenizer.from_pretrained(str(hf_dir), local_files_only=True)
    mdl = MarianMTModel.from_pretrained(str(hf_dir), local_files_only=True).eval()
    baseline = []
    t0 = time.time()
    for s in SENTENCES:
        b = tok([s], return_tensors="pt", padding=True)
        out = mdl.generate(**b, num_beams=2, max_new_tokens=128, no_repeat_ngram_size=4)
        baseline.append(tok.batch_decode(out, skip_special_tokens=True)[0])
    tf_ms = (time.time() - t0) / len(SENTENCES) * 1000
    print(f"\n[基准] transformers  {tf_ms:.0f}ms/句")
    for s, o in zip(SENTENCES, baseline):
        print(f"   日 {s}")
        print(f"   中 {o}")

    if not (ct2_dir / "model.bin").exists():
        print(f"\n[skip] 没有 CT2 模型：{ct2_dir}")
        print("       生成：python -m scripts.setup_local_mt --also-ct2")
        return 2

    src_sp = spm.SentencePieceProcessor()
    src_sp.load(str(ct2_dir / "source.spm"))
    tgt_sp = spm.SentencePieceProcessor()
    tgt_sp.load(str(ct2_dir / "target.spm"))

    # 词表一致性：vocab.json 的 id→token 顺序 与 source.spm 是否一致
    import json

    if (ct2_dir / "vocab.json").exists():
        hv = json.loads((ct2_dir / "vocab.json").read_text(encoding="utf-8"))
        inv = {v: k for k, v in hv.items()}
        mism = 0
        for i in range(min(2000, src_sp.get_piece_size())):
            if src_sp.id_to_piece(i) != inv.get(i):
                mism += 1
        print(f"\n[词表] vocab.json 与 source.spm 前 2000 项不一致：{mism}")

    tr = ctranslate2.Translator(str(ct2_dir), device="cpu", compute_type="int8")

    # ---------- 各种喂法 ----------
    def run_manual_tokens() -> list[str]:
        """自己用 source.spm 编码、target.spm 解码。"""
        out = []
        for s in SENTENCES:
            toks = src_sp.encode(s, out_type=str)
            r = tr.translate_batch([toks], beam_size=2, max_decoding_length=128)
            out.append(tgt_sp.decode(r[0].hypotheses[0]))
        return out

    def run_ct2_tokenizer() -> list[str] | None:
        """把分词器整体交给 CT2，由它自己张量化与反分词。"""
        try:
            out = []
            for s in SENTENCES:
                r = tr.translate_batch(
                    [[s]],
                    beam_size=2,
                    max_decoding_length=128,
                )
                out.append("".join(r[0].hypotheses[0]))
            return out
        except Exception as exc:
            print(f"    （该方式不可用：{type(exc).__name__}: {str(exc)[:90]}）")
            return None

    def run_prefix_target() -> list[str]:
        """手动编码，但给解码器一个显式起始 token。"""
        out = []
        for s in SENTENCES:
            toks = src_sp.encode(s, out_type=str)
            r = tr.translate_batch(
                [toks], beam_size=2, max_decoding_length=128,
                target_prefix=[["</s>"]],
            )
            h = r[0].hypotheses[0][1:]  # 去掉前缀
            out.append(tgt_sp.decode(h))
        return out

    def run_with_end_token() -> list[str]:
        """显式告诉 CT2 结束 token 是 </s>（Marian 的 eos id 是 0）。"""
        out = []
        for s in SENTENCES:
            toks = src_sp.encode(s, out_type=str)
            r = tr.translate_batch(
                [toks], beam_size=2, max_decoding_length=128, end_token="</s>",
            )
            out.append(tgt_sp.decode(r[0].hypotheses[0]))
        return out

    methods = [
        ("手动 source.spm / target.spm", run_manual_tokens),
        ("CT2 内置分词（直接传字符串）", run_ct2_tokenizer),
        ("target_prefix 给 </s>", run_prefix_target),
        ("显式 end_token=</s>", run_with_end_token),
    ]

    results: dict[str, list[str]] = {}
    for name, fn in methods:
        print(f"\n[试] {name}")
        try:
            t0 = time.time()
            seq = fn()
            ms = (time.time() - t0) / len(SENTENCES) * 1000
        except Exception as exc:
            print(f"    [失败] {type(exc).__name__}: {str(exc)[:120]}")
            continue
        if seq is None:
            continue
        results[name] = seq
        deg = sum(1 for o in seq if degenerate(o))
        sim = sum(jaccard(a, b) for a, b in zip(baseline, seq)) / len(baseline)
        print(f"    退化 {deg}/{len(seq)} 句 · 与基准重合度 {sim:.2f} · {ms:.0f}ms/句")
        for s, o in zip(SENTENCES[:2], seq[:2]):
            print(f"    日 {s}\n    中 {o[:80]}")

    # ---------- 结论 ----------
    print("\n" + "=" * 72)
    print(" 结论")
    print("=" * 72)
    good = [n for n, seq in results.items()
            if not any(degenerate(o) for o in seq)
            and sum(jaccard(a, b) for a, b in zip(baseline, seq)) / len(baseline) > 0.6]
    if good:
        print(f" 可用的 CT2 喂法：{good}")
        print(" → 发行版可以只带 CTranslate2 + sentencepiece，省掉 ~3GB 的 torch")
        return 0
    print(" 没有任何 CT2 喂法得到可用结果")
    print(" → 发行版仍需要 transformers + torch（体积会大很多）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
