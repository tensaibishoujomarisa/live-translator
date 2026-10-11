"""验证 LLM 翻译链路（provider=llm）。

这是用户实际会用的路径，但需要 API Key 才能跑真实服务；
所以这里用一个 OpenAI 兼容的 mock 服务，把「请求长什么样、错误怎么处理、
流式拼装对不对」全部断言一遍，不需要真 Key、不碰外网。

    python -m scripts.test_llm_path
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import Config  # noqa: E402
from backend.mt_context import RollingContext  # noqa: E402
from backend.translator import create_translator  # noqa: E402
from scripts.mock_llm_server import MockLlmServer  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✔ {name}")
    else:
        FAIL += 1
        print(f"  ✘ {name}" + (f"  ({detail})" if detail else ""))


def make_cfg(port_base: str, **llm_over):
    llm = {
        "base_url": port_base,
        "api_key": "test-key-123",
        "model": "mock-model",
        "stream": False,
        "retry": 0,
        "timeout_s": 10,
    }
    llm.update(llm_over)
    return Config.load().with_overrides(
        {"translate": {"provider": "llm", "llm": llm, "timeout_s": 10}}
    )


def main() -> int:
    print("=" * 70)
    print(" LLM 翻译链路验证（mock OpenAI 兼容服务）")
    print("=" * 70)

    with MockLlmServer() as srv:
        print(f"\nmock 服务：{srv.base_url}")
        JA = "ちょっと待って、いま何時？"

        # ---------- 1. 基本调用 ----------
        print("\n[1] 基本调用（非流式）")
        tr = create_translator(make_cfg(srv.base_url), logger_status=lambda m: print("    " + m))
        out = tr.translate(JA)
        check("返回了译文", bool(out), repr(out))
        check("译文内容正确", out == "〔译〕这是模拟的中文译文", repr(out))
        body = srv.state.requests[-1]["body"]
        check("模型名透传", body.get("model") == "mock-model", str(body.get("model")))
        check("temperature 已设置", "temperature" in body)
        check("endpoint 自动补 /chat/completions",
              srv.state.requests[-1]["path"].endswith("/chat/completions"),
              srv.state.requests[-1]["path"])

        # ---------- 1b. 原文回显过滤器 ----------
        print("\n[1b] 原文回显过滤（LLM 有时会把日文原样吐回来）")
        srv.state.reply_template = "{src}"          # 原样回显日文
        tr_echo = create_translator(make_cfg(srv.base_url), logger_status=lambda m: None)
        got_echo = tr_echo.translate(JA)
        check("原样回显被丢弃", got_echo == "", repr(got_echo))
        srv.state.reply_template = "「{src}」"        # 带引号回显：去引号后仍是日文，也该丢
        tr_echo2 = create_translator(make_cfg(srv.base_url), logger_status=lambda m: None)
        got_echo2 = tr_echo2.translate(JA)
        check("带引号回显被丢弃", got_echo2 == "", repr(got_echo2))
        srv.state.reply_template = "```\n〔译〕代码块里的译文\n```"
        tr_fence = create_translator(make_cfg(srv.base_url), logger_status=lambda m: None)
        got_fence = tr_fence.translate(JA)
        check("代码块包装被剥掉", got_fence == "〔译〕代码块里的译文", repr(got_fence))
        srv.state.reply_template = "翻译：〔译〕带前缀的译文"
        tr_pref = create_translator(make_cfg(srv.base_url), logger_status=lambda m: None)
        got_pref = tr_pref.translate(JA)
        check("「翻译：」前缀被剥掉", got_pref == "〔译〕带前缀的译文", repr(got_pref))
        srv.state.reply_template = "〔译〕这是模拟的中文译文"

        # ---------- 2. 上下文注入 ----------
        print("\n[2] 滚动上下文注入")
        ctx = RollingContext(max_pairs=3)
        ctx.add("前一句日文", "前一句中文")
        ctx.add("再前一句", "再前一句中文")
        tr.translate(JA, context_block=ctx.as_text())
        prompt = srv.state.last_user_prompt()
        check("user prompt 含【本句】", "【本句】" in prompt)
        check("user prompt 含本句原文", JA in prompt)
        check("user prompt 注入【前文】", "【前文】" in prompt)
        check("前文包含上一句译文", "前一句中文" in prompt)
        check("前文包含再上一句", "再前一句中文" in prompt)
        sysp = srv.state.last_system_prompt()
        check("system prompt 是字幕翻译角色", "字幕" in sysp)
        check("system prompt 提到日语", "日语" in sysp)
        check("system prompt 强调口语化", "口语" in sysp)

        # ---------- 3. 术语表注入 ----------
        print("\n[3] 术语表注入")
        gl_path = ROOT / "glossary.yaml"
        if gl_path.exists():
            tr_gl = create_translator(make_cfg(srv.base_url), logger_status=lambda m: None)
            tr_gl.translate("スパチャありがとう")
            prompt = srv.state.last_user_prompt()
            check("术语表进入 prompt", "【术语表】" in prompt)
            check("术语表条目可见（スパチャ）", "スパチャ" in prompt)
            check("术语表指定译法可见", "SC（打赏）" in prompt, prompt[:200])
        else:
            check("术语表文件存在", False, str(gl_path))

        # ---------- 4. 流式 ----------
        print("\n[4] 流式输出（stream=true）")
        srv.state.requests.clear()
        tr_s = create_translator(make_cfg(srv.base_url, stream=True), logger_status=lambda m: None)
        deltas: list[str] = []
        final = tr_s.translate(JA, on_delta=lambda d: deltas.append(d))
        check("流式请求带 stream=true", srv.state.requests[-1]["body"].get("stream") is True)
        check("流式结果完整", final == "〔译〕这是模拟的中文译文", repr(final))
        check("流式有增量回调", len(deltas) >= 1, f"{len(deltas)} 次")

        # ---------- 5. 错误处理 ----------
        print("\n[5] 错误处理")
        srv.state.mode = "unauthorized"
        tr_bad = create_translator(
            make_cfg(srv.base_url, api_key="wrong"), logger_status=lambda m: None
        )
        try:
            tr_bad.translate(JA)
            check("401 抛出异常", False, "没有抛异常")
        except Exception as exc:
            check("401 抛出异常", True)
            check("异常信息含 401", "401" in str(exc), str(exc)[:80])

        srv.state.mode = "server_error"
        tr_500 = create_translator(make_cfg(srv.base_url), logger_status=lambda m: None)
        try:
            tr_500.translate(JA)
            check("500 抛出异常", False, "没有抛异常")
        except Exception as exc:
            check("500 抛出异常", True)

        srv.state.mode = "empty"
        tr_empty = create_translator(make_cfg(srv.base_url), logger_status=lambda m: None)
        try:
            got = tr_empty.translate(JA)
            check("空 choices 不崩溃", True)
            check("空 choices 返回空串", got == "", repr(got))
        except Exception as exc:
            check("空 choices 不崩溃", False, f"{type(exc).__name__}: {exc}")

        srv.state.mode = "ok"

        # ---------- 5b. 连续失败熔断 ----------
        print("\n[5b] 连续失败熔断（Key 失效时不该每句话刷一条错误）")
        from backend.translator import TranslationDisabled

        srv.state.mode = "unauthorized"
        tr_cb = create_translator(make_cfg(srv.base_url, api_key="bad"), logger_status=lambda m: None)
        raised = 0
        for _ in range(tr_cb.FAILURE_LIMIT):
            try:
                tr_cb.translate(JA)
            except Exception:
                raised += 1
        check(f"前 {tr_cb.FAILURE_LIMIT} 次都抛异常", raised == tr_cb.FAILURE_LIMIT, f"{raised} 次")
        check("达到上限后进入熔断", tr_cb.disabled, tr_cb.disabled_reason)
        check("熔断原因可读（提到 Key）", "Key" in tr_cb.disabled_reason, tr_cb.disabled_reason)
        try:
            tr_cb.translate(JA)
            check("熔断后不再发请求", False, "没有抛 TranslationDisabled")
        except TranslationDisabled:
            check("熔断后不再发请求", True)
        n_before = len(srv.state.requests)
        try:
            tr_cb.translate(JA)
        except Exception:
            pass
        check("熔断后请求数不增长", len(srv.state.requests) == n_before,
              f"{n_before} -> {len(srv.state.requests)}")
        srv.state.mode = "ok"
        tr_cb.reset_circuit()
        out_cb = tr_cb.translate(JA)
        check("reset 后恢复正常", bool(out_cb), repr(out_cb))

        # ---------- 6. 缺 Key 时的降级 ----------
        print("\n[6] 未配置 API Key 时优雅降级")
        import os

        saved = os.environ.pop("JP_LIVE_TRANSLATOR_API_KEY", None)
        saved2 = os.environ.pop("DEEPSEEK_API_KEY", None)
        saved3 = os.environ.pop("OPENAI_API_KEY", None)
        try:
            cfg_nokey = Config.load().with_overrides(
                {"translate": {"provider": "llm", "llm": {"api_key": "", "api_key_env": "JP_LIVE_NO_SUCH_KEY"}}}
            )
            tr_nokey = create_translator(cfg_nokey, logger_status=lambda m: None)
            got = tr_nokey.translate(JA)
            check("无 Key 时 translate 不抛异常、返回空", got == "", repr(got))
            check("翻译器标记为 off", "off" in tr_nokey.describe().lower(), tr_nokey.describe())
            check("off 时 disabled 为真（流水线据此跳过）", tr_nokey.disabled)

            # 同一路径在 provider=off 下也必须安全
            cfg_off = Config.load().with_overrides({"translate": {"provider": "off"}})
            tr_off = create_translator(cfg_off, logger_status=lambda m: None)
            got_off = tr_off.translate(JA)
            check("provider=off 时 translate 返回空不抛异常", got_off == "", repr(got_off))
        finally:
            if saved:
                os.environ["JP_LIVE_TRANSLATOR_API_KEY"] = saved
            if saved2:
                os.environ["DEEPSEEK_API_KEY"] = saved2
            if saved3:
                os.environ["OPENAI_API_KEY"] = saved3

        # ---------- 7. 接进完整流水线 ----------
        print("\n[7] 接进完整流水线（播放音频 → 摄像头链路全通）")
        wav = ROOT / "testdata" / "ja_sample16k.wav"
        if not wav.exists():
            check("测试音频存在", False, str(wav))
        else:
            import wave

            import numpy as np

            from backend.bridge import Bridge
            from backend.pipeline import Pipeline

            with wave.open(str(wav), "rb") as w:
                audio = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0

            events: list[dict] = []
            lock = threading.Lock()

            class Collect(Bridge):
                def _write(self, msg: dict) -> None:
                    with lock:
                        events.append(msg)

            cfg = make_cfg(srv.base_url)
            cfg = cfg.with_overrides(
                {"translate": {"provider": "llm", "llm": {
                    "base_url": srv.base_url, "api_key": "test-key-123", "model": "mock-model"
                }}}
            )
            pl = Pipeline(cfg, Collect(mode="none"))
            pl.build()
            pl.start()
            time.sleep(1.0)
            try:
                import sounddevice as sd

                sd.play(audio * 0.85, samplerate=16000)
                sd.wait()
            except Exception as exc:
                print(f"    [warn] 播放失败：{exc}")
            time.sleep(3.0)
            pl.stop()

            with lock:
                got_events = list(events)
            finals = [e for e in got_events if e.get("kind") == "final"]
            trans = [e for e in got_events if e.get("kind") == "translation"]
            errs = [e for e in got_events if e.get("kind") == "error"]
            check("流水线产出定稿字幕", len(finals) > 0, f"{len(finals)} 条")
            check("流水线产出 LLM 译文", len(trans) > 0, f"{len(trans)} 条")
            if trans:
                check("译文来自 mock 服务", trans[0].get("text", "").startswith("〔译〕"),
                      repr(trans[0].get("text"))[:60])
                check("译文带 source 字段", bool(trans[0].get("source")))
                check("译文带延迟统计", isinstance(trans[0].get("latency_ms"), int))
            check("无错误事件", len(errs) == 0, str(errs[:2]))

    print("\n" + "=" * 70)
    print(f" 通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
