"""术语表 / 人名对照表。

两种用途：
  * LLM 翻译时作为 prompt 里的强制对照（最有效）
  * 翻译结果的后处理替换（本地 NMT 时的主要手段）

文件格式（glossary.yaml，缺文件也能跑）：
    entries:
      - ja: ぺこら
        zh: 佩克拉
        note: 主播名，不要意译
      - ja: ホロライブ
        zh: hololive
    # 也可以只写普通文本，一行一条：
    # ぺこら = 佩克拉
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .util import get_logger

log = get_logger("jplt.glossary")


def _yaml_scalar(v: str) -> str:
    """把字符串安全地写成 YAML 标量。

    日语里出现 `:` `#` `[` 之类字符时裸写会坏掉文件，统一加引号最稳。
    """
    s = str(v)
    if s == "" :
        return '""'
    if re.search(r"[:#\[\]{}&*!|>'\"%@`,\n]", s) or s != s.strip():
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


@dataclass
class Entry:
    ja: str
    zh: str
    note: str = ""
    case_sensitive: bool = False
    # 模型可能吐出来的音译/异体写法，例如 スパチャ → "Spacha"
    alts: list[str] = field(default_factory=list)


class Glossary:
    def __init__(self, entries: list[Entry] | None = None, path: str | Path | None = None):
        self.entries: list[Entry] = entries or []
        # 记住来源路径，这样「提升为术语」时知道该写回哪里
        self.path: Path | None = Path(path) if path else None

    # ---------- 加载 ----------
    @classmethod
    def load(cls, path: str | Path | None, enabled: bool = True) -> "Glossary":
        if not enabled or not path:
            return cls([])
        p = Path(path)
        if not p.exists():
            log.debug("术语表不存在：%s", p)
            # 即使文件不存在也记住路径，方便后续写入时创建
            return cls([], path=p)
        text = p.read_text(encoding="utf-8")
        entries: list[Entry] = []
        data = None
        try:
            import yaml  # type: ignore

            data = yaml.safe_load(text)
        except Exception:
            try:
                import json

                data = json.loads(text)
            except Exception:
                data = None

        if isinstance(data, dict):
            raw = data.get("entries") or data.get("terms") or []
            if isinstance(raw, dict):
                raw = [{"ja": k, "zh": v} for k, v in raw.items()]
            for item in raw:
                if not isinstance(item, dict):
                    continue
                ja = str(item.get("ja") or item.get("src") or "").strip()
                zh = str(item.get("zh") or item.get("dst") or "").strip()
                if ja and zh:
                    alts = item.get("alt") or item.get("alts") or []
                    if isinstance(alts, str):
                        alts = [alts]
                    entries.append(
                        Entry(
                            ja=ja,
                            zh=zh,
                            note=str(item.get("note") or ""),
                            case_sensitive=bool(item.get("case_sensitive")),
                            alts=[str(a) for a in alts if a],
                        )
                    )
        else:
            # 退化格式： あ = ア
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r"^(.+?)\s*[=＝:：]\s*(.+)$", line)
                if m:
                    entries.append(Entry(ja=m.group(1).strip(), zh=m.group(2).strip()))

        log.info("载入术语表 %d 条 (%s)", len(entries), p.name)
        return cls(entries, path=p)

    def __len__(self) -> int:
        return len(self.entries)

    # ---------- 写入 ----------
    def has(self, ja: str) -> bool:
        a = (ja or "").strip()
        return any(e.ja.strip() == a for e in self.entries) if a else False

    def append_entries(self, new: list["Entry"], path: str | Path | None = None) -> dict:
        """把新条目**追加**到 glossary.yaml 末尾，不动任何已有内容。

        为什么不用 write_entries：那个会整份重写，用户手写的注释、
        分组、alt 列表全都会丢。追加式写入只加行，已有内容一个字节都不碰 ——
        对「批量导入一份候选术语」这种场景安全得多。

        返回 {"added": [...], "skipped": [...], "path": ..., "total": ...}
        """
        p = Path(path) if path else self.path
        if p is None:
            raise ValueError("没有指定 glossary 路径")
        p = Path(p)

        added: list[str] = []
        skipped: list[str] = []
        have = {e.ja.strip() for e in self.entries}
        to_add: list[Entry] = []
        for e in new:
            ja = (e.ja or "").strip()
            if not ja or not (e.zh or "").strip():
                skipped.append(ja or "(空)")
                continue
            if ja in have:
                skipped.append(ja)      # 已存在就不动它（改译法请去改原文件）
                continue
            have.add(ja)
            to_add.append(e)
        if not to_add:
            return {"added": [], "skipped": skipped, "path": str(p),
                    "total": len(self.entries)}

        if p.exists():
            try:
                p.with_suffix(p.suffix + ".bak").write_bytes(p.read_bytes())
            except Exception as exc:
                log.debug("备份术语表失败：%s", exc)
            text = p.read_text(encoding="utf-8")
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            text = "entries:\n"

        if not re.search(r"(?m)^\s*entries\s*:", text):
            if not text.endswith("\n"):
                text += "\n"
            text += "\nentries:\n"
        if not text.endswith("\n"):
            text += "\n"

        lines = ["# ---- 以下由「导入候选术语」自动追加 ----"]
        for e in to_add:
            lines.append(f"  - ja: {_yaml_scalar(e.ja)}")
            lines.append(f"    zh: {_yaml_scalar(e.zh)}")
            if e.note:
                lines.append(f"    note: {_yaml_scalar(e.note)}")
            if e.alts:
                alts = ", ".join(_yaml_scalar(a) for a in e.alts)
                lines.append(f"    alt: [{alts}]")
            added.append(e.ja.strip())

        p.write_text(text + "\n".join(lines) + "\n", encoding="utf-8")
        self.entries.extend(to_add)     # 同步内存，调用方不用重新加载
        log.info("追加 %d 条术语 -> %s", len(added), p)
        return {"added": added, "skipped": skipped, "path": str(p),
                "total": len(self.entries)}

    def write_entries(self, path: str | Path | None = None) -> int:
        """把内存里的条目写回 glossary.yaml（按 ja 排序，保留文件头说明）。

        只用于「把修正提升为术语」这类操作。写之前会备份成 .bak，
        因为用户可能在文件里手写了注释 —— 重写会丢掉条目内的注释，
        但顺序和内容会保留。
        """
        p = Path(path) if path else self.path
        if p is None:
            raise ValueError("没有指定 glossary 路径")
        p = Path(p)
        if p.exists():
            try:
                p.with_suffix(p.suffix + ".bak").write_bytes(p.read_bytes())
            except Exception as exc:
                log.debug("备份术语表失败：%s", exc)

        lines = [
            "# ============================================================",
            "#  术语表 —— 人名 / 专有名词强制对照",
            "#  这里的词翻译时会被**强制**使用你指定的中文写法。",
            "#  界面里改字幕时勾「专有名词」会自动追加到这里。",
            "#  也可以手工编辑：注意 ja/zh 都要写全。",
            "# ============================================================",
            "",
            "entries:",
        ]
        for e in sorted(self.entries, key=lambda x: x.ja):
            lines.append(f"  - ja: {_yaml_scalar(e.ja)}")
            lines.append(f"    zh: {_yaml_scalar(e.zh)}")
            if e.note:
                lines.append(f"    note: {_yaml_scalar(e.note)}")
            if e.alts:
                alts = ", ".join(_yaml_scalar(a) for a in e.alts)
                lines.append(f"    alt: [{alts}]")
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("\n".join(lines) + "\n", encoding="utf-8")
            log.info("术语表已更新：%d 条 -> %s", len(self.entries), p)
            return len(self.entries)
        except Exception as exc:
            log.warning("写术语表失败：%s", exc)
            raise

    def add(self, ja: str, zh: str, note: str = "", alts: list[str] | None = None,
            path: str | Path | None = None) -> bool:
        """加一条术语并落盘。已存在同名词条时更新译法。

        返回 True 表示有变化（新加或改动），False 表示已存在且完全相同。
        """
        ja, zh = (ja or "").strip(), (zh or "").strip()
        if not ja or not zh:
            return False
        for e in self.entries:
            if e.ja.strip() == ja:
                if e.zh == zh and not (alts or []):
                    return False
                e.zh = zh
                if note:
                    e.note = note
                if alts:
                    e.alts = sorted(set(e.alts) | set(alts))
                self.write_entries(path)
                return True
        self.entries.append(Entry(ja=ja, zh=zh, note=note, alts=list(alts or [])))
        self.write_entries(path)
        return True

    # ---------- prompt ----------
    def prompt_block(self, limit: int = 80) -> str:
        if not self.entries:
            return ""
        lines = []
        for e in self.entries[:limit]:
            if e.note:
                lines.append(f"- {e.ja} → {e.zh}（{e.note}）")
            else:
                lines.append(f"- {e.ja} → {e.zh}")
        return "\n".join(lines)

    # ---------- 后处理 ----------
    def apply(self, text: str) -> str:
        """把译文里残留的日文原词替换成术语表指定的中文。

        本地 NMT 常把片假名音译成汉字（スパチャ → Spacha、リスナー → Risner），
        所以除了原词本身，也顺手把「假名互换写法」和条目里 alt 字段列出的变体一起替换。
        """
        if not text or not self.entries:
            return text
        out = text
        # 长的先替换，避免短词破坏长词
        for e in sorted(self.entries, key=lambda x: -len(x.ja)):
            if not e.ja or e.ja == e.zh:
                continue
            variants = [e.ja, _katakana_variant(e.ja)]
            variants += [str(v) for v in getattr(e, "alts", []) if v]
            for v in variants:
                if v and v != e.zh and v in out:
                    out = out.replace(v, e.zh)
        return out


def _katakana_variant(s: str) -> str:
    """把片假名转平假名（反之亦然），用于容忍识别结果的假名写法差异。"""
    out = []
    for ch in s:
        code = ord(ch)
        if 0x30A1 <= code <= 0x30F6:  # 片假名 → 平假名
            out.append(chr(code - 0x60))
        elif 0x3041 <= code <= 0x3096:  # 平假名 → 片假名
            out.append(chr(code + 0x60))
        else:
            out.append(ch)
    return "".join(out)
