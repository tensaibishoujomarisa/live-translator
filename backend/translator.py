"""翻译后端。

两个可切换实现，接口一致：
  * LLMTranslator   —— 任何 OpenAI 兼容的 /chat/completions（DeepSeek、OpenAI、硅基流动…）
  * LocalTranslator —— CTranslate2 + OPUS-MT/双语 NMT 模型，纯本地、零费用

工厂 create_translator 按配置选择；provider=off 时返回空翻译器（只显示日文）。
"""
from __future__ import annotations

import json
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Iterable

from .glossary import Glossary
from .mt_context import RollingContext, clean_translation, is_probably_source
from .util import get_logger

log = get_logger("jplt.mt")

StreamCb = Callable[[str], None]


class Translator(ABC):
    name = "base"

    # 连续失败多少次后熔断（避免 Key 失效时每句话刷一条错误）
    FAILURE_LIMIT = 4

    def __init__(self, glossary: Glossary | None = None):
        self.glossary = glossary or Glossary([])
        self.stats = {"calls": 0, "chars": 0, "total_s": 0.0, "errors": 0}
        self._fail_streak = 0
        self._disabled_reason = ""

    @abstractmethod
    def _run(self, source: str, context: str, glossary_block: str) -> str:
        ...

    @property
    def disabled(self) -> bool:
        """熔断中：翻译暂时不可用，调用方应跳过而不是重试。"""
        return bool(self._disabled_reason)

    @property
    def disabled_reason(self) -> str:
        return self._disabled_reason

    def reset_circuit(self) -> None:
        self._fail_streak = 0
        self._disabled_reason = ""

    def translate(
        self,
        source: str,
        context_block: str = "",
        on_delta: StreamCb | None = None,
    ) -> str:
        if self.disabled:
            raise TranslationDisabled(self._disabled_reason)

        t0 = time.time()
        gb = self.glossary.prompt_block() if self.glossary else ""
        try:
            out = self._run(source, context_block, gb)
            self._fail_streak = 0
        except Exception as exc:
            self.stats["errors"] += 1
            self._fail_streak += 1
            log.warning("翻译失败(%d/%d): %s: %s",
                        self._fail_streak, self.FAILURE_LIMIT, type(exc).__name__, exc)
            if self._fail_streak >= self.FAILURE_LIMIT:
                self._disabled_reason = self._explain_failure(exc)
                log.error("翻译已熔断：%s", self._disabled_reason)
            raise
        out = clean_translation(out, source)
        if self.glossary:
            out = self.glossary.apply(out)
        if is_probably_source(out, source):
            log.debug("译文疑似原文回显，丢弃：%s", out[:40])
            out = ""
        self.stats["calls"] += 1
        self.stats["chars"] += len(source)
        self.stats["total_s"] += time.time() - t0
        if on_delta and out:
            on_delta(out)
        return out

    def _explain_failure(self, exc: Exception) -> str:
        """把异常翻译成给人看的一句话。"""
        # 状态码可能来自 requests 的 HTTPError，也可能来自我们自己包的 TranslationHttpError
        status = getattr(exc, "status_code", None) or getattr(
            getattr(exc, "response", None), "status_code", None
        )
        if status in (401, 403):
            return "API Key 无效或无权限，已停止翻译（检查 Key 后重启后端）"
        if status == 429:
            return "API 限流或余额不足，已停止翻译（稍后重启后端重试）"
        if status and int(status) >= 500:
            return f"翻译服务返回 {status}，已停止翻译（稍后重启后端重试）"
        if isinstance(exc, FileNotFoundError):
            return f"本地翻译模型缺失，已停止翻译（{exc}）"
        return f"连续 {self._fail_streak} 次翻译失败，已停止翻译（{type(exc).__name__}: {exc}）"

    def warmup(self) -> None:
        return

    def describe(self) -> str:
        return self.name


class TranslationDisabled(RuntimeError):
    """熔断期间调用翻译抛这个，调用方据此跳过。"""


class TranslationHttpError(RuntimeError):
    """带状态码的翻译接口错误，方便上层给出「Key 无效/限流/服务故障」这类可读结论。"""

    def __init__(self, status_code: int, message: str):
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = int(status_code)


class NullTranslator(Translator):
    """翻译关闭时的占位实现。

    注意：这里必须**自己实现 translate**，不能走基类。
    基类里有「熔断中直接抛 TranslationDisabled」的守卫，
    而 provider=off 属于「本来就不翻」，不是故障 —— 调用方只想要一个空结果。
    """

    name = "off"

    def _run(self, source: str, context: str, glossary_block: str) -> str:
        return ""

    def translate(self, source: str, context_block: str = "", on_delta=None) -> str:
        return ""

    @property
    def disabled(self) -> bool:
        return True

    @property
    def disabled_reason(self) -> str:
        return "翻译已关闭"


# ---------------------------------------------------------------- LLM


SYSTEM_PROMPT = """你是资深日本直播同传字幕翻译。把主播的日语发言逐句翻成自然、口语化的{target}。

硬性要求：
1. 只输出译文本身：不要解释、不要注音、不要罗马字、不要保留日文原文、不要 markdown、不要引号包裹。
2. 保持口语和语气（吐槽、兴奋、无奈、敬语、关西腔等都要体现），不要翻译成书面新闻腔。
3. 译文长度尽量贴近原文，观众是一边看直播一边扫字幕，宁可短而准，不要展开。
4. 日语句子常省略主语，请根据【前文】判断指的是谁；拿不准时用最自然的说法，不要生硬补主语。
5. 专有名词、人名、组合名一律按【术语表】；术语表没有的，人名/昵称保留原样或音译，不要意译。
6. 语气词（えーと、あの、まあ、なんか）不要逐字翻，用中文口语里等价的停顿或语气处理；无意义时省略。
7. 如果这句话没听清或语义不完整，就按最可能的意思给一个通顺的短句，不要输出「听不清」之类的话。"""

USER_TEMPLATE = """{glossary}{context}【本句】
日：{source}

只输出这一句的{target}译文。"""


class LLMTranslator(Translator):
    name = "llm"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        target_lang: str = "简体中文",
        temperature: float = 0.2,
        max_tokens: int = 400,
        timeout: float = 25.0,
        stream: bool = False,
        retry: int = 1,
        extra_headers: dict[str, str] | None = None,
        glossary: Glossary | None = None,
    ):
        super().__init__(glossary)
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model
        self.target_lang = target_lang
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.stream = stream
        self.retry = max(0, int(retry))
        self.extra_headers = extra_headers or {}
        self.name = f"llm:{model}"
        self._session = None
        self._session_lock = threading.Lock()

    # ---------- 基础设施 ----------
    def _get_session(self):
        with self._session_lock:
            if self._session is None:
                import requests

                self._session = requests.Session()
                self._session.headers.update(
                    {
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.api_key}",
                        "Accept": "text/event-stream" if self.stream else "application/json",
                    }
                )
                self._session.headers.update(self.extra_headers)
            return self._session

    def _endpoint(self) -> str:
        base = self.base_url
        if not base:
            base = "https://api.deepseek.com"
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return base + "/chat/completions"
        return base + "/chat/completions"

    def _messages(self, source: str, context: str, glossary_block: str) -> list[dict[str, str]]:
        g = ""
        if glossary_block:
            g = f"【术语表】（必须遵守）\n{glossary_block}\n\n"
        c = ""
        if context:
            c = f"【前文】（仅供理解语境，不要翻译它们）\n{context}\n\n"
        return [
            {"role": "system", "content": SYSTEM_PROMPT.format(target=self.target_lang)},
            {
                "role": "user",
                "content": USER_TEMPLATE.format(
                    glossary=g, context=c, source=source, target=self.target_lang
                ),
            },
        ]

    # ---------- 主流程 ----------
    def _run(self, source: str, context: str, glossary_block: str) -> str:
        if self.stream:
            buf: list[str] = []
            for delta in self._stream_once(source, context, glossary_block):
                buf.append(delta)
            return "".join(buf)

        last_exc: Exception | None = None
        for attempt in range(self.retry + 1):
            try:
                return self._once(source, context, glossary_block)
            except Exception as exc:
                last_exc = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status in (400, 401, 403, 404):
                    raise
                if attempt < self.retry:
                    time.sleep(0.4 * (attempt + 1))
        raise last_exc if last_exc else RuntimeError("翻译失败")

    def _payload(self, source: str, context: str, glossary_block: str, stream: bool) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": self._messages(source, context, glossary_block),
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": stream,
        }

    def _once(self, source: str, context: str, glossary_block: str) -> str:
        s = self._get_session()
        r = s.post(
            self._endpoint(),
            data=json.dumps(self._payload(source, context, glossary_block, False), ensure_ascii=False).encode("utf-8"),
            timeout=self.timeout,
        )
        if r.status_code >= 400:
            snippet = (r.text or "")[:300]
            raise TranslationHttpError(r.status_code, snippet)
        data = r.json()
        choices = data.get("choices") or []
        if not choices:
            # 有些兼容实现会在被内容过滤时返回空 choices，视作「这句没译出来」而不是报错
            log.debug("响应没有 choices，跳过本句：%s", str(data)[:200])
            return ""
        msg = choices[0].get("message") or {}
        return str(msg.get("content") or "").strip()

    def _stream_once(self, source: str, context: str, glossary_block: str) -> Iterable[str]:
        s = self._get_session()
        payload = self._payload(source, context, glossary_block, True)
        with s.post(
            self._endpoint(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=self.timeout,
            stream=True,
        ) as r:
            if r.status_code >= 400:
                raise TranslationHttpError(r.status_code, (r.text or "")[:300])
            for raw in r.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                line = raw.strip()
                if line.startswith("data:"):
                    line = line[5:].strip()
                if not line or line == "[DONE]":
                    if line == "[DONE]":
                        break
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                for ch in obj.get("choices") or []:
                    delta = ch.get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        yield str(piece)

    def warmup(self) -> None:
        try:
            self._once("こんにちは", "", "")
        except Exception as exc:
            log.debug("LLM 预热失败: %s", exc)

    def describe(self) -> str:
        return f"LLM({self.model} @ {self.base_url})"


# ---------------------------------------------------------------- 本地 NMT


class TransformersNmtTranslator(Translator):
    """MarianMT / OPUS-MT 日语→中文，直接用 transformers + torch 在 CPU 上跑。

    为什么不用 CTranslate2：OPUS-MT 系（尤其社区微调版）的 CT2 转换很容易出问题，
    实测 shun89/opus-mt-ja-zh 转成 CT2 后解码器完全不收敛（输出「红红红红…」这种），
    而同一个模型用 transformers 原样加载质量很好。CPU 上 78M 参数约 130ms/句，
    对字幕来说绰绰有余，而且**不占显存**，不和语音识别抢 GPU。

    目录里需要：
        config.json / *.bin 或 *.safetensors / source.spm / target.spm / vocab.json
    """
    name = "local"

    def __init__(
        self,
        model_dir: str,
        device: str = "cpu",
        num_beams: int = 2,
        max_new_tokens: int = 128,
        num_threads: int = 6,
        no_repeat_ngram_size: int = 4,
        glossary: Glossary | None = None,
    ):
        super().__init__(glossary)
        self.model_dir = Path(model_dir)
        self.device = device if device in ("cpu", "cuda") else "cpu"
        self.num_beams = max(1, num_beams)
        self.max_new_tokens = max_new_tokens
        self.num_threads = max(1, num_threads)
        self.no_repeat_ngram_size = no_repeat_ngram_size
        self._tok = None
        self._mdl = None
        self._torch = None
        self._lock = threading.Lock()
        self.name = f"local:{self.model_dir.name}"

    # ---------- 加载 ----------
    def _resolve_dir(self) -> Path:
        """允许指向「装了多个模型」的 models 目录，自动挑一个可用的。"""
        cands: list[Path] = []
        if (self.model_dir / "config.json").exists():
            cands.append(self.model_dir)
        if self.model_dir.is_dir():
            for sub in sorted(self.model_dir.iterdir()):
                if sub.is_dir() and (sub / "config.json").exists() and (
                    list(sub.glob("*.bin")) or list(sub.glob("*.safetensors"))
                ):
                    cands.append(sub)
        if not cands:
            raise FileNotFoundError(
                f"找不到本地翻译模型：{self.model_dir}\n"
                f"先运行： python -m scripts.setup_local_mt\n"
                f"或者把 config.yaml 里 translate.provider 改成 llm 用在线翻译。"
            )
        return cands[0]

    def ensure_loaded(self) -> None:
        if self._mdl is not None:
            return
        with self._lock:
            if self._mdl is not None:
                return
            import warnings

            # transformers 会为每个 batch 打一遍这些提示，非常吵
            warnings.filterwarnings("ignore", message=r".*max_new_tokens.*max_length.*")
            warnings.filterwarnings("ignore", message=r".*Recommended: pip install sacremoses.*")
            try:
                import logging as _logging

                _logging.getLogger("transformers").setLevel(_logging.ERROR)
                _logging.getLogger("transformers.generation.utils").setLevel(_logging.ERROR)
            except Exception:
                pass
            import torch
            from transformers import MarianMTModel, MarianTokenizer

            d = self._resolve_dir()
            torch.set_num_threads(self.num_threads)
            t0 = __import__("time").time()
            self._tok = MarianTokenizer.from_pretrained(str(d), local_files_only=True)
            self._mdl = MarianMTModel.from_pretrained(str(d), local_files_only=True).eval()
            if self.device == "cuda" and torch.cuda.is_available():
                self._mdl = self._mdl.to("cuda")
            self._torch = torch
            self._resolved = d
            log.info("本地 NMT 就绪：%s（%.2fs，%s）", d.name, __import__("time").time() - t0, self.device)

    def warmup(self) -> None:
        try:
            self.translate_batch(["こんにちは"])
        except Exception as exc:
            log.debug("本地 NMT 预热失败：%s", exc)

    # ---------- 翻译 ----------
    def _run(self, source: str, context: str, glossary_block: str) -> str:
        return self.translate_batch([source])[0]

    def translate_batch(self, sources: list[str]) -> list[str]:
        self.ensure_loaded()
        assert self._tok is not None and self._mdl is not None and self._torch is not None
        torch = self._torch
        out_texts: list[str] = []
        with torch.no_grad():
            batch = self._tok(sources, return_tensors="pt", padding=True)
            if self.device == "cuda":
                batch = {k: v.to("cuda") for k, v in batch.items()}
            gen = self._mdl.generate(
                **batch,
                num_beams=self.num_beams,
                max_new_tokens=self.max_new_tokens,
                no_repeat_ngram_size=self.no_repeat_ngram_size,
            )
        for t in self._tok.batch_decode(gen, skip_special_tokens=True):
            out_texts.append(clean_translation(t, ""))
        return out_texts

    def describe(self) -> str:
        return f"本地NMT(MarianMT, {self.device})"


class LocalTranslator(Translator):
    """CTranslate2 后端（可选）。

    实测对部分 OPUS-MT 微调模型转换后解码会退化，所以默认不用它；
    如果你手上是官方发布的 CT2 模型（比如 Helsinki-NLP 官方 CT2 导出），
    把 config.yaml 的 translate.local.backend 设成 ct2 即可。
    纯 CPU int8，比 transformers 更快，也不占显存。
    """

    name = "local-ct2"

    def __init__(
        self,
        model_dir: str,
        tokenizer_dir: str = "",
        device: str = "cpu",
        compute_type: str = "int8",
        beam_size: int = 2,
        max_batch: int = 8,
        num_workers: int = 1,
        target_prefix: str = "",
        glossary: Glossary | None = None,
    ):
        super().__init__(glossary)
        self.model_dir = Path(model_dir)
        self.tokenizer_dir = Path(tokenizer_dir) if tokenizer_dir else self.model_dir
        self.device = device
        self.compute_type = compute_type
        self.beam_size = beam_size
        self.max_batch = max_batch
        self.num_workers = num_workers
        self.target_prefix = target_prefix
        self._translator = None
        self._tokenizer = None
        self._lock = threading.Lock()
        self.name = f"local:{self.model_dir.name}"

    def ensure_loaded(self) -> None:
        if self._translator is not None:
            return
        with self._lock:
            if self._translator is not None:
                return
            if not self.model_dir.exists():
                raise FileNotFoundError(
                    f"本地翻译模型不存在：{self.model_dir}\n"
                    f"跑 `python -m scripts.setup_local_mt` 下载并转换，或把 config.yaml 的 "
                    f"translate.provider 改成 llm。"
                )
            import ctranslate2
            import sentencepiece as spm

            self._translator = ctranslate2.Translator(
                str(self.model_dir),
                device=self.device,
                compute_type=self.compute_type,
                inter_threads=1,
                intra_threads=max(1, self.num_workers),
            )
            sp_path = self.tokenizer_dir / "source.spm"
            if not sp_path.exists():
                cands = sorted(self.tokenizer_dir.glob("*.spm"))
                # 优先 source.spm / 带 source 字样的
                pref = [c for c in cands if "source" in c.name.lower() or c.name.startswith("source")]
                if pref:
                    sp_path = pref[0]
                elif cands:
                    sp_path = cands[0]
                else:
                    raise FileNotFoundError(
                        f"{self.tokenizer_dir} 下找不到 sentencepiece 模型（source.spm）。"
                        f"重新跑 python -m scripts.setup_local_mt"
                    )
            self._tokenizer = spm.SentencePieceProcessor()
            if not self._tokenizer.load(str(sp_path)):
                raise RuntimeError(f"sentencepiece 模型加载失败：{sp_path}")
            log.info("本地 NMT 就绪：%s (%s/%s)", self.model_dir, self.device, self.compute_type)

    def _run(self, source: str, context: str, glossary_block: str) -> str:
        out = self.translate_batch([source])
        return out[0] if out else ""

    def warmup(self) -> None:
        try:
            self.translate_batch(["こんにちは"])
        except Exception as exc:
            log.debug("CT2 预热失败：%s", exc)

    def translate_batch(self, sources: list[str]) -> list[str]:
        self.ensure_loaded()
        assert self._translator is not None and self._tokenizer is not None
        tokens = [self._tokenizer.encode(s, out_type=str) for s in sources]
        prefix = [self.target_prefix] if self.target_prefix else None
        results = self._translator.translate_batch(
            tokens,
            beam_size=self.beam_size,
            max_batch_size=self.max_batch,
            target_prefix=[prefix] * len(tokens) if prefix else None,
        )
        out: list[str] = []
        for hyp in results:
            toks = hyp.hypotheses[0] if hyp.hypotheses else []
            text = self._tokenizer.decode(toks)
            out.append(clean_translation(text, ""))
        return out

    def describe(self) -> str:
        return f"本地NMT({self.model_dir.name}, {self.device}/{self.compute_type})"


# ---------------------------------------------------------------- 工厂


def create_translator(cfg, logger_status: Callable[[str], None] | None = None) -> Translator:
    """按配置创建翻译器。失败时回退到 NullTranslator，让字幕至少还能显示日文。"""
    from .config import resolve_api_key

    status = logger_status or (lambda _m: None)
    provider = str(cfg.get("translate", "provider", default="llm")).lower()
    glossary = Glossary.load(
        str(cfg.get_path("glossary", "path")),
        enabled=bool(cfg.get("glossary", "enabled", default=True)),
    )

    if provider in ("off", "none", "disable", "disabled"):
        status("翻译已关闭，只显示日文")
        return NullTranslator(glossary)

    if provider == "local":
        backend = str(cfg.get("translate", "local", "backend", default="transformers")).lower()
        # 相对路径按项目根解析，保证换工作目录 / 打包发行版后都能找到
        model_dir = str(cfg.get_path("translate", "local", "model_dir"))
        if backend in ("ct2", "ctranslate2"):
            return LocalTranslator(
                model_dir=model_dir,
                tokenizer_dir=cfg.get("translate", "local", "tokenizer_dir", default=""),
                device=cfg.get("translate", "local", "device", default="cpu"),
                compute_type=cfg.get("translate", "local", "compute_type", default="int8"),
                beam_size=int(cfg.get("translate", "local", "beam_size", default=2)),
                max_batch=int(cfg.get("translate", "local", "max_batch", default=8)),
                num_workers=int(cfg.get("translate", "local", "num_workers", default=1)),
                target_prefix=str(cfg.get("translate", "local", "target_prefix", default="") or ""),
                glossary=glossary,
            )
        return TransformersNmtTranslator(
            model_dir=model_dir,
            device=str(cfg.get("translate", "local", "device", default="cpu")),
            num_beams=int(cfg.get("translate", "local", "beam_size", default=2)),
            max_new_tokens=int(cfg.get("translate", "local", "max_new_tokens", default=128)),
            num_threads=int(cfg.get("translate", "local", "num_workers", default=6)),
            no_repeat_ngram_size=int(cfg.get("translate", "local", "no_repeat_ngram_size", default=4)),
            glossary=glossary,
        )

    # 默认 LLM
    api_key = resolve_api_key(cfg)
    if not api_key:
        status("未配置 API Key，暂时只显示日文（在设置里填 Key 或改用本地翻译）")
        return NullTranslator(glossary)
    return LLMTranslator(
        base_url=str(cfg.get("translate", "llm", "base_url", default="")),
        api_key=api_key,
        model=str(cfg.get("translate", "llm", "model", default="deepseek-chat")),
        target_lang=str(cfg.get("translate", "target_lang", default="简体中文")),
        temperature=float(cfg.get("translate", "llm", "temperature", default=0.2)),
        max_tokens=int(cfg.get("translate", "llm", "max_tokens", default=400)),
        timeout=float(cfg.get("translate", "timeout_s", default=25)),
        stream=bool(cfg.get("translate", "llm", "stream", default=False)),
        retry=int(cfg.get("translate", "llm", "retry", default=1)),
        extra_headers=dict(cfg.get("translate", "llm", "extra_headers", default={}) or {}),
        glossary=glossary,
    )
