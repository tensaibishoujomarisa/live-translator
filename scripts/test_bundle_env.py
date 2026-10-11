"""验证发行版依赖环境里的本地翻译 + 识别是否真的能用。

为什么单独测：`build_env` 建的是干净环境，装的依赖版本可能和开发环境不同
（实测 transformers 5.14.1 -> 5.19.0、huggingface-hub 1.26 -> 2.2）。
MarianMT / faster-whisper 这类库跨大版本常有 API 变动，
不能只看 "import 成功" 就发版。

    python -m scripts.test_bundle_env                 # 用 build/env
    python -m scripts.test_bundle_env --source <dir>  # 指定环境
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"

PROBE = r'''
import sys, warnings, json, pathlib, logging
warnings.filterwarnings("ignore")
sys.path.insert(0, r"{root}")
# 必须初始化日志：jplt.* 的记录器是 propagate=False 且默认无 handler，
# 不初始化的话所有 log.debug/log.error 都会被静默丢弃 ——
# 之前 VAD 失败的真正异常就是这样被吞掉的，白查了半天。
try:
    from backend.util import setup_logging
    setup_logging("debug", None)
    logging.getLogger("jplt").setLevel(logging.DEBUG)
except Exception:
    logging.basicConfig(level=logging.DEBUG)

out = {{"errors": [], "checks": {{}}}}

def note(k, v):
    out["checks"][k] = v

# ---- 版本 ----
import torch, transformers, numpy
note("torch", torch.__version__)
note("transformers", transformers.__version__)
note("numpy", numpy.__version__)
try:
    import huggingface_hub
    note("huggingface_hub", huggingface_hub.__version__)
except Exception:
    pass

# ---- 本地翻译（MarianMT）----
try:
    from transformers import MarianMTModel, MarianTokenizer
    note("MarianMTModel 可导入", True)
except Exception as e:
    out["errors"].append(f"MarianMT 导入失败: {{type(e).__name__}}: {{e}}")
    print(json.dumps(out, ensure_ascii=False))
    sys.exit(1)

model_dir = r"{model_dir}"
try:
    tok = MarianTokenizer.from_pretrained(model_dir, local_files_only=True)
    mdl = MarianMTModel.from_pretrained(model_dir, local_files_only=True).eval()
    note("模型加载", "ok")
    sents = ["ちょっと待って、いま何時？", "字幕作成者はどれですか"]
    res = []
    with torch.no_grad():
        for s in sents:
            b = tok([s], return_tensors="pt", padding=True)
            g = mdl.generate(**b, num_beams=2, max_new_tokens=64, no_repeat_ngram_size=4)
            res.append(tok.batch_decode(g, skip_special_tokens=True)[0])
    note("译文", res)
    if not all(res):
        out["errors"].append("译文为空")
    # 退化检测
    for r in res:
        if len(r) >= 30 and len(set(r)) < 8:
            out["errors"].append(f"译文疑似退化: {{r[:40]}}")
except Exception as e:
    out["errors"].append(f"本地翻译失败: {{type(e).__name__}}: {{e}}")

# ---- 识别（faster-whisper 能否构造 CTranslate2 模型）----
try:
    from faster_whisper import WhisperModel
    note("WhisperModel 可导入", True)
except Exception as e:
    out["errors"].append(f"faster-whisper 导入失败: {{type(e).__name__}}: {{e}}")

# ---- VAD（onnxruntime 能否跑 silero）----
try:
    import numpy as np
    from backend.vad import create_vad, FRAME, _find_onnx_model
    note("VAD 模型路径", str(_find_onnx_model(pathlib.Path(r"{models_dir}"))))
    vad = create_vad("", models_dir=r"{models_dir}")
    note("VAD 后端", vad.name)
    probs = [vad.probabilities(np.zeros(FRAME, dtype=np.float32)) for _ in range(3)]
    note("VAD 静音概率", [round(p, 4) for p in probs])
    if vad.name != "onnx":
        out["errors"].append(
            "VAD 退化为 " + vad.name + "（发行版会缺 silero 模型）。"
            "模型路径=" + str(_find_onnx_model(pathlib.Path(r"{models_dir}")))
        )
except Exception as e:
    import traceback
    out["errors"].append("VAD 失败: " + type(e).__name__ + ": " + str(e))
    note("VAD 回溯", traceback.format_exc()[-600:])

# ---- 音频采集 ----
try:
    from backend.audio_in import list_loopback_devices
    n = len(list_loopback_devices())
    note("回环端点", n)
    if n == 0:
        out["errors"].append("没有回环采集端点")
except Exception as e:
    out["errors"].append(f"音频枚举失败: {{type(e).__name__}}: {{e}}")

print(json.dumps(out, ensure_ascii=False))
'''


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=str(BUILD / "env"))
    ap.add_argument("--model-dir", default="")
    args = ap.parse_args()

    env = Path(args.source).resolve()
    py = env / "Scripts" / "python.exe"
    if not py.exists():
        print(f"[error] 找不到 {py}（先跑 python -m scripts.build_env）")
        return 2

    model_dir = Path(args.model_dir) if args.model_dir else (
        ROOT / "models" / "hf-shun89__opus-mt-ja-zh")
    if not (model_dir / "config.json").exists():
        print(f"[error] 找不到本地翻译模型 {model_dir}")
        return 2

    print("=" * 70)
    print(" 发行版依赖环境验证")
    print("=" * 70)
    print(f"环境：{env}")
    print(f"模型：{model_dir}\n")

    code = PROBE.format(root=str(ROOT), model_dir=str(model_dir),
                        models_dir=str(ROOT / "models"))
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(code)
        probe_path = f.name

    r = subprocess.run([str(py), probe_path], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=str(ROOT))
    Path(probe_path).unlink(missing_ok=True)

    tail = (r.stdout or "").strip().splitlines()
    data = None
    for line in reversed(tail):
        line = line.strip()
        if line.startswith("{"):
            try:
                import json

                data = json.loads(line)
                break
            except Exception:
                continue

    if not data:
        print("[FAIL] 没能解析出结果")
        print("stdout:", (r.stdout or "")[-1500:])
        print("stderr:", (r.stderr or "")[-1500:])
        return 1

    for k, v in data.get("checks", {}).items():
        print(f"  {k:<22} {v}")
    errs = data.get("errors") or []
    print()
    if errs:
        print(f"发现问题 {len(errs)} 个：")
        for e in errs:
            print(f"  ✘ {e}")
        return 1
    print("发行版依赖环境可用 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
