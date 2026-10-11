"""下载并转换本地日中翻译模型（CTranslate2）。

默认用 shun89/opus-mt-ja-zh（Marian ja→zh，体积小、纯 CPU int8 就能实时）。
转换后目录结构：
    models/opus-mt-ja-zh-ct2/
        model.bin, config.json, shared_vocabulary.json ...
        source.spm, target.spm

用法：
    python -m scripts.setup_local_mt                     # 默认模型
    python -m scripts.setup_local_mt --repo suirenka/opus-mt-ja-zh
    python -m scripts.setup_local_mt --list              # 列出候选模型

代理：脚本会自动读取 HTTPS_PROXY / http_proxy，也会尝试探测本机常见代理端口。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CANDIDATES = [
    "shun89/opus-mt-ja-zh",
    "suirenka/opus-mt-ja-zh",
    "shishi17777/opus-mt-ja-zh",
    "lemmonyjiang/opus-mt-ja-zh-jav",
    "facebook/nllb-200-distilled-600M",
]

COMMON_PROXY_PORTS = [7890, 7897, 10809, 10808, 20171, 2080, 1080, 8888, 9910]


def log(msg: str) -> None:
    print(msg, flush=True)


def ensure_proxy() -> str:
    """没有代理环境变量时，尝试探测一个可用的本机代理。"""
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        if os.environ.get(key):
            log(f"[proxy] 使用已有环境变量 {key}={os.environ[key]}")
            return os.environ[key]

    import socket

    for port in COMMON_PROXY_PORTS:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.4)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                url = f"http://127.0.0.1:{port}"
                os.environ["HTTPS_PROXY"] = url
                os.environ["HTTP_PROXY"] = url
                log(f"[proxy] 自动探测到代理 {url}")
                return url
    log("[proxy] 未发现代理，将直连下载")
    return ""


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}TB"


def list_candidates() -> None:
    log("候选模型（按推荐顺序）：")
    for c in CANDIDATES:
        log(f"  - {c}")
    log("\n提示：任何 Marian/OPUS-MT 的 ja→zh 模型都能用本脚本转换。")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=CANDIDATES[0], help="HuggingFace 仓库名")
    ap.add_argument("--out", default=None,
                    help="下载目录（默认 models/hf-<repo>；程序按 config.yaml 的 model_dir 找）")
    ap.add_argument("--also-ct2", action="store_true",
                    help="额外转换一份 CTranslate2 版本（注意：部分模型转换后解码会退化）")
    ap.add_argument("--quantization", default="int8", choices=["int8", "int8_float32", "float32", "float16"])
    ap.add_argument("--list", action="store_true", help="列出候选模型后退出")
    ap.add_argument("--force", action="store_true", help="已存在也重新下载")
    args = ap.parse_args()

    if args.list:
        list_candidates()
        return 0

    ensure_proxy()
    src_dir = Path(args.out) if args.out else (ROOT / "models" / ("hf-" + args.repo.replace("/", "__")))

    # 已经下好且能加载就跳过
    if (src_dir / "config.json").exists() and not args.force:
        weights = list(src_dir.glob("*.bin")) + list(src_dir.glob("*.safetensors"))
        if weights:
            log(f"[skip] 已存在：{src_dir}")
            _selftest(src_dir)
            return 0

    # 1) 下载原始 transformers 模型
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        log("缺少 huggingface_hub。先运行：pip install -r requirements.txt")
        return 2

    log(f"[1/2] 下载 {args.repo} → {src_dir}")
    try:
        snapshot_download(
            repo_id=args.repo,
            local_dir=str(src_dir),
            allow_patterns=[
                "*.json",
                "*.model",
                "*.spm",
                "*.txt",
                "*.bin",
                "*.safetensors",
                "*.vocab",
            ],
        )
    except Exception as exc:
        log(f"[error] 下载失败：{type(exc).__name__}: {exc}")
        log("       可以试试别的仓库： python -m scripts.setup_local_mt --repo suirenka/opus-mt-ja-zh")
        log("       或者直接用在线翻译：把 config.yaml 里 translate.provider 设为 llm")
        return 3

    got = sorted(p.name for p in src_dir.iterdir() if p.is_file())
    log(f"       下载完成：{', '.join(got)}")

    # 2) 自检：真跑一句，确保模型能用
    log("[2/2] 自检翻译")
    ok = _selftest(src_dir)

    if args.also_ct2:
        log("\n[附加] 转换 CTranslate2 版本（仅在你确认可用时才需要）")
        out_dir = src_dir.parent / (src_dir.name.replace("hf-", "") + "-ct2")
        if out_dir.exists():
            shutil.rmtree(out_dir, ignore_errors=True)
        try:
            import ctranslate2

            ctranslate2.converters.TransformersConverter(str(src_dir)).convert(
                str(out_dir), quantization=args.quantization, force=True
            )
            for name in ("source.spm", "target.spm", "vocab.json", "tokenizer_config.json"):
                src = src_dir / name
                if src.exists():
                    shutil.copy2(src, out_dir / name)
            log(f"       已生成 {out_dir}（把 translate.local.backend 设为 ct2 使用）")
        except Exception as exc:
            log(f"[warn] CT2 转换失败（不影响 transformers 后端）：{exc}")

    size = sum(f.stat().st_size for f in src_dir.rglob("*") if f.is_file())
    log(f"\n完成 ✔  {src_dir}  共 {human(size)}")
    if ok:
        log("现在把 config.yaml 里 translate.provider 设为 local 即可离线翻译。")
        return 0
    log("[warn] 自检没能跑通，但文件已下载。请检查 transformers 是否装好。")
    return 6


def _selftest(src_dir: Path) -> bool:
    """真跑一句翻译，确认模型可用（比只看文件存在靠谱）。"""
    try:
        import warnings

        warnings.filterwarnings("ignore")
        from transformers import MarianMTModel, MarianTokenizer

        tok = MarianTokenizer.from_pretrained(str(src_dir), local_files_only=True)
        mdl = MarianMTModel.from_pretrained(str(src_dir), local_files_only=True).eval()
        import torch

        s = "ちょっと待って、いま何時？"
        with torch.no_grad():
            b = tok([s], return_tensors="pt", padding=True)
            out = mdl.generate(**b, num_beams=2, max_new_tokens=64, no_repeat_ngram_size=4)
        text = tok.batch_decode(out, skip_special_tokens=True)[0]
        log(f"       日 {s}")
        log(f"       中 {text}")
        # 退化检测：重复片段说明模型/分词有问题
        if _looks_degenerate(text):
            log("       [warn] 译文疑似退化（大量重复），这个模型可能不可用，换一个 --repo 试试")
            return False
        return True
    except Exception as exc:
        log(f"       [warn] 自检失败：{type(exc).__name__}: {exc}")
        return False


def _looks_degenerate(text: str) -> bool:
    if not text:
        return True
    if len(text) < 40:
        return False
    for unit in (1, 2, 3):
        if text.count(text[-unit:]) > len(text) / (unit * 2):
            return True
    return len(set(text)) < max(4, len(text) // 30)


if __name__ == "__main__":
    sys.exit(main())
