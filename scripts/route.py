#!/usr/bin/env python3
"""第零步：判断输入走哪条路——LaTeX 源码（本技能主链）还是 PDF（pdf-formula-translate）。

两种产物的差别很大，先分流再干活：

  latex  有源码，重新排版，公式/编号/交叉引用都是真 LaTeX，质量上限最高
  pdf    只有 PDF，逐块覆盖文字层，版式靠原文件，公式是原图，质量取决于 PDF 本身

用法：
    python route.py <arXiv ID | 链接 | PDF | .tex 目录 | 压缩包> [--mode auto|latex|pdf] [--json]

退出码：0 可执行；2 输入无法识别；3 强制 latex 但拿不到源码；4 强制 pdf 但没有 PDF。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import die, ok, warn  # noqa: E402

ARXIV_ID = re.compile(r"(?<![\w.])(\d{4}\.\d{4,5})(v\d+)?(?![\w.])")
ARXIV_URL = re.compile(r"arxiv\.org/(?:abs|pdf)/([^?#]+)", re.I)
PDF_SUFFIX = ".pdf"
ARCHIVE_SUFFIX = (".zip", ".tar", ".tar.gz", ".tgz", ".gz", ".tar.bz2")


def classify(source: str, mode: str) -> dict:
    """返回 {kind, mode, reason, pdf_url}；kind 是输入形态，mode 是执行分支。"""
    s = source.strip().strip('"').strip("'")
    info = {"input": source, "kind": "", "mode": "", "reason": "", "pdf_url": ""}

    # 1) 本地路径
    p = Path(s).expanduser()
    if p.exists():
        if p.is_dir():
            tex = list(p.glob("*.tex"))
            info["kind"] = "latex-dir" if tex else "dir"
            info["mode"] = "latex" if tex else "pdf"
            info["reason"] = (
                f"本地目录，含 {len(tex)} 个 .tex" if tex else "本地目录里没有 .tex，按 PDF 分支处理"
            )
            return info
        suf = "".join(p.suffixes).lower()
        if suf.endswith(PDF_SUFFIX):
            info.update(kind="pdf", mode="pdf", reason="本地 PDF")
            return info
        if suf.endswith(ARCHIVE_SUFFIX):
            info.update(kind="archive", mode="latex", reason="本地压缩包（当作 LaTeX 工程）")
            return info
        if p.suffix.lower() == ".tex":
            info.update(kind="tex", mode="latex", reason="单个 .tex 文件")
            return info
        info.update(kind="unknown", mode="", reason=f"不认识的本地文件类型：{suf}")
        return info

    # 2) arXiv 链接
    m = ARXIV_URL.search(s)
    if m:
        raw = m.group(1)
        raw = re.sub(r"\.pdf$", "", raw)
        info.update(kind="arxiv", mode="latex", pdf_url=f"https://arxiv.org/pdf/{raw}")
        info["reason"] = "arXiv 链接；先取源码，取不到再退 PDF"
        return info

    # 3) 裸 arXiv ID
    m = ARXIV_ID.search(s)
    if m and len(s) <= 40:
        info.update(kind="arxiv", mode="latex", pdf_url=f"https://arxiv.org/pdf/{m.group(0)}")
        info["reason"] = "arXiv ID；先取源码，取不到再退 PDF"
        return info

    # 4) 远程 PDF
    if s.lower().startswith(("http://", "https://")):
        if s.lower().endswith(PDF_SUFFIX) or "/pdf/" in s.lower():
            info.update(kind="pdf-url", mode="pdf", pdf_url=s, reason="远程 PDF")
            return info
        info.update(kind="url", mode="", reason="普通网页链接，无法直接判断；请给 arXiv 链接或 PDF 直链")
        return info

    info.update(kind="unknown", mode="", reason="既不是已有路径，也不是 arXiv ID / 链接")
    return info


def apply_override(info: dict, mode: str) -> dict:
    if mode == "auto":
        return info
    if mode == info["mode"]:
        return info
    # 强制 latex：arXiv 可以，纯 PDF 不行
    if mode == "latex":
        if info["kind"] in ("arxiv", "latex-dir", "archive", "tex"):
            info["mode"] = "latex"
            info["reason"] += "（用户指定 LaTeX 分支）"
            return info
        info["mode"] = "latex"
        info["reason"] = "用户强制 LaTeX 分支，但输入没有源码——需要先拿到 .tex/.zip"
        info["blocked"] = True
        return info
    # 强制 pdf：arXiv 走 PDF 直链
    if mode == "pdf":
        if info["kind"] == "arxiv":
            info["mode"] = "pdf"
            info["reason"] = "用户指定 PDF 分支，直接下 arXiv PDF"
            return info
        if info["kind"] in ("pdf", "pdf-url"):
            info["mode"] = "pdf"
            info["reason"] += "（用户指定 PDF 分支）"
            return info
        info["mode"] = "pdf"
        info["reason"] = "用户强制 PDF 分支，但输入不是 PDF——请先导出 PDF"
        info["blocked"] = True
    return info


def main() -> int:
    ap = argparse.ArgumentParser(description="判断论文来源，选择 LaTeX 或 PDF 分支")
    ap.add_argument("source")
    ap.add_argument("--mode", choices=["auto", "latex", "pdf"], default="auto")
    ap.add_argument("--json", action="store_true", help="只输出 JSON，便于上层脚本消费")
    args = ap.parse_args()

    info = apply_override(classify(args.source, args.mode), args.mode)

    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
    else:
        ok(f"输入判定：{info['kind']} → {info['mode'] or '无法处理'}")
        print(f"    理由：{info['reason']}")
        if info["mode"] == "latex":
            print("    下一步：python fetch.py <输入> --out <项目> && python preflight.py <项目>")
        elif info["mode"] == "pdf":
            print("    下一步：python pdf_mode.py <输入> --project <项目> --compare")
            print("    依赖：pdf-formula-translate 技能 + Python 3.10–3.12 + pdf2zh==1.9.11")
            print("    首次先跑 pdf_mode.py ... --install（会建 .venv-pdf 并钉好 tmt 版本）")
        if info.get("blocked"):
            warn("这条分支当前不可执行，见上面的理由。")
            return 3 if info["mode"] == "latex" else 4

    if not info["mode"]:
        return 2
    return 3 if info.get("blocked") and info["mode"] == "latex" else (4 if info.get("blocked") else 0)


if __name__ == "__main__":
    sys.exit(main())
