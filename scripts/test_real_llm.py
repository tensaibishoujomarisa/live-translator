"""用真实 API Key 做一次 LLM 翻译连通性验证（读 ui-settings.json / config.yaml）。

    python -m scripts.test_real_llm

会打印：用的哪个 Key（脱敏）、base_url、模型、每句译文和耗时。
只发 2 次请求，成本可以忽略。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import Config, resolve_api_key  # noqa: E402
from backend.translator import create_translator  # noqa: E402

SENTENCES = [
    "ちょっと待って、いま何時？",
    "スパチャありがとうございます！めっちゃ嬉しいです。",
]


def main() -> int:
    cfg = Config.load()

    # 界面设置优先级高于 config.yaml，跟实际运行时保持一致
    ui = ROOT / "ui-settings.json"
    over = {}
    if ui.exists():
        try:
            j = json.loads(ui.read_text(encoding="utf-8"))
            over = {
                "provider": j.get("provider") or cfg.get("translate", "provider"),
                "target_lang": j.get("targetLang") or cfg.get("translate", "target_lang"),
                "llm": {
                    "api_key": j.get("apiKey") or "",
                    "base_url": j.get("baseUrl") or cfg.get("translate", "llm", "base_url"),
                    "model": j.get("llmModel") or cfg.get("translate", "llm", "model"),
                },
            }
            if not over["llm"]["api_key"]:
                over["llm"].pop("api_key")
        except Exception as exc:
            print(f"[warn] 读 ui-settings.json 失败：{exc}")

    if over:
        cfg = cfg.with_overrides({"translate": over})

    provider = cfg.get("translate", "provider")
    key = resolve_api_key(cfg)
    base = cfg.get("translate", "llm", "base_url")
    model = cfg.get("translate", "llm", "model")
    tgt = cfg.get("translate", "target_lang")

    print("=" * 66)
    print(" LLM 翻译真实连通性验证")
    print("=" * 66)
    print(f"provider   : {provider}")
    print(f"base_url   : {base}")
    print(f"model      : {model}")
    print(f"target_lang: {tgt}")
    if key:
        print(f"api_key    : {key[:8]}…{key[-4:]}（长度 {len(key)}）")
    else:
        print("api_key    : 未配置")
        return 2

    if provider != "llm":
        print(f"\n[提示] 当前 provider 是 {provider}，不是 llm。"
              f"下面仍然会强制用 llm 试一次。")
        cfg = cfg.with_overrides({"translate": {"provider": "llm"}})

    tr = create_translator(cfg, logger_status=lambda m: print("  " + m))
    print(f"translator : {tr.describe()}\n")

    ok = 0
    for s in SENTENCES:
        t0 = time.time()
        try:
            out = tr.translate(s)
            dt = (time.time() - t0) * 1000
            print(f"  日 {s}")
            print(f"  中 {out or '(空)'}    [{dt:.0f}ms]")
            if out:
                ok += 1
        except Exception as exc:
            print(f"  日 {s}")
            print(f"  ✘ {type(exc).__name__}: {exc}")
            if tr.disabled:
                print(f"  → {tr.disabled_reason}")
                break

    print()
    if ok == len(SENTENCES):
        print("结论：真实 LLM 翻译链路正常 ✔")
        print("      在字幕窗里把「翻译方式」选成「在线大模型」即可用这个 Key。")
        return 0
    print("结论：没能跑通 ✘  —— 检查 Key、余额、base_url")
    return 1


if __name__ == "__main__":
    sys.exit(main())
