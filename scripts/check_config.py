"""启动前的快速确认：provider 配置对不对、本地翻译能不能出中文。"""
import warnings

warnings.filterwarnings("ignore")
from backend.config import Config
from backend.translator import create_translator

cfg = Config.load()
print("provider =", cfg.get("translate", "provider"))
print("asr      =", cfg.get("asr", "model"), "/", cfg.get("asr", "device"))
print("audio    =", cfg.get("audio", "device") or "(默认输出设备)")

tr = create_translator(cfg, logger_status=lambda m: print("  [mt]", m))
print("backend  =", tr.describe())
for s in ["ちょっと待って、いま何時？", "スパチャありがとうございます！"]:
    print("  日", s)
    print("  中", tr.translate(s))
