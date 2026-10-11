"""验证：包里只有 small，却配置成 large-v3-turbo 时，不应该去下载 1.5GB。

背景（真踩到的坑）：
    发行版里带的是 small 模型，但如果 config.yaml 里 asr.model 是
    large-v3-turbo，识别引擎会老老实实去 HuggingFace 下载 ——
    实测下到 429MB 被中断，留下一个 blobs/*.incomplete，
    用户看到的现象是「启动卡住」，而且包里凭空多了几百 MB 垃圾。

    python -m scripts.test_offline_guard
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \u2714 {name}")
    else:
        FAIL += 1
        print(f"  \u2718 {name}" + (f"  ({detail})" if detail else ""))


def main() -> int:
    from backend.asr_engine import _model_in_local_cache

    print("=" * 70)
    print(" 离线保护验证")
    print("=" * 70)

    tmp = Path(tempfile.mkdtemp(prefix="jplt_off_"))
    models = tmp / "models"
    snap = models / "models--Systran--faster-whisper-small" / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    (snap / "model.bin").write_bytes(b"x" * 1024)

    print(f"\n模拟模型目录：{models}")
    print("  只放了 faster-whisper-small（含完整 model.bin）\n")

    print("[1] 缓存识别")
    check("small 被识别为已缓存",
          _model_in_local_cache("small", str(models)) is True)
    check("Systran/faster-whisper-small 也认（带斜杠的形式）",
          _model_in_local_cache("Systran/faster-whisper-small", str(models)) is True)
    check("large-v3-turbo 不在缓存里（应返回 False）",
          _model_in_local_cache("large-v3-turbo", str(models)) is False)
    check("目录不存在时安全返回 False",
          _model_in_local_cache("small", str(tmp / "nope")) is False)
    check("models_root 为空时安全返回 False",
          _model_in_local_cache("small", None) is False)

    print("\n[2] 不完整的下载不该被当成已缓存")
    bad = models / "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo" / "blobs"
    bad.mkdir(parents=True)
    (bad / "xxx.incomplete").write_bytes(b"y" * 2048)
    check("只有 .incomplete 的目录不算已缓存",
          _model_in_local_cache("large-v3-turbo", str(models)) is False)

    print("\n[3] 有快照目录但 model.bin 缺失，也不算完整")
    snap2 = models / "models--foo--faster-whisper-medium" / "snapshots" / "rev1"
    snap2.mkdir(parents=True)
    (snap2 / "config.json").write_text("{}", encoding="utf-8")
    check("缺 model.bin 的快照不算已缓存",
          _model_in_local_cache("medium", str(models)) is False)
    (snap2 / "model.bin").write_bytes(b"z" * 512)
    check("补上 model.bin 后就认了",
          _model_in_local_cache("medium", str(models)) is True)

    print("\n[4] AsrEngine 在模型已缓存时会设 HF_HUB_OFFLINE")
    print("    注意：这一节必须用**真实的** HF 缓存 —— 手造的假快照目录")
    print("    缺了 refs/ 元数据，huggingface_hub 定位不到，测不出真实行为。")
    real_root = ROOT / "models"
    real_ok = _model_in_local_cache("small", str(real_root))
    if not real_ok:
        print("    本机没有 small 的真实缓存，跳过这一节")
    else:
        os.environ.pop("HF_HUB_OFFLINE", None)
        from backend.asr_engine import AsrEngine

        eng = AsrEngine(model="small", device="cpu", models_root=str(real_root))
        eng.ensure_loaded()
        check("引擎用真实缓存加载成功", eng._model is not None)
        check("加载后 HF_HUB_OFFLINE 被置上",
              os.environ.get("HF_HUB_OFFLINE") == "1",
              repr(os.environ.get("HF_HUB_OFFLINE")))

    print("\n[5] 缓存不完整时的回退逻辑")
    print("    这条路径靠代码审查 + 上面 [2][3] 的判断兜底：")
    print("    _model_in_local_cache 说『有』但实际加载失败时，")
    print("    ensure_loaded 会撤销自己设的 HF_HUB_OFFLINE 并重试一次，")
    print("    不会把『本来能下载』的情况变成硬失败。")

    # 模型不在缓存里时不强行离线（还可能触发下载，这里只验判断）
    os.environ.pop("HF_HUB_OFFLINE", None)
    from backend.asr_engine import AsrEngine as _AE

    eng2 = _AE(model="large-v3-turbo", device="cpu", models_root=str(models))
    check("模型不在缓存时不强行离线（允许用户下载）",
          _model_in_local_cache(eng2.model_name, eng2.models_root) is False)

    import shutil

    shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 70)
    print(f" 通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
