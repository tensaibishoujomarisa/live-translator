"""直播数据采集与自动调参。

## 为什么做这个

语速快慢直接决定断句参数该怎么设：
  * 语速快（日常日语，主播型 6~8 字/秒）→ 停顿很短，`min_silence_ms` 设 380ms
    会把一句话切成两半，翻译出来是碎句；
  * 语速慢（讲解型 3~4 字/秒）→ 停顿长，阈值太短会把「嗯…」这种也切成句。

但每个人、每场直播都不一样，让用户自己去试参数不现实。
所以：**边跑边记，攒够数据自己算**。

## 能改善什么

| 指标 | 调什么 | 为什么有效 |
|---|---|---|
| 字/秒（语速） | `vad.min_silence_ms` | 快语速要更短的静音判定，否则句子被切碎 |
| 每段字数 | `vad.max_segment_s` | 快语速一句话更长，截断上限要够 |
| 电平 rms | `audio.gain` | 声音小 → 识别准确度直接掉，这是**真能提准确度**的 |
| 语速 | `asr.partial_interval_ms` | 快语速局部刷新要更勤，跟手感更好 |

**不能改善**：模型本身的识别能力（那是 large-v3-turbo 决定的）。
这里只是把「能调的旋钮」拧到适合她的位置。

## 数据文件

`data/stream_stats.jsonl`，一行一条定稿记录，只记数值不记文本
（文本在字幕日志里已经有了，这里不重复存，也避免隐私问题）。
"""
from __future__ import annotations

import json
import statistics
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .util import get_logger

log = get_logger("jplt.stats")


@dataclass
class SegStat:
    """一条定稿记录。"""
    t: float = field(default_factory=time.time)
    audio_s: float = 0.0      # 这段音频多长
    speech_ms: float = 0.0    # 其中真正有人声的部分
    chars: int = 0            # 识别出的日文字数
    rms: float = 0.0          # 音频电平
    no_speech: float = 0.0    # 模型给的非语音概率
    compute_ms: float = 0.0   # 识别耗时
    cps: float = 0.0          # 字/秒（说话期间的实际语速）

    def as_dict(self) -> dict:
        return asdict(self)


def _char_count(text: str) -> int:
    """只数「有效发音字符」：去掉空白和标点。

    这样算出来的字/秒才接近真实语速，不会被感叹号和空格稀释。
    """
    if not text:
        return 0
    n = 0
    for ch in text:
        if ch.isspace():
            continue
        o = ord(ch)
        # 常见中日标点与符号区间，跳过
        if 0x3000 <= o <= 0x303F and ch not in "ー":   # CJK 标点
            continue
        if 0xFF00 <= o <= 0xFFEF:                       # 全角符号
            continue
        if ch in "、。，．！？!?,.:;「」『』（）()【】…ー~〜":
            continue
        n += 1
    return n


class StatsStore:
    """追加式记录 + 聚合统计。线程安全，写失败不影响主流程。"""

    def __init__(self, path: str | Path, window: int = 300):
        self.path = Path(path)
        self.window = max(20, int(window))
        self._lock = threading.Lock()
        self._recent: list[SegStat] = []
        self._load_recent()

    # ---------- 读写 ----------
    def _load_recent(self) -> None:
        """只读文件尾部的 window 条，避免文件越攒越大导致启动变慢。"""
        if not self.path.exists():
            return
        try:
            size = self.path.stat().st_size
            with open(self.path, "rb") as f:
                # 从尾部读，最多 256KB
                back = min(size, 256 * 1024)
                f.seek(size - back)
                raw = f.read().decode("utf-8", "ignore")
            lines = [ln for ln in raw.splitlines() if ln.strip().startswith("{")]
            # 第一行可能是半截，丢掉
            if back < size and lines:
                lines = lines[1:]
            for ln in lines[-self.window:]:
                try:
                    d = json.loads(ln)
                    vals = {k: d[k] for k in d if k in SegStat.__dataclass_fields__}
                    # 数值字段必须是数字。手工改坏文件、或旧版本字段变动后，
                    # 不能把 "x" 这种塞进来 —— 后面 statistics 会直接崩。
                    for k in ("t", "audio_s", "speech_ms", "rms", "no_speech",
                              "compute_ms", "cps"):
                        if k in vals:
                            vals[k] = float(vals[k])
                    if "chars" in vals:
                        vals["chars"] = int(vals["chars"])
                    self._recent.append(SegStat(**vals))
                except Exception:
                    continue
        except Exception as exc:
            log.debug("读历史统计失败：%s", exc)

    def add(self, audio_s: float, text: str, speech_ms: float = 0.0,
            rms: float = 0.0, no_speech: float = 0.0, compute_ms: float = 0.0) -> SegStat:
        chars = _char_count(text)
        audio_s = float(audio_s or 0.0)
        # 语速用「说话时长」算更准；没有 speech_ms 就退回整段时长
        dur = (float(speech_ms) / 1000.0) if speech_ms else audio_s
        cps = (chars / dur) if dur > 0.2 else 0.0
        st = SegStat(audio_s=round(audio_s, 3), speech_ms=round(float(speech_ms), 1),
                     chars=chars, rms=round(float(rms), 5),
                     no_speech=round(float(no_speech), 3),
                     compute_ms=round(float(compute_ms), 1), cps=round(cps, 2))
        with self._lock:
            self._recent.append(st)
            if len(self._recent) > self.window:
                self._recent = self._recent[-self.window:]
            self._append(st)
        return st

    def _append(self, st: SegStat) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(st.as_dict(), ensure_ascii=False) + "\n")
        except Exception as exc:
            log.debug("写统计失败：%s", exc)

    def clear(self) -> int:
        with self._lock:
            n = len(self._recent)
            self._recent = []
        try:
            if self.path.exists():
                self.path.unlink()
        except Exception as exc:
            log.debug("清空统计失败：%s", exc)
        return n

    # ---------- 聚合 ----------
    def records(self) -> list[SegStat]:
        with self._lock:
            return list(self._recent)

    def profile(self) -> dict:
        """把最近的记录汇总成一份画像。"""
        recs = self.records()
        # 只统计「真的识别出字」的段，避免静音段把语速拉低
        voiced = [r for r in recs if r.chars >= 2 and r.cps > 0]
        cps = [r.cps for r in voiced]
        chars = [r.chars for r in voiced]
        rmss = [r.rms for r in recs if r.rms > 0]
        nsp = [r.no_speech for r in recs]
        comp = [r.compute_ms for r in recs if r.compute_ms > 0]

        def pct(vals: list[float], p: float) -> float:
            if not vals:
                return 0.0
            s = sorted(vals)
            i = min(len(s) - 1, max(0, int(round((len(s) - 1) * p))))
            return float(s[i])

        total_audio = sum(r.audio_s for r in recs)
        span = 0.0
        if len(recs) >= 2:
            span = max(r.t for r in recs) - min(r.t for r in recs)
        speech_span = sum(r.speech_ms for r in recs) / 1000.0

        return {
            "segments": len(recs),
            "voiced_segments": len(voiced),
            "cps_median": round(statistics.median(cps), 2) if cps else 0.0,
            "cps_p90": round(pct(cps, 0.9), 2),
            "chars_median": round(statistics.median(chars), 1) if chars else 0.0,
            "chars_avg": round(sum(chars) / len(chars), 1) if chars else 0.0,
            "rms_median": round(statistics.median(rmss), 5) if rmss else 0.0,
            "no_speech_median": round(statistics.median(nsp), 3) if nsp else 0.0,
            "compute_ms_median": round(statistics.median(comp), 1) if comp else 0.0,
            "audio_total_s": round(total_audio, 1),
            "speech_total_s": round(speech_span, 1),
            "span_s": round(span, 1),
            "speech_ratio": round(speech_span / span, 3) if span > 1 else 0.0,
        }

    # ---------- 建议 ----------
    def recommend(self) -> dict:
        """根据画像给出参数建议 + 理由。

        只在样本足够时才给建议（少于 20 段说明还没攒够，别乱调）。
        """
        p = self.profile()
        out: dict = {"overrides": {}, "notes": [], "profile": p}
        if p["voiced_segments"] < 20:
            out["notes"].append(
                f"样本不足（{p['voiced_segments']} 段，需要 20 段以上），暂不调整")
            return out

        cps = p["cps_median"]

        # ---- 断句阈值 ----
        # 日语日常语速约 5~7 字/秒；主播型偏快。
        if cps >= 6.5:
            silence, why = 300, "语速很快，缩短静音判定避免把句子切碎"
        elif cps >= 5.0:
            silence, why = 340, "语速偏快，静音判定略缩短"
        elif cps >= 3.5:
            silence, why = 400, "语速正常，使用标准静音判定"
        else:
            silence, why = 460, "语速偏慢，加长静音判定避免碎句"
        out["overrides"]["vad.min_silence_ms"] = silence
        out["notes"].append(f"语速 {cps} 字/秒 → 断句阈值 {silence}ms（{why}）")

        # ---- 音频增益 ----
        rms = p["rms_median"]
        if rms and rms < 0.008:
            gain = 3.0
            out["notes"].append(f"电平偏低（rms={rms}）→ 增益 3.0，能明显提高识别准确度")
        elif rms and rms < 0.02:
            gain = 1.8
            out["notes"].append(f"电平略低（rms={rms}）→ 增益 1.8")
        else:
            gain = 1.0
            if rms:
                out["notes"].append(f"电平正常（rms={rms}）→ 增益 1.0")
        if gain != 1.0:
            out["overrides"]["audio.gain"] = gain

        # ---- 局部刷新间隔 ----
        if cps >= 6.0:
            out["overrides"]["asr.partial_interval_ms"] = 450
            out["notes"].append("语速快 → 局部字幕刷新加快到 450ms")

        # ---- 每段字数异常（切得太碎或太长）----
        avg = p["chars_avg"]
        if avg and avg < 8:
            out["notes"].append(
                f"平均每段只有 {avg} 字，说明切得很碎；可以先调小 min_silence_ms 试试")
        elif avg > 40:
            out["notes"].append(
                f"平均每段 {avg} 字，句子偏长；翻译延迟会变高，可调大 min_silence_ms")

        return out

    def stats(self) -> dict:
        return {"profile": self.profile(), "path": str(self.path)}
