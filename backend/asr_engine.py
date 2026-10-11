"""日语 ASR 引擎（faster-whisper）。

针对「直播快速口语」做的关键处理：
  * condition_on_previous_text=False —— 从根上避免 Whisper 的自回归重复幻觉
  * 静音段/低置信段/口癖黑名单 三类过滤
  * 纯拉丁输出（无假名汉字）判为幻觉丢弃
  * partial 用贪心解码 + 短窗，final 用 beam search 保证质量
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .util import (
    ends_with_sentence_punct,
    get_logger,
    has_japanese,
    is_repetitive,
    normalize_ja,
    split_sentences,
)

log = get_logger("jplt.asr")


@dataclass
class AsrResult:
    text: str = ""
    raw: str = ""
    ok: bool = False
    reason: str = ""
    avg_logprob: float = 0.0
    no_speech_prob: float = 0.0
    compression_ratio: float = 0.0
    audio_s: float = 0.0
    compute_s: float = 0.0
    sentences: list[str] = field(default_factory=list)
    segments: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self, **extra: Any) -> dict[str, Any]:
        """给日志/事件用的元信息。

        刻意**不包含 text**：调用方通常已经单独传了 text，
        如果这里再带一个 text，展开成关键字参数时会和位置参数撞名
        （TypeError: got multiple values for argument 'text'）。
        """
        d = {
            "ok": self.ok,
            "avg_logprob": round(self.avg_logprob, 3),
            "no_speech_prob": round(self.no_speech_prob, 3),
            "compression_ratio": round(self.compression_ratio, 2),
            "audio_s": round(self.audio_s, 2),
            "compute_ms": int(self.compute_s * 1000),
        }
        if self.reason:
            d["reason"] = self.reason
        d.update(extra)
        return d


class AsrEngine:
    """懒加载的 Whisper 包装。模型加载一次，之后线程安全复用。"""

    def __init__(
        self,
        model: str = "large-v3-turbo",
        device: str = "cuda",
        compute_type: str = "",
        cpu_compute_type: str = "int8",
        beam_size_final: int = 3,
        no_speech_threshold: float = 0.55,
        log_prob_threshold: float = -1.05,
        compression_ratio_threshold: float = 2.2,
        blocklist: list[str] | None = None,
        initial_prompt: str = "",
        models_root: str | None = None,
        on_status=None,
    ):
        self.model_name = model
        self.device = device or "auto"
        # "auto"：有可用 CUDA 就用 GPU，否则 CPU（发行版默认走这条路）
        if str(self.device).lower() in ("auto", ""):
            self.device = "cuda"
            try:
                from .cuda_setup import prepare_cuda_dlls, preload_torch

                preload_torch()
                rep = prepare_cuda_dlls()
                if not rep.get("ok"):
                    log.info("auto: 没有可用的 CUDA 运行库，选 CPU")
                    self.device = "cpu"
            except Exception:
                self.device = "cpu"
        self.compute_type = compute_type or (cpu_compute_type if self.device == "cpu" else "float16")
        self.beam_size_final = beam_size_final
        self.no_speech_threshold = no_speech_threshold
        self.log_prob_threshold = log_prob_threshold
        self.compression_ratio_threshold = compression_ratio_threshold
        self.blocklist = [b for b in (blocklist or []) if b]
        self.initial_prompt = initial_prompt
        self.models_root = models_root
        self._on_status = on_status or (lambda _m: None)
        self._model = None
        self._lock = threading.Lock()
        self._load_s = 0.0
        # 历史：最近几句，作为 next 的 prompt 让专有名词写法保持一致
        self._recent: list[str] = []
        self.stats = {"calls": 0, "audio_s": 0.0, "compute_s": 0.0, "dropped": 0}

    # ---------- 模型 ----------
    def ensure_loaded(self) -> None:
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return

            # GPU 推理前先把 CUDA 运行库路径准备好，否则 ctranslate2 找不到 cublas/cudnn
            if self.device != "cpu":
                try:
                    from .cuda_setup import prepare_cuda_dlls, preload_torch

                    preload_torch()
                    rep = prepare_cuda_dlls(verbose=True)
                    if not rep.get("ok"):
                        # 明知道不可用就别先加载 GPU 模型了（白等几秒再回退）
                        missing = ", ".join(rep.get("missing", [])[:3]) or "未知"
                        log.warning("CUDA 运行库不可用（缺 %s），直接使用 CPU", missing)
                        self._on_status(
                            "没检测到可用的 CUDA 运行库，用 CPU 识别（会慢一些）。"
                            "想用显卡：装 nvidia-cublas-cu12 nvidia-cudnn-cu12，"
                            "或用带 GPU 支持的发行版。"
                        )
                        self.device = "cpu"
                        self.compute_type = "int8"
                except Exception as exc:
                    log.debug("CUDA 准备失败: %s", exc)

            from faster_whisper import WhisperModel

            t0 = time.time()
            self._on_status(f"加载识别模型 {self.model_name} ({self.device}/{self.compute_type}) …")
            kwargs: dict[str, Any] = {
                "device": self.device,
                "compute_type": self.compute_type,
                "download_root": self.models_root,
            }
            try:
                self._model = WhisperModel(self.model_name, **kwargs)
            except Exception as exc:
                msg = str(exc).lower()
                cuda_problem = any(
                    k in msg for k in ("cublas", "cudnn", "cuda", "not found or cannot be loaded")
                )
                if self.device != "cpu":
                    log.warning("GPU 加载失败(%s)，回退 CPU/int8", exc)
                    if cuda_problem:
                        self._on_status(
                            "GPU 不可用（缺 CUDA 运行库），已切到 CPU。"
                            "装 GPU 支持： pip install nvidia-cublas-cu12 nvidia-cudnn-cu12"
                        )
                    else:
                        self._on_status(f"GPU 加载失败，改用 CPU：{exc}")
                    self.device = "cpu"
                    self.compute_type = "int8"
                    kwargs.update(device="cpu", compute_type="int8")
                    self._model = WhisperModel(self.model_name, **kwargs)
                else:
                    raise
            self._load_s = time.time() - t0
            log.info("模型就绪，用时 %.1fs", self._load_s)
            self._on_status(f"识别模型就绪（{self._load_s:.1f}s）")

            # 关键：模型能加载 ≠ 能推理。
            # 缺 CUDA 运行库时 WhisperModel() 构造会成功，直到第一次 encode()
            # 才抛 "Library cublas64_12.dll is not found"。
            # 如果不在这里试跑一次，用户拿到包后第一句话就崩，
            # 而且是在音频线程里崩，表现是「完全没字幕」。
            self._probe_gpu()

    def _probe_gpu(self) -> None:
        """用一小段静音试推理，确认当前 device 真的可用；不行就回退 CPU。"""
        if self.device == "cpu" or self._model is None:
            return
        import numpy as np

        try:
            list(self._model.transcribe(
                np.zeros(3200, dtype=np.float32),  # 0.2s
                language="ja", beam_size=1, vad_filter=False,
                condition_on_previous_text=False, without_timestamps=True,
            ))
            log.info("GPU 试推理通过（device=%s/%s）", self.device, self.compute_type)
            return
        except Exception as exc:
            msg = str(exc).lower()
            gpu_problem = any(k in msg for k in
                              ("cublas", "cudnn", "cuda", "not found or cannot be loaded",
                               "no kernel image", "out of memory"))
            if not gpu_problem:
                log.debug("试推理失败（看起来不是 GPU 问题）：%s", exc)
                return
            log.warning("GPU 试推理失败(%s)，回退 CPU/int8", exc)

        # 回退 CPU
        try:
            from faster_whisper import WhisperModel

            self.device = "cpu"
            self.compute_type = "int8"
            self._model = WhisperModel(
                self.model_name, device="cpu", compute_type="int8",
                download_root=self.models_root,
            )
            self._on_status(
                "GPU 不可用（缺 CUDA 运行库），已自动切到 CPU 识别。"
                "想用显卡：装 nvidia-cublas-cu12 nvidia-cudnn-cu12，"
                "或用带 GPU 支持的发行版。"
            )
            log.warning("已回退到 CPU/int8 识别")
        except Exception as exc:
            log.error("回退 CPU 也失败：%s", exc)
            self._on_status(f"识别引擎不可用：{exc}")

    @property
    def load_seconds(self) -> float:
        return self._load_s

    # ---------- 识别 ----------
    def transcribe(
        self,
        audio: np.ndarray,
        *,
        beam_size: int = 1,
        partial: bool = False,
        window_s: float | None = None,
        extra_prompt: str = "",
        language: str = "ja",
    ) -> AsrResult:
        if audio is None or audio.size == 0:
            return AsrResult(ok=False, reason="empty")
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        if window_s and audio.size > window_s * 16000:
            audio = audio[-int(window_s * 16000):]

        self.ensure_loaded()
        res = AsrResult(audio_s=audio.size / 16000.0)

        prompt = self.initial_prompt
        if extra_prompt:
            prompt = (prompt + " " + extra_prompt).strip()
        if self._recent:
            tail = "".join(self._recent[-2:])[-180:]
            prompt = (prompt + " " + tail).strip()

        t0 = time.time()
        with self._lock:
            segments, info = self._model.transcribe(
                audio,
                language=language,
                task="transcribe",
                beam_size=max(1, beam_size),
                temperature=0.0 if partial else [0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
                condition_on_previous_text=False,
                initial_prompt=prompt or None,
                vad_filter=False,
                no_speech_threshold=self.no_speech_threshold,
                log_prob_threshold=self.log_prob_threshold,
                compression_ratio_threshold=self.compression_ratio_threshold,
                word_timestamps=False,
                without_timestamps=True,
            )
            seg_list = list(segments)
        res.compute_s = time.time() - t0

        if not seg_list:
            res.reason = "no_segments"
            return res

        texts: list[str] = []
        lps: list[float] = []
        nsps: list[float] = []
        crs: list[float] = []
        for s in seg_list:
            texts.append(s.text or "")
            lps.append(getattr(s, "avg_logprob", 0.0) or 0.0)
            nsps.append(getattr(s, "no_speech_prob", 0.0) or 0.0)
            crs.append(getattr(s, "compression_ratio", 0.0) or 0.0)
            res.segments.append(
                {
                    "start": round(getattr(s, "start", 0.0) or 0.0, 2),
                    "end": round(getattr(s, "end", 0.0) or 0.0, 2),
                    "text": normalize_ja(s.text or ""),
                    "no_speech_prob": round(getattr(s, "no_speech_prob", 0.0) or 0.0, 3),
                    "avg_logprob": round(getattr(s, "avg_logprob", 0.0) or 0.0, 3),
                }
            )

        res.raw = " ".join(t.strip() for t in texts if t and t.strip())
        res.avg_logprob = float(np.mean(lps)) if lps else 0.0
        res.no_speech_prob = float(np.max(nsps)) if nsps else 0.0
        res.compression_ratio = float(np.max(crs)) if crs else 0.0
        res.text = normalize_ja(res.raw)

        ok, reason = self._accept(res, partial=partial)
        res.ok = ok
        res.reason = reason
        if ok:
            res.sentences = split_sentences(res.text)
            self.stats["calls"] += 1
            self.stats["audio_s"] += res.audio_s
            self.stats["compute_s"] += res.compute_s
        else:
            self.stats["dropped"] += 1
        return res

    def _accept(self, res: AsrResult, partial: bool) -> tuple[bool, str]:
        t = res.text
        if not t:
            return False, "empty_text"
        if not has_japanese(t):
            return False, "no_japanese"
        if is_repetitive(t):
            return False, "repetitive"
        for b in self.blocklist:
            if b and b in t and len(t) <= len(b) + 8:
                return False, f"blocklist:{b}"
        # 短音频 + 高静音概率 = 典型的静音幻觉
        if res.no_speech_prob >= self.no_speech_threshold and res.audio_s < 2.5:
            return False, "no_speech"
        if res.avg_logprob < self.log_prob_threshold:
            return False, "low_logprob"
        if res.compression_ratio > self.compression_ratio_threshold * 1.6:
            return False, "compression"
        # partial 阶段放宽一点，避免字幕闪烁
        if not partial and len(t) < 2 and res.audio_s > 1.0:
            return False, "too_short"
        return True, ""

    def remember(self, text: str) -> None:
        """把确认过的句子记下来，供后续 prompt 保持写法一致。"""
        if text:
            self._recent.append(text)
            if len(self._recent) > 4:
                self._recent.pop(0)

    def warmup(self) -> float:
        """跑一次空音频，把 CUDA kernel 预热，避免第一句话延迟特别高。"""
        t0 = time.time()
        try:
            self.transcribe(np.zeros(16000, dtype=np.float32), beam_size=1, partial=True)
        except Exception as exc:
            log.debug("warmup 失败: %s", exc)
        return time.time() - t0

    # ---------- 增量合并（partial 用） ----------
    @staticmethod
    def merge_partial(committed: str, new_text: str) -> str:
        from .util import merge_committed

        return merge_committed(committed, new_text)

    @staticmethod
    def looks_complete(text: str) -> bool:
        return ends_with_sentence_punct(text) or text.endswith("」") or text.endswith("』")
