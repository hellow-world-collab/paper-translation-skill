#!/usr/bin/env python3
"""render_compare.py — 生成「左原文 / 右译文」页面级双栏对照 HTML。

与 render_html.py 的区别：那一个是段落级重排（KaTeX 公式、图文重组）；
本脚本不做任何排版改动，直接把两份 PDF 逐页渲成图，左右并排，
按章节书签建立页号锚点，实现联动滚动 / 各自滚动。

用法：
    python render_compare.py --root <项目目录> \
        --original .arz/original_arxiv.pdf \
        --translated out/main.pdf \
        --out html/compare

产物：
    <out>/index.html          目录版（图片在 <out>/pages/，首屏快）
    <out>/pages/*.webp        默认 WebP；没有 Pillow 时回退 .jpg
可选：
    --single-file <路径>      另存单文件版（图片内联为 data URI）

关于清晰度（v1.0.2）：
    页面是位图，浏览器打开 PDF 是矢量渲染，位图永远比不过，只能把差距压到看不见。
    两个变量：源图分辨率、压缩质量。
    - 单栏视图最宽 900 CSS px（模板 fitWidth 的上限），高分屏 dpr=2 需要 1800 物理像素，
      所以源图宽度必须 ≥1800 px。A4 宽 612 pt → scale 3.0 才够（旧默认 1.7 只有 1041 px，
      被浏览器放大 1.7 倍，这是「HTML 比 PDF 糊」的真正原因）。
    - JPEG q78 会在笔画边缘产生振铃，细笔画汉字尤其明显；WebP q88 同等体积下干净得多。
      实测 42 页：1.7×JPEG78=6.1 MB，3.0×WebP88=9.5 MB —— 分辨率翻倍只多 56%。
      对照组：2.6×JPEG q90 4:4:4 要 17.6 MB，纯亏，故不采用。
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
from pathlib import Path

try:
    import pymupdf
except ImportError:  # pragma: no cover
    import fitz as pymupdf

try:  # WebP 只能靠 Pillow 编，PyMuPDF 的 tobytes 只认 png/jpg/psd 一类
    from PIL import Image

    HAS_PIL = True
except ImportError:  # pragma: no cover
    HAS_PIL = False

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
TEMPLATE = SKILL / "assets" / "compare_template.html"


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


# ---------------------------------------------------------------- 渲染

_FALLBACK_WARNED = False


def encode_page(pix, fmt: str, quality: int) -> tuple[bytes, str, str]:
    """把 Pixmap 编成字节，返回 (数据, 扩展名, MIME)。

    webp 走 Pillow（PyMuPDF 只支持 png/jpg/pnm/psd 等）；没装 Pillow 就退到
    高质量 JPEG，并提示一次——不能静默降级，否则又变成「看起来能用但更糊」。
    """
    global _FALLBACK_WARNED
    if fmt == "webp":
        if not HAS_PIL:
            if not _FALLBACK_WARNED:
                print("    [!] 没装 Pillow，无法输出 WebP，本次回退 JPEG q95。"
                      "\n        想要更清晰：python -m pip install pillow")
                _FALLBACK_WARNED = True
            return pix.tobytes("jpg", jpg_quality=95), "jpg", "image/jpeg"
        im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        buf = io.BytesIO()
        im.save(buf, "WEBP", quality=quality, method=4)
        return buf.getvalue(), "webp", "image/webp"
    if fmt == "png":
        return pix.tobytes("png"), "png", "image/png"
    return pix.tobytes("jpg", jpg_quality=quality), "jpg", "image/jpeg"


def render_pdf(doc, outdir: Path, prefix: str, scale: float, quality: int,
               inline: bool, fmt: str = "webp") -> list[dict]:
    pages: list[dict] = []
    total = 0
    mat = pymupdf.Matrix(scale, scale)
    if not inline:
        outdir.mkdir(parents=True, exist_ok=True)
        # 上一次可能是别的格式，同名前缀的旧图留着会被误当成新产物
        stale = re.compile(rf"^{re.escape(prefix)}-\d{{3}}\.(?:jpg|jpeg|png|webp)$", re.I)
        for old in outdir.iterdir():
            if old.is_file() and stale.match(old.name):
                old.unlink()
    for i, page in enumerate(doc):
        pix = page.get_pixmap(matrix=mat, alpha=False)
        raw, ext, mime = encode_page(pix, fmt, quality)
        total += len(raw)
        name = f"{prefix}-{i+1:03d}.{ext}"
        if inline:
            src = f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
        else:
            (outdir / name).write_bytes(raw)
            src = f"{outdir.name}/{name}"
        pages.append({"src": src, "w": pix.width, "h": pix.height})
    print(f"    {prefix}: {len(pages)} 页  {human(total)}  ({pix.width}x{pix.height})")
    return pages


def page_words(doc) -> list[list[list]]:
    """每页的文字行，行内带每个词的横向范围：[[y0, y1, [[x0, x1, 词], ...]], ...]。

    x 按页宽、y 按页高归一化（0~1），前端按百分比定位，缩放时不用重算。
    页面是图片，正文选不中、也搜不到；把文字层存成小数组后，前端能像浏览器一样
    逐词高亮，而不是只能画一整行横带。

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
                            cur = [bb[0], bb[1], bb[2], bb[3], c]
                            words.append(cur)
                        else:
                            cur[2] = max(cur[2], bb[2])
                            cur[3] = max(cur[3], bb[3])
                            cur[4] += c
                words = [w for w in words if w[4].strip()]
                if not words:
                    continue
                y0 = min(w[1] for w in words)
                y1 = max(w[3] for w in words)
                if y1 - y0 < 1:
                    y1 = y0 + 1
                rows.append([
                    round(y0 / H, 4), round(y1 / H, 4),
                    [[round(w[0] / W, 4), round(w[2] / W, 4), w[4]] for w in words],
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

def fill(template: str, meta: dict, title: str, sub: str) -> str:
    blob = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = template.replace("__META__", blob)
    html = html.replace("__TITLE__", title.replace("&", "&amp;").replace("<", "&lt;"))
    html = html.replace("__SUB__", sub.replace("&", "&amp;").replace("<", "&lt;"))
    return html


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--original")
    ap.add_argument("--translated")
    ap.add_argument("--out", default="html/compare")
    # 默认 3.0 是按「单栏视图最宽 900 CSS px × dpr 2 ≈ 1800 物理像素」反推的，
    # 1.7 那版只有 1041 px，在缩放屏上会被放大 1.7 倍，肉眼就是糊的。
    ap.add_argument("--scale", type=float, default=3.0)
    ap.add_argument("--quality", type=int, default=92)
    ap.add_argument("--format", choices=["webp", "jpg", "png"], default="webp")
    ap.add_argument("--title")
    ap.add_argument("--sub")
    ap.add_argument("--single-file")
    # 单文件是全量内联，压在 q88 刚好 12 MB 上下；再高会顶到 20 MB 以上
    ap.add_argument("--single-scale", type=float, default=3.0)
    ap.add_argument("--single-quality", type=int, default=88)
    args = ap.parse_args()

    # 没 Pillow 就只能走 JPEG：3.0x 的 JPEG 会把单文件顶到 25 MB 以上，
    # 不如退回 2.4x（照样比旧版 1.7x 清楚），并明确告知怎么装。
    if args.format == "webp" and not HAS_PIL:
        print("[!] 未安装 Pillow：WebP 不可用。请 python -m pip install pillow\n"
              "    本次自动改用 JPEG q90 @2.4x（体积可控，清晰度仍优于旧默认）", flush=True)
        args.format = "jpg"
        args.scale = min(args.scale, 2.4)
        args.quality = max(args.quality, 90)
        if args.single_file:
            args.single_scale = min(args.single_scale, 2.4)
            args.single_quality = max(args.single_quality, 90)

    root = Path(args.root).resolve()
    out = (root / args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    a_pdf = pick(root, args.original,
                 [".arz/original_arxiv.pdf", "base/main.pdf", "out/main.pdf"], "原文 PDF")
    b_pdf = pick(root, args.translated,
                 ["out/main.pdf", "SimpleVLA-RL_zh.pdf"], "译文 PDF")

    a_doc, b_doc = pymupdf.open(a_pdf), pymupdf.open(b_pdf)
    print(f"[1/4] 原文 {a_pdf.name}  {len(a_doc)} 页")
    print(f"      译文 {b_pdf.name}  {len(b_doc)} 页")

    print("[2/4] 渲染页面")
    pages_dir = out / "pages"
    print(f"      {args.format.upper()} q{args.quality} @ {args.scale}x"
          f"（目标 ≥1800 px 宽，覆盖 dpr=2 的单栏视图）")
    en_pages = render_pdf(a_doc, pages_dir, "en", args.scale, args.quality,
                          inline=False, fmt=args.format)
    zh_pages = render_pdf(b_doc, pages_dir, "zh", args.scale, args.quality,
                          inline=False, fmt=args.format)

    print("[3/4] 建立书签锚点")
    anchors, rows = build_anchors(a_doc, b_doc)

    print("      抽取文字层（供全文搜索）")
    en_lines, zh_lines = page_words(a_doc), page_words(b_doc)
    n_words = sum(len(r[2]) for pg in en_lines for r in pg) + \
              sum(len(r[2]) for pg in zh_lines for r in pg)
    print(f"      文字词 {n_words} 个")

    stem = b_pdf.stem.replace("_中文版", "")
    title = args.title or stem.replace("_", " · ")
    if args.sub:
        sub = args.sub
    elif "_" in stem:
        sub = f"arXiv {stem.rsplit('_', 1)[-1]} · 左原文 / 右译文"
    else:
        sub = "左原文 / 右译文"

    tpl = TEMPLATE.read_text(encoding="utf-8")
    meta = {"en": {"pages": en_pages, "lines": en_lines},
            "zh": {"pages": zh_pages, "lines": zh_lines},
            "anchors": anchors, "toc": rows}

    index = out / "index.html"
    index.write_text(fill(tpl, meta, title, sub), encoding="utf-8")
    print(f"[4/4] 写出 {index}  ({human(index.stat().st_size)})")

    if args.single_file:
        print(f"      生成单文件版…（{args.format.upper()} q{args.single_quality} @ {args.single_scale}x）")
        en2 = render_pdf(a_doc, out, "en", args.single_scale, args.single_quality,
                         inline=True, fmt=args.format)
        zh2 = render_pdf(b_doc, out, "zh", args.single_scale, args.single_quality,
                         inline=True, fmt=args.format)
        meta2 = {"en": {"pages": en2, "lines": en_lines},
                 "zh": {"pages": zh2, "lines": zh_lines},
                 "anchors": anchors, "toc": rows}
        single = Path(args.single_file)
        if not single.is_absolute():
            single = root / single
        single.write_text(fill(tpl, meta2, title, sub), encoding="utf-8")
        print(f"      写出 {single}  ({human(single.stat().st_size)})")


if __name__ == "__main__":
    main()
