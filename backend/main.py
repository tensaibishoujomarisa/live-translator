"""命令行入口。

  python -m backend.main                  # 启动（JSON 事件写到 stdout）
  python -m backend.main --pretty          # 终端直接看双语字幕
  python -m backend.main --doctor          # 环境自检
  python -m backend.main --list-devices    # 列出可回环采集的设备
  python -m backend.main --probe-audio 3   # 采 3 秒声音看有没有电平
  python -m backend.main --test-asr file.wav
  python -m backend.main --serve           # 同时开本地 SSE 服务
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

# 允许 `python backend/main.py` 与 `python -m backend.main` 两种用法
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from backend.bridge import Bridge  # type: ignore
    from backend.config import Config, resolve_api_key  # type: ignore
    from backend.util import get_logger, setup_logging  # type: ignore
else:
    from .bridge import Bridge
    from .config import Config, resolve_api_key
    from .util import get_logger, setup_logging

log = get_logger("jplt.main")

# 当前进程的消息通道。_handle_command 等辅助函数需要用它发事件，
# 但它们不是 Pipeline 的方法，拿不到实例 —— 用模块级引用最省事。
# 之前忘了这层，导致 learn 命令抛 NameError 被 except 吞掉、事件根本没发出去。
_bridge: Bridge | None = None


# ---------------------------------------------------------------- 自检


def cmd_doctor(cfg: Config, as_json: bool = False) -> int:
    ok = True
    report: dict = {"checks": {}, "notes": []}

    def note(msg: str) -> None:
        report["notes"].append(msg)
        if not as_json:
            print(msg)

    if not as_json:
        print("=" * 68)
        print(" 日语直播实时翻译 — 环境自检")
        print("=" * 68)
        print(f"\n[Python] {sys.version.split()[0]}  ({sys.executable})")

    report["python"] = sys.version.split()[0]

    def check(name: str, fn, required: bool = True) -> None:
        nonlocal ok
        try:
            info = fn()
            report["checks"][name] = {"ok": True, "info": str(info)}
            if not as_json:
                print(f"[ OK ] {name:<22} {info}")
        except Exception as exc:
            report["checks"][name] = {"ok": False, "info": f"{type(exc).__name__}: {exc}"}
            if required:
                ok = False
            if not as_json:
                print(f"[{'FAIL' if required else 'WARN'}] {name:<22} {type(exc).__name__}: {exc}")

    def _torch():
        import torch

        cuda = torch.cuda.is_available()
        s = f"torch {torch.__version__}  CUDA={'yes' if cuda else 'no'}"
        if cuda:
            s += f"  {torch.cuda.get_device_name(0)}"
        return s

    def _fw():
        import faster_whisper

        return f"faster-whisper {getattr(faster_whisper, '__version__', '?')}"

    def _sd():
        import sounddevice

        return f"sounddevice {sounddevice.__version__}"

    def _ct2():
        import ctranslate2

        return f"ctranslate2 {ctranslate2.__version__}"

    def _spm():
        import sentencepiece

        return f"sentencepiece {sentencepiece.__version__}"

    def _req():
        import requests

        return f"requests {requests.__version__}"

    def _yaml():
        import yaml

        return f"pyyaml {yaml.__version__}"

    if not as_json:
        print()
    check("torch (可选)", _torch, required=False)
    check("faster-whisper", _fw)
    check("sounddevice", _sd)
    check("ctranslate2 (本地翻译)", _ct2, required=False)
    check("sentencepiece", _spm, required=False)
    check("requests", _req)
    check("pyyaml", _yaml, required=False)

    # CUDA 运行库
    if not as_json:
        print("\n[CUDA 运行库]")
    try:
        from backend.cuda_setup import prepare_cuda_dlls  # type: ignore

        rep = prepare_cuda_dlls()
        report["cuda"] = {k: (v if k != "dirs" else v[:3]) for k, v in rep.items()}
        if as_json:
            pass
        elif rep.get("ok"):
            print(f"[ OK ] 已加载 {len(rep['loaded'])} 个 DLL：{', '.join(rep['loaded'][:4])}…")
        else:
            print(f"[WARN] {rep.get('note')}")
            if rep.get("missing"):
                print(f"       缺少：{', '.join(rep['missing'][:4])}")
            print("       装 GPU 支持： pip install nvidia-cublas-cu12 nvidia-cudnn-cu12")
    except Exception as exc:
        if not as_json:
            print(f"[WARN] CUDA 检查失败：{exc}")

    # VAD
    if not as_json:
        print("\n[VAD]")
    try:
        from backend.vad import create_vad  # type: ignore

        vad = create_vad(
            str(cfg.get("vad", "backend", default="") or ""), models_dir=Path(cfg.root) / "models"
        )
        report["vad"] = vad.name
        if not as_json:
            quality = {
                "onnx": "首选，断句最准",
                "torch": "可用",
                "energy": "兜底方案，快速口语下断句会偏碎（建议装 Silero VAD）",
            }.get(vad.name, "")
            print(f"[ OK ] 后端 {vad.name}（frame={vad.frame}）— {quality}")
            if vad.name == "energy":
                print("       装 Silero VAD： python -m scripts.setup_vad")
    except Exception as exc:
        report["vad"] = f"error: {exc}"
        if not as_json:
            print(f"[WARN] VAD 探测失败：{exc}")

    # 音频
    if not as_json:
        print("\n[音频采集]")
    try:
        from backend.audio_in import describe_audio_backends, list_loopback_devices  # type: ignore

        info = describe_audio_backends()
        report["audio"] = info
        loops = list_loopback_devices()
        if not as_json:
            print(f"  sounddevice 支持 loopback：{'是' if info['sounddevice_loopback'] else '否'}（{info['sounddevice_note']}）")
            print(f"  PyAudio 实现：{info['pyaudio_module'] or '未安装'}")
            print(f"  可用回环端点：{len(loops)} 个，将使用：{info['chosen']}")
            for d in loops[:10]:
                mark = " ← 默认" if d.get("is_default") else ""
                print(f"    [{d['id']:>7}] {d['name'][:52]:<52} {d['samplerate']}Hz{mark}")
        if not loops:
            ok = False
            if not as_json:
                print("[FAIL] 没有任何可用的回环采集端点")
    except Exception as exc:
        ok = False
        if not as_json:
            print(f"[FAIL] 音频检查失败：{exc}")

    # 翻译
    if not as_json:
        print("\n[翻译后端]")
    provider = str(cfg.get("translate", "provider", default="llm"))
    report["translate"] = {"provider": provider}
    if not as_json:
        print(f"  provider = {provider}")
    if provider == "llm":
        key = resolve_api_key(cfg)
        base = cfg.get("translate", "llm", "base_url", default="")
        model = cfg.get("translate", "llm", "model", default="")
        report["translate"].update({"base_url": str(base), "model": str(model), "api_key": bool(key)})
        if not as_json:
            print(f"  base_url = {base}\n  model    = {model}")
            if key:
                print(f"  api_key  = 已配置（{len(key)} 字符）")
            else:
                print("  api_key  = 未配置（只显示日文）")
                print("             设置： setx JP_LIVE_TRANSLATOR_API_KEY \"sk-...\"")
    else:
        md = cfg.get_path("translate", "local", "model_dir")
        has = (md / "model.bin").exists() or (md / "config.json").exists()
        report["translate"]["local_model"] = str(md)
        report["translate"]["local_model_ok"] = has
        if not as_json:
            if has:
                print(f"  [ OK ] 本地模型 {md}")
            else:
                print(f"  [WARN] 本地模型不存在：{md}")
                print("         运行： python -m scripts.setup_local_mt")

    # 路径
    if not as_json:
        print("\n[路径]")
        print(f"  项目根目录 : {cfg.root}")
        print(f"  模型缓存   : {os.environ.get('HF_HOME', '(默认)')}")
    try:
        import shutil

        free = shutil.disk_usage(str(cfg.root)).free / 1024**3
        report["free_gb"] = round(free, 1)
        if not as_json:
            print(f"  剩余空间   : {free:.1f} GB")
            if free < 6:
                print("  [WARN] 空间偏紧，large-v3-turbo 约需 3GB")
    except Exception:
        pass

    # 已下载的模型
    try:
        mdir = Path(cfg.root) / "models"
        if mdir.is_dir():
            total = sum(f.stat().st_size for f in mdir.rglob("*") if f.is_file())
            report["models_gb"] = round(total / 1024**3, 2)
            if not as_json:
                print(f"  已下载模型 : {total / 1024**3:.2f} GB")
    except Exception:
        pass

    if not as_json:
        print("\n" + "=" * 68)
        print(" 结论：" + ("环境就绪 ✔" if ok else "存在问题 ✘，请看上面的 FAIL 项"))
        print("=" * 68)
    report["ok"] = ok
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def cmd_list_devices(as_json: bool = False) -> int:
    from backend.audio_in import describe_audio_backends, list_loopback_devices  # type: ignore

    loops = list_loopback_devices()
    info = describe_audio_backends()
    if as_json:
        print(json.dumps({"backends": info, "devices": loops}, ensure_ascii=False))
        return 0
    if not loops:
        print("没有找到任何可回环采集的设备。")
        print(f"  sounddevice loopback 支持：{info['sounddevice_loopback']}（{info['sounddevice_note']}）")
        print(f"  PyAudio 实现：{info['pyaudio_module'] or '未安装'}")
        print("  提示： pip install PyAudioWPatch")
        return 1
    print(f"{'id':>10}  {'设备名':<50} {'采样率':>7}  默认")
    print("-" * 82)
    for d in loops:
        print(f"{d['id']:>10}  {d['name'][:50]:<50} {d['samplerate']:>7}  {'是' if d.get('is_default') else ''}")
    print("\n把 id（如 pa:70）或设备名的一部分填到 config.yaml 的 audio.device。")
    print("留空 = 跟随系统默认输出设备。")
    return 0


def cmd_probe_audio(seconds: float, device: str) -> int:
    from backend.audio_in import probe_audio  # type: ignore

    print(f"开始采集 {seconds} 秒，请让直播/音乐出声…")
    res = probe_audio(seconds=seconds, device=device)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if not res.get("ok"):
        print("\n没采到声音。排查顺序：")
        print("  1. 确认正在播放声音（不是静音、耳机没拔）")
        print("  2. python -m backend.main --list-devices 找目标设备，填到 config.yaml 的 audio.device")
        print("  3. 用 Voicemeeter 等虚拟声卡时，采集目标要选你实际听的那个输出设备")
        return 1
    if res["rms"] < 0.001:
        print(f"\n电平偏低（rms={res['rms']}）。把 config.yaml 的 audio.gain 调到 2~4。")
    else:
        print(f"\n电平正常（rms={res['rms']}, {res['dbfs']} dBFS）✔")
    return 0


def cmd_test_asr(path: str, cfg: Config) -> int:
    from backend.asr_engine import AsrEngine  # type: ignore

    p = Path(path)
    if not p.exists():
        print(f"文件不存在：{p}")
        return 1
    audio = _load_audio(p)
    print(f"音频 {audio.size / 16000:.1f}s，开始识别…")
    eng = AsrEngine(
        model=str(cfg.get("asr", "model", default="large-v3-turbo")),
        device=str(cfg.get("asr", "device", default="cuda")),
        compute_type=str(cfg.get("asr", "compute_type", default="")),
        blocklist=list(cfg.get("asr", "hallucination_blocklist", default=[]) or []),
        initial_prompt=str(cfg.get("asr", "initial_prompt", default="")),
        models_root=str(Path(cfg.root) / "models"),
        on_status=lambda m: print("  " + m),
    )
    eng.ensure_loaded()
    res = eng.transcribe(audio, beam_size=int(cfg.get("asr", "beam_size_final", default=3)))
    print(json.dumps(res.as_dict(), ensure_ascii=False, indent=2))
    print("\n日文：", res.text or "(空)")
    if res.ok:
        from backend.translator import create_translator  # type: ignore

        tr = create_translator(cfg, logger_status=lambda m: print("  " + m))
        out = tr.translate(res.text)
        print("中文：", out or "(未翻译)")
    return 0 if res.ok else 2


def _load_audio(p: Path):
    import numpy as np

    if p.suffix.lower() == ".npy":
        return np.load(str(p)).astype(np.float32)
    if p.suffix.lower() == ".wav":
        import wave

        with wave.open(str(p), "rb") as w:
            rate = w.getframerate()
            ch = w.getnchannels()
            width = w.getsampwidth()
            raw = w.readframes(w.getnframes())
        dtype = {1: np.int8, 2: np.int16, 4: np.int32}.get(width, np.int16)
        data = np.frombuffer(raw, dtype=dtype).astype(np.float32)
        if width == 2:
            data /= 32768.0
        elif width == 4:
            data /= 2147483648.0
        if ch > 1:
            data = data.reshape(-1, ch).mean(axis=1)
        if rate != 16000:
            from backend.util import resample_linear  # type: ignore

            data = resample_linear(data, rate, 16000)
        return data
    raise SystemExit(f"只支持 .wav / .npy 测试音频，收到 {p.suffix}")


# ---------------------------------------------------------------- 主流程


def _import_terms(cfg: Config, src_file: str) -> int:
    """把候选术语文件**追加**进 glossary.yaml。

    刻意走「追加」而不是整份重写：用户手写的注释、分组、alt 都要保住。
    """
    from .glossary import Glossary

    src = Path(src_file)
    if not src.exists():
        print(f"[ERROR] 候选术语文件不存在：{src}")
        return 1
    dst_path = cfg.get_path("glossary", "path")
    if not dst_path:
        print("[ERROR] 配置里没有 glossary.path")
        return 1

    incoming = Glossary.load(src)
    if not len(incoming):
        print(f"[ERROR] {src.name} 里没解析出任何术语（检查 YAML 格式与缩进）")
        return 1

    target = Glossary.load(dst_path)
    before = len(target)
    print(f"候选术语 : {src}")
    print(f"  解析出 {len(incoming)} 条")
    print(f"目标术语表: {dst_path}")
    print(f"  现有 {before} 条\n")

    res = target.append_entries(incoming.entries)
    added, skipped = res["added"], res["skipped"]

    if added:
        print(f"新增 {len(added)} 条：")
        for ja in added:
            print(f"  + {ja}")
    if skipped:
        print(f"\n跳过 {len(skipped)} 条（已存在，未改动）：")
        for ja in skipped:
            print(f"  = {ja}")
    print(f"\n术语表：{before} -> {res['total']} 条")
    print(f"原文件已备份为 {Path(str(dst_path) + '.bak').name}")
    print("\n重启字幕程序后生效（术语表是启动时读取的）。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser("jp-live-translator", description="日语直播实时翻译字幕（本地识别）")
    ap.add_argument("--config", default=None, help="配置文件路径（默认 config.yaml）")
    ap.add_argument("--pretty", action="store_true", help="终端直接打印双语字幕（人类可读）")
    ap.add_argument("--stdin-commands", action="store_true", help="从 stdin 接收控制命令")
    ap.add_argument("--serve", action="store_true", help="同时启动本地 SSE 事件服务")
    ap.add_argument("--port", type=int, default=None,
                    help="SSE 服务端口（默认取配置；给 0 表示随机挑一个空闲端口）")
    ap.add_argument("--doctor", action="store_true", help="环境自检")
    ap.add_argument("--list-devices", action="store_true", help="列出可回环采集的音频设备")
    ap.add_argument("--probe-audio", type=float, default=None, metavar="SEC", help="采集若干秒测试电平")
    ap.add_argument("--test-asr", default=None, metavar="FILE", help="用 wav/npy 文件测试识别+翻译")
    ap.add_argument("--json", action="store_true", help="自检/设备列表输出 JSON")
    ap.add_argument("--model", default=None, help="覆盖 asr.model")
    ap.add_argument("--device", default=None, help="覆盖 asr.device (cuda/cpu)")
    ap.add_argument("--compute-type", default=None, help="覆盖 asr.compute_type")
    ap.add_argument("--provider", default=None, help="覆盖 translate.provider (llm/local/off)")
    ap.add_argument("--audio-device", default=None, help="覆盖 audio.device")
    ap.add_argument("--gain", type=float, default=None, help="覆盖 audio.gain")
    ap.add_argument("--min-silence", type=int, default=None, help="覆盖 vad.min_silence_ms（断句灵敏度）")
    ap.add_argument("--vad-threshold", type=float, default=None, help="覆盖 vad.threshold")
    ap.add_argument("--context-sentences", type=int, default=None, help="覆盖 translate.context_sentences")
    ap.add_argument("--learning-path", default=None,
                    help="覆盖 learning.path（修正记录存放位置；发行版必须传，否则会写到别处）")
    ap.add_argument("--glossary-path", default=None,
                    help="覆盖 glossary.path（测试必须传，否则会改到用户真实的术语表）")
    ap.add_argument("--force-cpu", action="store_true",
                    help="强制 device=cpu（模拟没有显卡的机器，用于验证发行版行为）")
    ap.add_argument("--import-terms", default=None, metavar="FILE",
                    help="把 FILE 里的术语**追加**到 glossary.yaml（不动已有内容）后退出")
    ap.add_argument("--api-key", default=None, help="翻译 API Key（也可用环境变量）")
    ap.add_argument("--base-url", default=None, help="翻译 API base_url")
    ap.add_argument("--llm-model", default=None, help="翻译模型名")
    ap.add_argument("--target-lang", default=None, help="目标语言，默认简体中文")
    ap.add_argument("--log-level", default=None)
    return ap


def apply_cli_overrides(cfg: Config, args: argparse.Namespace) -> Config:
    ov: dict = {}
    if args.model:
        ov.setdefault("asr", {})["model"] = args.model
    if args.device:
        ov.setdefault("asr", {})["device"] = args.device
    if args.compute_type:
        ov.setdefault("asr", {})["compute_type"] = args.compute_type
    if args.provider:
        ov.setdefault("translate", {})["provider"] = args.provider
    if args.audio_device is not None:
        ov.setdefault("audio", {})["device"] = args.audio_device
    if args.gain is not None:
        ov.setdefault("audio", {})["gain"] = args.gain
    if args.min_silence is not None:
        ov.setdefault("vad", {})["min_silence_ms"] = args.min_silence
    if args.vad_threshold is not None:
        ov.setdefault("vad", {})["threshold"] = args.vad_threshold
    if args.context_sentences is not None:
        ov.setdefault("translate", {})["context_sentences"] = args.context_sentences
    if args.learning_path:
        ov.setdefault("learning", {})["path"] = args.learning_path
    if args.glossary_path:
        ov.setdefault("glossary", {})["path"] = args.glossary_path
    if getattr(args, "force_cpu", False):
        ov.setdefault("asr", {})["device"] = "cpu"
    if args.api_key:
        ov.setdefault("translate", {}).setdefault("llm", {})["api_key"] = args.api_key
    if args.base_url:
        ov.setdefault("translate", {}).setdefault("llm", {})["base_url"] = args.base_url
    if args.llm_model:
        ov.setdefault("translate", {}).setdefault("llm", {})["model"] = args.llm_model
    if args.target_lang:
        ov.setdefault("translate", {})["target_lang"] = args.target_lang
    if args.log_level:
        ov.setdefault("log", {})["level"] = args.log_level
    if args.port is not None:
        ov.setdefault("server", {})["port"] = args.port
    return cfg.with_overrides(ov) if ov else cfg


def _handle_command(cmd: str, pipeline, server) -> None:
    """支持 GUI / 终端送进来的控制命令。"""
    parts = cmd.split()
    if not parts:
        return
    name, rest = parts[0].lower(), parts[1:]

    if name in ("pause", "resume"):
        pipeline.set_paused(name == "pause")
    elif name == "toggle-pause":
        pipeline.set_paused(not pipeline.paused)
    elif name in ("provider", "switch"):
        if rest:
            pipeline.set_translator_provider(rest[0])
    elif name == "tune":
        ov: dict = {}
        for kv in rest:
            if "=" not in kv:
                continue
            k, v = kv.split("=", 1)
            if "." in k:
                section, key = k.split(".", 1)
                ov.setdefault(section, {})[key] = _coerce(v)
            else:
                ov.setdefault("vad", {})[k] = _coerce(v)
        if ov:
            pipeline.update_tuning(ov)
    elif name == "context":
        # 上下文句数： GUI 用这个热更新，不用重启后端
        if rest:
            try:
                pipeline.update_tuning({"translate": {"context_sentences": int(rest[0])}})
            except ValueError:
                log.warning("context 参数不是整数：%s", rest[0])
    elif name in ("terms", "promote-terms", "terms-json"):
        # 把修正记录提升为术语表条目（写入 glossary.yaml）。
        # GUI 走 terms-json（JSON 传参，日语里的空格/tab 不会切错）。
        try:
            if name == "terms-json":
                raw = cmd.split(" ", 1)[1] if " " in cmd else "{}"
                payload = json.loads(raw)
                only = [str(x) for x in (payload.get("terms") or []) if str(x).strip()] or None
            else:
                only = [x for x in rest if x.strip()] or None
            res = pipeline.promote_terms(only=only)
            if not res.get("ok"):
                log.warning("提升术语失败：%s", res.get("error"))
        except Exception as exc:
            log.warning("提升术语失败：%s", exc)
    elif name == "learn":
        # 查看「越用越准」攒了多少修正。
        # 注意：**不能 print 到 stdout** —— stdout 是 JSONL 事件协议的通道，
        # 混进非 JSON 的行会让按行解析的客户端（字幕窗 / SSE 转发）直接解析失败。
        # 人类可读的输出交给 bridge 的 text 模式。
        try:
            stats = pipeline.learn_stats()
            if _bridge is not None:
                _bridge.emit("learn_stats", stats=stats)
                _bridge.status(
                    "learn",
                    f"已记住 {stats.get('total', 0)} 条修正"
                    + (f"，{stats.get('suggest_terms', 0)} 条建议加进术语表"
                       if stats.get("suggest_terms") else ""),
                )
            else:
                log.info("修正统计：%s", stats)
        except Exception as exc:
            log.warning("取修正统计失败：%s", exc)
    elif name in ("correct", "fix", "correct-json"):
        # GUI 走 correct-json（参数是 JSON，日语里出现 tab/空格都不会切错）；
        # 终端调试可以用 tab 分隔的 correct。
        if name == "correct-json":
            raw = cmd.split(" ", 1)[1] if " " in cmd else ""
            try:
                payload = json.loads(raw)
            except Exception as exc:
                log.warning("correct-json 解析失败：%s", exc)
                return
            res = pipeline.add_correction(
                ja=str(payload.get("ja") or ""),
                right=str(payload.get("right") or ""),
                wrong=str(payload.get("wrong") or ""),
                note=str(payload.get("note") or ""),
                scope=str(payload.get("scope") or "sentence"),
            )
            if not res.get("ok"):
                log.warning("记录修正失败：%s", res.get("error"))
            return
        parts = cmd.split("\t")[1:] if "\t" in cmd else []
        if len(parts) < 3:
            log.warning("correct 需要 tab 分隔的 ja/wrong/right：%r", cmd[:80])
        else:
            ja, wrong, right = parts[0], parts[1], parts[2]
            scope = parts[3].strip() if len(parts) > 3 else "sentence"
            res = pipeline.add_correction(ja=ja, right=right, wrong=wrong, scope=scope)
            if not res.get("ok"):
                log.warning("记录修正失败：%s", res.get("error"))
    elif name == "status":
        # 同样不能 print（见上面 learn 的说明），走 bridge 的 stats 事件
        if _bridge is not None:
            _bridge.stats(counters=pipeline.counters, paused=pipeline.paused)
        else:
            log.info("状态：%s", {"counters": pipeline.counters, "paused": pipeline.paused})
    elif name in ("quit", "exit"):
        raise KeyboardInterrupt
    else:
        log.warning("未知命令：%s", cmd)


def _coerce(v: str):
    try:
        if "." in v:
            return float(v)
        return int(v)
    except ValueError:
        low = v.lower()
        if low in ("true", "yes", "on"):
            return True
        if low in ("false", "no", "off"):
            return False
        return v


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = apply_cli_overrides(Config.load(args.config), args)

    if args.doctor:
        setup_logging("warning", None)
        return cmd_doctor(cfg, as_json=args.json)
    if args.list_devices:
        setup_logging("warning", None)
        return cmd_list_devices(as_json=args.json)
    if args.probe_audio is not None:
        setup_logging("info", None)
        return cmd_probe_audio(args.probe_audio, str(cfg.get("audio", "device", default="")))
    if args.test_asr:
        setup_logging(str(cfg.get("log", "level", default="info")), None)
        return cmd_test_asr(args.test_asr, cfg)

    setup_logging(str(cfg.get("log", "level", default="info")),
                  str(cfg.get_path("log", "file")) if cfg.get("log", "file") else None)

    # 导入候选术语：纯文件操作，不需要起识别引擎
    if args.import_terms:
        return _import_terms(cfg, args.import_terms)

    from backend.pipeline import Pipeline  # type: ignore

    mode = "text" if args.pretty else "jsonl"
    bridge = Bridge(mode=mode)
    global _bridge
    _bridge = bridge
    pipeline = Pipeline(cfg, bridge)

    server = None
    if args.serve:
        from backend.server import EventServer  # type: ignore

        server = EventServer(
            bridge,
            host=str(cfg.get("server", "host", default="127.0.0.1")),
            port=int(cfg.get("server", "port", default=8765)),
        )
        port = server.start()
        bridge.emit("server", url=f"http://127.0.0.1:{port}/events", port=port)
        server.state.cfg_snapshot = _redacted(cfg)
        server.state.apply_tuning = lambda ov: pipeline.update_tuning(ov)
        server.state.command = lambda name, payload: _http_command(name, payload, pipeline)

    def _sig(_signum, _frame):
        bridge.status("signal", "收到退出信号，正在收尾…")
        raise KeyboardInterrupt

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _sig)
        except Exception:
            pass

    rc = 0

    def _idle() -> None:
        """没有命令通道时的等待循环。"""
        while True:
            time.sleep(0.3)

    def _stdin_loop() -> None:
        """读命令；stdin 到 EOF 后**继续运行**而不是退出。

        这一点很重要：Electron 或其它宿主进程如果关掉了 stdin 写入端，
        EOF 会立刻到来，但用户显然还想继续看字幕。
        """
        while True:
            line = sys.stdin.readline()
            if line == "":  # EOF
                log.info("stdin 已关闭（EOF），继续在后台运行；用信号或 HTTP 命令退出")
                bridge.status("input", "命令通道已关闭，继续运行中")
                _idle()
                return
            line = line.strip()
            if not line:
                continue
            try:
                _handle_command(line, pipeline, server)
            except KeyboardInterrupt:
                return

    try:
        pipeline.start()
        if args.stdin_commands:
            _stdin_loop()
        else:
            _idle()
    except KeyboardInterrupt:
        bridge.status("shutdown", "正在停止…")
    except Exception as exc:
        log.exception("主循环异常")
        bridge.error("main", f"{type(exc).__name__}: {exc}")
        rc = 1
    finally:
        try:
            pipeline.stop()
        except Exception:
            pass
        if server:
            server.stop()
        log.info("已退出。统计：%s", pipeline.counters)
    return rc


def _http_command(name: str, payload: dict, pipeline) -> dict:
    if name == "pause":
        return {"paused": pipeline.set_paused(True)}
    if name == "resume":
        return {"paused": pipeline.set_paused(False)}
    if name == "toggle-pause":
        return {"paused": pipeline.set_paused(not pipeline.paused)}
    if name in ("provider", "switch-provider"):
        target = str(payload.get("provider") or payload.get("value") or "")
        if not target:
            raise ValueError("缺少 provider")
        return {"translator": pipeline.set_translator_provider(target)}
    if name == "tuning":
        pipeline.update_tuning(dict(payload.get("overrides") or {}))
        return {}
    if name in ("correct", "fix"):
        ja = str(payload.get("ja") or "").strip()
        right = str(payload.get("right") or payload.get("text") or "").strip()
        if not ja or not right:
            raise ValueError("需要 ja 和 right")
        res = pipeline.add_correction(
            ja=ja,
            right=right,
            wrong=str(payload.get("wrong") or "").strip(),
            note=str(payload.get("note") or "").strip(),
            scope=str(payload.get("scope") or "sentence"),
        )
        if not res.get("ok"):
            raise ValueError(res.get("error") or "记录失败")
        return res
    if name in ("terms", "promote-terms"):
        only = [str(x) for x in (payload.get("terms") or []) if str(x).strip()]
        res = pipeline.promote_terms(only=only or None)
        if not res.get("ok"):
            raise ValueError(res.get("error") or "提升失败")
        return res
    if name == "learn":
        return {"learn": pipeline.learn_stats()}
    if name == "status":
        return {"counters": pipeline.counters, "paused": pipeline.paused}
    raise ValueError(f"未知命令 {name}")


def _redacted(cfg: Config) -> dict:
    import copy

    d = copy.deepcopy(cfg.data)
    try:
        key = d["translate"]["llm"].get("api_key")
        if key:
            d["translate"]["llm"]["api_key"] = "***"
    except Exception:
        pass
    return d


if __name__ == "__main__":
    sys.exit(main())
