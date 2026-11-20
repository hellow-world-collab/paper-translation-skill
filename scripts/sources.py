#!/usr/bin/env python3
"""论文来源解析：把一个链接 / ID / 本地文件认成「哪家出版社、有哪些可下格式」。

只做**离线判断**（正则 + 本地文件系统），不发网络请求——探测 HTML 是否真的存在
由 route.py 用一次轻量 GET 完成，这样 sources.resolve() 可以被任何脚本安全复用。

返回结构（dict）：
    kind       来源类型，见下表
    arxiv_id   arXiv ID（带版本号），非 arXiv 为 None
    html_url   可直接抓正文 HTML 的地址（有则 HTML 分支可用）
    pdf_url    可直接抓 PDF 的地址
    mode_hint  这家来源最可能走哪条路：html / latex / pdf
    reason     一句话说明，直接打给用户看

支持（按可用格式从多到少）：
    arxiv       源码 + HTML + PDF   → 三条分支都能走
    ar5iv       HTML（LaTeXML 转的，覆盖老论文）
    openreview  PDF（forum 页是 JS 渲染的，抓不到正文）
    biorxiv / medrxiv / chemrxiv   全文 HTML + PDF
    aclanthology / pmlr / cvf / neurips   PDF（部分有 HTML 页，除摘要外没正文）
    期刊 DOI（Nature / Science / Springer / Elsevier / IEEE）  PDF，且常年在付费墙后
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

ARXIV_ID = re.compile(r"(?<![\w.])(\d{4}\.\d{4,5}(?:v\d+)?)(?![\w.])")
ARXIV_OLD_ID = re.compile(r"(?<![\w.])([a-z][a-z\-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)(?![\w.])")
ARXIV_URL = re.compile(r"arxiv\.org/(?:abs|pdf|html)/([^?#\s]+)", re.I)
AR5IV_URL = re.compile(r"ar5iv(?:\.labs\.arxiv\.org|\.org)/(?:html|abs)/([^?#\s]+)", re.I)

PDF_SUFFIX = ".pdf"
ARCHIVE_SUFFIX = (".zip", ".tar", ".tar.gz", ".tgz", ".gz", ".tar.bz2", ".tar.xz")
HTML_SUFFIX = (".html", ".htm", ".xhtml")

OPENREVIEW_ID = re.compile(r"openreview\.net/(?:forum|pdf)\?(?:.*&)?id=([\w\-]+)", re.I)
BIORXIV = re.compile(
    r"(?:www\.)?(biorxiv|medrxiv|chemrxiv)\.org/content/(10\.\d{4,9}/[^\s?#]+?)(?:v\d+)?(?:\.full(?:\.pdf)?|\.full-text)?(?:[?#].*)?$",
    re.I)
ACL = re.compile(r"aclanthology\.org/([\w.\-]+?)/?(?:[?#].*)?$", re.I)
PMLR = re.compile(r"proceedings\.mlr\.press/(v\d+)/([\w.\-]+?)\.html", re.I)
CVF = re.compile(r"openaccess\.thecvf\.com/content/(.+?)/html/([\w.\-]+?)\.html", re.I)
NEURIPS = re.compile(
    r"papers\.nips\.cc/paper_files/paper/(\d{4})/(?:hash|file)/([\w\-]+?)-(?:Abstract|Paper)[\w\-]*\.html?", re.I)
DOI = re.compile(r"\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)\b")


def _blank(source: str) -> dict:
    return {"input": source, "kind": "", "arxiv_id": None, "html_url": None,
            "pdf_url": None, "mode_hint": "", "reason": ""}


def _arxiv(idpart: str) -> dict:
    """arXiv 条目：源码、HTML、PDF 三种都有，具体走哪条由 route.py 探测后决定。"""
    aid = idpart.strip().rstrip("/")
    aid = re.sub(r"\.pdf$", "", aid, flags=re.I)
    return {
        "kind": "arxiv", "arxiv_id": aid,
        "html_url": f"https://arxiv.org/html/{aid}",
        "pdf_url": f"https://arxiv.org/pdf/{aid}",
        "mode_hint": "latex",
        "reason": "arXiv 条目：源码 / HTML / PDF 都能拿到",
    }


def resolve(source: str) -> dict:
    s = (source or "").strip().strip('"').strip("'")
    out = _blank(source)
    if not s:
        out.update(kind="unknown", reason="输入为空")
        return out

    # ---------- 1. 本地路径 ----------
    p = Path(s).expanduser()
    if p.exists():
        if p.is_dir():
            tex = list(p.glob("*.tex"))
            out.update(kind="latex-dir" if tex else "dir", mode_hint="latex" if tex else "pdf",
                       reason=f"本地目录，含 {len(tex)} 个 .tex" if tex else "本地目录里没有 .tex")
            return out
        suf = "".join(p.suffixes).lower()
        if suf.endswith(PDF_SUFFIX):
            out.update(kind="pdf", pdf_url=str(p), mode_hint="pdf", reason="本地 PDF")
        elif suf.endswith(ARCHIVE_SUFFIX):
            out.update(kind="archive", mode_hint="latex", reason="本地压缩包（当作 LaTeX 工程）")
        elif p.suffix.lower() == ".tex":
            out.update(kind="tex", mode_hint="latex", reason="单个 .tex 文件")
        elif suf.endswith(HTML_SUFFIX):
            out.update(kind="html-file", html_url=str(p), mode_hint="html", reason="本地 HTML 页面")
        else:
            out.update(kind="unknown", reason=f"不认识的本地文件类型：{suf}")
        return out

    if not s.lower().startswith(("http://", "https://")):
        # ---------- 2. 裸 arXiv ID ----------
        m = ARXIV_ID.search(s) or ARXIV_OLD_ID.search(s)
        if m and len(s) <= 48:
            out.update(_arxiv(m.group(1)))
            return out
        if DOI.search(s) and len(s) <= 80:
            out.update(kind="doi", mode_hint="pdf", reason=f"DOI {DOI.search(s).group(1)}：需要先解析成 PDF 地址")
            return out
        out.update(kind="unknown", reason="既不是已有路径，也不是 arXiv ID / 链接")
        return out

    # ---------- 3. 远程 ----------
    # ar5iv 的主机名里含 arxiv.org，必须比 arXiv 规则先判，否则会被当成 arXiv 条目
    m = AR5IV_URL.search(s)
    if m:
        aid = re.sub(r"\.pdf$", "", m.group(1), flags=re.I)
        out.update(kind="ar5iv", arxiv_id=aid,
                   html_url=f"https://ar5iv.labs.arxiv.org/html/{aid}",
                   pdf_url=f"https://arxiv.org/pdf/{aid}",
                   mode_hint="html", reason="ar5iv（LaTeXML 给老论文转的 HTML）：走 HTML 分支")
        return out
    m = ARXIV_URL.search(s)
    if m:
        out.update(_arxiv(m.group(1)))
        return out

    m = OPENREVIEW_ID.search(s)
    if m:
        oid = m.group(1)
        out.update(kind="openreview", pdf_url=f"https://openreview.net/pdf?id={oid}",
                   mode_hint="pdf",
                   reason="OpenReview：forum 页是 JS 渲染的、抓不到正文，用官方 PDF")
        return out

    m = BIORXIV.search(s)
    if m:
        host, rest = m.group(1).lower(), m.group(2)
        base = f"https://www.{host}.org/content/{rest}"
        out.update(kind=host, html_url=f"{base}.full", pdf_url=f"{base}.full.pdf",
                   mode_hint="html", reason=f"{host} 全文页是静态 HTML：走 HTML 分支")
        return out

    m = ACL.search(s)
    if m and not s.rstrip("/").lower().endswith(".pdf"):
        out.update(kind="aclanthology", pdf_url=f"https://aclanthology.org/{m.group(1)}.pdf",
                   mode_hint="pdf", reason="ACL Anthology：只有 PDF")
        return out

    m = PMLR.search(s)
    if m:
        out.update(kind="pmlr", pdf_url=f"https://proceedings.mlr.press/{m.group(1)}/{m.group(2)}.pdf",
                   mode_hint="pdf", reason="PMLR：只有 PDF")
        return out

    m = CVF.search(s)
    if m:
        out.update(kind="cvf",
                   pdf_url=f"https://openaccess.thecvf.com/content/{m.group(1)}/papers/{m.group(2)}.pdf",
                   mode_hint="pdf", reason="CVF Open Access：只有 PDF")
        return out

    m = NEURIPS.search(s)
    if m:
        out.update(kind="neurips", mode_hint="pdf",
                   reason="NeurIPS proceedings：PDF 地址要从页面里找")
        return out

    if s.lower().endswith(PDF_SUFFIX) or "/pdf/" in s.lower():
        out.update(kind="pdf-url", pdf_url=s, mode_hint="pdf", reason="远程 PDF")
        return out

    out.update(kind="html-url", html_url=s, mode_hint="html", reason="普通网页：按 HTML 正文抓取")
    return out


def http_get(url: str, timeout: int = 60, limit: int | None = None) -> tuple[str, str, bytes]:
    """抓一个 URL，返回 (最终地址, Content-Type, 字节)。禁用压缩，免得自己解 gzip。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "identity",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read() if limit is None else r.read(limit)
        return r.geturl(), r.headers.get("Content-Type", ""), data


def probe_html(url: str, timeout: int = 20) -> dict:
    """探测某个地址有没有可用的论文正文 HTML，并顺带评估转换质量。

    `degraded=True` 表示这份 HTML 是 LaTeXML 的失败品：参考文献整个丢掉
    （`ltx_missing_citation` 满天飞）或有 `ltx_ERROR` 残渣。这时宁可多花几分钟
    走源码重排，也不要交一份没有参考文献的中文页。
    """
    try:
        final, ctype, blob = http_get(url, timeout=timeout)
    except urllib.error.HTTPError as exc:
        return {"ok": False, "why": f"HTTP {exc.code}"}
    except Exception as exc:                               # noqa: BLE001
        return {"ok": False, "why": f"连不上（{type(exc).__name__}）"}
    if "html" not in ctype.lower():
        return {"ok": False, "why": f"不是 HTML（{ctype}）"}
    html = blob.decode("utf-8", errors="replace")
    if "ltx_page_main" not in html and "<article" not in html and "<main" not in html:
        return {"ok": False, "why": "页面里没有正文容器（可能只是摘要页）"}
    missing = html.count("ltx_missing_citation")
    errors = html.count("ltx_ERROR")
    # 少量 ltx_ERROR 是转换器对个别命令的抱怨，不影响阅读；真正致命的是
    # 参考文献整体转换失败——那时 `<cite>` 里全是 `dreamerv3` 这样的原始键。
    return {"ok": True, "url": final, "bytes": len(blob),
            "missing_citation": missing, "error_span": errors,
            "degraded": missing > 20 or errors > 10}


if __name__ == "__main__":  # 便于手测：python sources.py <任意输入>
    import json
    import sys

    print(json.dumps(resolve(sys.argv[1] if len(sys.argv) > 1 else ""), ensure_ascii=False, indent=2))
