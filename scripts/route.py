#!/usr/bin/env python3
"""第零步：判断输入走哪条路——HTML（最快）、LaTeX 源码（最保真）还是 PDF（兜底）。

三条分支的差别：

  html   抓论文网页版（arXiv /html、ar5iv、bioRxiv、任意论文页），公式是 MathML，
         不需要 TeX、不需要编译，几秒钟出中文页面。**最快**。
  latex  有源码时重新排版，公式/编号/交叉引用/参考文献都是真 LaTeX。**质量上限最高**，
         但要跑字体注入与多轮编译，慢几分钟。
  pdf    只有 PDF 时逐块覆盖文字层，版式 1:1，公式是原图。首次还要建环境。

默认（--mode auto）按**最快可用**选：HTML → LaTeX → PDF。
但 HTML 版是 LaTeXML 的降级产物，若它缺参考文献或有转换错误，auto 会退到 LaTeX
（有源码时），并明确告诉你为什么——想要快就用 `--mode html` 强制。

用法：
    python route.py <arXiv ID | 链接 | PDF | .tex 目录 | 压缩包 | .html> [选项]

选项：
    --mode auto|html|latex|pdf|best   best = 质量优先（LaTeX → HTML → PDF）
    --offline                        不联网探测（按经验判断，arXiv 默认 LaTeX）
    --json                           机器可读
    --no-probe-timeout               兼容旧参数，等同于 --offline

退出码：0 可执行；2 输入无法识别；3 需要源码但拿不到；4 需要 PDF 但拿不到；5 需要 HTML 但拿不到。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import ok, warn  # noqa: E402
import sources  # noqa: E402

MODES = ("auto", "html", "latex", "pdf", "best")


def _next_steps(mode: str, info: dict) -> list[str]:
    if mode == "html":
        src = info.get("html_url") or info["input"]
        return [f"python html_mode.py fetch {src} --project ./项目",
                "python html_mode.py extract ./项目",
                "python pending.py ./项目 --budget 18000   # 之后按批翻译 + submit.py",
                "python html_mode.py apply ./项目 --pdf"]
    if mode == "latex":
        return ["python fetch.py <输入> --out ./项目",
                "python preflight.py ./项目",
                "python inject_cjk.py ./项目 && python extract.py ./项目",
                "python check_coverage.py ./项目   # 必须清零",
                "python apply.py ./项目 && python build.py ./项目",
                "python audit.py ./项目 --png"]
    return [f"python pdf_mode.py {info.get('pdf_url') or info['input']} --project ./项目 --compare",
            "首次要加 --install 建 .venv-pdf（Python 3.10–3.12，pdf2zh==1.9.11）"]


def decide(info: dict, mode: str, offline: bool = False) -> dict:
    kind = info["kind"]
    out = {**info, "probe": None}

    # ---- 本地输入：直接按类型定 ----
    if kind in ("latex-dir", "archive", "tex"):
        out.update(mode="latex")
        return out
    if kind == "pdf" or kind == "pdf-url":
        out.update(mode="pdf")
        return out
    if kind == "html-file":
        out.update(mode="html")
        return out
    if kind == "unknown" or kind == "dir":
        out.update(mode="")
        return out
    if kind == "doi":
        out.update(mode="pdf", blocked=True,
                   reason="DOI 需要先解析成 PDF 地址：用 doi.org 打开后把 PDF 链接贴进来，"
                          "或直接把 PDF 文件拖进来")
        return out
    if kind == "neurips":
        out.update(mode="pdf",
                   reason="NeurIPS proceedings：页面里的 PDF 链接由 pdf_mode.py 自己找")
        return out

    # ---- 只支持 PDF 的来源 ----
    if kind in ("openreview", "aclanthology", "pmlr", "cvf"):
        out.update(mode="pdf")
        return out

    # ---- HTML 优先的来源 ----
    if kind in ("ar5iv", "biorxiv", "medrxiv", "chemrxiv", "html-url"):
        out.update(mode="html")
        if mode == "latex":
            out.update(mode="latex", blocked=True,
                       reason="这类来源没有可下载的 LaTeX 源码，请用 --mode html 或 pdf")
        elif mode == "pdf":
            out.update(mode="pdf" if info.get("pdf_url") else "pdf")
            if not info.get("pdf_url"):
                out.update(blocked=True, reason="这个来源没有直接的 PDF 链接")
        return out

    # ---- arXiv：三条分支都能走 ----
    if kind == "arxiv":
        probe = None
        if mode in ("auto", "html", "best") and not offline:
            probe = sources.probe_html(info["html_url"])
            out["probe"] = probe
            if not probe.get("ok") and mode == "html":
                out.update(mode="html", blocked=True,
                           reason=f"arXiv 没有这份论文的 HTML 版（{probe.get('why')}）")
                return out
        has_html = bool(probe and probe.get("ok")) if probe is not None else None

        if mode == "html":
            out.update(mode="html", reason="按指定走 HTML 分支（最快，但可能是降级产物）")
            return out
        if mode == "latex":
            out.update(mode="latex", reason="按指定走 LaTeX 分支（源码重排，质量最高）")
            return out
        if mode == "pdf":
            out.update(mode="pdf", reason="按指定走 PDF 分支")
            return out

        if mode == "best":
            out.update(mode="latex", reason="质量优先：源码重排，公式与参考文献都是真 LaTeX")
            if has_html is False:
                out["note"] = "顺带探测：这份论文没有 HTML 版，PDF 分支仍可用"
            return out

        # auto：最快可用
        if has_html is None:                      # --offline 或探测没跑
            out.update(mode="latex",
                       reason="未联网探测，按经验走 LaTeX 分支（arXiv 一般有源码）")
            return out
        if not has_html:
            out.update(mode="latex",
                       reason=f"没有 HTML 版（{probe.get('why')}），改走源码重排")
            return out
        if probe.get("degraded"):
            out.update(mode="latex",
                       reason=f"HTML 版有转换缺陷（缺引用 {probe.get('missing_citation')} 处 / "
                              f"错误 {probe.get('error_span')} 处），auto 退到源码重排；"
                              f"想快就加 --mode html")
            return out
        out.update(mode="html",
                   reason="有可用的 HTML 版：免编译，最快（要原版式中文 PDF 用 --mode latex）")
        return out

    out.update(mode="")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="判断论文来源，选择 HTML / LaTeX / PDF 分支")
    ap.add_argument("source")
    ap.add_argument("--mode", choices=MODES, default="auto")
    ap.add_argument("--offline", action="store_true", help="不联网探测 HTML 是否可用")
    ap.add_argument("--no-probe-timeout", action="store_true", help="兼容旧参数，等同 --offline")
    ap.add_argument("--json", action="store_true", help="只输出 JSON，便于上层脚本消费")
    args = ap.parse_args()

    info = decide(sources.resolve(args.source), args.mode,
                  offline=args.offline or args.no_probe_timeout)

    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
    else:
        names = {"html": "HTML（最快，免编译）", "latex": "LaTeX 源码（最保真）",
                 "pdf": "PDF（版式保留）"}
        ok(f"输入判定：{info['kind']} → {names.get(info['mode'], '无法处理')}")
        print(f"    理由：{info['reason']}")
        if info.get("probe", None) and info["probe"].get("ok"):
            p = info["probe"]
            print(f"    HTML：{p['bytes'] // 1024} KB，"
                  f"缺引用 {p.get('missing_citation')} / 错误 {p.get('error_span')}"
                  f"{'（降级产物）' if p.get('degraded') else ''}")
        if info["mode"]:
            print("    下一步：")
            for line in _next_steps(info["mode"], info):
                print(f"      {line}")
        if info.get("blocked"):
            warn("这条分支当前不可执行，见上面的理由。")
            return {"latex": 3, "pdf": 4, "html": 5}.get(info["mode"], 2)

    if not info["mode"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
