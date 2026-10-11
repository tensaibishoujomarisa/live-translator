"""主流水线：采集 → VAD 切句 → 识别 → 翻译 → 字幕事件。

线程模型（都是 daemon 线程，主线程只负责收信号）：
  audio    : WASAPI 回调 → ring buffer → VAD 分段
  asr      : 段队列 → faster-whisper final（可选 partial 推测）
  mt       : 句子队列 → 翻译后端 → 译文事件
  hb       : 心跳/统计

关键实时性设计：
  * partial 只对「当前正在讲的这句」做，按固定间隔重算，让字幕边讲边长
  * final 一到就立刻送翻译，不等整段结束
  * 翻译线程与识别线程分离，长句翻译不会卡住后面的识别
"""
from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .asr_engine import AsrEngine, AsrResult
from .audio_in import AudioCapture
from .bridge import Bridge
from .config import Config
from .mt_context import RollingContext, needs_translation
from .translator import TranslationDisabled, Translator, create_translator
from .util import get_logger, split_sentences
from .vad import Segmenter, create_vad

log = get_logger("jplt.pipeline")


class Pipeline:
    # 重复定稿的判定窗口（秒）：这段时间内「文本 + 音频时长」都一样就判为重复
    DUP_WINDOW_S = 2.5

    def __init__(self, cfg: Config, bridge: Bridge | None = None):
        self.cfg = cfg
        self.bridge = bridge or Bridge(mode="text")
        self._stop = threading.Event()
        self._seg_q: "queue.Queue[dict]" = queue.Queue(maxsize=64)
        self._mt_q: "queue.Queue[dict]" = queue.Queue(maxsize=256)

        self.segmenter: Segmenter | None = None
        self.capture: AudioCapture | None = None
        self.asr: AsrEngine | None = None
        self.translator: Translator | None = None

        self._partial_lock = threading.Lock()
        self._partial_stop = threading.Event()
        self._last_partial_at = 0.0
        self._last_partial_text = ""
        self._working_audio: np.ndarray | None = None
        self._working_id = 0
        self._working_started = 0.0

        self.context = RollingContext(
            max_pairs=int(cfg.get("translate", "context_sentences", default=4))
        )
        self.start_time = time.time()
        self.counters = {
            "segments": 0,
            "finals": 0,
            "translations": 0,
            "partials": 0,
            "dropped": 0,
            "errors": 0,
        }
        self._last_progress = time.time()
        self.paused = False
        self._base_gain = float(cfg.get("audio", "gain", default=1.0))
        # 同一段识别结果的多句译文累积区：{seg_id: [译文1, 译文2, ...]}
        self._seg_mt: dict[int, list[str]] = {}
        # 最近一条定稿的指纹 (文本, 时长档位) 与时间，用于丢弃重复
        self._last_final: tuple[tuple[str, int], float] | None = None

    # ---------------------------------------------------------------- 启动
    def build(self) -> None:
        c = self.cfg
        models_dir = Path(c.root) / "models"

        self.bridge.status("init", "初始化语音活动检测…")
        vad = create_vad(
            str(c.get("vad", "backend", default="") or ""),
            sample_rate=int(c.get("audio", "sample_rate", default=16000)),
            models_dir=models_dir,
        )
        # 退化成 energy 是明显的质量损失，主动提示一次（别等用户看日志才发现）
        if getattr(vad, "name", "") == "energy":
            self.bridge.warn(
                "语音断句在用兜底方案（energy），快速口语下会切得比较碎。"
                "修复：安装 silero-vad（pip install silero-vad）或跑 scripts/setup_vad.py"
            )
        self.segmenter = Segmenter(
            vad,
            sample_rate=int(c.get("audio", "sample_rate", default=16000)),
            threshold=float(c.get("vad", "threshold", default=0.5)),
            min_speech_ms=int(c.get("vad", "min_speech_ms", default=320)),
            min_silence_ms=int(c.get("vad", "min_silence_ms", default=380)),
            max_speech_s=float(c.get("vad", "max_speech_s", default=11.0)),
            pad_ms=int(c.get("vad", "pad_ms", default=120)),
            min_rms=float(c.get("vad", "min_rms", default=0.0025)),
        )

        self.bridge.status("init", "初始化识别引擎…")
        self.asr = AsrEngine(
            model=str(c.get("asr", "model", default="large-v3-turbo")),
            device=str(c.get("asr", "device", default="cuda")),
            compute_type=str(c.get("asr", "compute_type", default="")),
            cpu_compute_type=str(c.get("asr", "cpu_compute_type", default="int8")),
            beam_size_final=int(c.get("asr", "beam_size_final", default=3)),
            no_speech_threshold=float(c.get("asr", "no_speech_threshold", default=0.55)),
            log_prob_threshold=float(c.get("asr", "log_prob_threshold", default=-1.05)),
            compression_ratio_threshold=float(c.get("asr", "compression_ratio_threshold", default=2.2)),
            blocklist=list(c.get("asr", "hallucination_blocklist", default=[]) or []),
            initial_prompt=str(c.get("asr", "initial_prompt", default="")),
            models_root=str(models_dir),
            on_status=lambda m: self.bridge.status("asr", m),
        )

        self.bridge.status("init", "初始化翻译后端…")
        self.translator = create_translator(c, logger_status=lambda m: self.bridge.status("mt", m))
        self.bridge.status("init", f"翻译后端：{self.translator.describe()}")

    def start(self) -> None:
        if self.segmenter is None:
            self.build()
        if self.asr is not None:
            self.asr.ensure_loaded()
            w = self.asr.warmup()
            log.info("识别预热 %.2fs", w)
        # 翻译后端也预热：本地模型懒加载要好几秒，不预热的话第一句字幕会明显延迟
        if self.translator is not None:
            t0 = time.time()
            try:
                self.translator.warmup()
                log.info("翻译预热 %.2fs", time.time() - t0)
            except Exception as exc:
                log.debug("翻译预热失败：%s", exc)

        self.capture = AudioCapture(
            device=self.cfg.get("audio", "device", default=""),
            target_rate=int(self.cfg.get("audio", "sample_rate", default=16000)),
            gain=float(self.cfg.get("audio", "gain", default=1.0)),
            on_chunk=self._on_audio,
            on_status=lambda m: self.bridge.status("audio", m),
        )
        info = self.capture.start()
        # 采集线程里才会解析出真正的设备，稍等一下再读，让 ready 事件带上设备信息
        for _ in range(20):
            if self.capture._device_info:
                break
            time.sleep(0.05)
        self.bridge.ready(
            asr=self.asr.model_name if self.asr else "",
            device=self.asr.device if self.asr else "",
            translator=self.translator.describe() if self.translator else "",
            audio=self.capture._device_info or info.get("device", {}),
        )

        threading.Thread(target=self._asr_worker, name="asr-worker", daemon=True).start()
        threading.Thread(target=self._mt_worker, name="mt-worker", daemon=True).start()
        threading.Thread(target=self._level_worker, name="level-worker", daemon=True).start()
        threading.Thread(target=self._partial_worker, name="partial-worker", daemon=True).start()

        self.bridge.status("running", "正在监听系统声音，开始看直播吧")

    def stop(self) -> None:
        self._stop.set()
        self._partial_stop.set()
        if self.capture:
            self.capture.stop()
        if self.segmenter:
            seg = self.segmenter.flush()
            if seg:
                self._enqueue_segment(seg)
        # 给队列一点时间收尾
        deadline = time.time() + 6.0
        while time.time() < deadline:
            if self._seg_q.empty() and self._mt_q.empty():
                break
            time.sleep(0.15)

    def run_forever(self, on_command: Callable[[str], None] | None = None) -> None:
        """主线程：可选地从 stdin 读命令（GUI 用得到暂停/切换）。"""
        self.start()
        if on_command is None:
            while not self._stop.is_set():
                time.sleep(0.3)
            return
        import sys

        for line in sys.stdin:
            cmd = (line or "").strip()
            if not cmd:
                continue
            if cmd in ("quit", "exit", "q"):
                break
            try:
                on_command(cmd)
            except Exception as exc:
                log.warning("命令 %s 处理失败: %s", cmd, exc)
        self.stop()

    # ---------------------------------------------------------------- 音频
    def _on_audio(self, chunk: np.ndarray) -> None:
        if self._stop.is_set() or self.segmenter is None:
            return
        if self.paused:
            return
        try:
            segments = self.segmenter.push(chunk)
        except Exception as exc:
            log.exception("VAD 处理异常: %s", exc)
            return
        for seg in segments:
            self._enqueue_segment(seg)
        # 更新「当前正在讲的句子」缓冲，供 partial 使用
        with self._partial_lock:
            if self.segmenter.in_speech:
                if self._working_audio is None:
                    self._working_id = self.segmenter.current_id() + 1
                    self._working_started = time.time()
                self._working_audio = self.segmenter.current_audio()
            else:
                self._working_audio = None
                self._working_id = 0

    def _enqueue_segment(self, seg: dict) -> None:
        self.counters["segments"] += 1
        try:
            self._seg_q.put_nowait(seg)
            self._last_progress = time.time()
        except queue.Full:
            self.counters["dropped"] += 1
            self.bridge.warn("识别队列已满，丢弃一段音频（说明识别跟不上语速）")

    # ---------------------------------------------------------------- 识别线程
    def _asr_worker(self) -> None:
        assert self.asr is not None
        while not self._stop.is_set():
            try:
                seg = self._seg_q.get(timeout=0.3)
            except queue.Empty:
                continue
            if seg is None:
                continue
            try:
                res = self.asr.transcribe(
                    seg["audio"],
                    beam_size=int(self.cfg.get("asr", "beam_size_final", default=3)),
                    partial=False,
                    language=str(self.cfg.get("asr", "language", default="ja")),
                )
            except Exception as exc:
                self.counters["errors"] += 1
                self.bridge.error("asr", f"{type(exc).__name__}: {exc}")
                log.exception("识别失败")
                continue

            seg_id = int(seg["id"])
            if not res.ok:
                log.debug("丢弃片段 %s: %s (%.1fs)", seg_id, res.reason, res.audio_s)
                self.bridge.emit(
                    "asr_drop",
                    id=seg_id,
                    reason=res.reason,
                    audio_s=round(res.audio_s, 2),
                )
                continue

            # 重复定稿防护：同一段音频如果因为采集/分段的边界情况被送进来两次，
            # 屏幕上就会同一句话连着出两条（日文中文各自重复一遍，很显眼）。
            # 判据用「文本相同 + 音频时长几乎一致 + 时间相邻」：
            # 主播真的把同一句话说了两遍时，两次的音频时长不可能精确到毫秒级相同。
            key = (res.text, int(round(res.audio_s * 1000 / 50.0)))
            now = time.time()
            if self._last_final and self._last_final[0] == key and \
                    now - self._last_final[1] < self.DUP_WINDOW_S:
                self.counters["dup_dropped"] = self.counters.get("dup_dropped", 0) + 1
                log.info("丢弃重复定稿（%dms 内内容与时长都相同）：%s",
                         int((now - self._last_final[1]) * 1000), res.text[:40])
                self.bridge.emit("asr_dup", id=seg_id, text=res.text)
                continue
            self._last_final = (key, now)

            self.counters["finals"] += 1
            self._last_progress = time.time()
            self.asr.remember(res.text)
            rtf = res.compute_s / max(res.audio_s, 1e-6)
            payload = res.as_dict(rtf=round(rtf, 3))
            # 双保险：万一以后有人往 as_dict 里加回 text，这里也不会炸
            payload.pop("text", None)
            self.bridge.final(
                seg_id,
                res.text,
                duration_ms=seg.get("duration_ms"),
                speech_ms=seg.get("speech_ms"),
                rms=round(seg.get("rms", 0.0), 4),
                **payload,
            )
            log.info("日 %s", res.text)

            for s in (res.sentences or split_sentences(res.text)):
                if needs_translation(s):
                    try:
                        self._mt_q.put_nowait({"id": seg_id, "text": s})
                    except queue.Full:
                        self.bridge.warn("翻译队列已满，丢弃一句")
            # 让翻译线程知道这段一共有几句，翻完最后一句好清掉累积缓存
            try:
                self._mt_q.put_nowait({"id": seg_id, "text": "", "last": True,
                                       "total": len([x for x in (res.sentences or split_sentences(res.text))
                                                     if needs_translation(x)])})
            except queue.Full:
                pass
    # ---------------------------------------------------------------- partial 推测
    def _partial_worker(self) -> None:
        interval = float(self.cfg.get("asr", "partial_interval_ms", default=550)) / 1000.0
        min_ms = int(self.cfg.get("asr", "partial_min_ms", default=900))
        win = float(self.cfg.get("asr", "partial_window_s", default=22.0))
        max_s = float(self.cfg.get("asr", "partial_max_s", default=16.0))
        if interval <= 0:
            return

        while not self._partial_stop.is_set():
            time.sleep(0.1)
            if self._stop.is_set():
                break
            now = time.time()
            if now - self._last_partial_at < interval:
                continue
            # 识别队列积压时不抢 GPU
            if self._seg_q.qsize() > 0:
                continue
            with self._partial_lock:
                audio = self._working_audio
                seg_id = self._working_id
            if audio is None or audio.size < int(16000 * min_ms / 1000):
                continue
            dur = audio.size / 16000.0
            if dur > max_s:
                continue
            self._last_partial_at = now
            try:
                assert self.asr is not None
                res = self.asr.transcribe(audio, beam_size=1, partial=True, window_s=win)
            except Exception as exc:
                log.debug("partial 失败: %s", exc)
                continue
            if not res.ok or not res.text:
                continue
            if res.text == self._last_partial_text:
                continue
            self._last_partial_text = res.text
            self.counters["partials"] += 1
            self.bridge.partial(seg_id or 0, res.text, compute_ms=int(res.compute_s * 1000))

    # ---------------------------------------------------------------- 翻译线程
    def _mt_worker(self) -> None:
        assert self.translator is not None
        debounce = float(self.cfg.get("translate", "debounce_ms", default=260)) / 1000.0
        pending: list[dict] = []
        last_add = 0.0
        announced_disabled = False

        while not self._stop.is_set() or not self._mt_q.empty():
            timeout = 0.1 if pending else 0.3
            try:
                item = self._mt_q.get(timeout=timeout)
                pending.append(item)
                last_add = time.time()
            except queue.Empty:
                pass

            # 熔断后就别再堆了：把队列排空，只提示一次
            if self.translator.disabled:
                if pending or not self._mt_q.empty():
                    drained = len(pending)
                    pending = []
                    while True:
                        try:
                            self._mt_q.get_nowait()
                            drained += 1
                        except queue.Empty:
                            break
                    if drained:
                        log.debug("翻译熔断中，丢弃 %d 句待译文本", drained)
                if not announced_disabled:
                    announced_disabled = True
                    self.bridge.warn(
                        f"翻译已停用：{self.translator.disabled_reason}"
                    )
                continue
            announced_disabled = False

            if not pending:
                continue
            ready = (time.time() - last_add) >= debounce
            # 队列空了就立刻翻，不必等 debounce
            if self._mt_q.empty():
                ready = True
            if not ready:
                continue

            batch, pending = pending, []
            for it in batch:
                self._translate_one(it)

    def _translate_one(self, item: dict) -> None:
        assert self.translator is not None
        if self.translator.disabled:
            return
        seg_id = int(item.get("id") or 0)

        # 收尾标记：这段翻完了，丢掉累积缓存
        if item.get("last") and not item.get("text"):
            self._seg_mt.pop(seg_id, None)
            return

        src = item["text"]
        t0 = time.time()
        try:
            out = self.translator.translate(src, context_block=self.context.as_text())
        except TranslationDisabled:
            return
        except Exception as exc:
            self.counters["errors"] += 1
            # 熔断的那一刻已经把原因说清楚了，不用再刷一条
            if self.translator.disabled:
                self.bridge.warn(f"翻译已停用：{self.translator.disabled_reason}")
            else:
                self.bridge.error("mt", f"{type(exc).__name__}: {exc}")
            return
        if not out:
            return

        self.counters["translations"] += 1
        self._last_progress = time.time()
        self.context.add(src, out)

        # 一段识别结果会被切成好几句分别送翻译（这样能更早出字幕）。
        # 这里把同一段的多句译文累积起来一起发出去，
        # 否则每句都带同一个 id、后面会把前面的覆盖掉 —— 观众只能看到最后一句。
        parts = self._seg_mt.setdefault(seg_id, [])
        parts.append(out)
        full = "".join(parts)
        self.bridge.translation(
            seg_id,
            full,
            source=src,
            part=len(parts),
            latency_ms=int((time.time() - t0) * 1000),
        )
        log.info("中 %s%s", full if len(parts) > 1 else out,
                 f"  (第{len(parts)}句)" if len(parts) > 1 else "")

    # ---------------------------------------------------------------- 电平心跳
    def _level_worker(self) -> None:
        every = float(self.cfg.get("audio", "level_interval_ms", default=250)) / 1000.0
        last_stats = time.time()
        while not self._stop.is_set():
            time.sleep(max(0.05, every))
            if self.capture is None:
                continue
            try:
                lvl = self.capture.last_level
                speaking = bool(self.segmenter.in_speech) if self.segmenter else False
                self.bridge.level(lvl, lvl, speaking)
            except Exception:
                pass
            if time.time() - last_stats >= 5.0:
                last_stats = time.time()
                self.bridge.stats(
                    uptime_s=int(time.time() - self.start_time),
                    **self.counters,
                    asr_queue=self._seg_q.qsize(),
                    mt_queue=self._mt_q.qsize(),
                    asr_stats={
                        k: (round(v, 2) if isinstance(v, float) else v)
                        for k, v in (self.asr.stats if self.asr else {}).items()
                    },
                )

    # ---------------------------------------------------------------- 运行时控制
    def set_paused(self, paused: bool) -> bool:
        """暂停 = 停止识别（音频仍被采集但直接丢弃），用于接电话/走开。"""
        self.paused = bool(paused)
        if self.paused and self.segmenter is not None:
            seg = self.segmenter.flush()
            if seg:
                self._enqueue_segment(seg)
        self.bridge.status("paused" if self.paused else "running",
                           "已暂停识别" if self.paused else "已恢复识别")
        return self.paused

    def set_translator_provider(self, provider: str) -> str:
        """热切换翻译后端（GUI 设置里用）。"""
        self.cfg = self.cfg.with_overrides({"translate": {"provider": provider}})
        self.translator = create_translator(
            self.cfg, logger_status=lambda m: self.bridge.status("mt", m)
        )
        desc = self.translator.describe()
        self.bridge.status("mt", f"翻译后端已切换：{desc}")
        return desc

    def update_tuning(self, overrides: dict) -> None:
        """调整运行时参数（VAD/音频/上下文），尽量立即生效不用重启。"""
        self.cfg = self.cfg.with_overrides(overrides)
        if self.segmenter is not None:
            v = overrides.get("vad", {})
            if "threshold" in v:
                self.segmenter.threshold = float(v["threshold"])
            if "min_silence_ms" in v:
                self.segmenter.min_silence = int(
                    self.segmenter.sr * float(v["min_silence_ms"]) / 1000
                )
            if "max_speech_s" in v:
                self.segmenter.max_speech = int(
                    self.segmenter.sr * float(v["max_speech_s"])
                )
            if "pad_ms" in v:
                self.segmenter.pad = int(self.segmenter.sr * float(v["pad_ms"]) / 1000)
            if "min_rms" in v:
                self.segmenter.min_rms = float(v["min_rms"])
        if self.capture is not None and "audio" in overrides:
            g = overrides["audio"].get("gain")
            if g is not None:
                self.capture.gain = float(g)
        # 上下文句数：直接换掉滚动窗口，不用重启
        if "translate" in overrides and "context_sentences" in overrides["translate"]:
            n = int(overrides["translate"]["context_sentences"])
            self.context = RollingContext(max_pairs=max(0, n))
        self.bridge.status("tuning", "参数已更新")
