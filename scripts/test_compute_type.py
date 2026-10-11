"""验证 device/compute_type 的组合不会算错。

真踩到的坑（发行版一启动就崩）：
    config.yaml 里是 `device: auto` + `compute_type: float16`
    "auto" 先把 device 设成 cuda → compute_type 算成 float16
    接着发现没有 CUDA 运行库 → device 改成 cpu
    但 compute_type **没跟着改**，于是 CPU + float16，ctranslate2 直接拒绝：

        ValueError: Requested float16 compute type, but the target device
                    or backend do not support efficient float16 computation.

    崩在 ensure_loaded 里，用户看到的现象是「启动了但没字幕」。

    python -m scripts.test_compute_type
"""
from __future__ import annotations

import sys
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
    from backend.asr_engine import AsrEngine

    print("=" * 72)
    print(" device / compute_type 组合验证")
    print("=" * 72)

    print("\n[1] _pick_compute_type 的取值规则")
    # 直接构造一个只用来问规则的实例（不加载模型）
    e = AsrEngine(model="small", device="cpu")
    e.device = "cpu"
    check("CPU + 未配置 -> int8", e._pick_compute_type("") == "int8",
          e._pick_compute_type(""))
    check("CPU + float16 -> 改成 int8（CPU 不支持 fp16）",
          e._pick_compute_type("float16") == "int8",
          e._pick_compute_type("float16"))
    check("CPU + int8_float16 -> 保留（不是纯 float16）",
          e._pick_compute_type("int8_float16") == "int8_float16",
          e._pick_compute_type("int8_float16"))
    check("CPU + int8 -> 保留", e._pick_compute_type("int8") == "int8",
          e._pick_compute_type("int8"))
    e.device = "cuda"
    check("GPU + 未配置 -> float16", e._pick_compute_type("") == "float16",
          e._pick_compute_type(""))
    check("GPU + int8_float16 -> 尊重配置",
          e._pick_compute_type("int8_float16") == "int8_float16",
          e._pick_compute_type("int8_float16"))

    print("\n[2] auto + float16（就是这个组合把发行版搞崩的）")
    print("    注意：这台机器有 GPU，auto 会选 cuda —— 所以必须**另外**")
    print("    用 --force-cpu 的等价路径验证 CPU 分支，见 [5]。")
    eng = AsrEngine(model="tiny", device="auto", compute_type="float16")
    print(f"      device={eng.device}  compute_type={eng.compute_type}")
    if eng.device == "cpu":
        check("无 CUDA 时 auto 选 CPU", True)
        check("并且 compute_type 跟着改成 int8（不是 float16）",
              eng.compute_type == "int8", eng.compute_type)
    else:
        check("有 CUDA 时 auto 选 cuda", True)
        check("cuda 下保留 float16", eng.compute_type == "float16", eng.compute_type)

    print("\n[3] 真的能加载（这是崩过的地方）")
    real_root = ROOT / "models"
    eng2 = AsrEngine(model="small", device="auto", compute_type="float16",
                     models_root=str(real_root))
    try:
        eng2.ensure_loaded()
        check(f"加载成功（device={eng2.device}/{eng2.compute_type}）",
              eng2._model is not None)
        check("compute_type 与 device 匹配",
              (eng2.compute_type == "int8") if eng2.device == "cpu"
              else (eng2.compute_type == "float16"),
              f"{eng2.device}/{eng2.compute_type}")
    except Exception as exc:
        check("加载成功", False, f"{type(exc).__name__}: {exc}")

    print("\n[4] device=cpu 显式指定 + float16（用户手改配置的常见写法）")
    try:
        eng3 = AsrEngine(model="small", device="cpu", compute_type="float16",
                         models_root=str(real_root))
        check("compute_type 被纠正为 int8", eng3.compute_type == "int8",
              eng3.compute_type)
        eng3.ensure_loaded()
        check("加载成功", eng3._model is not None)
    except Exception as exc:
        check("显式 cpu + float16 能加载", False, f"{type(exc).__name__}: {exc}")

    print("\n[5] 发行版实际走的路径：force-cpu + float16 + 包里只有 small")
    # 这一节最关键 —— 发行版在没有显卡的机器上就是这个组合，
    # 之前直接抛 ValueError 崩掉（而且因为本机有 GPU，一直测不出来）。
    try:
        eng4 = AsrEngine(model="small", device="cpu", compute_type="float16",
                         cpu_compute_type="int8", models_root=str(real_root))
        eng4.ensure_loaded()
        check("force-cpu 场景加载成功", eng4._model is not None)
        check("compute_type 被纠正", eng4.compute_type == "int8", eng4.compute_type)
        import numpy as np

        segs, _ = eng4._model.transcribe(
            np.zeros(8000, dtype=np.float32), language="ja", beam_size=1,
            vad_filter=False, without_timestamps=True)
        list(segs)
        check("能真正推理", True)
    except Exception as exc:
        check("force-cpu 场景可加载可推理", False, f"{type(exc).__name__}: {exc}")

    print("\n" + "=" * 72)
    print(f" 通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 72)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
