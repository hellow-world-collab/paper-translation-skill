#!/usr/bin/env python3
"""第四步：抽取可翻译片段（segment），公式与命令替换为 ⟪Pnnnn⟫ 标记。

设计来自 TeXGlot 的核心原则：模型永远不生成 TeX 语法。
我们只把自然语言交给模型，受保护内容以标记占位，回填时原样还原。

用法：
    python extract.py <项目目录> [--min-ratio 0.6]
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import (MARKER, MARKER_RE, die, load_json, meta_dir, ok,
                     read_text, save_json, warn)

# ── 环境分类 ──────────────────────────────────────────────
# 整体跳过：图形源码改了会破坏布局，代码环境不应翻译
OPAQUE_ENVS = {
    "tikzpicture", "pgfplots", "axis", "semilogxaxis", "semilogyaxis",
    "loglogaxis", "picture", "circuitikz", "pspicture", "pgfonlayer",
    "verbatim", "Verbatim", "lstlisting", "minted", "filecontents",
    "filecontents*", "comment", "algorithm", "algorithmic", "algorithmicx",
    "alltt", "thebibliography", "biblist",
}
# 数学环境：整体保护
MATH_ENVS = {
    "equation", "equation*", "eqnarray", "eqnarray*", "align", "align*",
    "gather", "gather*", "multline", "multline*", "split", "cases",
    "matrix", "pmatrix", "bmatrix", "vmatrix", "Vmatrix", "array",
    "displaymath", "math", "flalign", "flalign*", "alignat", "alignat*",
}

# 参数是可译文本的命令：递归进去
TEXT_COMMANDS = {
    "section", "subsection", "subsubsection", "paragraph", "subparagraph",
    "caption", "title", "author", "date", "thanks", "footnote", "marginpar",
    "textbf", "textit", "emph", "textsf", "texttt", "textrm", "textsc",
    "textsl", "textup", "textmd", "textnormal", "underline", "uline",
    "item", "text", "mbox", "makebox", "fbox", "framebox", "parbox",
    # 缩放/旋转盒子的内容是正文：论文常写
    # \resizebox{\columnwidth}{!}{\begin{tabular}...}，不进去就整张表都不译
    "resizebox", "scalebox", "adjustbox", "rotatebox", "resizebox*", "raisebox",
    "shortstack", "centerline", "leftline", "rightline",
    "affiliation", "institution", "email", "keywords", "keyword",
    "acknowledgments", "acknowledgements", "acks",
    "abstractname", "appendixname", "contentsname", "refname",
    "subcaption", "captionof", "part", "chapter",
    # makecell 宏包的表头：\makecell[c]{Static Cylinder\\Obstacle} 里的文字是正文，
    # 整块保护会让中文 PDF 的表格留着英文表头（2607.15036 实测漏 2 个）。
    # 可选参数 [c] / [l] 是排版对齐，走 opaque_idx 之外仍由 opts_end 保护。
    "makecell", "thead",
}
# 参数不译的命令
OPAQUE_COMMANDS = {
    "label", "ref", "eqref", "autoref", "cref", "Cref", "pageref",
    "cite", "citep", "citet", "citealp", "citealt", "citeauthor",
    "citeyear", "nocite", "bibitem",
    "url", "href", "includegraphics", "includesvg",
    "input", "include", "subfile", "import", "subimport",
    "bibliography", "bibliographystyle", "addbibresource",
    "usepackage", "documentclass", "RequirePackage", "PassOptionsToPackage",
    "newcommand", "renewcommand", "providecommand", "def", "gdef", "edef",
    "xdef", "let", "newenvironment", "renewenvironment", "DeclareMathOperator",
    "definecolor", "color", "vspace", "hspace", "setlength", "addtolength",
    "setcounter", "addtocounter", "phantom", "hphantom", "vphantom",
    "rule", "cline", "hline", "toprule", "midrule", "bottomrule",
    # resizebox/scalebox/rotatebox/adjustbox 的内容是正文（见 TEXT_COMMANDS 与 MIXED_ARGS），
    # 不能整体保护，否则 \resizebox 包住的整张表都不会被抽取
    "graphicspath", "DeclareGraphicsExtensions", "hypersetup",
    "geometry", "linespread", "pagestyle", "thispagestyle", "pagenumbering",
    "clubpenalty", "widowpenalty", "interlinepenalty", "tolerance",
    "vfill", "hfill", "vskip", "hskip", "noindent", "indent", "par",
    "newpage", "clearpage", "cleardoublepage", "pagebreak", "linebreak",
    "bibliographyitem", "printbibliography", "appendix",
}
# 无参声明命令：只保护命令名本身，紧随其后的 {..} 分组仍是正文。
# 论文里常见 \noindent{\textbf{小标题}} {同一行紧接的长段}：若按「带参的不译命令」
# 处理，循环会把两个分组一并吞掉，小标题与整段正文都不进 segments，
# 最终留在中文 PDF 里是英文（2607.15036 实测漏 8 个小标题 + 3 段正文）。
DECLARATION_COMMANDS = {
    "noindent", "indent", "centering", "raggedright", "raggedleft",
    "flushleft", "flushright", "small", "normalsize", "large", "Large", "LARGE",
}

# 前后参数角色不同：值为「不译的下标」
MIXED_ARGS = {
    "href": {0}, "textcolor": {0}, "colorbox": {0}, "fcolorbox": {0, 1},
    "multicolumn": {0, 1}, "multirow": {0, 1}, "parbox": set(),
    "raisebox": {0}, "scalebox": {0}, "resizebox": {0, 1},
    "captionof": {0}, "import": {0}, "subimport": {0},
}

TEX_SOURCE_SUFFIX = {".tex", ".ltx", ".sty", ".cls"}

# 表格环境：内部按单元格处理（一个词也要译，否则表头永远英文）
TABLE_ENVS = {
    "tabular", "tabular*", "tabularx", "tabulary", "longtable", "longtable*",
    "supertabular", "xtabular", "xltabular", "tblr",
}
# 第一个必选参数是标题的环境
TITLE_ENVS = {"frame", "block", "alertblock", "exampleblock"}
# 可选参数是定理名的环境（\begin{theorem}[Convergence]）
THEOREM_ENVS = {
    "theorem", "lemma", "corollary", "proposition", "definition", "remark",
    "example", "assumption", "claim", "proof", "conjecture",
}


@dataclass
class Token:
    kind: str          # text / protect / break
    start: int
    end: int
    value: str
    # 所在语境：body / caption / cell / item / heading。
    # 语境决定「几个词才算值得翻译」——表格单元格、图注、\item[术语] 常常只有一个词
    # （Walk / Simulation. / Ours），按正文的「至少两个词」规则会被整条丢掉，
    # 结果正文全中文、表头和子图标题还是英文。
    ctx: str = "body"


@dataclass
class Seg:
    id: str
    file: str
    start: int
    end: int
    source: str
    masked: str
    protected: list[str]
    role: str = "paragraph"
    extra: dict = field(default_factory=dict)


# ── 扫描器 ────────────────────────────────────────────────
def find_env_end(s: str, start: int, name: str) -> int:
    """从 \\begin{name} 之后开始，找到配对的 \\end{name}，返回其结束位置。"""
    pattern = re.compile(
        r"\\(begin|end)\s*\{" + re.escape(name) + r"\}", re.S
    )
    depth = 1
    for m in pattern.finditer(s, start):
        depth += 1 if m.group(1) == "begin" else -1
        if depth == 0:
            return m.end()
    return -1


def read_group(s: str, i: int) -> int:
    """i 指向 '{'，返回配对 '}' 之后的位置。"""
    depth = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def read_bracket(s: str, i: int) -> int:
    """i 指向 '['，返回配对 ']' 之后的位置。"""
    depth = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == "]" and depth == 0:
            return i + 1
        i += 1
    return n


def read_math(s: str, i: int) -> int:
    """i 指向 $ 或 \\[ 或 \\(，返回数学区结束位置。"""
    n = len(s)
    if s.startswith("$$", i):
        j = s.find("$$", i + 2)
        return n if j < 0 else j + 2
    if s.startswith(r"\[", i):
        j = s.find(r"\]", i + 2)
        return n if j < 0 else j + 2
    if s.startswith(r"\(", i):
        j = s.find(r"\)", i + 2)
        return n if j < 0 else j + 2
    j = i + 1
    while j < n:
        if s[j] == "\\":
            j += 2
            continue
        if s[j] == "$":
            return j + 1
        j += 1
    return n


CAPTION_RE = re.compile(r"\\caption\s*(?:\[[^\]]*\])?\s*\{")


def carve_caption(s: str, start: int, stop: int) -> list[Token]:
    """把不译环境（algorithm）里的 \\caption{...} 参数挖成可译片段。

    其余部分整体保护，回填时按标记原样还原，所以伪代码一个字都不会变。
    algorithm2e 的标题就是正文里的「Algorithm 1: Training procedure」，
    不译的话整篇中文里会突兀地留一行英文。
    """
    spans = []
    for m in CAPTION_RE.finditer(s, start, stop):
        gs = m.end() - 1
        ge = read_group(s, gs)
        if ge > stop:
            break
        inner = tokenize(s, gs + 1, ge - 1, ctx="caption")
        if any(t.kind == "text" and t.value.strip() for t in inner):
            spans.append((gs, ge, inner))

    toks: list[Token] = []
    cursor = start
    for gs, ge, inner in spans:
        if gs > cursor:
            toks.append(Token("protect", cursor, gs, s[cursor:gs], "body"))
        toks.append(Token("protect", gs, gs + 1, s[gs:gs + 1], "body"))
        toks.extend(inner)
        toks.append(Token("protect", ge - 1, ge, s[ge - 1:ge], "body"))
        cursor = ge
    if cursor < stop:
        toks.append(Token("protect", cursor, stop, s[cursor:stop], "body"))
    return toks


def tokenize(s: str, start: int = 0, end: int | None = None,
             ctx: str = "body") -> list[Token]:
    """把源码切成 text / protect / break 三种 token。

    start/end 支持对命令参数做子区间递归，这样 \\caption{\\textbf{X} $y$} 里的
    \\textbf 与公式仍会被正确保护。ctx 随参数递归下传、并随环境切换
    （tabular → cell），决定这段文字要按「正文」还是「短标签」的规则判定。
    """
    out: list[Token] = []
    n = len(s) if end is None else min(end, len(s))
    i = start
    stack = [ctx]

    def cur() -> str:
        return stack[-1]

    def push(kind: str, a: int, b: int):
        if b > a:
            out.append(Token(kind, a, b, s[a:b], cur()))

    while i < n:
        c = s[i]

        # 注释：作为保护片段，回填时原样保留
        if c == "%":
            j = s.find("\n", i)
            j = n if j < 0 or j > n else j
            push("protect", i, j)
            i = j

        # 空白行 = 段落边界
        elif c == "\n":
            m = re.match(r"\n[ \t\r]*\n", s[i:n])
            if m:
                push("break", i, i + m.end())
                i += m.end()
            else:
                j = i + 1
                while j < n and s[j] in " \t\r":
                    j += 1
                if j < n and s[j] == "\n":
                    push("break", i, j)
                else:
                    push("text", i, j)
                i = j

        # 反斜杠：命令 / 环境 / 转义
        elif c == "\\":
            if s.startswith(r"\[", i) or s.startswith(r"\(", i):
                j = read_math(s, i)
                push("protect", i, j)
                i = j
                continue
            if s.startswith(r"\\", i):
                j = i + 2
                if j < n and s[j] == "*":
                    j += 1
                m = re.match(r"\s*\[[^\]]*\]", s[j:])
                if m:
                    j += m.end()
                # 换行是结构符号：表格行、标题断行都靠它，必须断段
                push("break", i, j)
                i = j
                continue
            m = re.match(r"\\([A-Za-z@]+\*?)", s[i:n])
            if not m:
                # 转义字符：\% \& \$ \_ \{ \} 等
                j = min(i + 2, n)
                push("protect", i, j)
                i = j
                continue
            name = m.group(1).rstrip("*")
            j = i + m.end()

            # \begin{env} / \end{env}
            if name in ("begin", "end"):
                k = j
                while k < n and s[k] in " \t":
                    k += 1
                if k < n and s[k] == "{":
                    e = read_group(s, k)
                    env = s[k + 1: e - 1].strip()

                    if name == "begin" and env in OPAQUE_ENVS | MATH_ENVS:
                        stop = find_env_end(s, e, env)
                        if stop > 0:
                            # 算法块整体不译，但标题要译：把 \caption 的参数挖出来，
                            # 否则正文全中文、只有「Algorithm 1: Training procedure」是英文。
                            if env == "algorithm":
                                out.extend(carve_caption(s, i, stop))
                            else:
                                push("protect", i, stop)
                            i = stop
                            continue

                    # 环境定界符后的可选参数（[ht]、[t]）与必选参数（列格式）
                    # 都属于结构，不能进入正文，否则 {lccc} 会被当成散文翻译
                    k2 = e
                    while k2 < n and s[k2] in " \t":
                        k2 += 1
                    opts: list[tuple[int, int]] = []
                    while k2 < n and s[k2] == "[":
                        be = read_bracket(s, k2)
                        opts.append((k2, be))
                        k2 = be
                        while k2 < n and s[k2] in " \t":
                            k2 += 1
                    args: list[tuple[int, int]] = []
                    while k2 < n and s[k2] == "{":
                        ge = read_group(s, k2)
                        args.append((k2, ge))
                        k2 = ge
                        while k2 < n and s[k2] in " \t":
                            k2 += 1

                    if name == "end":
                        if env in TABLE_ENVS and len(stack) > 1:
                            stack.pop()
                        push("break", i, k2)
                        i = k2
                        continue

                    # beamer 的 \begin{frame}{标题}、\begin{block}{标题}，
                    # 以及 \begin{theorem}[名字]：这些是纸面上的字，必须译
                    title_span = None
                    if env in TITLE_ENVS and args:
                        title_span = args[0]
                    elif env in THEOREM_ENVS and opts:
                        title_span = opts[0]

                    if title_span:
                        ts, te = title_span
                        push("break", i, ts)
                        push("protect", ts, ts + 1)
                        out.extend(tokenize(s, ts + 1, te - 1, ctx="heading"))
                        push("protect", te - 1, te)
                        push("break", te, k2)
                    else:
                        push("break", i, k2)

                    if env in TABLE_ENVS:
                        stack.append("cell")
                    i = k2
                    continue
                push("protect", i, j)
                i = j
                continue

            # 无参声明命令：保留命令名，让后面的 {..} 分组按正文抽取。
            # 必须放在「必选参数」解析之前，否则 \noindent{\textbf{X}} {长段}
            # 的两个分组会被当成同一条命令的参数整体保护。
            if name in DECLARATION_COMMANDS:
                push("protect", i, j)
                i = j
                continue

            # 段落命令
            if name in ("par", "item", "newpage", "clearpage", "appendix"):
                k = j
                while k < n and s[k] in " \t":
                    k += 1
                # \item[术语] 的可选参数是正文（description 列表的条目名），
                # 之前当成结构整体跳过，结果条目名永远英文、解释却是中文。
                if name == "item" and k < n and s[k] == "[":
                    e = read_bracket(s, k)
                    push("break", i, k)
                    push("protect", k, k + 1)
                    out.extend(tokenize(s, k + 1, e - 1, ctx="item"))
                    push("protect", e - 1, e)
                    i = e
                    continue
                if k < n and s[k] == "[":
                    k = read_bracket(s, k)
                push("break", i, k)
                i = k
                continue

            # 可选参数 [..]，通常不译（尺寸、位置、标签）
            k = j
            opts_end = k
            while k < n and s[k] in " \t":
                k += 1
            while k < n and s[k] == "[":
                k = read_bracket(s, k)
                while k < n and s[k] in " \t":
                    k += 1
                opts_end = k
            # 必选参数 {..}
            args_start = k
            groups = []
            while k < n and s[k] == "{":
                e = read_group(s, k)
                groups.append((k, e))
                k = e
                while k < n and s[k] in " \t":
                    k += 1

            translatable = name in TEXT_COMMANDS or (
                name in MIXED_ARGS and len(groups) > 0
            )
            if translatable and name not in OPAQUE_COMMANDS:
                # 命令本身与其可选参数保持保护，只让文本参数进入正文
                if opts_end > j:
                    push("protect", i, opts_end)
                    local_start = opts_end
                else:
                    push("protect", i, args_start)
                    local_start = args_start
                opaque_idx = MIXED_ARGS.get(name, set())
                # 题注 / 标题里的文字按短标签处理（一个词也译）。
                # 必须取 cur() 而不是函数参数 ctx：表格里 \textbf{Environment}
                # 的 ctx 是 cell，用参数会退回 body，单词表头就被丢掉。
                sub_ctx = cur()
                if name in ("caption", "subcaption", "captionof"):
                    sub_ctx = "caption"
                elif name in ("section", "subsection", "subsubsection", "paragraph",
                              "subparagraph", "part", "chapter", "title"):
                    sub_ctx = "heading"
                cursor = local_start
                for idx, (gs, ge) in enumerate(groups):
                    if idx in opaque_idx:
                        push("protect", cursor, ge)
                    else:
                        # 左括号保护，内容递归扫描，右括号保护
                        push("protect", cursor, gs + 1)
                        out.extend(tokenize(s, gs + 1, ge - 1, ctx=sub_ctx))
                        push("protect", ge - 1, ge)
                    cursor = ge
                i = k
                continue

            # 其它命令：连参数整体保护
            push("protect", i, k if k > j else j)
            i = k if k > j else j

        # 数学
        elif c == "$":
            j = read_math(s, i)
            push("protect", i, j)
            i = j

        # 表格列分隔符：结构符号，断段
        elif c == "&":
            push("break", i, i + 1)
            i += 1

        else:
            j = i
            while j < n and s[j] not in "\\$%&\n":
                j += 1
            if j == i:
                j = i + 1
            push("text", i, j)
            i = j

    return out


def merge_text(tokens: list[Token]) -> list[Token]:
    """把相邻的 text token 合并，避免碎段。"""
    merged: list[Token] = []
    for t in tokens:
        if merged and t.kind == "text" and merged[-1].kind == "text":
            prev = merged[-1]
            merged[-1] = Token("text", prev.start, t.end, prev.value + t.value, prev.ctx)
        else:
            merged.append(t)
    return merged


HEADING_RE = re.compile(r"^\s*\\(?:sub)*section\*?\s*\{|^\s*\\paragraph\s*\{")


def _has_words(s: str) -> bool:
    return bool(re.search(r"[A-Za-z\u4e00-\u9fff]{2,}", s))


SHORT_WORDS = {"no", "yes", "on", "off"}


def looks_like_prose(text: str, min_words: int = 2,
                     allow_caps: bool = False) -> bool:
    r"""判断一段文本是否值得翻译。

    正文要求「至少两个英文词」，避免把符号、编号、缩写送去翻译；但题注、表格单元格、
    \item 术语常常只有一个词（Ours / Walk / Simulation.），这些位置 min_words 传 1，
    单个人名化的普通单词（首字母大写或全小写、至少 3 个字母）也算可译。

    全大写缩写（PPO / GPU）默认跳过，但 IEEE 系模板的章节标题恰恰是整词大写
    （\section{METHOD} / \section{EXPERIMENTS}）：把这类标题丢掉，中文 PDF 里就会
    留着「III. METHOD」。因此 heading / caption / item 语境由调用方传 allow_caps=True，
    表格单元格（cell）仍保持过滤 RVO / PPO 之类缩写。
    """
    stripped = re.sub(r"\s+", " ", text).strip()
    if len(stripped) < 2:
        return False
    if re.fullmatch(r"[\s\d\W]*", stripped):
        return False
    words = re.findall(r"[A-Za-z]{2,}", stripped)
    if len(re.findall(r"[\u4e00-\u9fff]", stripped)) >= 2:
        return True
    if min_words >= 2:
        return len(words) >= 2
    if len(words) >= 2:
        return True
    if len(words) == 1:
        w = words[0]
        if w.isupper() and not allow_caps:
            return False
        # 表格里的开关值常常只有两个字母（no / on），也要译
        return len(w) >= 3 or w.lower() in SHORT_WORDS
    return False


def group_segments(tokens: list[Token], text: str, rel: str,
                   id_counter: list[int], role: str = "paragraph") -> list[Seg]:
    """把连续的可译内容合并成 segment。"""
    segs: list[Seg] = []
    buf: list[Token] = []

    def flush():
        nonlocal buf
        if not buf:
            return
        # 去掉首尾纯空白
        while buf and buf[0].kind == "text" and not buf[0].value.strip():
            buf.pop(0)
        while buf and buf[-1].kind == "text" and not buf[-1].value.strip():
            buf.pop()
        if not buf:
            return

        source = text[buf[0].start: buf[-1].end]
        # 章节标题常只有一个词（Introduction / Preliminaries），表格单元格、图注、
        # \item[术语] 也一样，这些位置放宽到「一个词也算」。
        # 语境取第一个真正的文字 token：segment 往往以保护标记开头（{\caption 的
        # 左括号、\item 的 '['），那是 body 语境，会把「一个词也译」的规则吃掉。
        ctx = next((t.ctx for t in buf if t.kind == "text" and t.value.strip()), "body")
        # 普通 \item 后面紧跟的短语（`\item Vectors:`）也是条目名，往往只有一个词，
        # 之前按正文的「至少两个词」规则整条丢掉，列表里就留下英文小标题
        head = text[max(0, buf[0].start - 32): buf[0].start]
        after_item = re.search(r"\\item\s*$", head) is not None
        heading = bool(HEADING_RE.match(source)) and _has_words(source)
        min_words = 1 if (heading or after_item
                          or ctx in ("heading", "caption", "cell", "item")) else 2
        # 整词大写的章节标题（METHOD / EXPERIMENTS）要收；表格单元格里的
        # PPO / RVO 之类缩写仍按原规则跳过，所以不给 cell 开这个口子。
        allow_caps = heading or ctx in ("heading", "caption", "item")
        if not looks_like_prose(source, min_words, allow_caps):
            buf = []
            return

        parts, protected = [], []
        i = 0
        while i < len(buf):
            if buf[i].kind != "protect":
                parts.append(buf[i].value)
                i += 1
                continue
            # 连续的结构标记合并成一个，中间纯空白并入，减少模型看到的碎片
            j, end_pos = i, buf[i].end
            while j + 1 < len(buf):
                nxt = buf[j + 1]
                if nxt.kind == "protect":
                    j += 1
                    end_pos = nxt.end
                elif nxt.kind == "text" and not nxt.value.strip():
                    j += 1
                else:
                    break
            protected.append(text[buf[i].start:end_pos])
            parts.append(MARKER.format(len(protected)))
            i = j + 1
        masked = re.sub(r"[ \t]+", " ", "".join(parts)).strip()
        masked = re.sub(r" *\n *", " ", masked)

        # 全部是标记、没有实际词的段落没有翻译价值
        core = MARKER_RE.sub("", masked)
        if not looks_like_prose(core, min_words, allow_caps):
            buf = []
            return

        id_counter[0] += 1
        segs.append(Seg(
            id=f"{rel}#{id_counter[0]:04d}",
            file=rel,
            start=buf[0].start,
            end=buf[-1].end,
            source=source,
            masked=masked,
            protected=protected,
            role=role,
        ))
        buf = []

    for tok in tokens:
        if tok.kind == "break":
            flush()
        else:
            buf.append(tok)
    flush()
    return segs


def extract_file(path: Path, rel: str, in_body_only: bool = True) -> list[Seg]:
    text = read_text(path)
    m = re.search(r"\\begin\s*\{document\}", text)

    if not (in_body_only and m):
        return group_segments(merge_text(tokenize(text)), text, rel, [0])

    segs: list[Seg] = []
    # preamble 里只取「会印到纸面上」的命令参数，正文另有大量定义不应翻译。
    # role / setheadertext / thanks 之前漏了：结果是页眉还写 Technical Report、
    # 作者脚注还是 Equal Contributions，正文却全中文（典型的半中半英）。
    for cmd in ("title", "role", "setheadertext", "thanks"):
        pat = re.compile(r"\\" + cmd + r"\s*(?:\[[^\]]*\])?\s*\{")
        for mm in pat.finditer(text[: m.start()]):
            gs = mm.end() - 1
            ge = read_group(text, gs)
            if ge <= gs + 1:
                continue
            inner = merge_text(tokenize(text, gs + 1, ge - 1))
            segs.extend(group_segments(inner, text, rel, [0], role="head"))

    # 部分模板（如 thu 系列的 thuc3i.cls）把 abstract 写在 \begin{document} 之前，
    # 只按正文抽取会整段漏掉摘要——这是论文最该译的部分。
    pre = text[: m.start()]
    for mm in re.finditer(r"\\begin\s*\{abstract\}(.*?)\\end\s*\{abstract\}", pre, re.S):
        inner = merge_text(tokenize(text, mm.start(1), mm.end(1)))
        segs.extend(group_segments(inner, text, rel, [0]))

    body = group_segments(merge_text(tokenize(text, m.start())), text, rel, [0])
    return segs + body


def fix_ids(segs: list[Seg]) -> None:
    for i, seg in enumerate(segs, 1):
        seg.id = f"{seg.file}#{i:04d}"


def learn_listing_envs(texts: list[str]) -> set[str]:
    """论文常自建 \\lstnewenvironment{normalcode} 之类的代码环境，
    名字不在 OPAQUE_ENVS 里就会被当成正文抽出来。这里动态收集。
    """
    found = set()
    for t in texts:
        found |= set(re.findall(r"\\lstnewenvironment\s*\{([^}]+)\}", t))
        found |= set(re.findall(r"\\newenvironment\s*\{([^}]+)\}\s*\{\s*\\begin\{lstlisting\}", t))
    return found


def zh_fingerprint(zh: Path, files: list[str]) -> str:
    """zh/ 内容的指纹。注入块长度变化会让所有偏移量平移，旧 segments 就不能再用。"""
    h = hashlib.sha256()
    targets = [zh / f for f in files]
    targets += sorted(zh.glob("*.sty")) + sorted(zh.glob("*.cls"))
    for p in targets:
        if not p.is_file():
            continue
        h.update(p.name.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:16]


def main():
    ap = argparse.ArgumentParser(description="抽取可翻译片段")
    ap.add_argument("project")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    root = Path(args.project).resolve()
    arz = meta_dir(root)
    manifest = load_json(arz / "manifest.json")
    if not manifest:
        die("缺少 manifest.json，请先跑 fetch.py")
    zh = root / "zh"
    if not zh.is_dir():
        die("zh/ 不存在，请先跑 inject_cjk.py")

    files = [f for f in manifest["tex_files"]
             if (zh / f).is_file() and not re.search(r"(^|/)00README", f)]
    stamp = zh_fingerprint(zh, files)

    out = arz / "segments.json"
    if out.is_file() and not args.force:
        data = load_json(out)
        if data.get("fingerprint") == stamp:
            ok(f"复用已有抽取结果：{len(data['segments'])} 段")
            return
        # 旧包没有 fingerprint 字段时也在这里重新抽，宁可多跑几秒
        warn("zh/ 内容与上次抽取时不同（多半是重新注入了字体或改了类文件），重新抽取")

    # 先扫一遍全文，把自定义代码环境登记进不译集合
    discovered = learn_listing_envs([read_text(zh / f) for f in files])
    if discovered:
        OPAQUE_ENVS.update(discovered)
        ok(f"识别到自定义代码环境：{', '.join(sorted(discovered))}")

    all_segs: list[Seg] = []
    per_file = {}

    print(f"\n[{root.name}] 抽取可译片段\n")
    for rel in files:
        segs = extract_file(zh / rel, rel, in_body_only=True)
        fix_ids(segs)
        all_segs.extend(segs)
        total = len(read_text(zh / rel))
        covered = sum(s.end - s.start for s in segs)
        per_file[rel] = {
            "segments": len(segs),
            "chars": covered,
            "file_chars": total,
            "ratio": round(covered / total, 3) if total else 0,
        }
        print(f"  {rel:<28} {len(segs):>3} 段  "
              f"覆盖 {covered:>6} / {total:>6} 字符  ({per_file[rel]['ratio']:.0%})")

    save_json(out, {
        "project": str(root),
        "fingerprint": stamp,
        "files": per_file,
        "segments": [asdict(s) for s in all_segs],
        "stats": {
            "total": len(all_segs),
            "chars": sum(s.end - s.start for s in all_segs),
        },
    })

    total_chars = sum(v["file_chars"] for v in per_file.values())
    covered = sum(v["chars"] for v in per_file.values())
    ratio = covered / total_chars if total_chars else 0
    print(f"\n  合计 {len(all_segs)} 段，覆盖源码 {ratio:.0%}")
    if ratio < 0.18:
        warn("覆盖率异常低：可能主文件用 \\input 拆分，或大量内容在不译环境里")
    print(f"\n  输出 {out}")
    print(f"\n下一步：翻译 segments.json 里的 masked 字段，写入 translations.json")


if __name__ == "__main__":
    main()
