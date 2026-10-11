"""小工具：日志、重采样、RMS、文本规范化、日语检测。"""
from __future__ import annotations

import logging
import logging.handlers
import math
import re
import sys
import unicodedata
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------- 日志


def setup_logging(level: str = "info", file: str | None = None) -> logging.Logger:
    logger = logging.getLogger("jplt")
    if logger.handlers:
        return logger
    lvl = getattr(logging, str(level).upper(), logging.INFO)
    logger.setLevel(lvl)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%H:%M:%S")

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    sh.setLevel(lvl)
    logger.addHandler(sh)

    if file:
        try:
            p = Path(file)
            p.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                p, maxBytes=4 * 1024 * 1024, backupCount=2, encoding="utf-8"
            )
            fh.setFormatter(fmt)
            fh.setLevel(lvl)
            logger.addHandler(fh)
        except Exception as exc:  # 日志写不了不该影响主流程
            logger.warning("日志文件不可用: %s", exc)
    logger.propagate = False

    # 第三方库很吵：transformers 每个 batch 打一遍提示，huggingface_hub 也爱刷。
    # 它们走的是各自的 logger，我们统一压到 ERROR。
    for noisy in (
        "transformers",
        "transformers.generation.utils",
        "huggingface_hub",
        "filelock",
        "urllib3",
        "PIL",
    ):
        logging.getLogger(noisy).setLevel(logging.ERROR)

    return logger


def get_logger(name: str = "jplt") -> logging.Logger:
    return logging.getLogger(name)


# ---------------------------------------------------------------- 音频数学


def rms(x: np.ndarray) -> float:
    if x.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))


def peak(x: np.ndarray) -> float:
    if x.size == 0:
        return 0.0
    return float(np.max(np.abs(x)))


def to_mono(x: np.ndarray) -> np.ndarray:
    if x.ndim == 1:
        return x.astype(np.float32, copy=False)
    return x.mean(axis=1).astype(np.float32, copy=False)


def resample_linear(x: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """线性插值重采样。够用且极快；16k 语音识别对阻带要求不高。"""
    if src_rate == dst_rate or x.size == 0:
        return x.astype(np.float32, copy=False)
    n_out = int(round(x.size * dst_rate / float(src_rate)))
    if n_out <= 0:
        return np.zeros(0, dtype=np.float32)
    if n_out == 1:
        return x[:1].astype(np.float32, copy=False)
    src_idx = np.linspace(0.0, x.size - 1.0, n_out, dtype=np.float64)
    out = np.interp(src_idx, np.arange(x.size, dtype=np.float64), x.astype(np.float64))
    return out.astype(np.float32)


def dbfs(x: float) -> float:
    return 20.0 * math.log10(max(x, 1e-9))


# ---------------------------------------------------------------- 文本处理

_JA_RE = re.compile(r"[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_WS_RE = re.compile(r"[ \t\u3000]+")
_PUNCT = "。、！？!?…,.，"


def has_japanese(text: str) -> bool:
    """是否含假名/汉字。纯拉丁文本通常是幻觉或弹幕串台。"""
    return bool(_JA_RE.search(text))


def normalize_ja(text: str) -> str:
    """清理识别结果的空白与重复标点，但保留口语原貌（不改写用词）。"""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = _WS_RE.sub(" ", t).strip()
    t = re.sub(rf"([{re.escape(_PUNCT)}])\1+", r"\1", t)
    return t.strip()


def strip_trailing_punct(text: str) -> str:
    return text.rstrip(_PUNCT + " ").strip()


def ends_with_sentence_punct(text: str) -> bool:
    return bool(text) and text.rstrip()[-1] in _PUNCT


def is_repetitive(text: str, min_len: int = 4, max_period: int = 12) -> bool:
    """检测 Whisper 经典幻觉：同一小段无限重复。"""
    t = strip_trailing_punct(text)
    if len(t) < min_len * 2:
        return False
    n = len(t)
    for period in range(1, min(max_period, n // 2) + 1):
        unit = t[-period:]
        repeats = n // period
        if repeats >= 3 and unit * repeats == t[n - period * repeats:]:
            return True
    return False


def longest_common_suffix_prefix(a: str, b: str, cap: int = 200) -> int:
    """a 的后缀与 b 的前缀最长公共长度，用于 partial 结果去重叠。"""
    if not a or not b:
        return 0
    max_len = min(len(a), len(b), cap)
    for size in range(max_len, 0, -1):
        if a.endswith(b[:size]):
            return size
    return 0


def merge_committed(committed: str, new_text: str) -> str:
    """把一次新的识别结果并进已确定文本，去掉重叠部分。"""
    if not committed:
        return new_text
    if not new_text:
        return committed
    if new_text.startswith(committed):
        return new_text
    if committed.endswith(new_text):
        return committed
    ov = longest_common_suffix_prefix(committed, new_text)
    if ov >= 2:
        return committed + new_text[ov:]
    # 完全没有重叠：把已确定部分接上（宁可重复也不要丢字）
    return committed + new_text


def jaccard(a: str, b: str) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(len(sa | sb))


def split_sentences(text: str, max_len: int = 60) -> list[str]:
    """把长段按句末标点切开，没有标点时按 max_len 硬切。"""
    if not text:
        return []
    parts: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in "。！？!?…" and len(buf) >= 2:
            parts.append(buf)
            buf = ""
    if buf.strip():
        parts.append(buf)
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(p) <= max_len:
            out.append(p)
        else:
            for i in range(0, len(p), max_len):
                chunk = p[i:i + max_len].strip()
                if chunk:
                    out.append(chunk)
    return out
