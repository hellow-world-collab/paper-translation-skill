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
from _common import (cjk_ratio, count_errors, die, error_lines, find_bin,
                     load_json, meta_dir, ok, pdf_pages, pdf_text, read_text,
                     run, save_json, warn, write_text, xetex_flags)

PROBE = "中文探针检测ABC"

# 只支持 pdfTeX、在 XeTeX 下会引发回退或冲突的字体包
TYPE1_PACKAGES = {
    "newtxtext", "newtxmath", "newtxsf", "txfonts", "pxfonts",
    "mathptmx", "mathptm", "times", "pslatex", "tmmath",
}
# 与 unicode-math 不兼容的数学包
MATH_CONFLICT = {"amssymb", "amsfonts", "bm", "mathbbol", "mathrsfs"}
# PDF 可访问性宏包：用 pdfTeX 原语重定义 \caption，XeLaTeX + xeCJK 下会在
# 每个 \caption / \label 处报 `bad native font flag in map_char_to_glyph`
# （I-JEPA 2301.08243 实测 44 错 / 0 页）。它只写 PDF 辅助文本，不影响排版。
XETEX_INCOMPATIBLE = {"axessibility", "accsupp"}

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


def detect_type1_tree(root: Path) -> list[str]:
    r"""扫描整个项目（不只主文件）的 \usepackage，找出 Type1 字体包。

    很多投稿把字体设置放在 \input{preamble} 或 cls 里，只扫主文件会漏掉；
    漏掉的后果是 A1 方案照样加 unicode-math，跟 preamble 里的 newtxmath 硬碰硬，
    一次编译报上千条错误（实测 1380 条），最后退回 A2 才勉强通过。
    """
    found: set[str] = set()
    for f in list(root.rglob("*.tex")) + list(root.rglob("*.sty")) + list(root.rglob("*.cls")):
        try:
            found |= set(detect_type1(read_text(f)))
        except OSError:
            continue
    return sorted(found)


def strip_in_tree(work: Path, main_path: Path, names: set[str]) -> list[str]:
    r"""把 Type1 宏包从「被 \input 的其它文件」里也摘掉，返回被改的文件名。"""
    touched: list[str] = []
    if not names:
        return touched
    main_res = main_path.resolve()
    for f in work.rglob("*.tex"):
        if f.resolve() == main_res:
            continue
        try:
            text = read_text(f)
        except OSError:
            continue
        new, gone = strip_packages(text, names)
        if gone:
            write_text(f, new)
            touched.append(f"{f.name}（{', '.join(sorted(set(gone)))}）")
    return touched


def build_injection(text: str, recipe: dict,
                    found_type1: list[str] | None = None) -> tuple[str, list[str], list[str]]:
    """返回 (注入块, 摘掉的包, 提示)。"""
    removed: list[str] = []
    lines: list[str] = []
    notes: list[str] = []

    if recipe["latin"] == "termes":
        body, gone = strip_packages(text, TYPE1_PACKAGES)
        # 主文件里没有、但被 \input 的文件里有，同样要按「会加载 Type1」处理
        gone = sorted(set(gone) | set(found_type1 or []))
        if gone:
            removed += gone
        # 只要选了 termes 配方，就显式把 \rmdefault 交给 fontspec。
        # 旧逻辑只在「检出 Type1 字体包」时才 \setmainfont；遇到在 .cls 里用
        # \pdfmapline + \renewcommand{\rmdefault}{NVIDIASans} 挂一套 TFM 字体的模板
        # （nvidiatechreport，2501.03575 / 2602.15922 实测）检不出任何 \usepackage，
        # \rmdefault 就一直是那套 TFM 字体，XeLaTeX 的 dvipdfmx 在 shipout 时
        # 找不到 NVIDIASans_It.tfm，直接报 fatal 且一个 PDF 都不出。
        # 用户已用 fontspec 自定义主字体时不覆盖。
        if not re.search(r"\\setmainfont\s*\{", text):
            lines.append("\\usepackage{fontspec}")
            # XCharter 一类宏包在 XeLaTeX 下经 *.fontspec 留下继承的字体内联特性，
            # 会让按「字体名」解析的 TeX Gyre Termes / Heros / Cursor 全部报
            # "The font ... cannot be found"（2501.03575 实测 9 处），Latin 文字
            # 静默丢字形。必须在第一条 \set*font 之前清空，放在 xeCJK 之后已太晚。
            lines.append("\\defaultfontfeatures{}")
            lines.append("\\setmainfont{TeX Gyre Termes}")
            lines.append("\\setsansfont{TeX Gyre Heros}")
            lines.append("\\setmonofont{TeX Gyre Cursor}")
        if recipe["math"] == "unicode":
            if not re.search(r"\\usepackage(\[[^\]]*\])?\{unicode-math\}", text):
                # mathbf=sym：默认 unicode-math 让 \mathbf 落到拉丁文本字体，
                # \mathbf{\Omega} 会被解析成 U+1D6EA（数学粗体 Omega），
                # lmroman10-bold 没有该字形 → 编译 0 错误但公式里一片空白。
                # 2610.31617 实测 11 处。官方选项让 \mathbf 走 \symbf 的数学
                # 字母表，字形由 TeX Gyre Termes Math 提供；不动源码一个字符。
                lines.append("\\usepackage[mathbf=sym]{unicode-math}")
                # \setmathfont 在导言区就要解析字体名；XCharter.fontspec 之类会留下
                # 继承的字体特性（Extension / Path），只放在 xeCJK 之后清一次已经太晚
                # （2501.03575 实测报 The font "TeX Gyre Termes Math" cannot be found）。
                # unicode-math 已加载 fontspec，这里先清空再设数学字体。
                lines.append("\\defaultfontfeatures{}")
                lines.append("\\setmathfont{TeX Gyre Termes Math}")
                # amsbsy 的 \boldsymbol 在 unicode-math 下遇到 \pi_{0.5} 这类
                # 带下标的希腊字母会报 `Improper alphabetic constant`（LingBot-VA 1.0
                # 附录表格实测 6 处）。unicode-math 自带 \symbf 能正确处理。
                lines.append("\\AtBeginDocument{\\ifdefined\\boldsymbol\\let\\boldsymbol\\symbf\\fi}")
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
                # bm 被摘掉后正文里的 \bm{...} 会变成未定义命令（非停模式下只是静默丢符号），
                # 补一个同义宏，交给 unicode-math 的 \symbf 承担
                if "bm" in removed:
                    lines.append("\\providecommand{\\bm}[1]{\\symbf{#1}}")
                    notes.append("bm 与 unicode-math 互斥，\\bm 已由 \\symbf 接管")

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
    # 摘掉 amssymb 后 \checkmark 由 unicode-math 定义成字符 ✓，而拉丁字体没有
    # U+2713，表格里对勾会静默变成空白。AtBeginDocument 里的重定义要借 pifont 的
    # \ding{51}，但旧版从没加载 pifont，\ifdefined\ding 恒为假，那段是死代码。
    # 这里条件加载一次：同名包重复 \usepackage 是安全的，缺包时也不报错。
    lines.append("\\IfFileExists{pifont.sty}{\\usepackage{pifont}}{}")
    # 模板固定标题本地化。必须延后到 babel 的 \selectlanguage 之后，
    # 否则会被 babel 的语言定义覆盖回 Contents / References。
    lines.append("\\AtBeginDocument{")
    # 并非所有文档类都定义 \contentsname / \refname（ieeeconf.cls 就没有），
    # 裸 \renewcommand 会报 "LaTeX Error: Command \contentsname undefined."。
    # 先 provide 再 renew：类里有没有定义都安全，且最终一定是中文。
    lines.append("  \\providecommand{\\contentsname}{目录}\\renewcommand{\\contentsname}{目录}")
    lines.append("  \\providecommand{\\refname}{参考文献}\\renewcommand{\\refname}{参考文献}")
    # 摘要标题：article 系列与 IEEEtran 都读 \abstractname，只有它不译会出现
    # 「Abstract— 中文摘要」这种半中半英
    lines.append("  \\ifdefined\\abstractname\\renewcommand{\\abstractname}{摘要}\\fi")
    lines.append("  \\ifdefined\\bibname\\renewcommand{\\bibname}{参考文献}\\fi")
    lines.append("  \\ifdefined\\appendixname\\renewcommand{\\appendixname}{附录}\\fi")
    lines.append("  \\ifdefined\\partname\\renewcommand{\\partname}{部分}\\fi")
    lines.append("  \\ifdefined\\chaptername\\renewcommand{\\chaptername}{章}\\fi")
    lines.append("  \\ifdefined\\proofname\\renewcommand{\\proofname}{证明}\\fi")
    lines.append("  \\ifdefined\\indexname\\renewcommand{\\indexname}{索引}\\fi")
    lines.append("  \\ifdefined\\listfigurename\\renewcommand{\\listfigurename}{插图目录}\\fi")
    lines.append("  \\ifdefined\\listtablename\\renewcommand{\\listtablename}{表格目录}\\fi")
    lines.append("  \\ifdefined\\keywordsname\\renewcommand{\\keywordsname}{关键词}\\fi")
    lines.append("  \\ifdefined\\IEEEkeywordsname\\renewcommand{\\IEEEkeywordsname}{关键词}\\fi")
    # 图/表题注前缀。有的模板把 figurename 写死在 \captionsetup 里（thuc3i.cls 的
    # figurename=Figure），babel 又只给英文，不处理就会出现「Figure 1 | 中文题注」。
    lines.append("  \\renewcommand{\\figurename}{图}")
    lines.append("  \\renewcommand{\\tablename}{表}")
    lines.append("  \\ifdefined\\lstlistingname\\renewcommand{\\lstlistingname}{代码}\\fi")
    # caption 宏包的 \DeclareCaptionType{listing}[Listing][...] 定义的是 \listingname
    lines.append("  \\ifdefined\\listingname\\renewcommand{\\listingname}{代码}\\fi")
    # minted 的 listing 浮动体走的是 \listingscaption（\floatname{listing}{\listingscaption}），
    # 跟 listings 宏包的 \lstlistingname 不是一个宏，漏了就是「Listing 1: 中文题注」
    lines.append("  \\ifdefined\\listingscaption\\renewcommand{\\listingscaption}{代码}\\fi")
    # algorithm2e 的 \algorithmcfname 在宏包内部由 \SetAlgorithmName 设定，
    # 直接 renew 命令即可；algorithm 宏包用 \algorithmname
    lines.append("  \\ifdefined\\algorithmcfname\\renewcommand{\\algorithmcfname}{算法}\\fi")
    lines.append("  \\ifdefined\\algorithmname\\renewcommand{\\algorithmname}{算法}\\fi")
    # amssymb 被摘掉后 \checkmark 由 unicode-math 在 \begin{document} 时才定义成
    # “字符 ✓”，而拉丁字体没有 U+2713 —— 编译不报错、表格里却是一格空白。
    # 借上方加载的 pifont 的 Zapf Dingbats 出勾叉；这段必须放在 AtBeginDocument 里，
    # 因为导言区此时 \checkmark 还没定义（实测 \ifdefined 为假）。
    lines.append("  \\ifdefined\\ding")
    lines.append("    \\ifdefined\\checkmark\\renewcommand{\\checkmark}{\\ding{51}}\\fi")
    lines.append("    \\ifdefined\\xmark\\renewcommand{\\xmark}{\\ding{55}}\\fi")
    lines.append("  \\fi")
    lines.append("}")
    # caption 宏包的 \captionsetup 只允许在导言区调用；放进 \AtBeginDocument 会报
    # "Package caption Error: Can be used only in preamble"（每个 \caption 都再报一次），
    # 所以这一句必须留在 \begin{document} 之前的顶层
    lines.append("\\ifdefined\\captionsetup\\captionsetup{figurename=图,tablename=表}\\fi")
    # 装了 caption 宏包时，algorithm 浮动体的名字在宏包加载时就被抓走了，
    # 改 \algorithmname 不生效（实测仍出 "Algorithm 1"），必须走 captionsetup
    lines.append("\\ifdefined\\captionsetup\\captionsetup[algorithm]{name=算法}\\fi")

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
    # CoRL / ICLR 一类模板把标签写死成「Abstract:」「Keywords:」（带冒号），
    # 上面那条不带冒号的正则匹配不到，PDF 里就会留下英文标签
    (re.compile(r"\\textbf\{Abstract:\}"), r"\\textbf{摘要：}"),
    (re.compile(r"\\textbf\{Keywords:\}"), r"\\textbf{关键词：}"),
    (re.compile(r"\\textbf\{Keywords\}"), r"\\textbf{关键词}"),
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
    # ── 文档类里写死的结构标签（ieeeconf / IEEEtran 系，2603.09460 实测）──
    # 题注编号前缀由 \fnum@figure / \fnum@table 直接排版，改 \figurename 压不住；
    # 写完 caption 后才套用，所以只能改类文件本身。
    # ICML 模板写的是 `\def\fnum@figure{Figure \thefigure}`（英文词后带一个空格，
    # 既不是 `Fig.~` 也不是 `Figure~`）；旧正则要求紧跟 \thefigure，匹配不到，
    # 1803.10122 实测中文 PDF 里仍留着「Figure 1.」。允许中间的空白。
    (re.compile(r"(\\def\\fnum@figure\{)Fig(?:ure)?\.?\s*~?\s*(\\thefigure)"), r"\1图~\2"),
    (re.compile(r"(\\def\\fnum@table\{)(?:TABLE|Table)\.?\s*~?\s*(\\thetable)", re.I), r"\1表~\2"),
    # ICML 的 abstract 环境把标题写死成 `\centerline{\large\bf Abstract}`，
    # 不走 \abstractname，注入块里的 renewcommand 对它无效。
    (re.compile(r"(\\(?:bf|bfseries)\s+)Abstract(?=\s|\})"), r"\1摘要"),
    # NVIDIA nvidiatechreport.cls 写成 {\large\bfseries\headingfont Abstract}：
    # \bfseries 与 Abstract 之间还隔着一个 \headingfont，上面那条匹配不到，
    # 首页摘要标题会留英文（2501.03575 / 2602.15922 实测）。
    (re.compile(r"(\\(?:bf|bfseries)\b\s*(?:\\[A-Za-z@]+\s*)*)Abstract(?=\s|\})"), r"\1摘要"),
    # 摘要标签：\bfseries\textit{Abstract}---（不带冒号，也不走 \abstractname）
    (re.compile(r"(\\(?:textit|textbf)\{)Abstract(\})"), r"\1摘要\2"),
    # 附录标题：\@IEEEappendixsavesection*{Appendix \* #1} 与目录条目
    (re.compile(r"(\\@IEEEappendixsavesection\*\{)Appendix(?=\s|\\|\})"), r"\1附录"),
    (re.compile(r"(\\addcontentsline\{toc\}\{section\}\{)Appendix(?=[:}])"), r"\1附录"),
    # 参考文献标题：\def\thebibliography#1{\section*{References} 与目录条目
    (re.compile(r"(\\section\*\{)(?:References|REFERENCES)(\})"), r"\1参考文献\2"),
    (re.compile(r"(\\addcontentsline\{toc\}\{section\}\{)(?:References|REFERENCES)(\})"),
     r"\1参考文献\2"),
    # microtype 的 \DisableLigatures 只在 pdftex 下有效；XeLaTeX 会在类文件加载处
    # 报 "Package microtype Error: Disabling ligatures of a font is only possible
    # with pdftex version 1.30 or newer"（fairmeta.cls，2404.08471 实测 1 个错误）。
    # 对中文排版无意义，整行注释掉；行首已有 % 时不会再匹配，天然幂等。
    (re.compile(r"(?m)^([ \t]*)(\\DisableLigatures[^\n]*)$"), r"%\1\2"),
]


def localize_class_files(root: Path) -> list[str]:
    """把 cls/sty 中硬编码的结构标签（摘要/图表前缀/附录/文献标题）换成中文。

    返回改过的文件；逐条 sub 天然幂等，重复跑不会再改。
    """
    touched = []
    for f in list(root.rglob("*.cls")) + list(root.rglob("*.sty")):
        text = read_text(f)
        new = text
        for pat, rep in CLASS_LOCALIZE:
            new = pat.sub(rep, new)
        if new != text:
            write_text(f, new)
            touched.append(f.name)
    return touched


INPUT_FILE_RE = re.compile(r"\\(?:input|include)\s*\{([^}]+)\}")
DOC_BEGIN_RE = re.compile(r"\\begin\s*\{document\}")


def resolve_inject_target(src: Path, main: str) -> tuple[str, str | None]:
    r"""找出真正承载 \begin{document} 的文件，作为字体注入的目标。

    arXiv 有些投稿把「兼容入口」标成 00README 的 toplevel，例如
    main_highlighted.tex 只有一行 \input{main.tex}，导言区与 \begin{document}
    都在被 input 的文件里（2610.31617 实测）。注入块必须落在真正导言区的末尾，
    否则 apply_injection 找不到 \begin{document} 直接报错；但编译入口仍应是
    toplevel —— build.py / audit.py 都按 manifest.main 算产物 PDF 名。
    所以这里只改「注入目标」，不动 manifest.main。
    """
    seen: set[str] = set()
    queue: list[str] = [main]
    while queue:
        rel = queue.pop(0)
        if rel in seen:
            continue
        seen.add(rel)
        f = src / rel
        if not f.is_file():
            continue
        text = read_text(f)
        if DOC_BEGIN_RE.search(text):
            note = None if rel == main else (
                f"主文件 {main} 是纯 \\input 兼容壳，字体注入改写到 {rel}（编译入口不变）"
            )
            return rel, note
        for m in INPUT_FILE_RE.finditer(text):
            name = m.group(1).strip()
            if not name.endswith(".tex"):
                name += ".tex"
            if (src / name).is_file():
                queue.append(name)
            else:
                cand = str(Path(rel).parent / name)
                if (src / cand).is_file():
                    queue.append(cand)
    return main, None


def apply_injection(text: str, recipe: dict, probe: bool,
                    found_type1: list[str] | None = None) -> tuple[str, list[str], list[str]]:
    block, removed, notes = build_injection(text, recipe, found_type1)
    # 无条件执行：即使没有 Type1 包要摘，也要摘掉 XeTeX 不兼容的
    # axessibility / accsupp（主文件里的 \usepackage 只有在这里才会被处理，
    # strip_in_tree 只管被 \input 的文件）。
    text, _ = strip_packages(text, set(removed) | XETEX_INCOMPATIBLE)

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


def try_recipe(root: Path, src: Path, main: str, rid: str, keep: bool,
               tree_type1: list[str] | None = None,
               inject_target: str | None = None) -> dict:
    """在一份临时副本里注入并编译，用哨兵文本判断中文是否真的输出。

    main 是编译入口（产物 PDF 名按它算），inject_target 是注入块真正落地的
    文件；两者只有「toplevel 是纯 \\input 兼容壳」时才不同。
    """
    work = root / (".probe-" + rid)
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(src, work)

    target = inject_target or main
    main_path = work / target
    original = read_text(main_path)
    injected, removed, notes = apply_injection(
        original, RECIPES[rid], probe=True, found_type1=tree_type1
    )
    write_text(main_path, injected)
    touched = strip_in_tree(work, main_path, set(removed) | XETEX_INCOMPATIBLE)
    if touched:
        notes.append("被 \\input 的文件里也摘掉了字体包：" + "、".join(touched))

    binary = find_bin("xelatex")
    flags = xetex_flags(work)
    log = ""
    for _ in range(2):
        _, log = run([binary, *flags, main], cwd=work, timeout=900)

    errors = count_errors(log)
    pdf = work / (Path(main).stem + ".pdf")
    text = pdf_text(pdf) if pdf.is_file() else ""
    probe_hit = "探针" in text
    ratio = cjk_ratio(text)

    # 诊断：区分「字体没找到」与「字体找到了但没有 ToUnicode」
    diag = []
    if "cannot be found" in log:
        diag.append("字体名解析失败（可能被 \\defaultfontfeatures 的 Extension 污染）")
    miss_chars = re.findall(r"Missing character: There is no (\S+) in font", log)
    if miss_chars:
        uniq = sorted(set(miss_chars))
        sample = " ".join(uniq[:12]) + (" …" if len(uniq) > 12 else "")
        diag.append(f"缺字 {len(miss_chars)} 处 / {len(uniq)} 种：{sample}"
                    f"（该字体里没有这些字形，编译不报错但 PDF 会留白）")
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
        "log_tail": "\n".join(error_lines(log)[:5]),
    }

    if keep and probe_hit:
        # 落地到 zh/：去掉哨兵，重新编译，供后续翻译使用
        zh = root / "zh"
        if zh.exists():
            shutil.rmtree(zh)
        shutil.copytree(src, zh)
        clean, removed_zh, _ = apply_injection(
            read_text(zh / target), RECIPES[rid], probe=False, found_type1=tree_type1
        )
        write_text(zh / target, clean)
        strip_in_tree(zh, zh / target, set(removed_zh) | XETEX_INCOMPATIBLE)
        for name in localize_class_files(zh):
            ok(f"类文件固定标题已本地化：{name}")
        for _ in range(2):
            run([binary, *xetex_flags(zh), main], cwd=zh, timeout=900)
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
    inject_target, shell_note = resolve_inject_target(src, main_file)
    if shell_note:
        ok(shell_note)
    # 字体包、\documentclass、导言区都在注入目标里，检测要看它而不是壳。
    main_text = read_text(src / inject_target)

    type1 = detect_type1(main_text)
    tree_type1 = detect_type1_tree(src)
    if tree_type1 and tree_type1 != type1:
        print(f"\n被 \\input 的文件里还有 Type1 字体包：{', '.join(tree_type1)}")
        print("  这些也会让中文静默消失，一并按 OpenType 替换处理。")
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
        res = try_recipe(root, src, main_file, rid, keep=False,
                         tree_type1=tree_type1, inject_target=inject_target)
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
    final = try_recipe(root, src, main_file, chosen["recipe"], keep=True,
                       tree_type1=tree_type1, inject_target=inject_target)

    data = {
        "project": str(root),
        "chosen": final,
        "trials": trials,
        "type1_detected": type1,
        "inject_target": inject_target,
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
