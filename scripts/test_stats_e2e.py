"""端到端验证：采集 → 报告 → 自动调参生效。

1. 造一份「快语速 + 小声」的合成数据
2. 跑 --stats-report 看报告
3. 起后端看日志里有没有「自动调参已应用」，以及 segmenter 是否真的用上了新阈值
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PY = ROOT / ".venv" / "Scripts" / "python.exe"


def make_data(path: Path, *, n: int = 40, chars: int = 22, speech_s: float = 2.9,
              rms: float = 0.004) -> None:
    """快语速（约 7.6 字/秒）+ 低电平。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    with open(path, "w", encoding="utf-8") as f:
        for i in range(n):
            f.write(json.dumps({
                "t": t0 + i * 5,
                "audio_s": 3.2, "speech_ms": speech_s * 1000,
                "chars": chars, "rms": rms, "no_speech": 0.02,
                "compute_ms": 180.0, "cps": round(chars / speech_s, 2),
            }, ensure_ascii=False) + "\n")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="jplt_e2e_stats_"))
    path = tmp / "stats.jsonl"

    print("=" * 70)
    print(" 端到端：采集 → 报告 → 自动调参")
    print("=" * 70)

    print("\n[1] 空数据时的报告")
    r = subprocess.run([str(PY), "-m", "backend.main", "--stats-report",
                        "--stats-path", str(path)],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="ignore")
    out = r.stdout or ""
    print("  " + ("\n  ".join(out.strip().splitlines()[-3:])))
    if "还没有采集到数据" not in out:
        print("  ✘ 空数据提示缺失")
        return 1

    print("\n[2] 造 40 段「快语速 + 小声」数据")
    make_data(path)
    print(f"  写入 {path.name}：40 段，语速约 7.6 字/秒，rms=0.004")

    print("\n[3] 再看报告")
    r2 = subprocess.run([str(PY), "-m", "backend.main", "--stats-report",
                         "--stats-path", str(path)],
                        cwd=str(ROOT), capture_output=True, text=True,
                        encoding="utf-8", errors="ignore")
    out2 = r2.stdout or ""
    for line in out2.strip().splitlines():
        if any(k in line for k in ("语速", "电平", "断句", "增益", "建议", "字/秒")):
            print("  " + line.strip())
    ok_silence = "300ms" in out2
    ok_gain = "增益 3.0" in out2
    print(f"\n  快语速→阈值 300ms: {'✔' if ok_silence else '✘'}")
    print(f"  小声→增益 3.0   : {'✔' if ok_gain else '✘'}")

    print("\n[4] 起后端，看自动调参是否真的生效")
    code = (
        "import sys, json; sys.path.insert(0, '.');"
        "from backend.config import Config;"
        "from backend.pipeline import Pipeline;"
        "from backend.bridge import Bridge;"
        "cfg = Config.load().with_overrides({'tuning': {'path': r'%s'}});"
        "p = Pipeline(cfg, Bridge(mode='none'));"
        "p._apply_auto_tuning();"
        "print('SILENCE', p.cfg.get('vad','min_silence_ms'));"
        "print('GAIN', p.cfg.get('audio','gain'));"
        "print('PARTIAL', p.cfg.get('asr','partial_interval_ms'));"
        "print('AUTO', json.dumps(p._auto_tuning.get('overrides', {}), ensure_ascii=False))"
    ) % str(path).replace("\\", "\\\\")
    r3 = subprocess.run([str(PY), "-c", code], cwd=str(ROOT),
                        capture_output=True, text=True, encoding="utf-8", errors="ignore")
    vals = {}
    for line in (r3.stdout or "").splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[0] in ("SILENCE", "GAIN", "PARTIAL", "AUTO"):
            vals[parts[0]] = parts[1].strip()
            print(f"  {parts[0]:<8} = {parts[1].strip()}")
    if r3.returncode != 0:
        print("  stderr:", (r3.stderr or "")[-500:])
        return 1

    ok = vals.get("SILENCE") == "300" and vals.get("GAIN", "").startswith("3")
    print(f"\n  调参实际生效: {'✔' if ok else '✘'}")
    if not ok:
        return 1

    print("\n[5] 真正走 build()，确认参数被用上（不是只改了 cfg）")
    # 这一步很关键：之前 build() 里先 `c = self.cfg` 再调参，
    # 结果调参创建了新 Config 但后续对象仍用旧值 —— 等于白调。
    # 这里检查真正创建出来的 segmenter / capture 拿到的是新值。
    code2 = (
        "import sys, json; sys.path.insert(0, '.');"
        "from backend.config import Config;"
        "from backend.pipeline import Pipeline;"
        "from backend.bridge import Bridge;"
        "cfg = Config.load().with_overrides({'tuning': {'path': r'%s'},"
        "  'translate': {'provider': 'off'}});"
        "p = Pipeline(cfg, Bridge(mode='none'));"
        "p.build();"
        "sr = int(p.cfg.get('audio','sample_rate', default=16000));"
        "print('SEG_MIN_SILENCE', p.segmenter.min_silence);"
        "print('EXPECT', int(sr * 300 / 1000));"
        "print('SEG_MAX', p.segmenter.max_speech if hasattr(p.segmenter,'max_speech') else 'n/a');"
        "print('CAP_GAIN', getattr(p.capture, 'gain', 'n/a'));"
        "print('CFG_GAIN', p.cfg.get('audio','gain'));"
    ) % str(path).replace("\\", "\\\\")
    r4 = subprocess.run([str(PY), "-c", code2], cwd=str(ROOT),
                        capture_output=True, text=True, encoding="utf-8", errors="ignore")
    vals2 = {}
    for line in (r4.stdout or "").splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2:
            vals2[parts[0]] = parts[1].strip()
            print(f"  {parts[0]:<16} = {parts[1].strip()}")
    if r4.returncode != 0:
        print("  stderr:", (r4.stderr or "")[-800:])
        return 1

    seg_ok = vals2.get("SEG_MIN_SILENCE") == vals2.get("EXPECT")
    gain_ok = vals2.get("CFG_GAIN", "").startswith("3")
    print(f"\n  segmenter 用了新阈值: {'✔' if seg_ok else '✘'} "
          f"({vals2.get('SEG_MIN_SILENCE')} vs 期望 {vals2.get('EXPECT')})")
    print(f"  cfg 里的增益是新值  : {'✔' if gain_ok else '✘'} ({vals2.get('CFG_GAIN')})")
    if not seg_ok:
        return 1

    print("\n[6] 清空")
    subprocess.run([str(PY), "-m", "backend.main", "--stats-clear",
                    "--stats-path", str(path)],
                   cwd=str(ROOT), capture_output=True, text=True,
                   encoding="utf-8", errors="ignore")
    print(f"  文件还在吗: {'否 ✔' if not path.exists() else '是 ✘'}")

    import shutil

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + "=" * 70)
    print(" 端到端验证通过 ✔")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
