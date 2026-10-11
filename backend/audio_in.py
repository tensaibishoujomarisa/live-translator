"""WASAPI loopback 采集：把「系统正在播放的声音」变成 16k 单声道 float32 流。

Windows 上做系统声音回环有两条路，本模块按可用性自动选择：

1. pyaudiowpatch（PyAudioWPatch）—— **首选**
   它自带 WASAPI loopback 补丁，会枚举出 `设备名 [Loopback]` 端点。
   注意：PyAudio 自带的 PortAudio 通常没有打 loopback 补丁。
2. sounddevice —— 仅当其 PortAudio 支持 `WasapiSettings(loopback=True)` 时可用。
   很多发行版的 PortAudio 没有这个参数，构造时会直接抛 TypeError，所以要先探测。

设计要点：真实异常一定要带上类型和消息往上抛。PortAudio 有些失败会抛出
空消息的异常，直接丢掉消息会变成「采集失败：」这种没法排查的日志。
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable

import numpy as np

from .util import get_logger, peak, resample_linear, rms

log = get_logger("jplt.audio")


# ---------------------------------------------------------------- 能力探测


def _sounddevice_loopback_ok() -> tuple[bool, str]:
    """sounddevice 是否支持 WASAPI loopback。返回 (是否支持, 说明)。"""
    try:
        import sounddevice as sd
    except Exception as exc:
        return False, f"sounddevice 不可用：{exc}"
    try:
        sd.WasapiSettings(loopback=True)
    except TypeError as exc:
        return False, f"PortAudio 不支持 loopback 参数（{exc}）"
    except Exception as exc:
        return False, f"WasapiSettings 初始化失败：{exc}"
    return True, "支持"


def _pyaudio_module():
    """返回 (module, 名称)。优先 PyAudioWPatch。"""
    try:
        import pyaudiowpatch as pa  # type: ignore

        return pa, "pyaudiowpatch"
    except Exception:
        pass
    try:
        import pyaudio  # type: ignore

        return pyaudio, "pyaudio"
    except Exception:
        return None, ""


# ---------------------------------------------------------------- 设备枚举


def list_loopback_devices() -> list[dict[str, Any]]:
    """列出所有可用于「回环采集系统声音」的端点。给 UI 下拉框用。"""
    out: list[dict[str, Any]] = []

    # --- pyaudiowpatch 的 loopback 端点（最可靠）---
    pa, pa_name = _pyaudio_module()
    if pa is not None and hasattr(pa, "paWASAPI"):
        inst = None
        try:
            inst = pa.PyAudio()
            default_out = None
            try:
                api = inst.get_host_api_info_by_type(pa.paWASAPI)
                default_out = api.get("defaultOutputDevice")
            except Exception:
                pass
            for d in inst.get_loopback_device_info_generator():
                idx = int(d.get("index", -1))
                out.append(
                    {
                        "id": f"pa:{idx}",
                        "index": idx,
                        "backend": pa_name,
                        "name": str(d.get("name", "")),
                        "samplerate": int(float(d.get("defaultSampleRate", 48000))),
                        "channels": int(d.get("maxInputChannels") or 2),
                        "is_default": idx == default_out,
                    }
                )
        except Exception as exc:
            log.debug("pyaudiowpatch 枚举 loopback 失败：%s", exc)
        finally:
            if inst is not None:
                try:
                    inst.terminate()
                except Exception:
                    pass

    # --- sounddevice 的 loopback（若支持）---
    ok, _why = _sounddevice_loopback_ok()
    if ok:
        try:
            import sounddevice as sd

            hostapis = sd.query_hostapis()
            for i, hp in enumerate(hostapis):
                if "wasapi" not in str(hp.get("name", "")).lower():
                    continue
                default_out = hp.get("default_output_device", -1)
                for idx, d in enumerate(sd.query_devices()):
                    if d.get("hostapi") != i or int(d.get("max_output_channels", 0)) < 1:
                        continue
                    out.append(
                        {
                            "id": f"sd:{idx}",
                            "index": idx,
                            "backend": "sounddevice",
                            "name": str(d["name"]),
                            "samplerate": int(d.get("default_samplerate", 48000)),
                            "channels": min(2, int(d.get("max_output_channels", 2))),
                            "is_default": idx == default_out,
                        }
                    )
        except Exception as exc:
            log.debug("sounddevice 枚举失败：%s", exc)

    return out


def list_devices() -> list[dict[str, Any]]:
    """兼容旧接口：列出所有输出设备（含非 WASAPI），给 --list-devices 用。"""
    devs: list[dict[str, Any]] = []
    try:
        import sounddevice as sd

        hostapis = sd.query_hostapis()
        wasapi_default = None
        for i, hp in enumerate(hostapis):
            if "wasapi" in str(hp.get("name", "")).lower():
                wasapi_default = hp.get("default_output_device")
                break
        for idx, d in enumerate(sd.query_devices()):
            if int(d.get("max_output_channels", 0)) < 1:
                continue
            hname = str(hostapis[d["hostapi"]]["name"]) if d.get("hostapi") is not None else ""
            devs.append(
                {
                    "index": idx,
                    "name": str(d["name"]),
                    "hostapi": hname,
                    "is_loopback": idx == wasapi_default,
                    "default_samplerate": int(d.get("default_samplerate", 48000)),
                    "max_output_channels": int(d.get("max_output_channels", 0)),
                }
            )
    except Exception as exc:
        log.debug("sounddevice 枚举失败：%s", exc)
    if devs:
        return devs

    # 退回 PyAudio 列表
    pa, pa_name = _pyaudio_module()
    inst = None
    try:
        inst = pa.PyAudio()
        for i in range(inst.get_device_count()):
            info = inst.get_device_info_by_index(i)
            if int(info.get("maxOutputChannels", 0)) < 1:
                continue
            devs.append(
                {
                    "index": i,
                    "name": str(info.get("name", "")),
                    "hostapi": pa_name,
                    "is_loopback": False,
                    "default_samplerate": int(info.get("defaultSampleRate", 48000)),
                    "max_output_channels": int(info.get("maxOutputChannels", 0)),
                }
            )
    except Exception:
        pass
    finally:
        if inst is not None:
            try:
                inst.terminate()
            except Exception:
                pass
    return devs


def describe_audio_backends() -> dict[str, Any]:
    """给自检用：把两条采集路径的可用性讲清楚。"""
    ok, why = _sounddevice_loopback_ok()
    pa, pa_name = _pyaudio_module()
    loop = list_loopback_devices()
    best = "none"
    if any(d["backend"].startswith("pyaudiowpatch") for d in loop):
        best = "pyaudiowpatch"
    elif any(d["backend"] == "sounddevice" for d in loop):
        best = "sounddevice"
    return {
        "chosen": best,
        "sounddevice_loopback": ok,
        "sounddevice_note": why,
        "pyaudio_module": pa_name,
        "loopback_count": len(loop),
    }


# ---------------------------------------------------------------- 采集

ChunkCb = Callable[[np.ndarray], None]


class AudioCapture:
    """采集线程 + 回调。向下游推送 16kHz 单声道 float32 的等长小块。"""

    def __init__(
        self,
        device: str | int = "",
        target_rate: int = 16000,
        gain: float = 1.0,
        block_ms: int = 32,
        on_chunk: ChunkCb | None = None,
        on_status: Callable[[str], None] | None = None,
    ):
        self.device = device
        self.target_rate = int(target_rate)
        self.gain = float(gain)
        self.block_ms = int(block_ms)
        self.on_chunk = on_chunk
        self.on_status = on_status or (lambda _m: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=400)
        self._impl = ""
        self._device_info: dict[str, Any] = {}
        self._last_level = 0.0
        self._lock = threading.Lock()
        self._error: str = ""

    # ---------- 生命周期 ----------
    def start(self) -> dict[str, Any]:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="audio-capture", daemon=True)
        self._thread.start()
        return {"impl": self._impl, "device": self._device_info}

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)

    def feed(self, chunk: np.ndarray) -> None:
        """外部直接投喂音频（测试/从文件播放用）。"""
        try:
            self._q.put_nowait(np.asarray(chunk, dtype=np.float32))
        except queue.Full:
            pass

    @property
    def last_level(self) -> float:
        with self._lock:
            return self._last_level

    @property
    def error(self) -> str:
        return self._error

    # ---------- 内部 ----------
    def _run(self) -> None:
        errors: list[str] = []

        # 用户指定了设备就优先用 PyAudioWPatch；没指定则先挑 loopback 端点
        order = self._backend_order()
        for backend in order:
            if self._stop.is_set():
                return
            try:
                if backend == "pyaudio":
                    self._run_pyaudio()
                else:
                    self._run_sounddevice()
                return  # 正常结束（stop 被调用）
            except Exception as exc:
                msg = f"{backend}: {type(exc).__name__}: {exc or '(无错误消息)'}"
                log.warning("采集后端 %s 失败：%s", backend, msg)
                errors.append(msg)
                self.on_status(f"{backend} 采集失败，尝试下一个方案…")
                time.sleep(0.3)

        self._error = " | ".join(errors)
        log.error("所有采集后端都失败：%s", self._error)
        self.on_status(f"音频采集失败：{self._error}")

    def _backend_order(self) -> list[str]:
        pa, _name = _pyaudio_module()
        sd_ok, _why = _sounddevice_loopback_ok()
        has_loop = bool(list_loopback_devices())
        order: list[str] = []
        # PyAudioWPatch 的 loopback 补丁最完整，优先
        if has_loop:
            order.append("pyaudio")
        if sd_ok:
            order.append("sounddevice")
        if pa is not None and "pyaudio" not in order:
            order.append("pyaudio")
        if not order:
            order.append("sounddevice")
        return order

    # ---------- 公共：把原始块变成 16k mono ----------
    def _emit(self, raw: np.ndarray, in_rate: int) -> None:
        if raw.ndim == 2:
            mono = raw.mean(axis=1)
        else:
            mono = raw
        if mono.size == 0:
            return
        if self.gain != 1.0:
            mono = mono * self.gain
        out = resample_linear(np.ascontiguousarray(mono, dtype=np.float32), in_rate, self.target_rate)
        if out.size == 0:
            return
        with self._lock:
            self._last_level = rms(out)
        if self.on_chunk:
            self.on_chunk(out)

    # ---------- 后端 1：PyAudioWPatch loopback ----------
    @staticmethod
    def _norm_name(name: str) -> str:
        """把 '[Loopback]' 之类的后缀和空格去掉，方便名字比对。"""
        s = str(name or "")
        for junk in ("[Loopback]", "[loopback]", "(VB-Audio Voicemeeter VAIO)", " - "):
            s = s.replace(junk, " ")
        return " ".join(s.split()).lower()

    def _pick_pa_loopback(self, inst, pa) -> dict[str, Any]:
        """inst 是 PyAudio() 实例（loopback 枚举是实例方法），pa 是模块（放常量）。"""
        loops = list(inst.get_loopback_device_info_generator())
        if not loops:
            raise RuntimeError("PyAudioWPatch 没有枚举到任何 loopback 端点")

        want = str(self.device or "").strip()
        if want:
            # 允许 "pa:70" / "70" / 设备名关键字三种写法
            if want.startswith("pa:") and want[3:].isdigit():
                want_idx = int(want[3:])
                for d in loops:
                    if int(d.get("index", -1)) == want_idx:
                        return d
            if want.isdigit():
                for d in loops:
                    if int(d.get("index", -1)) == int(want):
                        return d
            lw = want.lower()
            for d in loops:
                if lw in str(d.get("name", "")).lower():
                    return d
            raise RuntimeError(f"找不到匹配的 loopback 设备：{want}")

        # 没指定：跟着系统默认输出设备走。
        # 注意不能拿 defaultOutputDevice 的数字下标直接去比 loopback 的下标 ——
        # 默认设备是 MME 空间的索引，loopback 端点是 WASAPI 空间的，两者对不上。
        # 正确做法是用「名字」匹配。
        default_names: list[str] = []
        try:
            api = inst.get_host_api_info_by_type(pa.paWASAPI)
            di = api.get("defaultOutputDevice")
            if di is not None and int(di) >= 0:
                default_names.append(str(inst.get_device_info_by_index(int(di)).get("name", "")))
        except Exception as exc:
            log.debug("取 WASAPI 默认输出设备失败：%s", exc)
        # MME/其它 hostapi 的默认输出也拿来当候选名字
        try:
            for i in range(inst.get_host_api_count()):
                info = inst.get_host_api_info_by_index(i)
                di = info.get("defaultOutputDevice")
                if di is not None and int(di) >= 0:
                    default_names.append(str(inst.get_device_info_by_index(int(di)).get("name", "")))
        except Exception:
            pass

        for dn in default_names:
            key = self._norm_name(dn)
            if not key:
                continue
            for d in loops:
                if key and key in self._norm_name(d.get("name", "")):
                    log.info("默认输出设备「%s」→ loopback 端点「%s」", dn, d.get("name"))
                    return d

        # 最后兜底：优先带真名（非纯虚拟声卡名）的第一个端点
        log.warning("没能按名字匹配到默认输出设备，退回第一个 loopback 端点：%s", loops[0].get("name"))
        return loops[0]

    def _run_pyaudio(self) -> None:
        pa, pa_name = _pyaudio_module()
        if pa is None:
            raise RuntimeError("pyaudiowpatch 未安装")

        inst = pa.PyAudio()
        stream = None
        try:
            dev = self._pick_pa_loopback(inst, pa)
            idx = int(dev.get("index", -1))
            ch = int(dev.get("maxInputChannels") or 2)
            rate = int(float(dev.get("defaultSampleRate", 48000)))
            frames = max(128, int(rate * self.block_ms / 1000))

            self._impl = pa_name
            self._device_info = {
                "index": idx,
                "name": str(dev.get("name", "")),
                "backend": pa_name,
                "samplerate": rate,
                "channels": ch,
            }
            self.on_status(f"开始采集：{self._device_info['name']} @ {rate}Hz x{ch}（系统声音回环）")
            log.info("采集设备 #%s %s @ %sHz x%s (%s)", idx, self._device_info["name"], rate, ch, pa_name)

            # 用阻塞读而不是回调：PyAudio 的回调路径在某些 Python/NumPy 组合下会抛
            # SystemError: new style getargs format but argument is not a tuple，
            # 而阻塞读走的是另一条代码路径，稳定得多。
            stream = inst.open(
                format=pa.paFloat32,
                channels=ch,
                rate=rate,
                frames_per_buffer=frames,
                input=True,
                input_device_index=idx,
                start=False,
            )
            stream.start_stream()
            read_fail = 0
            while not self._stop.is_set():
                try:
                    raw = stream.read(frames, exception_on_overflow=False)
                except Exception as exc:
                    read_fail += 1
                    if read_fail == 1 or read_fail % 50 == 0:
                        log.warning("读取音频失败(%d 次): %s: %s", read_fail, type(exc).__name__, exc)
                    if read_fail > 200:
                        raise RuntimeError(f"连续读取失败 {read_fail} 次：{exc}")
                    time.sleep(0.02)
                    continue
                arr = np.frombuffer(raw, dtype=np.float32)
                if arr.size == 0:
                    continue
                if ch > 1 and arr.size % ch == 0:
                    arr = arr.reshape(-1, ch)
                self._emit(arr, rate)
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
            try:
                inst.terminate()
            except Exception:
                pass

    # ---------- 后端 2：sounddevice WASAPI loopback ----------
    def _pick_sd_device(self):
        import sounddevice as sd

        devs = sd.query_devices()
        hostapis = sd.query_hostapis()
        want = str(self.device or "").strip()

        if want:
            if want.startswith("sd:") and want[3:].isdigit():
                i = int(want[3:])
                return i, devs[i]
            if want.isdigit():
                return int(want), devs[int(want)]
            lw = want.lower()
            for idx, d in enumerate(devs):
                if int(d.get("max_output_channels", 0)) < 1:
                    continue
                if lw in str(d["name"]).lower():
                    return idx, d
            raise RuntimeError(f"找不到匹配的设备：{want}")

        wasapi_idx = None
        for i, hp in enumerate(hostapis):
            if "wasapi" in str(hp.get("name", "")).lower():
                wasapi_idx = i
                break
        if wasapi_idx is not None:
            default_out = hostapis[wasapi_idx].get("default_output_device", -1)
            if default_out is not None and int(default_out) >= 0:
                return int(default_out), devs[int(default_out)]
        for idx, d in enumerate(devs):
            if int(d.get("max_output_channels", 0)) >= 1:
                return idx, d
        raise RuntimeError("没有可用的输出设备")

    def _run_sounddevice(self) -> None:
        import sounddevice as sd

        ok, why = _sounddevice_loopback_ok()
        if not ok:
            raise RuntimeError(f"sounddevice 不支持 loopback（{why}）")

        idx, info = self._pick_sd_device()
        rate = int(info.get("default_samplerate") or 48000)
        ch = min(2, max(1, int(info.get("max_output_channels", 2))))
        block = max(128, int(rate * self.block_ms / 1000))

        self._impl = "sounddevice"
        self._device_info = {
            "index": idx,
            "name": str(info["name"]),
            "backend": "sounddevice",
            "samplerate": rate,
            "channels": ch,
        }
        self.on_status(f"开始采集：{info['name']} @ {rate}Hz x{ch}（系统声音回环）")

        def _cb(indata, _frames, _time_info, status):  # noqa: ANN001
            if status:
                log.debug("采集状态标记: %s", status)
            self.feed(np.asarray(indata, dtype=np.float32).copy())

        with sd.InputStream(
            device=idx,
            channels=ch,
            samplerate=rate,
            blocksize=block,
            dtype="float32",
            callback=_cb,
            extra_settings=sd.WasapiSettings(loopback=True),
        ):
            while not self._stop.is_set():
                try:
                    raw = self._q.get(timeout=0.2)
                except queue.Empty:
                    continue
                self._emit(raw, rate)


# ---------------------------------------------------------------- 工具


def silence(seconds: float = 0.5, rate: int = 16000) -> np.ndarray:
    """留白音频，用于把一句话和下一句分开（给识别器一个明确的边界）。"""
    n = max(1, int(rate * seconds))
    return np.zeros(n, dtype=np.float32)


def probe_audio(seconds: float = 2.0, device: str = "") -> dict[str, Any]:
    """自检：采几秒声音，回报是否有电平。"""
    chunks: list[np.ndarray] = []
    cap = AudioCapture(device=device, target_rate=16000, on_chunk=lambda c: chunks.append(c))
    cap.start()
    time.sleep(seconds)
    cap.stop()
    if not chunks:
        return {
            "ok": False,
            "reason": cap.error or "没收到任何音频块",
            "impl": cap._impl,
            "backends": describe_audio_backends(),
        }
    audio = np.concatenate(chunks)
    r = rms(audio)
    return {
        "ok": True,
        "impl": cap._impl,
        "device": cap._device_info,
        "seconds": round(audio.size / 16000.0, 2),
        "rms": round(r, 5),
        "peak": round(peak(audio), 5),
        "dbfs": round(20 * np.log10(max(r, 1e-9)), 1),
    }
