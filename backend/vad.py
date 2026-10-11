"""语音活动检测。三种后端按可用性自动选择：

1. onnx  —— Silero VAD 的 ONNX 模型，CPU 上极快（首选）
2. torch —— torch.hub 的 Silero VAD（已在本地缓存时可用，可走 GPU）
3. energy—— 纯能量+零穿越率，零依赖兜底

对外只暴露 probabilities(frame) 和 Segmenter（把概率流切成一句话）。
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import numpy as np

from .util import get_logger, rms

log = get_logger("jplt.vad")

FRAME = 512  # silero 固定 512 采样（16k 下 32ms）
FRAME_MS = 32


class VadBackend(ABC):
    name = "base"
    frame = FRAME

    @abstractmethod
    def probabilities(self, frame: np.ndarray) -> float:
        """输入 frame 个采样点的 float32，返回语音概率 0~1。"""

    def reset(self) -> None:
        pass


# ---------------------------------------------------------------- onnx


class OnnxSileroVad(VadBackend):
    """Silero VAD 的 ONNX 推理。

    关键细节：Silero 要求每个 512 采样的帧前面拼接「上一帧末尾 64 个采样」当上下文，
    模型实际吃 576 个采样点。不做这个拼接的话输出会恒等于 ~0.001，
    VAD 等于完全失效（表现为无论怎么说话都分不出一段）。
    参考 silero-vad 官方 OnnxWrapper 的实现。
    """

    name = "onnx"
    ctx_size = 64

    def __init__(self, model_path: Path, sample_rate: int = 16000, intra_threads: int = 1):
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = max(1, intra_threads)
        opts.log_severity_level = 3
        self.sess = ort.InferenceSession(
            str(model_path), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self.path = model_path
        self.sample_rate = sample_rate
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._ctx = np.zeros(self.ctx_size, dtype=np.float32)
        self._sr = np.array([sample_rate], dtype=np.int64)
        self._lock = threading.Lock()
        inames = {i.name for i in self.sess.get_inputs()}
        self._has_state = "state" in inames
        self._has_sr = "sr" in inames
        if not self._has_state:
            log.warning("VAD 模型 %s 没有 state 输入，可能不是 Silero VAD", model_path.name)

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._ctx = np.zeros(self.ctx_size, dtype=np.float32)

    def probabilities(self, frame: np.ndarray) -> float:
        chunk = np.asarray(frame, dtype=np.float32).reshape(-1)
        if chunk.size == 0:
            return 0.0
        # 拼上下文
        inp = np.concatenate([self._ctx, chunk])
        feeds: dict[str, Any] = {"input": inp.reshape(1, -1)}
        if self._has_sr:
            feeds["sr"] = self._sr
        if self._has_state:
            feeds["state"] = self._state
        with self._lock:
            out = self.sess.run(None, feeds)
        prob = float(np.asarray(out[0]).reshape(-1)[0])
        if self._has_state and len(out) > 1:
            self._state = np.asarray(out[1], dtype=np.float32)
        # 更新上下文：保留本次输入的最后 64 个采样
        if inp.size >= self.ctx_size:
            self._ctx = inp[-self.ctx_size:].copy()
        else:
            self._ctx = np.concatenate([self._ctx, inp])[-self.ctx_size:]
        return prob


# ---------------------------------------------------------------- torch


class TorchSileroVad(VadBackend):
    name = "torch"

    def __init__(self, sample_rate: int = 16000, device: str = "cpu"):
        import torch

        self.torch = torch
        self.device = torch.device(device)
        self.sample_rate = sample_rate
        hub = Path(__import__("os").environ.get("TORCH_HOME", ".")) / "hub"
        torch.hub.set_dir(str(hub))
        self.model, _utils = torch.hub.load(
            repo_or_dir="snakers4/silero-vad",
            model="silero_vad",
            force_reload=False,
            onnx=False,
            trust_repo=True,
        )
        self.model = self.model.to(self.device).eval()

    def reset(self) -> None:
        try:
            self.model.reset_states()
        except Exception:
            pass

    def probabilities(self, frame: np.ndarray) -> float:
        t = self.torch.from_numpy(frame.astype(np.float32)).to(self.device)
        with self.torch.no_grad():
            p = self.model(t, self.sample_rate)
        return float(p.detach().cpu().reshape(-1)[0])


# ---------------------------------------------------------------- energy


class EnergyVad(VadBackend):
    """零依赖兜底：自适应底噪 + 过零率过滤。

    直播环境里没有模型时至少能用；代价是安静段落会被切成碎片。
    """

    name = "energy"

    def __init__(self, sample_rate: int = 16000, floor_rms: float = 0.006):
        self.sample_rate = sample_rate
        self.floor = float(floor_rms)
        self._noise = float(floor_rms)
        self._hist: list[float] = []

    def reset(self) -> None:
        self._hist.clear()

    def probabilities(self, frame: np.ndarray) -> float:
        r = rms(frame)
        # 底噪自适应：取最近帧能量的低分位数
        self._hist.append(r)
        if len(self._hist) > 100:
            self._hist.pop(0)
        if len(self._hist) >= 20:
            self._noise = float(np.percentile(self._hist, 15))
        thresh = max(self.floor, self._noise * 2.6)
        zcr = float(np.mean(np.abs(np.diff(np.sign(frame))) > 0)) if frame.size > 1 else 0.0
        if r < thresh:
            return 0.0
        # 语音的过零率通常低于摩擦音/噪声
        scaled = min(1.0, (r - thresh) / max(thresh, 1e-6))
        penalty = 0.5 if zcr > 0.35 else 1.0
        return float(min(1.0, 0.45 + 0.55 * scaled) * penalty)


# ---------------------------------------------------------------- 工厂


def create_vad(kind: str = "", sample_rate: int = 16000, models_dir: Path | None = None) -> VadBackend:
    """按 backend 配置创建 VAD。kind 为空则自动探测。"""
    order = [kind] if kind else ["onnx", "torch", "energy"]
    errors: list[str] = []
    for name in order:
        try:
            if name == "onnx":
                path = _find_onnx_model(models_dir)
                if not path:
                    raise FileNotFoundError("找不到 silero_vad.onnx")
                log.info("VAD: onnx (%s)", path.name)
                return OnnxSileroVad(path, sample_rate=sample_rate)
            if name == "torch":
                dev = "cpu"
                try:
                    import torch

                    if torch.cuda.is_available():
                        dev = "cuda"
                except Exception:
                    pass
                log.info("VAD: torch silero (device=%s)", dev)
                return TorchSileroVad(sample_rate=sample_rate, device=dev)
            if name == "energy":
                log.info("VAD: energy 兜底")
                return EnergyVad(sample_rate=sample_rate)
            raise ValueError(f"未知的 VAD 后端 {name}")
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            log.debug("VAD %s 不可用: %s", name, exc)
    log.warning("所有指定 VAD 都不可用，使用 energy 兜底。详情: %s", " | ".join(errors))
    return EnergyVad(sample_rate=sample_rate)


def _find_onnx_model(models_dir: Path | None) -> Path | None:
    cands = []
    if models_dir:
        cands += [
            models_dir / "silero_vad.onnx",
            models_dir / "silero-vad" / "silero_vad.onnx",
            models_dir / "vad" / "silero_vad.onnx",
            models_dir / "silero_vad" / "silero_vad.onnx",
        ]
    # pip 安装的 silero-vad 包会带模型
    try:
        import silero_vad  # type: ignore

        base = Path(silero_vad.__file__).parent
        cands += [
            base / "data" / "silero_vad.onnx",
            base / "silero_vad.onnx",
        ]
        cands += sorted(base.rglob("silero_vad*.onnx"))
    except Exception:
        pass
    # torch.hub 缓存
    try:
        import os as _os

        hub = Path(_os.environ.get("TORCH_HOME", "")) / "hub"
        cands += sorted(hub.rglob("silero_vad*.onnx"))
    except Exception:
        pass
    for c in cands:
        if c and Path(c).exists():
            return Path(c)
    return None


# ---------------------------------------------------------------- 分段器


class Segmenter:
    """把连续的音频小块变成「一句话」。

    状态机：
      idle  --prob>=th--> speech（带 pre-roll，避免吃掉首音）
      speech --静音达到 min_silence--> 结束，产出片段
      speech --时长超过 max_speech--> 强制在最近的低能量处切断（长句也要出字幕）
    """

    def __init__(
        self,
        vad: VadBackend,
        sample_rate: int = 16000,
        threshold: float = 0.5,
        min_speech_ms: int = 320,
        min_silence_ms: int = 380,
        max_speech_s: float = 11.0,
        pad_ms: int = 120,
        min_rms: float = 0.0025,
    ):
        self.vad = vad
        self.sr = sample_rate
        self.threshold = threshold
        self.min_speech = int(sample_rate * min_speech_ms / 1000)
        self.min_silence = int(sample_rate * min_silence_ms / 1000)
        self.max_speech = int(sample_rate * max_speech_s)
        self.pad = int(sample_rate * pad_ms / 1000)
        self.min_rms = min_rms

        self._pre: list[np.ndarray] = []
        self._pre_len = 0
        self._pre_cap = int(sample_rate * 0.30)
        self._acc: list[np.ndarray] = []
        self._acc_len = 0
        self._speech_len = 0
        self._silence_len = 0
        self._in_speech = False
        self._frame_tail = np.zeros(0, dtype=np.float32)
        self._seg_id = 0
        self._last_stats = {"prob": 0.0, "rms": 0.0, "speaking": False}

    # ---------- 主入口 ----------
    def push(self, chunk: np.ndarray) -> list[dict[str, Any]]:
        """喂入任意长度音频，返回本次产出的完整句子列表。"""
        out: list[dict[str, Any]] = []
        buf = np.concatenate([self._frame_tail, chunk]) if self._frame_tail.size else chunk
        n_frames = buf.size // FRAME
        for i in range(n_frames):
            frame = buf[i * FRAME:(i + 1) * FRAME]
            seg = self._push_frame(frame)
            if seg is not None:
                out.append(seg)
        self._frame_tail = buf[n_frames * FRAME:].copy()
        return out

    def flush(self) -> dict[str, Any] | None:
        """外部要求立刻结束当前句（例如停止采集/暂停瞬间）。"""
        if self._in_speech and self._acc_len > 0:
            return self._finish(reason="flush")
        self._reset_utt()
        return None

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    @property
    def stats(self) -> dict[str, Any]:
        return dict(self._last_stats)

    @property
    def current_len(self) -> int:
        return self._acc_len + self._speech_len

    def current_audio(self) -> np.ndarray | None:
        """当前未结束句子的音频（用于 partial 识别）。"""
        if not self._acc:
            return None
        return np.concatenate(self._acc)

    def current_id(self) -> int:
        return self._seg_id

    # ---------- 内部 ----------
    def _push_frame(self, frame: np.ndarray) -> dict[str, Any] | None:
        prob = float(self.vad.probabilities(frame))
        level = rms(frame)
        self._last_stats = {"prob": round(prob, 3), "rms": round(level, 5), "speaking": self._in_speech}

        if not self._in_speech:
            self._pre.append(frame.copy())
            self._pre_len += frame.size
            while self._pre_len - self._pre[0].size >= self._pre_cap and len(self._pre) > 1:
                self._pre_len -= self._pre.pop(0).size
            if prob >= self.threshold:
                self._in_speech = True
                self._speech_len = self._pre_len
                self._acc = list(self._pre)
                self._acc_len = self._pre_len
                self._silence_len = 0
                self._pre = []
                self._pre_len = 0
            return None

        # 讲话中
        self._acc.append(frame.copy())
        self._acc_len += frame.size
        if prob >= self.threshold:
            self._speech_len += frame.size
            self._silence_len = 0
        else:
            self._silence_len += frame.size

        if self._silence_len >= self.min_silence:
            preview = np.concatenate(self._acc) if self._acc else np.zeros(0, np.float32)
            if rms(preview) < self.min_rms:
                self._reset_utt()  # 全是底噪，丢掉
                return None
            return self._finish(reason="silence")

        if self._acc_len >= self.max_speech:
            return self._finish(reason="maxlen")
        return None

    def _finish(self, reason: str) -> dict[str, Any] | None:
        audio = np.concatenate(self._acc) if self._acc else np.zeros(0, dtype=np.float32)
        speech_len = self._speech_len
        self._reset_utt()
        if audio.size == 0:
            return None
        if speech_len < self.min_speech:
            log.debug("丢弃过短片段 (%d < %d)", speech_len, self.min_speech)
            return None

        # 去掉尾巴上多余的静音，但保留 pad
        if reason == "silence":
            keep = max(0, audio.size - max(0, self._silence_len - self.pad))
            if keep > 0:
                audio = audio[:keep]
        if audio.size < self.sr // 8:
            return None
        # 首尾各加 pad（不可能超出，已经是采集到的最长范围）
        self._seg_id += 1
        return {
            "id": self._seg_id,
            "audio": np.ascontiguousarray(audio, dtype=np.float32),
            "duration_ms": int(audio.size * 1000 / self.sr),
            "speech_ms": int(speech_len * 1000 / self.sr),
            "rms": rms(audio),
            "reason": reason,
        }

    def _reset_utt(self) -> None:
        self._in_speech = False
        self._acc = []
        self._acc_len = 0
        self._speech_len = 0
        self._silence_len = 0
        self._pre = []
        self._pre_len = 0
        try:
            self.vad.reset()
        except Exception:
            pass
