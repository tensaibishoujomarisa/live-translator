"""「越用越准」：把用户的修正沉淀下来，下次翻译自动参考。

设计思路（**不训练模型权重**，而是把修正变成上下文）：

  1. 用户改了一条字幕 → 记下「日语原文 / 原来的错译 / 用户改成什么」
  2. 翻译时把最相关的几条修正作为「示例」塞进 prompt（few-shot）
  3. 反复出现的专有名词 → 自动建议加进术语表（强制对照，最可靠）

为什么不做权重微调：
    * 微调需要**正确的**训练数据。系统自己跑出来的字幕不是正确答案，
      拿它去训练等于「用自己错误的输出去训练自己」，只会把错误固化。
    * 要真正微调得先攒够几百上千条人工修正 —— 这个文件就是那批数据的第一来源。
    * 对翻译质量提升最快的手段其实是「给它看几个例子」，成本为零、立刻生效。

存储格式（data/corrections.jsonl，一行一条，方便手工编辑也方便将来拿去训练）：
    {"ts": 1712345678.9, "ja": "ぺこら", "wrong": "佩科拉", "right": "佩克拉",
     "note": "", "hits": 0}
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .util import get_logger

log = get_logger("jplt.learn")


@dataclass
class Correction:
    ja: str = ""            # 日语原文（整句或短语）
    wrong: str = ""         # 系统原来的译文
    right: str = ""         # 用户改成
    note: str = ""          # 可选备注（为什么这么改）
    ts: float = field(default_factory=time.time)
    hits: int = 0           # 被用作示例的次数，用于挑「最常出现」的
    scope: str = "sentence"  # sentence=整句译法；term=专有名词

    def key(self) -> str:
        """去重键只用**日语原文**。

        同一句日语你改了两次，应该以最后一次为准 ——
        如果按「原文+修正」去重，会同时留着两个互相矛盾的版本，
        翻译时被一起塞进 prompt，模型只会更糊涂。
        """
        return _norm(self.ja)


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").strip())


class CorrectionStore:
    """修正记录的读写。

    * 追加写 JSONL，不怕中途崩溃
    * 内存里维护一份，加载时按 (ja, right) 去重（同一条改多次只留最新）
    * 线程安全：识别/翻译/UI 三个线程都可能碰
    """

    def __init__(self, path: str | Path, max_examples: int = 6):
        self.path = Path(path)
        self.max_examples = max(0, int(max_examples))
        self._items: list[Correction] = []
        self._lock = threading.Lock()
        self._load()

    # ---------- 读写 ----------
    def _load(self) -> None:
        if not self.path.exists():
            return
        seen: dict[str, Correction] = {}
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                c = Correction(
                    ja=str(obj.get("ja") or ""),
                    wrong=str(obj.get("wrong") or ""),
                    right=str(obj.get("right") or ""),
                    note=str(obj.get("note") or ""),
                    ts=float(obj.get("ts") or 0),
                    hits=int(obj.get("hits") or 0),
                    scope=str(obj.get("scope") or "sentence"),
                )
                if c.ja and c.right:
                    seen[c.key()] = c          # 后出现的覆盖先出现的（同一句以最新为准）
            self._items = list(seen.values())
            log.info("载入 %d 条修正记录（%s）", len(self._items), self.path.name)
        except Exception as exc:
            log.warning("读修正记录失败：%s", exc)

    def add(self, ja: str, right: str, wrong: str = "", note: str = "",
            scope: str = "sentence") -> Correction | None:
        ja, right = (ja or "").strip(), (right or "").strip()
        if not ja or not right:
            return None
        if _norm(ja) == _norm(right):
            return None  # 没改，不用记
        c = Correction(ja=ja, wrong=(wrong or "").strip(), right=right,
                       note=(note or "").strip(), scope=scope)
        with self._lock:
            existed = any(x.key() == c.key() for x in self._items)
            self._items = [x for x in self._items if x.key() != c.key()]
            self._items.append(c)
            if existed:
                # 同一句改过多次：重写文件（否则旧版本还留在 JSONL 里，
                # 下次加载虽然会被覆盖，但文件会越滚越大且看着混乱）
                self.rewrite()
            else:
                self._append(c)
        log.info("记下修正：%s -> %s", ja[:24], right[:24])
        return c

    def _append(self, c: Correction) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")
        except Exception as exc:
            log.warning("写修正记录失败：%s", exc)

    def rewrite(self) -> None:
        """把内存里的全量（去重后）重写回文件，用于删除/计数更新。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                for c in self._items:
                    f.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")
            tmp.replace(self.path)
        except Exception as exc:
            log.warning("重写修正记录失败：%s", exc)

    # ---------- 查询 ----------
    def __len__(self) -> int:
        return len(self._items)

    def all(self) -> list[Correction]:
        with self._lock:
            return list(self._items)

    def remove(self, ja: str, right: str = "") -> int:
        with self._lock:
            before = len(self._items)
            if right:
                self._items = [x for x in self._items
                               if not (x.ja.strip() == ja.strip() and x.right.strip() == right.strip())]
            else:
                self._items = [x for x in self._items if x.ja.strip() != ja.strip()]
            removed = before - len(self._items)
        if removed:
            self.rewrite()
        return removed

    def _score(self, c: Correction, source: str) -> float:
        """相关度评分。

        关键：**不能只靠字符重合度**。日语里「今日はいい天気ですね」和
        「全く関係ない長い文章です」共享大量假名与汉字，
        按 Jaccard 算相似度会很高，但内容毫无关系 —— 实测就因此把无关修正
        塞进了 prompt。所以这里要求真正的「包含关系」或很高的重合度。
        """
        a, b = _norm(c.ja), _norm(source)
        if not a or not b:
            return 0.0

        # 1) 原文出现在当前句子里（术语、人名这类最典型）—— 最可靠
        if a in b or b in a:
            return 3.0 + min(len(a), 40) / 100.0

        # 2) 共同子串：连续 4 个字符以上重合才算「有关」
        #    （日语用字符级 n-gram 比集合重合靠谱得多）
        n = 4
        grams_a = {a[i:i + n] for i in range(max(0, len(a) - n + 1))} if len(a) >= n else set()
        grams_b = {b[i:i + n] for i in range(max(0, len(b) - n + 1))} if len(b) >= n else set()
        if grams_a and grams_b:
            inter = len(grams_a & grams_b)
            if inter:
                # 重合比例 + 长度接近程度
                ratio = inter / min(len(grams_a), len(grams_b))
                len_pen = 1.0 / (1.0 + abs(len(a) - len(b)) / 15.0)
                return ratio * len_pen * 2.0

        # 3) 短原文（人名/术语）允许更松的匹配：字符完全被覆盖
        if len(a) <= 6:
            if set(a) <= set(b):
                return 1.5

        return 0.0

    def examples_for(self, source: str, limit: int | None = None) -> list[Correction]:
        """挑出对当前这句最有参考价值的修正。"""
        n = self.max_examples if limit is None else max(0, int(limit))
        if n == 0 or not self._items:
            return []
        scored = [(self._score(c, source), c) for c in self._items]
        # 阈值 1.0：只有「包含关系」或「连续 4 字以上重合」才够格。
        # 阈值太低会把无关修正也塞进 prompt，反而干扰翻译。
        scored = [(s, c) for s, c in scored if s >= 1.0]
        scored.sort(key=lambda t: (-t[0], -t[1].ts))
        picked = [c for _, c in scored[:n]]
        if picked:
            with self._lock:
                for c in picked:
                    c.hits += 1
        return picked

    def prompt_block(self, source: str, limit: int | None = None) -> str:
        """拼成给 LLM 看的「以前的修正」段落。"""
        ex = self.examples_for(source, limit)
        if not ex:
            return ""
        lines = []
        for c in ex:
            if c.wrong and _norm(c.wrong) != _norm(c.right):
                lines.append(f"- 「{c.ja}」以前译成「{c.wrong}」，正确的是「{c.right}」")
            else:
                lines.append(f"- 「{c.ja}」应译成「{c.right}」")
            if c.note:
                lines[-1] += f"（{c.note}）"
        return "\n".join(lines)

    def apply(self, text: str) -> str:
        """后处理：本地 NMT 用不上 prompt，就直接替换见过的错译。

        只在「用户改过的错译」原样出现时替换，避免误伤正常译文。
        """
        if not text:
            return text
        with self._lock:
            items = list(self._items)
        out = text
        for c in sorted(items, key=lambda x: -len(x.wrong or "")):
            if c.wrong and c.right and c.wrong != c.right and c.wrong in out:
                out = out.replace(c.wrong, c.right)
        return out

    # ---------- 术语建议 ----------
    def suggest_terms(self, min_hits: int = 2) -> list[Correction]:
        """从修正里捞「反复出现的短术语」，建议提升为术语表条目。

        真实场景：某个主播名字会散落在很多句子里 ——
        「ぺこら」「ぺこらの歌枠」「ぺこらは今日配信しない」各改过一次。
        这时「ぺこら」就是该进术语表的东西。

        做法：把每条修正的日语原文切成 2~12 字的片段当候选，
        统计它在**多少条不同修正**里出现；出现次数够多就建议。
        比「同一条原文改两次」靠谱得多 —— 后者其实已经被去重合并了。
        """
        items = self.all()
        if not items:
            return []

        counts: dict[str, set[int]] = {}
        for idx, c in enumerate(items):
            a = _norm(c.ja)
            if len(a) < 2:
                continue
            # 只取长度 2~12 的连续片段（人名/术语的典型长度）
            for size in range(2, min(12, len(a)) + 1):
                for start in range(0, len(a) - size + 1):
                    frag = a[start:start + size]
                    counts.setdefault(frag, set()).add(idx)

        out: list[Correction] = []
        seen_frags: list[str] = []
        for frag, idxs in sorted(counts.items(), key=lambda kv: (-len(kv[1]), -len(kv[0]))):
            if len(idxs) < min_hits:
                continue
            # 已经建议过更长的、且包含这个片段的就别重复（长的一般更具体）
            if any(frag in s and len(s) > len(frag) for s in seen_frags):
                continue
            rep = items[min(idxs)]
            out.append(Correction(ja=frag, right=rep.right, wrong=rep.wrong,
                                  note=rep.note, ts=rep.ts, scope="term"))
            seen_frags.append(frag)
            if len(out) >= 8:
                break
        return out

    def stats(self) -> dict:
        with self._lock:
            items = list(self._items)
        return {
            "total": len(items),
            "terms": sum(1 for c in items if c.scope == "term"),
            "reused": sum(1 for c in items if c.hits > 0),
            "suggest_terms": len(self.suggest_terms()),
            "path": str(self.path),
        }
