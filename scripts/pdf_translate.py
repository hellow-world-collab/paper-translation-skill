#!/usr/bin/env python3
"""内置的 PDF 翻译器：直接调 pdf2zh，不依赖外部技能。

`pdf_mode.py` 会优先用这个脚本；找不到时才退回 `~/.workbuddy/skills/pdf-formula-translate`。
这样仓库 clone 下来就是自包含的——只需要 Python 3.10–3.12 和 `pdf2zh==1.9.11`。

用法（由 pdf_mode.py 调用，也可单独用）：
    python pdf_translate.py 论文.pdf -o ./out -li en -lo zh -s bing -t 4

踩过的两个坑（都已在这里处理）：
  1. pdf2zh 1.9.11 的 `high_level.translate()` 自己不加载版式模型，不传 model 会在
     `translate_stream` 里 `AttributeError: 'NoneType' object has no attribute 'predict'`；
     必须显式 `OnnxModel.load_available()`。
  2. `translate()` 读完位于系统临时目录的输入 PDF 会 `unlink` 掉它（"Cleaned temp file"），
     所以调用方要保证传进来的是项目目录里的副本——这件事由 pdf_mode.py 负责。
"""

from __future__ import annotations

import argparse
import os
import sys


def parse_pages(spec: str):
    """'1,3-5' → [0, 2, 3, 4]（pdf2zh 内部按 0 起算）。"""
    if not spec:
        return None
    pages: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            pages.extend(range(int(a) - 1, int(b)))
        else:
            pages.append(int(part) - 1)
    return pages


def parse_env(pairs: list[str]) -> dict:
    envs: dict[str, str] = {}
    for kv in pairs or []:
        if "=" in kv:
            k, v = kv.split("=", 1)
            envs[k.strip()] = v.strip()
    return envs


def main() -> int:
    ap = argparse.ArgumentParser(description="用 pdf2zh 逐块翻译 PDF，保留版式与公式")
    ap.add_argument("input", help="PDF 路径")
    ap.add_argument("-o", "--output", default="", help="输出目录")
    ap.add_argument("-li", "--lang-in", default="en")
    ap.add_argument("-lo", "--lang-out", default="zh")
    ap.add_argument("-s", "--service", default="bing", help="翻译后端，默认 bing")
    ap.add_argument("-t", "--thread", type=int, default=4)
    ap.add_argument("--pages", default="", help="只翻指定页，1 起，如 1,3-5")
    ap.add_argument("--env", action="append", default=[], help="额外环境变量 KEY=VALUE")
    ap.add_argument("--ignore-cache", action="store_true")
    ap.add_argument("--compatible", action="store_true", help="输出 PDF/A 兼容格式")
    ap.add_argument("--skip-install", action="store_true", help="兼容参数，当前实现不做安装")
    args = ap.parse_args()

    if not os.path.isfile(args.input):
        print(f"[错误] 找不到输入 PDF：{args.input}", file=sys.stderr)
        return 2

    try:
        from pdf2zh import translate
        from pdf2zh.doclayout import OnnxModel
    except ImportError as exc:  # noqa: BLE001
        print(f"[错误] 导不进 pdf2zh（{exc}）。用 pdf_mode.py --install 建 .venv-pdf。", file=sys.stderr)
        return 3

    try:
        model = OnnxModel.load_available()
    except Exception as exc:  # noqa: BLE001
        print(f"[错误] 版式模型加载失败（{exc}）。首次运行需要联网下载约 40 MB；"
              f"国内可设 HF_ENDPOINT=https://hf-mirror.com", file=sys.stderr)
        return 4

    out_dir = args.output or os.path.dirname(os.path.abspath(args.input)) or "."
    os.makedirs(out_dir, exist_ok=True)
    envs = parse_env(args.env)
    for k, v in envs.items():
        os.environ.setdefault(k, v)

    print(f"翻译 {os.path.basename(args.input)}：{args.lang_in} → {args.lang_out}"
          f"（{args.service}，{args.thread} 线程）")
    result = translate(
        files=[args.input],
        output=out_dir,
        pages=parse_pages(args.pages),
        lang_in=args.lang_in,
        lang_out=args.lang_out,
        service=args.service,
        thread=args.thread,
        envs=envs,
        compatible=args.compatible,
        ignore_cache=args.ignore_cache,
        model=model,
    )
    for mono, dual in result:
        print(f"  译文 {mono}")
        print(f"  双语 {dual}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
