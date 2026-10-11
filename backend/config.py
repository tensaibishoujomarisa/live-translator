"""配置加载：默认值 <- config.yaml <- 环境变量。

设计原则：任何一项都能通过 config.yaml 或环境变量覆盖，方便在 GUI 之外调参。
"""
from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

# 模型缓存统一放到项目所在盘，避免写满 C 盘
for _env, _sub in (
    ("HF_HOME", "models"),
    ("HUGGINGFACE_HUB_CACHE", "models"),
    ("TORCH_HOME", "models\\torch"),
):
    if _env not in os.environ:
        _p = ROOT / _sub
        _p.mkdir(parents=True, exist_ok=True)
        os.environ[_env] = str(_p)

DEFAULTS: dict[str, Any] = {
    "audio": {
        # 设备名关键字；空字符串 = 默认扬声器（WASAPI loopback）
        "device": "",
        "sample_rate": 16000,
        "channels": 1,
        # 采集增益，直播声音偏小可以调大
        "gain": 1.0,
        "level_interval_ms": 250,
        "queue_seconds": 30,
    },
    "vad": {
        # onnx | torch | energy
        "backend": "",
        "threshold": 0.5,
        "min_speech_ms": 320,
        # 静音多久算一句话结束（越小越实时，但会把长句切碎）
        "min_silence_ms": 380,
        # 最长一段，连续讲话时强制切段，避免字幕无限等下去
        "max_speech_s": 11.0,
        # 送到识别器时前后各留一点余量，避免吃掉开头的助词
        "pad_ms": 120,
        # 整段音量低于此值直接丢弃（环境底噪）
        "min_rms": 0.0025,
    },
    "asr": {
        # tiny|base|small|medium|large-v3|large-v3-turbo|distil-large-v3
        "model": "large-v3-turbo",
        "device": "cuda",
        "compute_type": "float16",
        "cpu_compute_type": "int8",
        "beam_size_final": 3,
        "language": "ja",
        # partial 推测：每隔多久重跑一次当前句
        "partial_interval_ms": 550,
        # 小于这个长度的当前句不做 partial（浪费算力）
        "partial_min_ms": 900,
        # 最长送进 partial 的音频长度
        "partial_window_s": 22.0,
        # 超过这个时长就不做 partial 了（太长重算很贵）
        "partial_max_s": 16.0,
        "vad_filter": False,
        "no_speech_threshold": 0.55,
        "log_prob_threshold": -1.05,
        "compression_ratio_threshold": 2.2,
        # 主播固定口癖/观众弹幕造成的幽灵句子
        "hallucination_blocklist": [
            "ご視聴ありがとうございました",
            "ご視聴ありがとうございます",
            "おやすみなさい",
            "チャンネル登録",
            "高評価",
            "ありがとうございました",
            "お疲れ様でした",
            "字幕",
            "Subscribe",
            "Thank you for watching",
        ],
        # 日语识别提示词：放入常见直播用语和语气，帮模型稳定标点与用词
        "initial_prompt": (
            "これは日本語のライブ配信の音声です。話し言葉で、"
            "「えーと」「あの」「まあ」「なんか」などのフィラーや、"
            "「〜だね」「〜じゃん」「〜っぽい」「〜かな」などの口語表現が多く含まれます。"
            "句読点を補い、漢字かな交じりで表記してください。"
        ),
    },
    "translate": {
        # llm | local | off
        "provider": "llm",
        "target_lang": "简体中文",
        "context_sentences": 4,
        # 攒多久的句子送去翻译（0 = 每句立刻翻）
        "debounce_ms": 260,
        "timeout_s": 25,
        "llm": {
            "base_url": "https://api.deepseek.com",
            "api_key": "",
            "api_key_env": "JP_LIVE_TRANSLATOR_API_KEY",
            "model": "deepseek-chat",
            "temperature": 0.2,
            "max_tokens": 400,
            "stream": False,
            "extra_headers": {},
            "retry": 1,
        },
        # 本地 NMT：transformers(推荐) | ct2(需自己提供 CT2 模型)
        "local": {
            "backend": "transformers",
            # 目录里要有 config.json + 权重 + source.spm/target.spm
            "model_dir": str(ROOT / "models" / "hf-shun89__opus-mt-ja-zh"),
            "tokenizer_dir": "",
            "device": "cpu",
            "compute_type": "int8",
            "beam_size": 2,
            "max_new_tokens": 128,
            "no_repeat_ngram_size": 4,
            "max_batch": 8,
            "num_workers": 6,
        },
    },
    "glossary": {
        "enabled": True,
        "path": str(ROOT / "glossary.yaml"),
        "max_entries_in_prompt": 80,
    },
    "subtitle": {
        # 字幕窗：丢给 Electron 的默认参数
        "font_size": 30,
        "source_font_size": 20,
        "opacity": 0.82,
        "background": "#0b0b10",
        "show_source": True,
        "max_lines": 3,
        # 每条字幕停留时长（0 = 直到下一条）
        "hold_ms": 0,
        # 直播结束后仍无新内容的清屏时间
        "clear_after_ms": 25000,
    },
    "server": {
        "host": "127.0.0.1",
        "port": 8765,
    },
    "log": {
        "level": "info",
        "file": str(ROOT / "logs" / "jp-live-translator.log"),
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text) or {}
    except Exception:
        # 没有 pyyaml 时退化：只支持简单 JSON
        try:
            return json.loads(text)
        except Exception:
            return {}


_ENV_MAP = {
    "JP_LIVE_ASR_MODEL": ("asr", "model"),
    "JP_LIVE_ASR_DEVICE": ("asr", "device"),
    "JP_LIVE_ASR_COMPUTE_TYPE": ("asr", "compute_type"),
    "JP_LIVE_TRANSLATE_PROVIDER": ("translate", "provider"),
    "JP_LIVE_TRANSLATOR_API_KEY": ("translate", "llm", "api_key"),
    "JP_LIVE_TRANSLATOR_BASE_URL": ("translate", "llm", "base_url"),
    "JP_LIVE_TRANSLATOR_MODEL": ("translate", "llm", "model"),
    "JP_LIVE_AUDIO_DEVICE": ("audio", "device"),
    "JP_LIVE_AUDIO_GAIN": ("audio", "gain"),
    "JP_LIVE_LOCAL_NMT_DIR": ("translate", "local", "model_dir"),
    "JP_LIVE_SERVER_PORT": ("server", "port"),
    "JP_LIVE_LOG_LEVEL": ("log", "level"),
}


def _apply_env(cfg: dict) -> dict:
    for env, path in _ENV_MAP.items():
        raw = os.environ.get(env)
        if raw is None or raw == "":
            continue
        node = cfg
        for key in path[:-1]:
            node = node.setdefault(key, {})
        cur = node.get(path[-1])
        if isinstance(cur, bool):
            val: Any = raw.strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(cur, int) and not isinstance(cur, bool):
            try:
                val = int(raw)
            except ValueError:
                val = cur
        elif isinstance(cur, float):
            try:
                val = float(raw)
            except ValueError:
                val = cur
        else:
            val = raw
        node[path[-1]] = val
    return cfg


@dataclass
class Config:
    data: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        cfg_path = Path(path) if path else ROOT / "config.yaml"
        merged = _deep_merge(DEFAULTS, _load_yaml(cfg_path))
        merged = _apply_env(merged)
        return cls(data=merged)

    def get(self, *path: str, default: Any = None) -> Any:
        node: Any = self.data
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def with_overrides(self, overrides: dict) -> "Config":
        return Config(data=_deep_merge(self.data, overrides or {}))

    @property
    def root(self) -> Path:
        return ROOT


def resolve_api_key(cfg: Config) -> str:
    key = str(cfg.get("translate", "llm", "api_key", default="") or "").strip()
    if key:
        return key
    env_name = str(cfg.get("translate", "llm", "api_key_env", default="") or "")
    if env_name:
        env_val = os.environ.get(env_name, "").strip()
        if env_val:
            return env_val
    for fallback in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        v = os.environ.get(fallback, "").strip()
        if v:
            return v
    return ""
