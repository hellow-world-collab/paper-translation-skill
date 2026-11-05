#!/usr/bin/env python3
"""第三步：注入中文支持，并验证中文是否真的进了 PDF。

关键教训：编译零错误不等于中文成功。Type1 字体（newtx/txfonts/mathptmx）在
XeLaTeX 下会触发 METAFONT 回退，中文静默消失——PDF 正常生成、页数正常，
但一个字都没有。所以这里必须插哨兵文本做实证检验，不能看退出码。

用法：
    python inject_cjk.py <项目目录> [--recipe auto|A1|C1|A2|B|D] [--force]
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import (cjk_ratio, die, find_bin, load_json, meta_dir, ok,
                     pdf_pages, pdf_text, read_text, run, save_json, warn,
                     write_text)

PROBE = "中文探针检测ABC"

# 只支持 pdfTeX、在 XeTeX 下会引发回退或冲突的字体包
TYPE1_PACKAGES = {
    "newtxtext", "newtxmath", "newtxsf", "txfonts", "pxfonts",
    "mathptmx", "mathptm", "times", "pslatex", "tmmath",
}
# 与 unicode-math 不兼容的数学包
MATH_CONFLICT = {"amssymb", "amsfonts", "bm", "mathbbol", "mathrsfs"}

CJK_STACKS = {
    "simsun": dict(
        main="SimSun", sans="SimHei", mono="SimSun", fakebold=True,
        note="Windows 系统宋体",
    ),
    "fandol": dict(
        main="FandolSong-Regular.otf", sans="FandolHei-Regular.otf",
        mono="FandolFang-Regular.otf", fakebold=False, note="MiKTeX 自带 Fandol",
    ),
    "noto": dict(
        main="Noto Serif CJK SC", sans="Noto Sans CJK SC",
        mono="Noto Sans Mono CJK SC", fakebold=False, note="Noto CJK",
    ),
}

RECIPES = {
    "A1": dict(latin="termes", math="unicode", cjk="simsun"),
    "C1": dict(latin="termes", math="unicode", cjk="fandol"),
    "A2": dict(latin="termes", math=None, cjk="simsun"),
    "C2": dict(latin="termes", math=None, cjk="fandol"),
    "B": dict(latin=None, math=None, cjk="simsun"),
    "D": dict(latin=None, math=None, cjk="fandol"),
}
AUTO_ORDER = ["A1", "C1", "A2", "C2", "B", "D"]


def strip_packages(text: str, names: set[str]) -> tuple[str, list[str]]:
    """从 \\usepackage 列表里摘掉指定宏包，返回 (新文本, 实际摘掉的包)。"""
    removed: list[str] = []

    def repl(m):
        opts = m.group(1) or ""
        pkgs = [p.strip() for p in m.group(2).split(",") if p.strip()]
        keep = []
        for pkg in pkgs:
            base = pkg.split("=")[0]
            if base in names:
                removed.append(base)
            else:
                keep.append(pkg)
        if not keep:
            return ""
        return r"\usepackage" + opts + "{" + ",".join(keep) + "}"

    return re.sub(r"\\usepackage(\[[^\]]*\])?\{([^}]*)\}", repl, text), removed


def detect_type1(text: str) -> list[str]:
    found = []
    for m in re.finditer(r"\\usepackage(\[[^\]]*\])?\{([^}]*)\}", text):
        for pkg in m.group(2).split(","):
            base = pkg.strip().split("=")[0]
            if base in TYPE1_PACKAGES:
                found.append(base)
    return sorted(set(found))


def build_injection(text: str, recipe: dict) -> tuple[str, list[str], list[str]]:
    """返回 (注入块, 摘掉的包, 提示)。"""
    removed: list[str] = []
    lines: list[str] = []
    notes: list[str] = []

    if recipe["latin"] == "termes":
        body, gone = strip_packages(text, TYPE1_PACKAGES)
        if gone:
            removed += gone
            lines.append("\\usepackage{fontspec}")
            lines.append("\\setmainfont{TeX Gyre Termes}")
            lines.append("\\setsansfont{TeX Gyre Heros}")
            lines.append("\\setmonofont{TeX Gyre Cursor}")
        if recipe["math"] == "unicode":
            if not re.search(r"\\usepackage(\[[^\]]*\])?\{unicode-math\}", text):
                lines.append("\\usepackage{unicode-math}")
                lines.append("\\setmathfont{TeX Gyre Termes Math}")
                # unicode-math 与 amssymb 家族互斥，必须摘掉
                body2, gone2 = strip_packages(
                    text if not removed else _strip_only(text, TYPE1_PACKAGES), MATH_CONFLICT
                )
                if gone2:
                    removed += gone2
                    notes.append(
                        f"unicode-math 与 {', '.join(gone2)} 互斥，已移除；"
                        "\\mathbb 等符号由 unicode-math 提供"
                    )

    stack = CJK_STACKS[recipe["cjk"]]
    if not re.search(r"\\usepackage(\[[^\]]*\])?\{xeCJK\}", text):
        lines.append("\\usepackage{xeCJK}")
    # 某些文档类（如 thuc3i.cls）经 XCharter.fontspec 设了 \defaultfontfeatures{Extension=.otf}，
    # 会把 SimSun 解析成 SimSun.otf → 字体找不到 → 降级 nullfont → 汉字静默丢弃。
    # 必须在 fontspec 已加载后（xeCJK 之后）、设 CJK 字体前清空继承特性。
    lines.append("\\defaultfontfeatures{}")
    fake = "[AutoFakeBold=2]" if stack["fakebold"] else ""
    lines.append(f"\\setCJKmainfont{fake}{{{stack['main']}}}")
    lines.append(f"\\setCJKsansfont{fake}{{{stack['sans']}}}")
    lines.append(f"\\setCJKmonofont{{{stack['mono']}}}")
    lines.append("\\xeCJKsetup{CJKspace=true}")
    # 模板固定标题本地化。必须延后到 babel 的 \selectlanguage 之后，
    # 否则会被 babel 的语言定义覆盖回 Contents / References。
    lines.append("\\AtBeginDocument{")
    lines.append("  \\renewcommand{\\contentsname}{目录}")
    lines.append("  \\renewcommand{\\refname}{参考文献}")
    # 图/表题注前缀。有的模板把 figurename 写死在 \captionsetup 里（thuc3i.cls 的
    # figurename=Figure），babel 又只给英文，不处理就会出现「Figure 1 | 中文题注」。
    lines.append("  \\renewcommand{\\figurename}{图}")
    lines.append("  \\renewcommand{\\tablename}{表}")
    lines.append("  \\ifdefined\\lstlistingname\\renewcommand{\\lstlistingname}{代码}\\fi")
    # caption 宏包的 \DeclareCaptionType{listing}[Listing][...] 定义的是 \listingname
    lines.append("  \\ifdefined\\listingname\\renewcommand{\\listingname}{代码}\\fi")
    lines.append("  \\ifdefined\\captionsetup\\captionsetup{figurename=图,tablename=表}\\fi")
    lines.append("}")

    block = (
        "\n% ===== arxiv-zh-tex: 字体与中文支持（自动注入）=====\n"
        + "\n".join(lines)
        + "\n% ===== arxiv-zh-tex: end =====\n"
    )
    return block, removed, notes


def _strip_only(text: str, names: set[str]) -> str:
    body, _ = strip_packages(text, names)
    return body


# 类文件里写死的英文固定标题，renewcommand 抓不到，只能替换文本
CLASS_LOCALIZE = [
    (re.compile(r"\\textbf\{Abstract\s*\$\\vert\$\}"), r"\\textbf{摘要 $\\vert$}"),
    (re.compile(r"\\textbf\{Abstract\}"), r"\\textbf{摘要}"),
    # babel 的 captionsenglish 钩子会在 \begin{document} 时把 \refname 改回英文，
    # 注入块里的 renewcommand 会被覆盖，只能改类文件里的这行
    (re.compile(r"(\\addto\{\\captionsenglish\}\{\\renewcommand\{\\refname\}\{)References(\}\})"), r"\1参考文献\2"),
    (re.compile(r"\\renewcommand\{\\contentsname\}\{Contents\}"), r"\\renewcommand{\\contentsname}{目录}"),
    # \captionsetup{figurename=Figure} 这类写死的题注名，renewcommand 压不住
    # （caption 宏包在每次 \caption 时重新套用选项），只能改类文件本身
    (re.compile(r"(figurename\s*=\s*)\{?Figure\}?"), r"\1图"),
    (re.compile(r"(tablename\s*=\s*)\{?Table\}?"), r"\1表"),
    (re.compile(r"\\renewcommand\{\\figurename\}\{Figure\}"), r"\\renewcommand{\\figurename}{图}"),
    (re.compile(r"\\renewcommand\{\\tablename\}\{Table\}"), r"\\renewcommand{\\tablename}{表}"),
]


def localize_class_files(root: Path) -> list[str]:
    """把 cls/sty 中硬编码的 Abstract 标题换成中文，返回改过的文件。"""
    touched = []
    for f in list(root.glob("*.cls")) + list(root.glob("*.sty")):
        text = read_text(f)
        new = text
        for pat, rep in CLASS_LOCALIZE:
            new = pat.sub(rep, new)
        if new != text:
            write_text(f, new)
            touched.append(f.name)
    return touched


def apply_injection(text: str, recipe: dict, probe: bool) -> tuple[str, list[str], list[str]]:
    block, removed, notes = build_injection(text, recipe)
    if removed:
        text, _ = strip_packages(text, set(removed))

    if "arxiv-zh-tex: 字体与中文支持" in text:
        text = re.sub(
            r"\n?% ===== arxiv-zh-tex: 字体与中文支持.*?% ===== arxiv-zh-tex: end =====\n",
            "\n", text, flags=re.S,
        )

    m = re.search(r"\\begin\s*\{document\}", text)
    if not m:
        die("找不到 \\begin{document}，无法注入中文支持。")
    text = text[: m.start()] + block + text[m.start():]

    if probe:
        e = re.search(r"\\end\s*\{document\}", text)
        if e:
            text = text[: e.start()] + f"\n{PROBE}\n" + text[e.start():]
    return text, removed, notes


def try_recipe(root: Path, src: Path, main: str, rid: str, keep: bool) -> dict:
    """在一份临时副本里注入并编译，用哨兵文本判断中文是否真的输出。"""
    work = root / (".probe-" + rid)
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(src, work)

    main_path = work / main
    original = read_text(main_path)
    injected, removed, notes = apply_injection(original, RECIPES[rid], probe=True)
    write_text(main_path, injected)

    binary = find_bin("xelatex")
    log = ""
    for _ in range(2):
        _, log = run([binary, "-interaction=nonstopmode", "-file-line-error", main],
                     cwd=work, timeout=900)

    errors = len(re.findall(r"^!", log, flags=re.M))
    pdf = work / (Path(main).stem + ".pdf")
    text = pdf_text(pdf) if pdf.is_file() else ""
    probe_hit = "探针" in text
    ratio = cjk_ratio(text)

    # 诊断：区分「字体没找到」与「字体找到了但没有 ToUnicode」
    diag = []
    if "cannot be found" in log:
        diag.append("字体名解析失败（可能被 \\defaultfontfeatures 的 Extension 污染）")
    missing = len(re.findall(r"Missing character: There is no", log))
    if missing:
        diag.append(f"缺字警告 {missing} 条（汉字走了非 CJK 字体）")
    if not diag and not probe_hit:
        diag.append("字体已加载但提取不到中文（多半是无 ToUnicode）")

    result = {
        "recipe": rid,
        "label": (
            f"{RECIPES[rid]['latin'] or '原 Latin'}"
            f" + {'unicode-math' if RECIPES[rid]['math'] else '原数学'}"
            f" + {CJK_STACKS[RECIPES[rid]['cjk']]['note']}"
        ),
        "errors": errors,
        "pages": pdf_pages(pdf) if pdf.is_file() else 0,
        "probe_ok": probe_hit,
        "cjk_chars": len(re.findall(r"[\u4e00-\u9fff]", text)),
        "removed": removed,
        "notes": notes + diag,
        "log_tail": "\n".join(
            [l for l in log.splitlines() if l.startswith("!")][:5]
        ),
    }

    if keep and probe_hit:
        # 落地到 zh/：去掉哨兵，重新编译，供后续翻译使用
        zh = root / "zh"
        if zh.exists():
            shutil.rmtree(zh)
        shutil.copytree(src, zh)
        clean, _, _ = apply_injection(read_text(zh / main), RECIPES[rid], probe=False)
        write_text(zh / main, clean)
        for name in localize_class_files(zh):
            ok(f"类文件固定标题已本地化：{name}")
        for _ in range(2):
            run([binary, "-interaction=nonstopmode", main], cwd=zh, timeout=900)
        result["zh_ready"] = True

    shutil.rmtree(work, ignore_errors=True)
    return result


def main():
    ap = argparse.ArgumentParser(description="注入中文支持并验证")
    ap.add_argument("project")
    ap.add_argument("--recipe", default="auto", help="auto 或 A1/C1/A2/C2/B/D")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    root = Path(args.project).resolve()
    arz = meta_dir(root)
    manifest = load_json(arz / "manifest.json")
    if not manifest:
        die("缺少 manifest.json，请先跑 fetch.py")
    pre = load_json(arz / "preflight.json") or {}
    out = arz / "cjk.json"

    if out.is_file() and not args.force and (root / "zh").is_dir():
        data = load_json(out)
        ok(f"复用已有中文字体配置：{data['chosen']['label']}")
        return

    src = root / manifest["src"]
    main_file = manifest["main"]
    main_text = read_text(src / main_file)

    type1 = detect_type1(main_text)
    order = AUTO_ORDER if args.recipe == "auto" else [args.recipe]
    if args.recipe == "auto" and type1:
        print(f"\n检测到 Type1 字体包：{', '.join(type1)}")
        print("  这类包在 XeLaTeX 下会让中文静默消失，优先走 OpenType 替换路线。")
        order = ["A1", "C1", "A2", "C2", "B", "D"]

    trials = []
    chosen = None
    print(f"\n[{root.name}] 依次尝试字体方案（每个方案实际编译验证）\n")
    for rid in order:
        print(f"  → {rid}", end="", flush=True)
        res = try_recipe(root, src, main_file, rid, keep=False)
        trials.append(res)
        status = "中文已进 PDF" if res["probe_ok"] else f"中文丢失（{res['cjk_chars']} 字）"
        print(f"  {res['errors']} 错误 / {res['pages']} 页 / {status}")
        if res["probe_ok"] and res["errors"] == 0:
            chosen = res
            break
        if res["probe_ok"] and chosen is None:
            chosen = res  # 有中文但有错误，先记下，继续找更干净的

    if not chosen:
        save_json(out, {"project": str(root), "trials": trials, "chosen": None})
        die(
            "所有字体方案都失败，中文无法进入 PDF。\n"
            "已尝试：" + ", ".join(t["recipe"] for t in trials) + "\n"
            "请查看 .arz/cjk.json 里的 log_tail，或退到 PDF 重排方案。"
        )

    print(f"\n  采用 {chosen['recipe']}：{chosen['label']}")
    final = try_recipe(root, src, main_file, chosen["recipe"], keep=True)

    data = {
        "project": str(root),
        "chosen": final,
        "trials": trials,
        "type1_detected": type1,
        "probe": PROBE,
    }
    save_json(out, data)

    if final["removed"]:
        print(f"  摘除宏包：{', '.join(sorted(set(final['removed'])))}")
    for note in final["notes"]:
        print(f"  · {note}")
    print(f"\n  zh/ 已就绪（无哨兵，可开始抽取段落）")
    print(f"\n下一步：python extract.py {root}")


if __name__ == "__main__":
    main()
