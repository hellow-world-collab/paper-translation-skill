#!/usr/bin/env python3
"""render_compare.py — 生成「左原文 / 右译文」页面级双栏对照 HTML。

与 render_html.py 的区别：那一个是段落级重排（KaTeX 公式、图文重组）；
本脚本不做任何排版改动，两份 PDF 原样并排，
按章节书签建立页号锚点，实现联动滚动 / 各自滚动。

用法：
    python render_compare.py --root <项目目录> \
        --original .arz/original_arxiv.pdf \
        --translated out/main.pdf \
        --out html/compare

产物：
    <out>/index.html          目录版（PDF 数据在 <out>/pdf-en.js、pdf-zh.js）
    <out>/pdf-*.js            base64 后的 PDF，按需 <script> 载入
可选：
    --single-file <路径>      另存单文件版（PDF 数据一并内联）

渲染架构（v1.0.2）：
    页面不是预先烘好的图片，而是把 PDF 交给浏览器里的 pdf.js 现渲。
    canvas 的位图尺寸按「CSS 宽度 × devicePixelRatio」实时计算，缩放一次重绘一次，
    所以任何缩放比下都是 1:1 物理像素——和浏览器内置 PDF 阅读器同一路线。
    早期版本用 PyMuPDF 预渲染位图（1.7× ≈ 1041 px）再让 CSS 拉伸，
    放大 1.73 倍就是糊的，这才是「HTML 不如 PDF 清楚」的根因；现方案已彻底换掉。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path

try:
    import pymupdf
except ImportError:  # pragma: no cover
    import fitz as pymupdf

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
TEMPLATE = SKILL / "assets" / "compare_template.html"
PDFJS = SKILL / "assets" / "pdf.min.js"
PDFJS_WORKER = SKILL / "assets" / "pdf.worker.min.js"


# ---------------------------------------------------------------- 工具

def human(n: float) -> str:
    return f"{n/1048576:.1f} MB" if n >= 1048576 else f"{n/1024:.0f} KB"


def pick(root: Path, explicit: str | None, candidates: list[str], label: str) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = root / p
        if not p.exists():
            sys.exit(f"[x] 找不到{label}：{p}")
        return p
    for c in candidates:
        p = root / c
        if p.exists():
            return p
    sys.exit(f"[x] 找不到{label}，请用 --original/--translated 指定")


def clean_title(t: str) -> str:
    t = t.replace("``", "“").replace("''", "”")
    t = t.replace("`", "“", 1) if t.count("`") == 1 else t.replace("`", "")
    t = re.sub(r"\s+", " ", t).strip()
    return t


# ---------------------------------------------------------------- 交付

def page_dims(doc) -> list[dict]:
    """每页的物理尺寸（pt）。前端据此设 aspect-ratio，图未渲染时也占好位、不跳动。"""
    return [{"w": round(p.rect.width, 2), "h": round(p.rect.height, 2)} for p in doc]


def pdf_payload(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def write_pdf_js(outdir: Path, key: str, payload: str) -> str:
    """把 base64 PDF 写成独立 js。用 <script> 载入而不是 fetch：
    file:// 下 fetch 被 CORS 挡死，classic script 不受限制。"""
    name = f"pdf-{key}.js"
    body = f'window.__PDF=window.__PDF||{{}};window.__PDF["{key}"]="{payload}";'
    (outdir / name).write_text(body, encoding="utf-8")
    return name


def page_words(doc) -> list[list[list]]:
    """每页的文字行，行内带每个词的横向范围：[[y0, y1, [[x0, x1, 词, [字x0…]], ...]], ...]。

    x 按页宽、y 按页高归一化（0~1），前端按百分比定位，缩放时不用重算。
    前端另铺了一层透明 textLayer 负责划选复制；这里的数组是给搜索用的——
    高亮框、滚动条刻度、跳转定位都靠它，且未渲染的页也能搜到。

    第 4 项是词内每个字符自身的起点 x。没有它，前端只能按「字符数平均」在词框里
    内插，混排词（"SimpleVLA-RL:"、中文夹 VLA）就会偏半个字——中文尤其明显，
    因为它整行没有空格，一个词就是一行。存了字符坐标后，命中框直接取真实字形边界。

    用 rawdict 而不是 words：后者给的 (块号, 行号) 在这类 PDF 里会跨行重复，
    同一组里混进页面上下两端的词，算出来的行高能到半页；rawdict 的字符是
    真正按行组织的，逐字符框拼成词框，行高就正常了。
    """
    out: list[list[list]] = []
    for page in doc:
        W = page.rect.width or 1.0
        H = page.rect.height or 1.0
        rows: list[list] = []
        try:
            data = page.get_text("rawdict")
        except Exception:
            out.append(rows)
            continue
        for blk in data.get("blocks", []):
            if blk.get("type") != 0:      # 图片块跳过
                continue
            for ln in blk.get("lines", []):
                words: list[list] = []
                cur: list | None = None
                for sp in ln.get("spans", []):
                    for ch in sp.get("chars", []):
                        c = ch.get("c", "")
                        if not c.strip():          # 空格 / 制表符：断开当前词
                            cur = None
                            continue
                        bb = ch.get("bbox") or (0, 0, 0, 0)
                        if cur is None:
                            cur = [bb[0], bb[1], bb[2], bb[3], c, [bb[0]]]
                            words.append(cur)
                        else:
                            cur[2] = max(cur[2], bb[2])
                            cur[3] = max(cur[3], bb[3])
                            cur[4] += c
                            cur[5].append(bb[0])
                words = [w for w in words if w[4].strip()]
                if not words:
                    continue
                y0 = min(w[1] for w in words)
                y1 = max(w[3] for w in words)
                if y1 - y0 < 1:
                    y1 = y0 + 1
                rows.append([
                    round(y0 / H, 4), round(y1 / H, 4),
                    [[round(w[0] / W, 4), round(w[2] / W, 4), w[4],
                      [round(x / W, 4) for x in w[5]]] for w in words],
                ])
        out.append(rows)
    return out


# ---------------------------------------------------------------- 锚点

def heading_frac(doc, page_no: int, title: str, dest=None) -> float:
    """标题在该页的纵向位置（0~1，按页高归一）。

    优先用书签自带的跳转坐标（PDF 目标点 y 轴自下而上）；没有坐标时退回按标题文字搜索。
    都拿不到就返回 0，退化为「页首对齐」。
    """
    if not (1 <= page_no <= len(doc)):
        return 0.0
    page = doc[page_no - 1]
    H = page.rect.height

    if isinstance(dest, dict):
        pt = dest.get("to")
        if pt is not None and hasattr(pt, "y"):
            return max(0.0, min(0.92, (H - pt.y) / H))

    probe = title.strip("“”\"'`’‘ ")
    for n in (30, 22, 16, 11, 7, 4, 2):
        s = probe[:n].strip()
        if len(s) < 2 or (len(s) < 4 and not re.search(r"[\u4e00-\u9fff]", s)):
            continue
        try:
            rects = [r for r in page.search_for(s) if r.y0 > 0.07 * H]
        except Exception:
            rects = []
        if rects:
            y = min(r.y0 for r in rects)
            return max(0.0, min(0.92, y / H))
    return 0.0


def build_anchors(a_doc, b_doc) -> tuple[list[list[float]], list[dict]]:
    """按两版 PDF 的书签顺序建立页号锚点。返回 (anchors, toc_rows)。

    锚点形如 [原文位置, 译文位置]，位置 = 页序号(0 基) + 标题在该页的纵向百分比，
    这样「同一节在两栏里对齐」而不是「同一页对齐」（两份页数不同，页级对齐本来就无解）。
    """
    ta, tb = a_doc.get_toc(simple=False), b_doc.get_toc(simple=False)
    na, nb = len(a_doc), len(b_doc)

    if len(ta) != len(tb) or not ta:
        print(f"[!] 书签数量不一致（原文 {len(ta)} / 译文 {len(tb)}），改用等比例映射")
        return [[0.0, 0.0], [float(na - 1), float(nb - 1)]], []

    anchors: list[list[float]] = [[0.0, 0.0]]
    rows: list[dict] = []
    last_a, last_b = 0.0, 0.0
    c1 = c2 = 0
    hit = 0
    for ea, eb in zip(ta, tb):
        lva, tia, pa, da = ea[0], ea[1], ea[2], (ea[3] if len(ea) > 3 else None)
        lvb, tib, pb, db = eb[0], eb[1], eb[2], (eb[3] if len(eb) > 3 else None)
        if lva == 1:
            c1 += 1
            c2 = 0
            no = str(c1)
        else:
            c2 += 1
            no = f"{c1}.{c2}" if c1 else str(c2)
        fa = heading_frac(a_doc, pa, clean_title(tia), da)
        fb = heading_frac(b_doc, pb, clean_title(tib), db)
        ia, ib = (pa - 1) + fa, (pb - 1) + fb
        hit += 1 if (fa or fb) else 0
        rows.append({"lv": 1 if lva == 1 else 2, "no": no,
                     "en": clean_title(tia), "zh": clean_title(tib),
                     "p": pa, "q": pb, "a": round(ia, 3), "b": round(ib, 3)})
        if ia > last_a and ib > last_b:
            anchors.append([round(ia, 3), round(ib, 3)])
            last_a, last_b = ia, ib
        elif ia >= last_a and ib >= last_b:
            continue  # 同一页上的多个小节，锚点已被覆盖
    anchors.append([float(na - 1), float(nb - 1)])
    print(f"    锚点 {len(anchors)} 个（书签 {len(rows)} 条，其中 {hit} 条定位到行内）")
    return anchors, rows


# ---------------------------------------------------------------- 组装

def fill(template: str, meta: dict, title: str, sub: str,
         pdfjs_tag: str, worker_tag: str, data_tag: str) -> str:
    """把数据塞进模板。

    先替换小占位符、最后塞几 MB 的大块：反过来的话，pdf.js 源码里万一出现
    "__META__" 之类字样，后续 replace 就会改错地方。
    """
    html = template.replace("__TITLE__", title.replace("&", "&amp;").replace("<", "&lt;"))
    html = html.replace("__SUB__", sub.replace("&", "&amp;").replace("<", "&lt;"))
    blob = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = html.replace("__META__", blob)
    html = html.replace("__PDFJS__", pdfjs_tag)
    html = html.replace("__PDFWORKER__", worker_tag)
    html = html.replace("__PDFDATA__", data_tag)
    return html


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--original")
    ap.add_argument("--translated")
    ap.add_argument("--out", default="html/compare")
    ap.add_argument("--title")
    ap.add_argument("--sub")
    ap.add_argument("--single-file")
    # 1.0.2 起页面由 pdf.js 现渲，不再预生成位图；这几个参数留着只为不弄坏旧脚本
    for legacy in ("--scale", "--quality", "--single-scale", "--single-quality", "--format"):
        ap.add_argument(legacy, help=argparse.SUPPRESS)
    args = ap.parse_args()
    if any(getattr(args, k) is not None for k in
           ("scale", "quality", "single_scale", "single_quality", "format")):
        print("[i] --scale/--quality/--format 已废弃并被忽略：页面改为 pdf.js 现渲，"
              "分辨率随缩放自适应", flush=True)

    root = Path(args.root).resolve()
    out = (root / args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    a_pdf = pick(root, args.original,
                 [".arz/original_arxiv.pdf", "base/main.pdf", "out/main.pdf"], "原文 PDF")
    b_pdf = pick(root, args.translated,
                 ["out/main.pdf", "SimpleVLA-RL_zh.pdf"], "译文 PDF")

    a_doc, b_doc = pymupdf.open(a_pdf), pymupdf.open(b_pdf)
    print(f"[1/5] 原文 {a_pdf.name}  {len(a_doc)} 页")
    print(f"      译文 {b_pdf.name}  {len(b_doc)} 页")

    print("[2/5] 建立书签锚点")
    anchors, rows = build_anchors(a_doc, b_doc)

    print("      抽取文字层（供全文搜索）")
    en_lines, zh_lines = page_words(a_doc), page_words(b_doc)
    n_words = sum(len(r[2]) for pg in en_lines for r in pg) + \
              sum(len(r[2]) for pg in zh_lines for r in pg)
    print(f"      文字词 {n_words} 个")

    print("[3/5] 打包 PDF（base64）")
    payloads = {"en": pdf_payload(a_pdf), "zh": pdf_payload(b_pdf)}
    for k, v in payloads.items():
        print(f"      {k}: {human(len(v) * 3 / 4)} 原始 → {human(len(v))} base64")

    stem = b_pdf.stem.replace("_中文版", "")
    title = args.title or stem.replace("_", " · ")
    if args.sub:
        sub = args.sub
    elif "_" in stem:
        sub = f"arXiv {stem.rsplit('_', 1)[-1]} · 左原文 / 右译文"
    else:
        sub = "左原文 / 右译文"

    tpl = TEMPLATE.read_text(encoding="utf-8")
    pdfjs = PDFJS.read_text(encoding="utf-8")
    worker = PDFJS_WORKER.read_text(encoding="utf-8")
    for name, blob in (("pdf.min.js", pdfjs), ("pdf.worker.min.js", worker)):
        if "</script" in blob.lower():
            sys.exit(f"[x] {name} 里出现 </script，无法整段内联，请换构建或改成分文件引用")
    # worker 必须以文本形式留在 DOM 里：file:// 下 new Worker('pdf.worker.min.js')
    # 会被当跨源，只能自己拼 Blob URL，所以源码得先随页面带过去。
    pdfjs_tag = f"<script>{pdfjs}</script>"
    worker_tag = f'<script type="text/plain" id="pdfjs-worker">{worker}</script>'

    print("[4/5] 渲染版面数据")
    meta = {"en": {"pages": page_dims(a_doc), "lines": en_lines},
            "zh": {"pages": page_dims(b_doc), "lines": zh_lines},
            "anchors": anchors, "toc": rows}

    # 上一次可能是位图版，pages/ 里的旧图留着既占地方又容易被误认
    stale_dir = out / "pages"
    if stale_dir.is_dir():
        old = re.compile(r"^(?:en|zh)-\d{3}\.(?:jpg|jpeg|png|webp)$", re.I)
        for f in stale_dir.iterdir():
            if f.is_file() and old.match(f.name):
                f.unlink()
        if not any(stale_dir.iterdir()):
            stale_dir.rmdir()

    names = {k: write_pdf_js(out, k, v) for k, v in payloads.items()}
    data_tag = "".join(f'<script src="{n}"></script>' for n in names.values())
    index = out / "index.html"
    index.write_text(fill(tpl, meta, title, sub, pdfjs_tag, worker_tag, data_tag),
                     encoding="utf-8")
    print(f"[5/5] 写出 {index}  ({human(index.stat().st_size)})")
    for n in names.values():
        print(f"      {n}  ({human((out / n).stat().st_size)})")

    if args.single_file:
        print("      生成单文件版（PDF 与引擎全内联）…")
        inline = "window.__PDF={" + ",".join(
            f'"{k}":"{v}"' for k, v in payloads.items()) + "};"
        single_tag = f"<script>{inline}</script>"
        single = Path(args.single_file)
        if not single.is_absolute():
            single = root / single
        single.write_text(fill(tpl, meta, title, sub, pdfjs_tag, worker_tag, single_tag),
                          encoding="utf-8")
        print(f"      写出 {single}  ({human(single.stat().st_size)})")


if __name__ == "__main__":
    main()
