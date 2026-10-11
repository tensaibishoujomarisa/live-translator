"""翻译上下文与译文清理。

直播口语翻译最大的两个质量来源：
  1. 前文语境（谁在说、刚才聊什么、代词指谁）
  2. 译文格式干净（不要解释、不要罗马字、不要原文）
"""
from __future__ import annotations

import re
import threading
from collections import deque
from dataclasses import dataclass

from .util import jaccard, strip_trailing_punct

_SENT_END = "。！？!?…"


@dataclass
class Pair:
    src: str
    dst: str


class RollingContext:
    """最近若干句「日→中」对照，用于给下一句翻译提供语境。"""

    def __init__(self, max_pairs: int = 4):
        self.max_pairs = max(0, int(max_pairs))
        self._items: deque[Pair] = deque(maxlen=max(1, self.max_pairs) if self.max_pairs else 1)
        self._lock = threading.Lock()
        # 当前正在讲的话题句（长文摘要用），由 LLM 自己从对照里读，不额外维护

    def add(self, src: str, dst: str) -> None:
        if not src or not dst or self.max_pairs == 0:
            return
        with self._lock:
            self._items.append(Pair(src=src, dst=dst))

    def pairs(self) -> list[Pair]:
        with self._lock:
            return list(self._items)

    def as_text(self) -> str:
        ps = self.pairs()
        if not ps:
            return ""
        return "\n".join(f"日：{p.src}\n中：{p.dst}" for p in ps)

    def reset(self) -> None:
        with self._lock:
            self._items.clear()


# ---------------------------------------------------------------- 译文清理

_QUOTE_PAIRS = [("「", "」"), ("『", "』"), ("“", "”"), ("\"", "\""), ("'", "'")]
_LEAD_NOISE = re.compile(
    r"^\s*(翻译|译文|中文|翻訳|訳|translation|output|结果)\s*[:：]\s*", re.IGNORECASE
)
_JA_KANA = re.compile(r"[\u3040-\u309f\u30a0-\u30ff]")
_JA_ONLY = re.compile(r"^[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff\u3000-\u303f\s]+$")


def clean_translation(text: str, source: str = "") -> str:
    """去掉 LLM 常见的多余包装，返回可以直接上屏的一行中文。"""
    if not text:
        return ""
    t = text.strip()

    # 去掉 ``` 代码块
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()

    # 去掉 markdown 引用/列表前缀
    t = re.sub(r"^\s*[-*+]\s+", "", t)
    t = t.replace("**", "").strip()

    # 去掉「翻译：」这类前缀
    t = _LEAD_NOISE.sub("", t)

    # 去掉整体包裹的引号（仅当整行被一对引号包住）
    for left, right in _QUOTE_PAIRS:
        if len(t) > 2 and t.startswith(left) and t.endswith(right):
            inner = t[len(left):-len(right)].strip()
            if inner and left not in inner and right not in inner:
                t = inner
                break

    # 多行：只保留第一行有效内容（LLM 偶尔会加注释）
    if "\n" in t:
        lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
        if lines:
            # 若首行以「注：」开头，取第二行
            if lines[0].startswith(("注", "备注", "说明")) and len(lines) > 1:
                t = lines[1]
            else:
                t = lines[0]

    t = t.strip()
    # 去重标点
    t = re.sub(r"([。！？!?…])\1+", r"\1", t)
    return t.strip()


def is_probably_source(text: str, source: str) -> bool:
    """判断「译文」是不是把日文原文原样吐回来了。"""
    if not text or not source:
        return False
    if _JA_KANA.search(text) and jaccard(text, source) > 0.75:
        return True
    if text.strip() == source.strip():
        return True
    return False


def needs_translation(src: str, min_japanese: int = 2) -> bool:
    """太短或纯符号的片段不值得翻（「あ」「ん？」之类交给上下文）。"""
    if not src:
        return False
    s = strip_trailing_punct(src)
    if len(s) < min_japanese:
        return False
    if not _JA_KANA.search(s) and not re.search(r"[\u4e00-\u9fff]", s):
        return False
    return True


def join_for_display(parts: list[str]) -> str:
    if not parts:
        return ""
    out = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if not out:
            out = p
        elif out[-1] in _SENT_END or p[0] in _SENT_END:
            out += p
        else:
            out += p
    return out
