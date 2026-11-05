#!/usr/bin/env python3
"""第七步：审计产出，抓编译看不见的问题。

四类检查：
  1. 中文是否真的铺满全文（分页统计，抓静默吞字）
  2. 残留英文段落（抓漏译）
  3. 术语一致性（抓同一术语多种译法、该保留英文却被翻掉）
  4. 版面溢出（抓表格撞出版心）

用法：
    python audit.py <项目目录> [--png]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import (cjk_ratio, die, find_bin, load_json, meta_dir, ok,
                     pdf_pages, pdf_text, run, save_json, warn, write_text)

SKILL_ROOT = Path(__file__).resolve().parent.parent
GLOSSARY = SKILL_ROOT / "glossary.tsv"

CJK = re.compile(r"[\u4e00-\u9fff]")
WORD = re.compile(r"[A-Za-z]{2,}")


def load_glossary() -> list[dict]:
    if not GLOSSARY.is_file():
        return []
    rows = []
    for line in GLOSSARY.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        rows.append({
            "en": parts[0].strip(),
            "zh": parts[1].strip(),
            "note": parts[2].strip() if len(parts) > 2 else "",
        })
    return rows


def english_residue(pages: list[str], tail_guard: int = 4) -> list[dict]:
    """按页聚合残留英文。图内标注与作者名会混进来，所以只报成片的段落。

    判据：页内没有任何中文（纯图页或字体失效），或英文行达到 8 行以上
    （成段漏译的特征，图注通常只有两三行）。
    """
    hits = []
    for idx, page in enumerate(pages, 1):
        if idx > len(pages) - tail_guard:
            continue
        lines = []
        for line in page.splitlines():
            s = line.strip()
            if len(s) < 45 or CJK.search(s):
                continue
            if len(WORD.findall(s)) < 7:
                continue
            lines.append(s)
        if not lines:
            continue
        cjk_here = len(CJK.findall(page))
        if cjk_here == 0 or len(lines) >= 8:
            hits.append({"page": idx, "count": len(lines),
                         "zero_cjk": cjk_here == 0,
                         "sample": lines[:3]})
    return hits


def page_cjk(pages: list[str]) -> list[dict]:
    out = []
    for idx, page in enumerate(pages, 1):
        chars = len(CJK.findall(page))
        out.append({"page": idx, "cjk": chars, "total": len(page.strip())})
    return out


def term_consistency(segments: list[dict], translations: dict) -> list[dict]:
    """比较术语在原文与译文中的出现频次，发现漏译或译法漂移。"""
    src = "\n".join(s["masked"] for s in segments)
    dst = "\n".join(translations.get(s["id"], "") for s in segments)
    rows = []
    for item in load_glossary():
        en, zh, note = item["en"], item["zh"], item["note"]
        if len(en) < 3:
            continue
        # motion-tracking / motion  tracking 都要算命中
        flex = re.escape(en).replace(r"\ ", r"[\s\-]+")
        pattern = re.compile(flex + r"s?\b", re.I)
        src_n = len(pattern.findall(src))
        if src_n < 3:
            continue

        if "保留" in note:
            kept = len(pattern.findall(dst))
            ratio = kept / src_n
            status = "ok" if ratio >= 0.85 else ("warn" if ratio >= 0.5 else "bad")
            rows.append({"en": en, "zh": zh, "note": note, "src": src_n,
                         "dst": kept, "ratio": round(ratio, 2), "status": status,
                         "mode": "keep"})
        else:
            got = dst.count(zh)
            ratio = got / src_n
            status = "ok" if ratio >= 0.6 else ("warn" if ratio >= 0.25 else "bad")
            rows.append({"en": en, "zh": zh, "note": note, "src": src_n,
                         "dst": got, "ratio": round(ratio, 2), "status": status,
                         "mode": "translate"})
    return rows


def untranslated(segments: list[dict], translations: dict) -> list[dict]:
    """源码层检查：原文有实质英文、译文却没有中文的片段。"""
    out = []
    for seg in segments:
        tr = translations.get(seg["id"])
        if tr is None:
            continue
        stripped = re.sub(r"⟪P\d+⟫", " ", tr)
        en_words = len(WORD.findall(re.sub(r"⟪P\d+⟫", " ", seg["masked"])))
        if en_words >= 5 and len(CJK.findall(stripped)) < 2:
            out.append({"id": seg["id"], "words": en_words,
                        "masked": seg["masked"][:90], "translated": tr[:90]})
    return out


def missing_terms(segments: list[dict], glossary: list[dict]) -> list[tuple[str, int]]:
    """统计原文里的高频英文词，看哪些没被术语表覆盖。"""
    known = set()
    for g in glossary:
        for w in re.findall(r"[a-z]+", g["en"].lower()):
            known.add(w)
    counter: dict[str, int] = {}
    for seg in segments:
        for w in WORD.findall(re.sub(r"⟪P\d+⟫", " ", seg["masked"])):
            lw = w.lower()
            if len(lw) >= 5:
                counter[lw] = counter.get(lw, 0) + 1
    return [(w, n) for w, n in sorted(counter.items(), key=lambda kv: -kv[1])
            if w not in known and n >= 12][:20]


def export_png(pdf: Path, out_dir: Path, pages: list[int], dpi: int = 100) -> list[str]:
    pdftoppm = find_bin("pdftoppm", required=False)
    if not pdftoppm:
        return []
    made = []
    for p in pages:
        rc, _ = run([pdftoppm, "-f", str(p), "-l", str(p), "-r", str(dpi),
                     "-png", str(pdf), str(out_dir / f"page")], timeout=180)
        target = out_dir / f"page-{p:02d}.png"
        if target.is_file():
            made.append(str(target))
    return made



def pdf_audit(root: Path, arz: Path, pm: dict, want_png: bool = False) -> None:
    """PDF 分支的审计：没有源码层，只能查「翻没翻全」和「有没有整页漏掉」。

    LaTeX 分支能靠 segments.json 精确到片段，PDF 分支只有渲染结果，所以判据换成：
    每页中文字符数 / 拉丁字符数，异常页单独列出来给人工看。
    """
    zh = Path(pm.get("translated") or "")
    if not zh.is_file():
        die(f"pdf_mode.json 里记的译文不存在：{zh}")

    pages = [p for p in pdf_text(zh).split("\f")]
    if pages and not pages[-1].strip():
        pages.pop()

    total_cjk = sum(len(CJK.findall(p)) for p in pages)
    total_latin = sum(len(WORD.findall(p)) for p in pages)
    chars = sum(len(re.sub(r"\s+", "", p)) for p in pages)
    ratio = total_cjk / chars if chars else 0

    suspect = []
    for i, p in enumerate(pages, 1):
        c, l = len(CJK.findall(p)), len(WORD.findall(p))
        if l >= 40 and c < 8:
            suspect.append((i, c, l))

    print(f"\n[{root.name}] 审计（PDF 分支）\n")
    print(f"  页数      {len(pages)}")
    print(f"  中文字数  {total_cjk}")
    print(f"  中文占比  {ratio:.1%}（按字符）")
    if suspect:
        warn(f"{len(suspect)} 页几乎没有中文：{', '.join('p%d' % x[0] for x in suspect[:12])}")
        print("      常见原因：扫描页（无文字层）、整页是图、或该页没被 pdf2zh 识别成文本块")
    else:
        ok("每页都有中文，没有整页漏译")

    dual = pm.get("dual")
    if dual:
        print(f"  双语版    {Path(dual).name}（{Path(dual).stat().st_size / 1048576:.1f} MB）")

    report = {
        "mode": "pdf",
        "pages": len(pages),
        "cjk_chars": total_cjk,
        "latin_words": total_latin,
        "cjk_ratio": round(ratio, 4),
        "suspect_pages": [{"page": p, "cjk": c, "latin": l} for p, c, l in suspect],
        "engine": pm.get("engine"),
        "service": pm.get("service"),
    }
    save_json(arz / "audit.json", report)

    md = [f"# 审计报告 · {root.name}（PDF 分支）", ""]
    md.append(f"- 页数 {len(pages)}")
    md.append(f"- 中文字数 {total_cjk}，占比 {ratio:.1%}")
    md.append(f"- 引擎 {pm.get('engine')}，后端 {pm.get('service')}")
    if suspect:
        md += ["", f"## 疑似未译页（{len(suspect)}）",
               *[f"- p{p}｜中文 {c} 字｜英文 {l} 词" for p, c, l in suspect[:30]]]
    write_text(arz / "audit.md", "\n".join(md))
    print(f"\n  报告  {arz / 'audit.md'}")

    if want_png and suspect:
        made = export_png(zh, arz, [p for p, _, _ in suspect[:3]])
        if made:
            ok(f"导出 {len(made)} 张页面图：{', '.join(Path(m).name for m in made)}")

def main():
    ap = argparse.ArgumentParser(description="审计产出")
    ap.add_argument("project")
    ap.add_argument("--png", action="store_true", help="导出关键页图片供人工核验")
    args = ap.parse_args()

    root = Path(args.project).resolve()
    arz = meta_dir(root)

    pm = load_json(arz / "pdf_mode.json")
    if pm and not (arz / "segments.json").exists():
        pdf_audit(root, arz, pm, args.png)
        return

    manifest = load_json(arz / "manifest.json") or {}
    pre = load_json(arz / "preflight.json") or {}
    build = load_json(arz / "build.json") or {}
    seg_data = load_json(arz / "segments.json") or {}
    translations = load_json(arz / "translations.json", default={}) or {}

    pdf = Path(build.get("pdf") or "")
    if not pdf.is_file():
        cand = root / "out" / (Path(manifest.get("main", "main.tex")).stem + ".pdf")
        if cand.is_file():
            pdf = cand
        else:
            die("找不到编译产出的 PDF，请先跑 build.py")

    pages = [p for p in pdf_text(pdf).split("\f")]
    if pages and not pages[-1].strip():
        pages.pop()

    total_cjk = sum(len(CJK.findall(p)) for p in pages)
    total_chars = sum(len(re.sub(r"\s+", "", p)) for p in pages)
    ratio = total_cjk / total_chars if total_chars else 0
    base_pages = (pre.get("baseline") or {}).get("pages", 0)

    print(f"\n[{root.name}] 审计\n")
    print(f"  页数      {len(pages)}" + (f"（原版 {base_pages}）" if base_pages else ""))
    print(f"  中文字数  {total_cjk}")
    print(f"  中文占比  {ratio:.1%}（按字符）")

    # ① 分页中文覆盖
    pc = page_cjk(pages)
    empty_pages = [p["page"] for p in pc if p["cjk"] == 0 and p["total"] > 50]

    # ② 残留英文
    residue = english_residue(pages)
    if residue:
        zero = [r for r in residue if r["zero_cjk"]]
        heavy = [r for r in residue if not r["zero_cjk"]]
        if zero:
            warn(f"{len(zero)} 页整页无中文（纯图页属正常，否则字体失效）："
                 + ", ".join(str(r["page"]) for r in zero[:12]))
        if heavy:
            warn(f"{len(heavy)} 页英文成片（可能是漏译）："
                 + ", ".join(f"p{r['page']}({r['count']}行)" for r in heavy[:10]))
            for r in heavy[:3]:
                print(f"      p{r['page']}  {r['sample'][0][:80]}")

    miss = untranslated(seg_data.get("segments", []), translations)
    if miss:
        warn(f"源码层未译片段 {len(miss)} 条（原文英文、译文无中文）")
        for m in miss[:6]:
            print(f"      {m['id']:<22} {m['masked'][:70]}")

    # ②b 段级补漏：check_tr 能看见「整段没提交译文」和「译文里夹着成片英文」
    from check_tr import scan as scan_tr
    ct = scan_tr(root)
    if ct["missing"]:
        warn(f"完全没提交译文 {len(ct['missing'])} 段（编译后会原样是英文）")
        for m in ct["missing"][:6]:
            print(f"      {m['id']:<22} {m['src'][:70]}")
    if ct["latin_runs"]:
        warn(f"译文里夹着成片英文 {len(ct['latin_runs'])} 段（可能漏抄一截）")
        for m in ct["latin_runs"][:5]:
            print(f"      {m['id']:<22} {m['words']} 词：{m['run'][:70]}")

    # ③ 术语
    terms = term_consistency(seg_data.get("segments", []), translations)
    bad = [t for t in terms if t["status"] != "ok"]
    print(f"\n  术语      检查 {len(terms)} 条，异常 {len(bad)} 条")
    for t in sorted(bad, key=lambda x: x["ratio"])[:10]:
        flag = "✗" if t["status"] == "bad" else "⚠"
        arrow = "应保留英文" if t["mode"] == "keep" else f"应译作「{t['zh']}」"
        print(f"      {flag} {t['en']:<26} 原文 {t['src']:>3} 次 / 命中 {t['dst']:>3}  "
              f"({t['ratio']:.0%})  {arrow}")

    uncovered = missing_terms(seg_data.get("segments", []), load_glossary())
    if uncovered:
        print("      术语表未覆盖的高频词：")
        print("      " + "、".join(f"{w}({n})" for w, n in uncovered[:10]))

    # ④ 版面
    over = build.get("overfull") or []
    if over:
        warn(f"版面溢出 {len(over)} 处，最宽 {max(o['pt'] for o in over):.0f}pt")

    report = {
        "pages": len(pages),
        "baseline_pages": base_pages,
        "cjk_chars": total_cjk,
        "cjk_ratio": round(ratio, 4),
        "empty_pages": empty_pages,
        "residue": residue,
        "untranslated": miss,
        "missing_segments": ct["missing"],
        "latin_runs": ct["latin_runs"],
        "terms": terms,
        "uncovered": [{"word": w, "count": n} for w, n in uncovered],
        "overfull": over,
        "errors": build.get("errors", 0),
    }
    save_json(arz / "audit.json", report)

    # markdown 报告
    md = [f"# 审计报告 · {root.name}", ""]
    md.append(f"- 页数 {len(pages)}" + (f"（原版 {base_pages}）" if base_pages else ""))
    md.append(f"- 中文字数 {total_cjk}，占比 {ratio:.1%}")
    md.append(f"- 编译错误 {build.get('errors', 0)}，溢出 {len(over)}")
    if empty_pages:
        md.append(f"- 无中文页：{', '.join(map(str, empty_pages[:20]))}")
    md.append("")
    if residue:
        md.append(f"## 残留英文（{len(residue)} 页）")
        md += [f"- p{r['page']}｜{r['count']} 行｜"
               + ("整页无中文" if r["zero_cjk"] else "中文偏少") for r in residue[:30]]
        md.append("")
    if miss:
        md.append(f"## 源码层未译片段（{len(miss)} 条）")
        md += [f"- `{m['id']}` {m['masked'][:70]}" for m in miss[:20]]
        md.append("")
    if bad:
        md.append(f"## 术语异常（{len(bad)} 条）")
        md += [f"- {t['en']} → {t['zh']}：原文 {t['src']}，命中 {t['dst']}（{t['ratio']:.0%}）"
               for t in sorted(bad, key=lambda x: x["ratio"])[:20]]
        md.append("")
    write_text(arz / "audit.md", "\n".join(md))

    print(f"\n  报告  {arz / 'audit.md'}")

    if args.png:
        key = [1, 2] + sorted({r["page"] for r in residue[:2]})
        made = export_png(pdf, arz, key)
        if made:
            ok(f"导出 {len(made)} 张页面图：{', '.join(Path(m).name for m in made)}")


if __name__ == "__main__":
    main()
