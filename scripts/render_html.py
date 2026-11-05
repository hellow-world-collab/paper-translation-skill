#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把已对齐的段落数据渲染成「原文 / 译文」双语 HTML 阅读页。

与 build.py 产出的 PDF 互补：PDF 保排版，HTML 保可读性——公式用 KaTeX 渲染、
插图从源码 PDF 直接高清重绘、表格从编译结果裁切、引用可点击跳到文末条目。
"""

from __future__ import annotations

import argparse
import base64
import html as H
import re
import shutil
from pathlib import Path

from _common import MARKER_RE, die, load_json, meta_dir, ok, read_text, warn

SKILL_ROOT = Path(__file__).resolve().parent.parent
ASSETS_ROOT = SKILL_ROOT / "assets"
KATEX_ROOT = ASSETS_ROOT / "katex"


def katex_asset(*rel: str) -> Path | None:
    """定位 KaTeX 文件，两种布局都认。

    仓库里是 `assets/katex/katex.min.css`（上游原样，含 fonts/）；
    技能市场包只允许两级目录，所以打包时压成扁平的 `assets/katex.min.css`
    （字体已内联成 data URI）和 `assets/auto-render.min.js`。
    """
    for p in (KATEX_ROOT.joinpath(*rel), ASSETS_ROOT / rel[-1]):
        if p.is_file():
            return p
    return None

# --------------------------------------------------------------------------
# 结构扫描
# --------------------------------------------------------------------------

SECTION_RE = re.compile(
    r"\\(section|subsection|subsubsection)\*?\s*(?:\[[^\]]*\])?\s*\{")
FLOAT_RE = re.compile(r"\\begin\{(figure\*?|table\*?)\}")
ABSTRACT_RE = re.compile(r"\\begin\{abstract\}")
CAPTION_RE = re.compile(r"\\caption\s*(?:\[[^\]]*\])?\s*\{")
LABEL_RE = re.compile(r"\\label\s*\{([^}]*)\}")
INCLUDE_RE = re.compile(r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}")

SECTION_LEVEL = {"section": 1, "subsection": 2, "subsubsection": 3}


def brace_end(text: str, i: int) -> int:
    """i 指向 '{'，返回配对的 '}' 下标；失败返回 -1。"""
    depth = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def scan_structure(text: str) -> list[dict]:
    """抽出章节标题与浮动体的位置区间，用于和段落按偏移量交错排序。"""
    items: list[dict] = []

    for m in SECTION_RE.finditer(text):
        b = m.end() - 1
        e = brace_end(text, b)
        if e < 0:
            continue
        # 紧跟在标题后的 \label 用作锚点，让正文里的 \ref 能跳过来
        lm = re.match(r"\s*\\label\s*\{([^}]*)\}", text[e + 1:e + 160])
        items.append({
            "kind": "section",
            "level": SECTION_LEVEL[m.group(1)],
            "start": m.start(),
            "arg_start": b + 1,
            "arg_end": e,
            "title_tex": text[b + 1:e],
            "label": lm.group(1) if lm else "",
        })

    for m in FLOAT_RE.finditer(text):
        env = m.group(1)
        end_tag = "\\end{%s}" % env
        e = text.find(end_tag, m.end())
        if e < 0:
            continue
        body = text[m.end():e]
        # 有子图时前面的 \caption 都是子图题注，最后一个才是整图题注
        cap_start = cap_end = -1
        cap_tex = ""
        for cap in CAPTION_RE.finditer(body):
            b = m.end() + cap.end() - 1
            ce = brace_end(text, b)
            if ce > 0:
                cap_start, cap_end = b + 1, ce
                cap_tex = text[b + 1:ce]
        label = LABEL_RE.search(body)
        code_blocks = []
        for cm in re.finditer(
                r"\\begin\{(normalcode|diffdelcode|diffaddcode|lstlisting\*?)\}"
                r"(.*?)\\end\{\1\}", body, re.S):
            code_blocks.append((cm.group(1), cm.group(2)))
        items.append({
            "kind": "table" if env.startswith("table") else "figure",
            "start": m.start(),
            "end": e + len(end_tag),
            "images": INCLUDE_RE.findall(body),
            "label": label.group(1) if label else "",
            "cap_start": cap_start,
            "cap_end": cap_end,
            "caption_tex": cap_tex,
            "code_blocks": code_blocks,
        })

    return items


# --------------------------------------------------------------------------
# 辅助信息：标签编号、引用编号、参考文献、自定义宏
# --------------------------------------------------------------------------

def parse_aux(aux: Path) -> tuple[dict, dict]:
    """从 .aux 取 \newlabel 编号与 \bibcite 引用序号。"""
    labels, bibs = {}, {}
    if not aux.is_file():
        return labels, bibs
    text = read_text(aux)
    for m in re.finditer(r"\\newlabel\{([^}]+)\}\{\{([^}]*)\}\{([^}]*)\}", text):
        labels[m.group(1)] = {"num": m.group(2), "page": m.group(3)}
    for m in re.finditer(r"\\bibcite\{([^}]+)\}\{\{?(\d+)\}?", text):
        bibs[m.group(1)] = m.group(2)
    return labels, bibs


def parse_bbl(bbl: Path) -> list[dict]:
    """解析 thebibliography，得到按编号排序的文献条目。"""
    if not bbl.is_file():
        return []
    text = read_text(bbl)
    body = text
    m = re.search(r"\\begin\{thebibliography\}", text)
    if m:
        body = text[m.end():]
        e = body.find("\\end{thebibliography}")
        if e > 0:
            body = body[:e]

    chunks = re.split(r"\\bibitem(?:\[[^\]]*\])?\{([^}]+)\}", body)
    # chunks: [前言, key1, 正文1, key2, 正文2, ...]
    out = []
    for i in range(1, len(chunks) - 1, 2):
        key = chunks[i]
        raw = chunks[i + 1]
        raw = raw.replace("\\newblock", " ")
        out.append({"key": key, "raw": raw.strip()})
    return out


def parse_macros(paths: list[Path]) -> dict:
    """收集 \\newcommand / \\def 定义的简单宏，供正文展开。"""
    macros: dict[str, tuple[int, str]] = {}
    pat = re.compile(
        r"\\(?:newcommand|renewcommand|providecommand)\*?\s*\{\s*\\([A-Za-z@]+)\s*\}"
        r"\s*(?:\[(\d)\])?\s*\{")
    for p in paths:
        if not p.is_file():
            continue
        text = read_text(p)
        for m in pat.finditer(text):
            b = m.end() - 1
            e = brace_end(text, b)
            if e < 0:
                continue
            name, arity = m.group(1), int(m.group(2) or 0)
            if name in macros:
                continue
            macros[name] = (arity, text[b + 1:e])
    # \def\name{...}
    for p in paths:
        if not p.is_file():
            continue
        for m in re.finditer(r"\\def\s*\\([A-Za-z@]+)\s*\{", read_text(p)):
            b = m.end() - 1
            e = brace_end(read_text(p), b)
            if e > 0 and m.group(1) not in macros:
                macros[m.group(1)] = (0, read_text(p)[b + 1:e])
    return macros


# --------------------------------------------------------------------------
# LaTeX → HTML
# --------------------------------------------------------------------------

TAG_CMDS = {
    "textbf": "strong", "bf": "strong", "textit": "em", "it": "em", "emph": "em",
    "textsl": "em", "textsc": "span", "texttt": "code", "underline": "u",
    "uline": "u", "textsuperscript": "sup", "textsubscript": "sub",
    "textrm": "span", "textsf": "span", "textnormal": "span", "mbox": "span",
    "text": "span", "hl": "mark", "sout": "s", "mathrm": "span",
}

ESCAPES = {
    "%": "%", "&": "&amp;", "_": "_", "#": "#", "$": "$", "{": "{", "}": "}",
    " ": " ", ",": " ", ";": " ", ":": " ", "!": "", "\\": "<br>",
    "-": "", "/": "", "@": "",
}

COLOR_NAMES = {
    "darkred": "#8b1a1a", "red": "#c0392b", "blue": "#1f4e9c", "green": "#1e6b3a",
    "gray": "#6b7280", "grey": "#6b7280", "black": "#000", "orange": "#b45309",
}


class Tex2HTML:
    def __init__(self, labels, bibs, macros):
        self.labels = labels
        self.bibs = bibs
        self.macros = macros

    # -- 对外 ------------------------------------------------------------
    def inline(self, s: str) -> str:
        return self._conv(s, 0)

    # -- 内部 ------------------------------------------------------------
    def _group(self, s: str, i: int) -> tuple[str, int]:
        """i 指向 '{' 之后。返回 (内容, 结束后的下标)。"""
        start, depth = i, 0
        while i < len(s):
            c = s[i]
            if c == "\\":
                i += 2
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                if depth == 0:
                    return s[start:i], i + 1
                depth -= 1
            i += 1
        return s[start:], len(s)

    def _conv(self, s: str, depth: int) -> str:
        if depth > 12:
            return H.escape(s)
        out: list[str] = []
        i, n = 0, len(s)
        while i < n:
            c = s[i]

            # ---- 数学 ----
            if c == "$":
                display = s.startswith("$$", i)
                delim = "$$" if display else "$"
                j = i + len(delim)
                while j < n:
                    if s[j] == "\\":
                        j += 2
                        continue
                    if s.startswith(delim, j):
                        break
                    j += 1
                if j >= n:
                    i += len(delim)
                    continue
                body = s[i + len(delim):j]
                out.append(self._math(body, display))
                i = j + len(delim)
                continue

            # ---- 命令 ----
            if c == "\\":
                m = re.match(r"\\([A-Za-z@]+\*?|.)", s[i:])
                if not m:
                    i += 1
                    continue
                name = m.group(1)
                i += m.end()
                cur = [i]
                out.append(self._command(name, s, cur, depth))
                i = cur[0]
                continue

            # ---- 符号 ----
            if c == "~":
                out.append("&nbsp;")
                i += 1
                continue
            if s.startswith("---", i):
                out.append("—")
                i += 3
                continue
            if s.startswith("--", i):
                out.append("–")
                i += 2
                continue
            if s.startswith("``", i):
                out.append("“")
                i += 2
                continue
            if s.startswith("''", i):
                out.append("”")
                i += 2
                continue
            if c == "`":
                out.append("‘")
                i += 1
                continue
            if c == "'":
                out.append("’")
                i += 1
                continue
            if c in "{}":
                i += 1
                continue
            out.append(H.escape(c))
            i += 1
        return "".join(out)

    def _math(self, body: str, display: bool) -> str:
        # KaTeX 不认识 \label / \nonumber，带着它们整条公式会渲染成红色错误
        body = re.sub(r"\\label\s*\{[^}]*\}", "", body)
        body = re.sub(r"\\(?:nonumber|notag|allowdisplaybreaks)\b", "", body)
        body = re.sub(r"\\eqref\s*\{([^}]*)\}", r"(\\ref{\1})", body)
        body = body.strip()
        esc = body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if display:
            return '<span class="math math-display">\\[%s\\]</span>' % esc
        return '<span class="math">\\(%s\\)</span>' % esc

    def _command(self, name: str, s: str, cur: list, depth: int) -> str:
        """处理命令；cur[0] 回传消费后的位置（用列表而非实例属性，避免递归污染）。"""
        i = cur[0]
        base = name.rstrip("*")

        # 单字符转义
        if len(name) == 1:
            if name in ESCAPES:
                cur[0] = i
                return ESCAPES[name]
            return ""

        # --- 交叉引用 ---
        if base in ("cite", "citep", "citet", "citealp", "citeauthor", "citep*"):
            arg, j = self._opt_group(s, i)
            cur[0] = j
            keys = [k.strip() for k in arg.split(",") if k.strip()]
            nums = [self.bibs.get(k) for k in keys]
            shown = ", ".join(x for x in nums if x) or "?"
            first = keys[0] if keys else ""
            return '<a class="cite" href="#bib-%s">[%s]</a>' % (
                H.escape(first), H.escape(shown))

        if base in ("ref", "autoref", "cref", "Cref", "eqref", "pageref"):
            arg, j = self._opt_group(s, i)
            cur[0] = j
            info = self.labels.get(arg.strip())
            num = info["num"] if info else "?"
            if base == "eqref":
                return "(%s)" % H.escape(num)
            return '<a class="xref" href="#%s">%s</a>' % (H.escape(arg.strip()), H.escape(num))

        if base == "label":
            arg, j = self._opt_group(s, i)
            cur[0] = j
            return '<a id="%s"></a>' % H.escape(arg.strip())

        if base in ("url", "href"):
            arg, j = self._opt_group(s, i)
            if base == "href":
                text, j2 = self._opt_group(s, j)
                cur[0] = j2
                return '<a href="%s" target="_blank" rel="noopener">%s</a>' % (
                    H.escape(arg), self._conv(text, depth + 1))
            cur[0] = j
            return '<a href="%s" target="_blank" rel="noopener">%s</a>' % (
                H.escape(arg), H.escape(arg))

        # --- 样式 ---
        if base == "textcolor":
            color, j = self._opt_group(s, i)
            text, j2 = self._opt_group(s, j)
            cur[0] = j2
            col = COLOR_NAMES.get(color.strip().lstrip("@").strip(), None)
            style = ' style="color:%s"' % col if col else ' class="hl-color"'
            return "<span%s>%s</span>" % (style, self._conv(text, depth + 1))

        if base in TAG_CMDS:
            text, j = self._opt_group(s, i)
            cur[0] = j
            tag = TAG_CMDS[base]
            if tag == "span" and base == "textsc":
                return '<span class="sc">%s</span>' % self._conv(text, depth + 1)
            return "<%s>%s</%s>" % (tag, self._conv(text, depth + 1), tag)

        if base in ("xspace", "relax", "noindent", "centering", "protect", "hspace"):
            if base in ("hspace", "vspace"):
                cur[0] = self._opt_group(s, i)[1]
            return ""

        # --- 环境 ---
        if base == "begin":
            env, j = self._opt_group(s, i)
            env = env.strip()
            if env in ("itemize", "enumerate", "description"):
                endtag = "\\end{%s}" % env
                e = s.find(endtag, j)
                if e < 0:
                    cur[0] = j
                    return ""
                body = s[j:e]
                cur[0] = e + len(endtag)
                tag = "ol" if env == "enumerate" else "ul"
                items = [x for x in re.split(r"\\item", body) if x.strip()]
                lis = "".join("<li>%s</li>" % self._conv(x.strip(), depth + 1) for x in items)
                return "<%s>%s</%s>" % (tag, lis, tag)
            if env in ("equation", "equation*", "align", "align*", "eqnarray",
                       "gather", "gather*", "multline", "multline*"):
                endtag = "\\end{%s}" % env
                e = s.find(endtag, j)
                if e < 0:
                    cur[0] = j
                    return ""
                body = s[j:e]
                cur[0] = e + len(endtag)
                return self._math(body, True)
            cur[0] = j
            return ""

        if base == "end":
            arg, j = self._opt_group(s, i)
            cur[0] = j
            return ""

        # --- 自定义宏 ---
        if name in self.macros or base in self.macros:
            arity, body = self.macros.get(name) or self.macros.get(base)
            args = []
            j = i
            for _ in range(arity):
                a, j = self._opt_group(s, j)
                args.append(a)
            cur[0] = j
            expanded = body
            for k, a in enumerate(args, 1):
                expanded = expanded.replace("#%d" % k, a)
            return self._conv(expanded, depth + 1)

        # --- 希腊字母等数学符号 ---
        if base in GREEK:
            cur[0] = i
            return '<span class="math">\\(%s\\)</span>' % GREEK[base]

        # --- 未知命令：保住参数里的文字 ---
        if i < len(s) and s[i] == "{":
            text, j = self._opt_group(s, i)
            cur[0] = j
            return self._conv(text, depth + 1)
        return ""

    def _opt_group(self, s: str, i: int) -> tuple[str, int]:
        """跳过可选参数 [..]，再读必选参数 {..}；返回 (内容, 新下标)。"""
        while i < len(s) and s[i] in " \t\n":
            i += 1
        if i < len(s) and s[i] == "[":
            e = s.find("]", i)
            if e > 0:
                i = e + 1
                while i < len(s) and s[i] in " \t\n":
                    i += 1
        if i < len(s) and s[i] == "{":
            return self._group(s, i + 1)
        # 无花括号：取到空白为止
        m = re.match(r"[^\s{}]+", s[i:])
        if m:
            return m.group(0), i + m.end()
        return "", i


GREEK = {
    "alpha": "\\alpha", "beta": "\\beta", "gamma": "\\gamma", "delta": "\\delta",
    "epsilon": "\\epsilon", "theta": "\\theta", "lambda": "\\lambda", "mu": "\\mu",
    "pi": "\\pi", "sigma": "\\sigma", "tau": "\\tau", "phi": "\\phi", "psi": "\\psi",
    "omega": "\\omega", "Delta": "\\Delta", "Sigma": "\\Sigma", "Omega": "\\Omega",
}


def plain(tex: str) -> str:
    """把 LaTeX 片段压成纯文本，用于搜索定位 / alt 文本。"""
    t = re.sub(r"\$[^$]*\$", " ", tex)
    t = re.sub(r"\\[A-Za-z@]+\*?", " ", t)
    t = re.sub(r"[{}~]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# --------------------------------------------------------------------------
# 浮动体：插图重绘 / 表格裁切
# --------------------------------------------------------------------------

def render_images(src_root: Path, paths: list[str], out_dir: Path, slug: str) -> list[str]:
    names = []
    for idx, rel in enumerate(paths):
        candidates = [src_root / rel, src_root / "figures" / Path(rel).name,
                      src_root / Path(rel).name]
        f = next((c for c in candidates if c.is_file()), None)
        if not f:
            warn(f"找不到插图文件：{rel}")
            continue
        name = "%s-%d.png" % (slug, idx)
        try:
            if f.suffix.lower() == ".pdf":
                import pymupdf
                doc = pymupdf.open(str(f))
                pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(3, 3), alpha=False)
                pix.save(str(out_dir / name))
            elif f.suffix.lower() in (".png", ".jpg", ".jpeg"):
                shutil.copy(f, out_dir / name)
            else:
                continue
        except Exception as exc:  # noqa: BLE001
            warn(f"插图渲染失败 {rel}: {exc}")
            continue
        names.append(name)
    return names


def render_code(blocks: list) -> str:
    """把 lstlisting 风格环境渲成带增删配色的代码块。"""
    chunks = []
    for kind, text in blocks:
        cls = {"diffdelcode": "del", "diffaddcode": "add"}.get(kind, "")
        lines = []
        for ln in text.strip("\n").splitlines():
            body = esc(ln.rstrip())
            lines.append('<span class="%s">%s</span>' % (cls, body) if cls else body)
        chunks.append("\n".join(lines))
    return '<pre class="code">%s</pre>' % "\n".join(chunks)


def crop_float(pdf_path: Path, caption_tex: str, out_png: Path, direction: str) -> int:
    """按题注定位浮动体，从编译好的 PDF 裁成 PNG。返回页码（0 表示失败）。

    direction="below"：题注在上、内容在下（表格）；"above"：题注在下（图 / 代码框）。
    区域优先用矢量图元（表格横线、tcolorbox 边框、图片矩形）聚类确定，
    文本块只作为兜底——表格自身的单元格也是文本块，拿它当边界会算成零高度。
    """
    try:
        import pymupdf
    except ImportError:
        return 0
    if not pdf_path.is_file():
        return 0
    needle_src = plain(caption_tex)
    if len(needle_src) < 8:
        return 0
    doc = pymupdf.open(str(pdf_path))
    for page in doc:
        rects = []
        for ln in (56, 40, 28, 18):
            needle = needle_src[:ln].strip()
            if len(needle) < 8:
                continue
            rects = page.search_for(needle)
            if rects:
                break
        if not rects:
            continue
        blocks = [b for b in page.get_text("blocks") if b[6] == 0]
        # 目录页也会出现同样的句子（「5.3 Real-World Experiments」），
        # 必须确认命中位置所在的文本块确实是一条题注。
        cap = None
        for r in rects:
            for b in blocks:
                if b[1] - 1 <= r.y0 <= b[3] + 1 and b[0] - 2 <= r.x0 <= b[2] + 2:
                    if re.match(r"\s*(?:Table|Figure)\s*\d", (b[4] or "").strip()):
                        cap = b
                    break
            if cap is not None:
                break
        if cap is None:
            continue

        x0 = min(b[0] for b in blocks)
        x1 = max(b[2] for b in blocks)
        colw = max(x1 - x0, 1)
        page_h = page.rect.height

        # 注意：booktabs 的横线是零高度描边，不能用 height 过滤，
        # 否则只剩彩色单元格背景（\cellcolor），表格主体会被当成空白。
        pw = page.rect.width
        drawings = []
        for d in page.get_drawings():
            r = d["rect"]
            if r.width <= 8:
                continue
            if r.height <= 0.8 and r.width > 0.9 * pw and r.y0 < 80:
                continue  # 页眉下方的通栏细线
            drawings.append(r)

        # 先用正文段落定外层边界：正文从左边距起排且至少两行高，
        # 表格单元格与图注都没有这两个特征，比按间隙聚类稳。
        def body_side(above: bool) -> list:
            out = []
            for b in blocks:
                if b[0] > x0 + 5 or (b[2] - b[0]) < 0.45 * colw:
                    continue
                if (b[3] - b[1]) < 18:
                    continue
                if above:
                    if b[3] < cap[1] - 2:
                        out.append(b)
                elif b[1] > cap[3] + 2:
                    out.append(b)
            return out

        if direction == "below":
            lim = min((b[1] for b in body_side(False)), default=page_h - 40)
            y0, y1 = cap[3] + 2, lim - 4
        else:
            lim = max((b[3] for b in body_side(True)), default=45.0)
            y0, y1 = lim + 8, cap[1] - 2

        # 再用矢量图元收边：表格取最后一条横线，图/代码框取外框
        inside = [r for r in drawings if r.y0 >= y0 - 4 and r.y1 <= y1 + 4]
        if inside:
            dmin = min(r.y0 for r in inside)
            dmax = max(r.y1 for r in inside)
            y0 = min(y0, dmin - 3)
            if direction == "below":
                y1 = min(y1, dmax + 4)
            else:
                y1 = max(y1, min(dmax + 3, cap[1] - 2))

        # 同一页常有两张表：裁剪区不能越过下一个浮动体的题注
        caps_here = [b for b in blocks
                     if re.match(r"\s*(?:Table|Figure)\s*\d", (b[4] or "").strip())]
        if direction == "below":
            nxt = [b[1] for b in caps_here if b[1] > cap[3] + 2]
            if nxt:
                y1 = min(y1, min(nxt) - 6)
        else:
            prv = [b[3] for b in caps_here if b[3] < cap[1] - 2]
            if prv:
                y0 = max(y0, max(prv) + 6)

        # 留一点边距，避免最外侧的横线正好压在图边被吃掉
        y0 -= 4
        y1 += 4
        if direction == "below":
            y0 = max(y0, cap[3] + 1)
        else:
            y1 = min(y1, cap[1] - 1)

        clip = pymupdf.Rect(max(x0 - 4, 0), max(y0, 0), min(x1 + 4, page.rect.width),
                            min(y1, page_h))
        if clip.height < 14 or clip.width < 60:
            continue
        pix = page.get_pixmap(clip=clip, matrix=pymupdf.Matrix(2.6, 2.6), alpha=False)
        pix.save(str(out_png))
        return page.number + 1
    return 0


# --------------------------------------------------------------------------
# 组装
# --------------------------------------------------------------------------

def overlaps(seg: dict, a: int, b: int) -> bool:
    """段落区间与目标区间是否相交。

    抽取器会把 ``\\centering\\caption{...}`` 和整张表当成一个段落，
    段落起点因此落在题注花括号之前——用包含判断会全部匹配失败。
    """
    return seg["start"] <= b and seg["end"] >= a


def caption_text(translated: str, protected: list[str]) -> str:
    """从「脚手架标记 + 译文 + 脚手架标记」里取出译文。

    首尾标记指向 ``\\centering\\caption{`` 与 ``}\begin{tabular}...``，
    必须去掉；但题注内部的行内标记（如 \\method）要保留。
    """
    if not translated:
        return ""

    def repl(m):
        idx = int(m.group(1)) - 1
        frag = protected[idx] if 0 <= idx < len(protected) else ""
        if len(frag) > 60 or "\\caption" in frag or "\\begin{tabular}" in frag \
                or "\\includegraphics" in frag or "\\end{tabular}" in frag:
            return ""
        return frag

    return MARKER_RE.sub(repl, translated).strip()


def heading_text(translated: str, protected: list[str]) -> str:
    """取标题本身：译文形如 ⟪P0001⟫实验⟪P0002⟫，首尾标记就是花括号。"""
    ms = list(MARKER_RE.finditer(translated or ""))
    if len(ms) >= 2:
        idx = int(ms[0].group(1)) - 1
        frag = protected[idx] if 0 <= idx < len(protected) else ""
        if "section" in frag or "chapter" in frag or "paragraph" in frag:
            return translated[ms[0].end():ms[1].start()].strip()
    return caption_text(translated, protected)


def build_blocks(root: Path, manifest: dict, segs: list[dict],
                 translations: dict) -> tuple[list[dict], list[dict], list[dict]]:
    zh = root / "zh"
    files = [f for f in manifest["tex_files"] if (zh / f).is_file()]

    blocks: list[dict] = []
    for order, rel in enumerate(files):
        text = read_text(zh / rel)
        structs = scan_structure(text)
        for it in structs:
            it["file"], it["order"] = rel, order
        blocks.extend(structs)
        for s in segs:
            if s["file"] != rel:
                continue
            blocks.append({
                "kind": "para", "file": rel, "order": order, "start": s["start"],
                "seg": s,
            })

    blocks.sort(key=lambda b: (b["order"], b["start"]))

    # 标识哪些段落被标题/题注吸收
    consumed = set()
    headings = [b for b in blocks if b["kind"] == "section"]
    floats = [b for b in blocks if b["kind"] in ("figure", "table")]

    # 抽取器会把「标题 + 紧随其后的第一段正文」并成一个段落（中间没有受保护内容）。
    # 只按区间重叠匹配标题，正文部分拆回独立段落——直接丢弃会丢掉每节开头。
    extras: list[dict] = []
    for b in headings:
        for s in segs:
            if s["file"] == b["file"] and overlaps(s, b["arg_start"], b["arg_end"]):
                b["seg_id"] = s["id"]
                consumed.add(s["id"])
                if s["end"] > b["arg_end"] + 2:
                    en_rest = re.sub(r"^(?:\s*\\label\s*\{[^}]*\})+\s*", "",
                                     s["source"][b["arg_end"] + 1 - s["start"]:], count=1)
                    tr = translations.get(s["id"], "")
                    ms = list(MARKER_RE.finditer(tr))
                    zh_rest = tr[ms[1].end():].strip() if len(ms) >= 2 else ""
                    # 剩余部分只有 \label 之类的脚手架时不该单独成段
                    real = re.sub(r"\\[A-Za-z@]+", " ", en_rest)
                    if len(re.findall(r"[A-Za-z]{3,}", real)) >= 1 or zh_rest:
                        extras.append({
                            "kind": "para", "file": b["file"], "order": b["order"],
                            "start": b["arg_end"] + 1,
                            "custom": {"en": en_rest, "zh": zh_rest,
                                       "protected": s.get("protected", [])},
                        })
                break

    for b in floats:
        inside = [s for s in segs
                  if s["file"] == b["file"] and overlaps(s, b["start"], b["end"] - 1)]
        for s in inside:
            consumed.add(s["id"])
        if inside:
            # 题注优先取与题注区间重叠的那个，其余是子图题注
            cap = [s for s in inside if b["cap_start"] >= 0
                   and overlaps(s, b["cap_start"], b["cap_end"])]
            b["seg_id"] = (cap or inside)[0]["id"]

    abstract_ids = set()
    main_file = manifest["main"]
    if files:
        mpath = zh / main_file
        if mpath.is_file():
            mtext = read_text(mpath)
            m = ABSTRACT_RE.search(mtext)
            if m:
                e = mtext.find("\\end{abstract}", m.end())
                if e > 0:
                    for s in segs:
                        if s["file"] == main_file and overlaps(s, m.end(), e):
                            abstract_ids.add(s["id"])

    # 论文标题已经在页头，正文不再重复
    title = next((s for s in segs
                  if s["file"] == main_file and s.get("role") == "head"), None)
    if title is None:
        title = next((s for s in segs if s["file"] == main_file), None)
    if title:
        consumed.add(title["id"])

    out = []
    for b in blocks:
        if b["kind"] == "para":
            if b["seg"]["id"] in consumed or b["seg"]["id"] in abstract_ids:
                continue
            out.append(b)
        else:
            out.append(b)
    out.extend(extras)
    out.sort(key=lambda b: (b["order"], b["start"]))
    return out, headings, [b for b in out if b["kind"] in ("figure", "table")]


def anchor_of(h: dict) -> str:
    """标题锚点：有 \\label 就用它，保证正文 \\ref 的链接能对上。"""
    if h.get("label"):
        return h["label"]
    return "sec-%s-%d" % (re.sub(r"\W+", "-", h["file"]), h["start"])


def esc(s: str) -> str:
    return H.escape(s)


def render_html(root: Path, manifest: dict, blocks: list[dict], headings: list[dict],
                translations: dict, conv: Tex2HTML, labels: dict, reflect: dict) -> str:
    seg_by_id = {}
    for b in blocks:
        if b["kind"] == "para" and b.get("seg"):
            seg_by_id[b["seg"]["id"]] = b["seg"]

    # 标题编号
    sec_no = {}
    counters = [0, 0, 0]
    for h in headings:
        lv = h["level"]
        counters[lv - 1] += 1
        for k in range(lv, 3):
            counters[k] = 0
        sec_no[id(h)] = ".".join(str(c) for c in counters[:lv])

    # 目录
    toc = ['<nav class="toc" id="toc"><div class="toc-title">目录</div><ul>']
    for h in headings:
        if h["level"] > 2:
            continue
        zh_t = translations.get(h.get("seg_id", ""), "") if h.get("seg_id") else ""
        zh_t = plain(heading_text(zh_t, h.get("_protected", [])))
        en_t = plain(h["title_tex"])
        label = zh_t or en_t
        anchor = anchor_of(h)
        h["anchor"] = anchor
        cls = "toc-l%d" % h["level"]
        toc.append('<li class="%s"><a href="#%s"><span class="n">%s</span>%s</a></li>' % (
            cls, anchor, sec_no[id(h)], esc(label)))
    toc.append("</ul></nav>")

    body: list[str] = []
    counters2 = [0, 0, 0]
    fig_no = tab_no = code_no = 0
    prev_kind = None

    for b in blocks:
        kind = b["kind"]

        if kind == "section":
            lv = b["level"]
            if lv > 2 and prev_kind != "section":
                pass
            zh_raw = translations.get(b.get("seg_id", ""), "") if b.get("seg_id") else ""
            zh = plain(heading_text(zh_raw, b.get("_protected", [])))
            en = plain(b["title_tex"])
            if not b.get("anchor"):
                b["anchor"] = anchor_of(b)
            tag = "h2" if lv == 1 else ("h3" if lv == 2 else "h4")
            cls = "sec-head lv%d" % lv
            body.append(
                '<%s class="%s" id="%s"><span class="num">%s</span>'
                '<span class="t-zh">%s</span>'
                '<span class="t-en">%s</span></%s>' % (
                    tag, cls, b["anchor"], esc(sec_no[id(b)]), esc(zh or en), esc(en), tag))
            prev_kind = "section"
            continue

        if kind in ("figure", "table"):
            num = 0
            if b.get("label") and b["label"] in labels:
                try:
                    num = int(labels[b["label"]]["num"].split(".")[-1])
                except Exception:  # noqa: BLE001
                    num = 0
            if kind == "figure":
                fig_no += 1
                num = num or fig_no
                if b.get("code_blocks") and not b.get("caption_tex", "").strip():
                    code_no += 1
                    label_zh, label_en = "代码清单 %d" % code_no, "Listing %d" % code_no
                else:
                    label_zh, label_en = "图 %d" % num, "Figure %d" % num
            else:
                tab_no += 1
                num = num or tab_no
                label_zh, label_en = "表 %d" % num, "Table %d" % num

            cap_zh = translations.get(b.get("seg_id", ""), "") if b.get("seg_id") else ""
            cap_prot = _seg_protected(seg_by_id, b)
            cap_zh_html = conv.inline(caption_text(cap_zh, cap_prot)) if cap_zh else ""
            cap_en_html = conv.inline(b.get("caption_tex", ""))

            if kind == "figure":
                imgs = render_images(root / "src", b.get("images", []), reflect["img_dir"],
                                     "%s-%d" % (kind, num))
                if imgs:
                    grid = "multi" if len(imgs) > 1 else "single"
                    inner = "".join('<img src="assets/img/%s" alt="%s">' % (n, esc(label_en))
                                    for n in imgs)
                    media = '<div class="fig-grid %s">%s</div>' % (grid, inner)
                elif b.get("code_blocks"):
                    media = render_code(b["code_blocks"])
                else:
                    # 代码框 / 无 includegraphics 的图：直接从编译结果裁
                    png = reflect["img_dir"] / ("figure-%d.png" % num)
                    page = crop_float(reflect["base_pdf"], b.get("caption_tex", ""), png, "above")
                    media = ('<img src="assets/img/figure-%d.png" alt="%s">' % (num, esc(label_en))
                             if page else '<div class="float-missing">插图见原版 PDF</div>')
            else:
                png = reflect["img_dir"] / ("table-%d.png" % num)
                page = crop_float(reflect["base_pdf"], b.get("caption_tex", ""), png, "below")
                if page:
                    media = '<img class="tab-img" src="assets/img/table-%d.png" alt="%s">' % (num, esc(label_en))
                    reflect["table_pages"][num] = page
                else:
                    media = '<div class="float-missing">表格见原版 PDF</div>'

            body.append(
                '<figure class="float" id="%s">'
                '<div class="float-media">%s</div>'
                '<figcaption><span class="cap-zh"><b>%s</b>　%s</span>'
                '<span class="cap-en">%s　%s</span></figcaption></figure>' % (
                    esc(b.get("label") or "float-%d" % num), media,
                    esc(label_zh), cap_zh_html, esc(label_en), cap_en_html))
            prev_kind = "float"
            continue

        # 段落（含从标题里拆出来的正文）
        if b.get("custom"):
            c = b["custom"]
            en_html = conv.inline(c.get("en", ""))
            zh_raw = _unmask(c.get("zh", ""), c.get("protected", []))
            zh_html = conv.inline(zh_raw) if zh_raw else \
                '<em class="untranslated">（本段未译）</em>'
            body.append(
                '<div class="row" id="%s"><div class="col en" lang="en">%s</div>'
                '<div class="col zh" lang="zh-CN">%s</div></div>' % (
                    "p-%s-%d" % (re.sub(r"\W+", "-", b["file"]), b["start"]),
                    en_html, zh_html))
            prev_kind = "para"
            continue

        seg = b["seg"]
        src = seg["source"]
        tr = translations.get(seg["id"], "")
        en_html = conv.inline(src)
        zh_html = conv.inline(_unmask(tr, seg.get("protected", []))) if tr else \
            '<em class="untranslated">（本段未译）</em>'
        body.append(
            '<div class="row" id="%s"><div class="col en" lang="en">%s</div>'
            '<div class="col zh" lang="zh-CN">%s</div></div>' % (
                esc(seg["id"]), en_html, zh_html))
        prev_kind = "para"

    return "\n".join(body), "\n".join(toc)


def _seg_protected(seg_by_id: dict, block: dict) -> list[str]:
    sid = block.get("seg_id")
    if not sid:
        return []
    # 标题/题注段落不在 seg_by_id 里，从全局段表取
    return block.get("_protected", [])


def _unmask(text: str, protected: list[str]) -> str:
    if not text:
        return ""
    def repl(m):
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(protected):
            return protected[idx]
        return ""
    return MARKER_RE.sub(repl, text).strip()


# --------------------------------------------------------------------------
# 模板
# --------------------------------------------------------------------------

def katex_assets(inline: bool, out_dir: Path) -> tuple[str, str]:
    css_src = katex_asset("katex.min.css")
    js_src = katex_asset("katex.min.js")
    ar_src = katex_asset("contrib", "auto-render.min.js")
    if not (css_src and js_src and ar_src):
        warn("缺少 KaTeX 资源，公式将以原始 LaTeX 显示")
        return "", ""
    if inline:
        css = read_text(css_src)

        def repl(m):
            name = m.group(1)
            f = css_src.parent / name
            if not f.is_file():
                return m.group(0)
            mime = "font/woff2" if name.endswith(".woff2") else (
                "font/woff" if name.endswith(".woff") else "font/ttf")
            b64 = base64.b64encode(f.read_bytes()).decode("ascii")
            return "url(data:%s;base64,%s)" % (mime, b64)

        css = re.sub(r"url\((fonts/[^)]+)\)", repl, css)
        return ("<style>%s</style>" % css,
                "<script>%s</script><script>%s</script>" % (read_text(js_src), read_text(ar_src)))

    dst = out_dir / "assets" / "katex"
    dst.mkdir(parents=True, exist_ok=True)
    for src in (css_src, js_src, ar_src):
        shutil.copy2(src, dst / src.name)
    fonts = css_src.parent / "fonts"
    if fonts.is_dir():
        shutil.copytree(fonts, dst / "fonts", dirs_exist_ok=True)
    return ('<link rel="stylesheet" href="assets/katex/katex.min.css">',
            '<script src="assets/katex/katex.min.js"></script>'
            '<script src="assets/katex/auto-render.min.js"></script>')


CSS = """
:root{
  --bg:#fbfaf8; --panel:#fff; --ink:#191a1c; --muted:#6f7278; --line:#e4e1db;
  --accent:#8b3a2e; --accent-soft:rgba(139,58,46,.07); --code:#f4f1ec;
  --maxw:1420px;
}
html[data-theme=dark]{
  --bg:#0f1114; --panel:#15181d; --ink:#e7e8ea; --muted:#9aa0a8; --line:#2a2f37;
  --accent:#e08b74; --accent-soft:rgba(224,139,116,.1); --code:#1b1f25;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{
  margin:0; background:var(--bg); color:var(--ink);
  font-family:"PingFang SC","Microsoft YaHei","Hiragino Sans GB",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased;
}
#progress{position:fixed;top:0;left:0;height:2px;background:var(--accent);width:0;z-index:99;transition:width .1s}
.topbar{
  position:sticky;top:0;z-index:50;display:flex;align-items:center;gap:18px;
  padding:10px 26px;background:color-mix(in srgb,var(--bg) 88%,transparent);
  backdrop-filter:blur(12px);border-bottom:1px solid var(--line);
}
.topbar .brand{font-weight:600;font-size:13px;letter-spacing:.04em;color:var(--muted)}
.seg{display:flex;background:var(--code);border:1px solid var(--line);border-radius:8px;padding:2px}
.seg button{
  border:0;background:transparent;color:var(--muted);font:inherit;font-size:13px;
  padding:5px 13px;border-radius:6px;cursor:pointer;transition:.15s
}
.seg button.on{background:var(--panel);color:var(--ink);box-shadow:0 1px 3px rgba(0,0,0,.08)}
.tools{margin-left:auto;display:flex;align-items:center;gap:8px}
.tools button{
  border:1px solid var(--line);background:var(--panel);color:var(--ink);
  width:30px;height:30px;border-radius:7px;cursor:pointer;font-size:13px;line-height:1
}
.tools button:hover{border-color:var(--accent);color:var(--accent)}

.hero{max-width:var(--maxw);margin:0 auto;padding:52px 26px 30px}
.eyebrow{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--accent);font-weight:600}
.hero h1{
  font-family:"Songti SC","Source Han Serif SC",Georgia,serif;
  font-size:34px;line-height:1.35;margin:12px 0 10px;font-weight:700;letter-spacing:-.01em
}
.hero .orig{
  font-family:Georgia,"Times New Roman",serif;font-size:17px;color:var(--muted);
  line-height:1.5;margin:0 0 18px
}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{
  font-size:12px;color:var(--muted);border:1px solid var(--line);border-radius:999px;
  padding:4px 11px;background:var(--panel);text-decoration:none
}
a.chip:hover{color:var(--accent);border-color:var(--accent)}

.abstract{
  max-width:var(--maxw);margin:0 auto 10px;padding:0 26px;
}
.abstract-inner{
  border-left:3px solid var(--accent);background:var(--panel);border-radius:0 10px 10px 0;
  padding:20px 26px;
}
.abstract h2{font-size:13px;letter-spacing:.2em;color:var(--accent);margin:0 0 10px;font-weight:700}
.abstract .row{grid-template-columns:1fr}
.abstract .row .zh{border-left:0;padding-left:0;margin-top:6px}

.shell{max-width:var(--maxw);margin:0 auto;padding:0 26px 90px;display:grid;
  grid-template-columns:236px 1fr;gap:44px;align-items:start}

.toc{position:sticky;top:64px;max-height:calc(100vh - 90px);overflow:auto;padding:22px 0;
  font-size:13px;border-right:1px solid var(--line);padding-right:16px}
.toc-title{font-size:11px;letter-spacing:.18em;color:var(--muted);font-weight:700;margin-bottom:12px}
.toc ul{list-style:none;margin:0;padding:0}
.toc li{margin:0}
.toc a{display:block;padding:5px 8px;color:var(--muted);text-decoration:none;
  border-radius:6px;line-height:1.45;transition:.15s}
.toc a:hover{color:var(--ink);background:var(--accent-soft)}
.toc .n{display:inline-block;min-width:24px;color:var(--accent);font-variant-numeric:tabular-nums;font-size:12px}
.toc-l2 a{padding-left:26px;font-size:12.5px}
.toc a.active{color:var(--ink);background:var(--accent-soft);font-weight:600}

main{padding-top:18px;min-width:0}
.sec-head{margin:56px 0 8px;padding-bottom:10px;border-bottom:1px solid var(--line)}
.sec-head:first-child{margin-top:8px}
.sec-head .num{color:var(--accent);font-size:.72em;margin-right:12px;font-variant-numeric:tabular-nums}
.sec-head .t-zh{display:block}
.sec-head .t-en{display:block;font-family:Georgia,serif;font-size:14px;color:var(--muted);
  font-weight:400;margin-top:4px;letter-spacing:.02em}
h2.sec-head .t-zh{font-size:26px;font-weight:700;letter-spacing:-.01em}
h3.sec-head{margin-top:38px;border-bottom:0;padding-bottom:2px}
h3.sec-head .t-zh{font-size:19px;font-weight:600}
h3.sec-head .t-en{font-size:13px;margin-top:2px}
h4.sec-head{margin-top:26px;border-bottom:0}
h4.sec-head .t-zh{font-size:16px;font-weight:600}
h4.sec-head .t-en{font-size:12px}

.row{
  display:grid;grid-template-columns:1fr 1fr;column-gap:0;
  border-bottom:1px solid transparent;margin:0 -10px;padding:6px 10px;border-radius:8px;
  transition:background .15s
}
.row:hover{background:var(--accent-soft)}
.row .en{padding-right:34px;font-family:Georgia,"Times New Roman",serif;font-size:16.5px;
  line-height:1.72;color:var(--ink);opacity:.92}
.row .zh{padding-left:34px;border-left:1px solid var(--line);font-size:15.5px;line-height:1.95}
.row .en,.row .zh{min-width:0;overflow-wrap:break-word}
.row .en p,.row .zh p{margin:0 0 .7em}
.row .en p:last-child,.row .zh p:last-child{margin-bottom:0}

.math{font-size:1.02em}
.math-display{display:block;margin:8px 0;overflow-x:auto;overflow-y:hidden;max-width:100%}
.tabular,.katex{white-space:nowrap}
.cite{color:var(--accent);text-decoration:none;font-size:.9em;white-space:nowrap}
.xref{color:var(--accent);text-decoration:none}
.cite:hover,.xref:hover{text-decoration:underline}
code{background:var(--code);padding:.1em .35em;border-radius:4px;font-size:.88em}
.sc{font-variant:small-caps;letter-spacing:.03em}
mark{background:rgba(255,214,102,.5);color:inherit;padding:0 .15em}
.untranslated{color:var(--muted);font-size:13px}

.float{margin:34px 0;padding:0;border:1px solid var(--line);border-radius:12px;
  background:var(--panel);overflow:hidden}
.float-media{padding:18px 18px 6px;text-align:center;background:var(--panel)}
.float-media img{max-width:100%;height:auto;border-radius:4px}
.fig-grid.multi{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}
figcaption{padding:12px 20px 16px;display:flex;flex-direction:column;gap:4px}
.cap-zh{font-size:14.5px;line-height:1.7}
.cap-en{font-family:Georgia,serif;font-size:12.5px;line-height:1.55;color:var(--muted)}
.float-missing{padding:36px;color:var(--muted);font-size:13px}
pre.code{
  margin:0;padding:16px 18px;background:var(--code);border-radius:0;overflow-x:auto;
  font-family:"Cascadia Mono","JetBrains Mono",Consolas,monospace;font-size:12.5px;line-height:1.62;
  text-align:left;white-space:pre;
}
pre.code .del{color:#b03a2e;opacity:.75}
pre.code .add{color:#1e7a45}
html[data-theme=dark] pre.code .del{color:#e08074}
html[data-theme=dark] pre.code .add{color:#6fcf97}

.bib{margin-top:70px;padding-top:24px;border-top:2px solid var(--line)}
.bib h2{font-size:20px;margin:0 0 18px}
.bib ol{list-style:none;counter-reset:b;margin:0;padding:0;font-size:12.5px;line-height:1.65}
.bib li{counter-increment:b;position:relative;padding-left:38px;margin-bottom:7px;color:var(--muted)}
.bib li::before{content:"["counter(b)"]";position:absolute;left:0;color:var(--accent);
  font-variant-numeric:tabular-nums}
.bib li:target{background:var(--accent-soft)}

#totop{position:fixed;right:26px;bottom:26px;width:38px;height:38px;border-radius:50%;
  border:1px solid var(--line);background:var(--panel);color:var(--muted);cursor:pointer;
  display:none;font-size:15px}
#totop:hover{color:var(--accent);border-color:var(--accent)}

/* 模式切换 */
body[data-mode=en] .row{grid-template-columns:1fr}
body[data-mode=en] .col.zh,body[data-mode=en] .cap-zh{display:none}
body[data-mode=en] .row .en{padding-right:0;max-width:76ch}
body[data-mode=zh] .row{grid-template-columns:1fr}
body[data-mode=zh] .col.en,body[data-mode=zh] .cap-en{display:none}
body[data-mode=zh] .row .zh{border-left:0;padding-left:0;max-width:48em}
body[data-mode=zh] .row .zh,body[data-mode=zh] .abstract .zh{font-size:16.5px}

@media (max-width:1080px){
  .shell{grid-template-columns:1fr;gap:0}
  .toc{display:none}
  .row{grid-template-columns:1fr;gap:10px}
  .row .en{padding-right:0;font-size:16px}
  .row .zh{padding-left:0;border-left:0;padding-top:10px;border-top:1px dashed var(--line)}
  .hero h1{font-size:26px}
}
@media print{
  .topbar,.toc,#totop,#progress{display:none}
  .shell{display:block}
  .row{break-inside:avoid}
}
"""

JS = """
(function(){
  var body=document.body;
  var btns=document.querySelectorAll('.seg button');
  function setMode(m){
    body.setAttribute('data-mode',m);
    btns.forEach(function(b){b.classList.toggle('on',b.dataset.mode===m)});
    try{localStorage.setItem('arz-mode',m)}catch(e){}
  }
  btns.forEach(function(b){b.addEventListener('click',function(){setMode(b.dataset.mode)})});
  var saved='both'; try{saved=localStorage.getItem('arz-mode')||'both'}catch(e){}
  setMode(saved);
  document.addEventListener('keydown',function(e){
    if(e.target.tagName==='INPUT')return;
    if(e.key==='1')setMode('both'); if(e.key==='2')setMode('en'); if(e.key==='3')setMode('zh');
  });

  var zoom=1;
  function applyZoom(){document.documentElement.style.fontSize=(16*zoom)+'px'}
  document.getElementById('zin').onclick=function(){zoom=Math.min(1.5,zoom+0.06);applyZoom()};
  document.getElementById('zout').onclick=function(){zoom=Math.max(0.8,zoom-0.06);applyZoom()};

  var root=document.documentElement;
  var cur=window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';
  root.setAttribute('data-theme',cur);
  document.getElementById('theme').onclick=function(){
    cur=cur==='dark'?'light':'dark'; root.setAttribute('data-theme',cur);
  };

  var bar=document.getElementById('progress');
  var top=document.getElementById('totop');
  window.addEventListener('scroll',function(){
    var h=document.documentElement;
    var p=h.scrollTop/(h.scrollHeight-h.clientHeight)*100;
    bar.style.width=p+'%';
    top.style.display=h.scrollTop>600?'block':'none';
  },{passive:true});
  top.onclick=function(){window.scrollTo({top:0,behavior:'smooth'})};

  if(window.renderMathInElement){
    renderMathInElement(document.body,{
      delimiters:[{left:'\\\\(',right:'\\\\)',display:false},{left:'\\\\[',right:'\\\\]',display:true}],
      throwOnError:false, strict:false,
      ignoredTags:['script','noscript','style','textarea','pre','code']
    });
  }

  var links=[].slice.call(document.querySelectorAll('.toc a'));
  var map={}; links.forEach(function(a){map[a.getAttribute('href').slice(1)]=a});
  var heads=[].slice.call(document.querySelectorAll('.sec-head'));
  var io=new IntersectionObserver(function(es){
    es.forEach(function(e){
      if(e.isIntersecting){
        links.forEach(function(a){a.classList.remove('active')});
        var a=map[e.target.id]; if(a){a.classList.add('active')}
      }
    });
  },{rootMargin:'-70px 0px -75% 0px'});
  heads.forEach(function(h){io.observe(h)});
})();
"""

PAGE = """<!DOCTYPE html>
<html lang="zh-CN" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__ · 中英对照</title>
__KATEX_CSS__
<style>__CSS__</style>
</head>
<body data-mode="both">
<div id="progress"></div>

<div class="topbar">
  <span class="brand">双语对照阅读</span>
  <div class="seg">
    <button data-mode="both" class="on">对照</button>
    <button data-mode="zh">仅译文</button>
    <button data-mode="en">仅原文</button>
  </div>
  <div class="tools">
    <button id="zout" title="缩小字号">A−</button>
    <button id="zin" title="放大字号">A+</button>
    <button id="theme" title="切换深浅色">◐</button>
  </div>
</div>

<header class="hero">
  <div class="eyebrow">__EYEBROW__</div>
  <h1>__TITLE__</h1>
  <p class="orig">__ORIG_TITLE__</p>
  <div class="chips">__CHIPS__</div>
</header>

__ABSTRACT__

<div class="shell">
  __TOC__
  <main>
__BODY__
__BIB__
  </main>
</div>

<button id="totop" title="回到顶部">↑</button>
__KATEX_JS__
<script>__JS__</script>
</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description="生成双语对照 HTML")
    ap.add_argument("project")
    ap.add_argument("--out", default="html", help="输出目录（相对项目根）")
    ap.add_argument("--standalone", action="store_true", help="把 KaTeX 内联成单文件")
    args = ap.parse_args()

    root = Path(args.project).resolve()
    arz = meta_dir(root)
    manifest = load_json(arz / "manifest.json")
    seg_data = load_json(arz / "segments.json")
    translations = load_json(arz / "translations.json", default={})
    if not manifest or not seg_data:
        die("缺少 manifest.json / segments.json")
    if not translations:
        die("还没有译文，请先跑 apply.py")

    out_dir = root / args.out
    img_dir = out_dir / "assets" / "img"
    if img_dir.exists():
        shutil.rmtree(img_dir)
    img_dir.mkdir(parents=True, exist_ok=True)

    aux = root / "out" / ("%s.aux" % Path(manifest["main"]).stem)
    labels, bibs = parse_aux(aux)
    bbl = root / "out" / ("%s.bbl" % Path(manifest["main"]).stem)
    bib_entries = parse_bbl(bbl)

    macro_files = [root / "zh" / manifest["main"]] + sorted((root / "zh").glob("*.cls")) \
        + sorted((root / "zh").glob("*.sty"))
    macros = parse_macros(macro_files)
    conv = Tex2HTML(labels, bibs, macros)

    segs = seg_data["segments"]
    # 标题/题注段落在 blocks 之外，需要能查到 protected
    prot_of = {s["id"]: s.get("protected", []) for s in segs}
    for s in segs:
        s["_p"] = s.get("protected", [])

    blocks, headings, floats = build_blocks(root, manifest, segs, translations)
    for b in blocks:
        if b.get("seg_id"):
            b["_protected"] = prot_of.get(b["seg_id"], [])

    reflect = {
        "img_dir": img_dir,
        "base_pdf": root / "base" / ("%s.pdf" % Path(manifest["main"]).stem),
        "table_pages": {},
    }

    body_html, toc_html = render_html(root, manifest, blocks, headings,
                                      translations, conv, labels, reflect)

    # 摘要
    abstract_html = ""
    main_file = manifest["main"]
    mtext = read_text(root / "zh" / main_file) if (root / "zh" / main_file).is_file() else ""
    am = ABSTRACT_RE.search(mtext)
    abs_ids = []
    if am:
        ae = mtext.find("\\end{abstract}", am.end())
        for s in segs:
            if s["file"] == main_file and am.end() <= s["start"] < ae:
                abs_ids.append(s["id"])
    if abs_ids:
        rows = []
        for sid in abs_ids:
            seg = next((s for s in segs if s["id"] == sid), None)
            if not seg:
                continue
            tr = translations.get(sid, "")
            rows.append(
                '<div class="row"><div class="col en" lang="en">%s</div>'
                '<div class="col zh" lang="zh-CN">%s</div></div>' % (
                    conv.inline(seg["source"]),
                    conv.inline(_unmask(tr, seg.get("protected", []))) if tr else ""))
        abstract_html = ('<section class="abstract"><div class="abstract-inner">'
                         '<h2>摘 要 / ABSTRACT</h2>%s</div></section>' % "".join(rows))

    # 标题
    title_zh, title_en = "", ""
    for s in segs:
        if s["file"] == main_file and s.get("role") == "head":
            title_en = s["source"].strip()
            title_zh = translations.get(s["id"], "").strip()
            break
    if not title_zh:
        title_zh = title_en
    title_zh = plain(MARKER_RE.sub("", title_zh))

    # 参考文献
    bib_html = ""
    if bib_entries:
        items = []
        for e in bib_entries:
            n = bibs.get(e["key"], "")
            text = conv.inline(e["raw"])
            items.append('<li id="bib-%s">%s</li>' % (esc(e["key"]), text))
        bib_html = ('<section class="bib"><h2>参考文献</h2><ol>%s</ol></section>'
                    % "".join(items))

    n_cjk = sum(len(re.findall(r"[\u4e00-\u9fff]", v)) for v in translations.values())
    n_rows = sum(1 for b in blocks if b["kind"] == "para")
    chips = [
        '<a class="chip" href="https://arxiv.org/abs/%s" target="_blank" rel="noopener">arXiv:%s</a>'
        % (esc(manifest.get("arxiv_id", "")), esc(manifest.get("arxiv_id", ""))),
        '<span class="chip">%d 段对照</span>' % n_rows,
        '<span class="chip">%s 字</span>' % format(n_cjk, ","),
        '<span class="chip">本文由机器翻译辅助生成，以英文原文为准</span>',
    ]

    # index.html 始终用外链资源，保持体积小；单文件版另做一份内联的
    kcss, kjs = katex_assets(False, out_dir)
    page = (PAGE
            .replace("__KATEX_CSS__", kcss)
            .replace("__KATEX_JS__", kjs)
            .replace("__CSS__", CSS)
            .replace("__JS__", JS)
            .replace("__EYEBROW__", "arXiv %s · 中英逐段对照" % esc(manifest.get("arxiv_id", "")))
            .replace("__TITLE__", esc(title_zh))
            .replace("__ORIG_TITLE__", esc(title_en))
            .replace("__CHIPS__", "".join(chips))
            .replace("__ABSTRACT__", abstract_html)
            .replace("__TOC__", toc_html)
            .replace("__BODY__", body_html)
            .replace("__BIB__", bib_html))

    def inline_images(html_text: str) -> str:
        """单文件版：把插图塞成 data URI，脱离 assets 目录也能看。"""
        def repl(m):
            f = img_dir / m.group(1)
            if not f.is_file():
                return m.group(0)
            mime = "image/png" if f.suffix.lower() == ".png" else "image/jpeg"
            b64 = base64.b64encode(f.read_bytes()).decode("ascii")
            return 'src="data:%s;base64,%s"' % (mime, b64)
        return re.sub(r'src="assets/img/([^"]+)"', repl, html_text)

    index = out_dir / "index.html"
    index.write_text(page, encoding="utf-8")

    slug = manifest.get("short") or title_en.split(":")[0]
    slug = re.sub(r"[^A-Za-z0-9\-]+", "", slug)[:40] or "paper"
    name = "%s_%s_双语对照.html" % (slug, manifest.get("arxiv_id", ""))
    if args.standalone:
        target = root / name
        ikcss, ikjs = katex_assets(True, out_dir)
        single = page.replace(kcss, ikcss).replace(kjs, ikjs)
        target.write_text(inline_images(single), encoding="utf-8")
    ok("已生成 %s（%d 段对照，%d 幅图/%d 张表，%d 条文献）" % (
        index, n_rows, sum(1 for b in blocks if b["kind"] == "figure"),
        sum(1 for b in blocks if b["kind"] == "table"), len(bib_entries)))
    if args.standalone:
        ok("单文件版：%s" % target)


if __name__ == "__main__":
    main()
