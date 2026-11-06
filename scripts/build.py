#!/usr/bin/env python3
"""第六步：编译回填后的项目，并做可控的自动修复。

修复只做两类有确定解的问题：
  1. 缺宏包 → 调 mpm / tlmgr 安装
  2. 宽表格溢出 → 在该 table 环境插入 \\small 后重编（仅 --fit-tables）

其余错误只报告，不猜。

用法：
    python build.py <项目目录> [--fit-tables] [--open]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import (die, error_lines, find_bin, load_json, meta_dir, ok,
                     pdf_pages, pdf_text, read_text, run, save_json, warn,
                     write_text, xetex_flags)

OVERFULL = re.compile(r"^Overfull \\hbox \(([\d.]+)pt too wide\) in paragraph at lines (\d+)", re.M)
UNDEFINED_CMD = re.compile(r"^! Undefined control sequence\.|: Undefined control sequence\.", re.M)
# biblatex 的 .bbl 由 biber 生成；对它跑 bibtex 只会失败并留下空文献表
BIBLATEX_RE = re.compile(r"\\usepackage(?:\[[^\]]*\])?\{[^}]*biblatex\b|\\addbibresource\b")


def pick_bib_tool(work: Path, main: str) -> str | None:
    """判断该跑 bibtex 还是 biber（都没有就返回 None）。"""
    if not any(work.rglob("*.bib")):
        return None
    text = ""
    for i, p in enumerate(work.rglob("*.tex")):
        if i >= 40:
            break
        try:
            text += read_text(p)
        except OSError:
            continue
    if BIBLATEX_RE.search(text):
        return "biber" if find_bin("biber", required=False) else None
    return "bibtex" if find_bin("bibtex", required=False) else None


def classify(log: str) -> dict:
    errors = error_lines(log)
    cats: dict[str, int] = {}
    for e in errors:
        # "f.tex:12: Package caption Error: ..." → 取描述部分，按首句归类
        desc = e.split(": ", 1)[-1] if re.match(r"^[^\s:]+:\d+: ", e) else e.lstrip("! ")
        key = desc.split(".")[0][:60]
        cats[key] = cats.get(key, 0) + 1
    return {
        "errors": len(errors),
        "categories": dict(sorted(cats.items(), key=lambda kv: -kv[1])),
        "overfull": [
            {"pt": float(m.group(1)), "line": int(m.group(2))}
            for m in OVERFULL.finditer(log)
        ],
        "truncated": "That makes 100 errors" in log,
        "missing": sorted(set(re.findall(
            r"(?:File|file)\s+[`'\"]([^`'\"]+?\.(?:sty|cls|fd))['\"]\s+not found", log))),
    }


def install_missing(log: str) -> list[str]:
    from preflight import install_package

    installed = []
    for name in classify(log)["missing"]:
        stem = Path(name).stem
        if install_package(stem):
            installed.append(stem)
            ok(f"安装宏包 {stem}")
    return installed


def fit_wide_tables(root: Path, main: str, threshold: float = 12.0) -> int:
    """把溢出超过阈值的 table 环境内的 tabular 缩小一档。

    只对能用 \\small 解决的场合下手；跨栏表格交给人工。
    """
    path = root / main
    text = read_text(path)
    changed = 0

    for m in re.finditer(r"\\begin\{(table\*?)\}(.*?)\\end\{\1\}", text, re.S):
        body = m.group(0)
        if "\\small" in body or "\\resizebox" in body or "\\footnotesize" in body:
            continue
        # 列数 >= 7 的表格最容易溢出
        spec = re.search(r"\\begin\{tabular\*?\}(?:\[[^\]]*\])?\{([^}]*)\}", body)
        if not spec:
            continue
        cols = len(re.findall(r"[lcrpXm]", spec.group(1)))
        if cols < 7:
            continue
        new_body = body.replace(
            f"\\begin{{{m.group(1)}}}", f"\\begin{{{m.group(1)}}}\\small", 1
        )
        text = text.replace(body, new_body, 1)
        changed += 1

    if changed:
        write_text(path, text)
    return changed


def main():
    ap = argparse.ArgumentParser(description="编译中文版")
    ap.add_argument("project")
    ap.add_argument("--fit-tables", action="store_true", help="宽表格自动缩一档")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--allow-partial", action="store_true",
                    help="缺译也继续编译（默认拦下，避免产出半中半英的 PDF）")
    args = ap.parse_args()

    root = Path(args.project).resolve()
    arz = meta_dir(root)
    manifest = load_json(arz / "manifest.json")
    if not manifest:
        die("缺少 manifest.json，请先跑 fetch.py")

    work = root / "out"
    if not work.is_dir():
        die("out/ 不存在，请先跑 apply.py（它会从 zh/ 重建 out/）")

    main_file = manifest["main"]
    engine = "xelatex"
    cjk = load_json(arz / "cjk.json") or {}
    if cjk.get("chosen"):
        ok(f"字体方案 {cjk['chosen']['recipe']}：{cjk['chosen']['label']}")

    # 编译前先查译文完整性：LaTeX 不在乎中英混排，缺译只会静默变成英文段落
    from check_tr import scan as scan_tr
    chk = scan_tr(root)
    n_missing, n_thin = len(chk["missing"]), len(chk["low_cjk"])
    n_uncovered = len(chk.get("uncovered", []))
    print(f"\n[{root.name}] 译文体检：已译 {chk['translated']}/{chk['total']}"
          f"  缺译 {n_missing}  中文过少 {n_thin}  成片英文 {len(chk['latin_runs'])}"
          f"  未抽取正文 {n_uncovered}")
    if (n_missing or n_thin or n_uncovered) and not args.allow_partial:
        for it in chk["missing"][:5]:
            print(f"    缺译 {it['id']:<24} {it['src'][:60]}")
        for it in chk["low_cjk"][:5]:
            print(f"    过少 {it['id']:<24} 汉字 {it['cjk']:.0%}  {it['tr'][:50]}")
        for it in chk.get("uncovered", [])[:5]:
            print(f"    未抽取 {it['file']}:{it['line']}  {it['text'][:60]}")
        die(f"有 {n_missing + n_thin} 段没翻、{n_uncovered} 处正文没被抽取，"
            f"编出来会半中半英。\n"
            f"    补齐 {arz / 'translations.json'} 后重跑 apply.py；\n"
            f"    未抽取正文要修 extract.py 后重跑 extract.py --force；\n"
            f"    确实要带着英文编译就加 --allow-partial。")

    pdf = work / (Path(main_file).stem + ".pdf")
    log_path = work / (Path(main_file).stem + ".log")

    if args.fit_tables:
        n = fit_wide_tables(work, main_file)
        if n:
            ok(f"缩小 {n} 个宽表格")

    binary = find_bin(engine)
    flags = xetex_flags(work)
    if "-shell-escape" in flags:
        warn("文档用到 minted / pythontex / svg，已自动加 -shell-escape；"
             "它会让文档里的 \\write18 真的执行，请确认源码可信")
    bib_tool = pick_bib_tool(work, main_file)
    if bib_tool:
        print(f"  参考文献工具：{bib_tool}")

    print(f"\n[{root.name}] 编译（{engine}）")
    counter = {"n": 0}

    def compile_pass():
        counter["n"] += 1
        rc, out = run([binary, *flags, main_file], cwd=work, timeout=1200)
        log = read_text(log_path) if log_path.is_file() else out
        info = classify(log)
        print(f"  pass{counter['n']}: 退出码 {rc}  错误 {info['errors']}  溢出 {len(info['overfull'])}")
        return info, log

    info, log = compile_pass()
    installed = install_missing(log)
    if installed:
        info, log = compile_pass()

    # 参考文献必须在正文第一遍之后跑：bibtex/biber 要读 .aux/.bcf 才知道引用了什么。
    # 以前只在「首轮零错误」时跑 bibtex，基线稍有告警就整段文献表丢失。
    if bib_tool:
        rc_b, out_b = run([find_bin(bib_tool), Path(main_file).stem], cwd=work, timeout=600)
        if rc_b == 0:
            ok(f"{bib_tool} 完成")
        else:
            tail = [l for l in out_b.strip().splitlines() if l.strip()][-2:]
            warn(f"{bib_tool} 失败：{' / '.join(tail)[:200]}")
        info, log = compile_pass()

    # TeX 自己会在 log 里提示交叉引用没收敛；没有提示就说明再编译也不会变。
    # 默认 rounds=3 时，稳定论文原先固定跑 4 遍 xelatex；收敛后即停，
    # 2607.15036 实测 3 遍（省 1 遍），交叉引用真有变化时仍会跑满。
    RERUN_HINT = re.compile(
        r"Rerun to get (?:the )?cross-references right|"
        r"Label\(s\) may have changed|"
        r"There were undefined (?:references|citations)|"
        r"Rerun to get citations correct", re.I)
    for _ in range(max(1, args.rounds - 1)):
        if not RERUN_HINT.search(log):
            print(f"  交叉引用已收敛，跳过剩余编译遍数（已跑 {counter['n']} 遍）")
            break
        info, log = compile_pass()

    log = read_text(log_path) if log_path.is_file() else out
    info = classify(log)
    pages = pdf_pages(pdf) if pdf.is_file() else 0
    text = pdf_text(pdf) if pdf.is_file() else ""
    cjk_chars = len(re.findall(r"[\u4e00-\u9fff]", text))

    data = {
        "engine": engine,
        "pages": pages,
        "pdf": str(pdf),
        "pdf_bytes": pdf.stat().st_size if pdf.is_file() else 0,
        "cjk_chars": cjk_chars,
        **info,
    }
    save_json(arz / "build.json", data)

    print("\n" + "─" * 56)
    print(f"  页数      {pages}")
    print(f"  中文      {cjk_chars} 字")
    print(f"  错误      {info['errors']}")
    print(f"  溢出      {len(info['overfull'])} 处")
    if info["overfull"]:
        worst = max(info["overfull"], key=lambda x: x["pt"])
        print(f"            最宽 {worst['pt']:.0f}pt 于第 {worst['line']} 行")
    if info["categories"]:
        print("  错误类型")
        for k, v in list(info["categories"].items())[:6]:
            print(f"    {v:>3}× {k}")
    if info["truncated"]:
        print("  ! 撞上 100 错误上限，输出被截断")
    print("─" * 56)

    if cjk_chars == 0:
        warn("PDF 里没有中文！字体可能未真正嵌入，请检查 cjk.json 与 pdftotext 输出")
    print(f"\n下一步：python audit.py {root}")


if __name__ == "__main__":
    main()
