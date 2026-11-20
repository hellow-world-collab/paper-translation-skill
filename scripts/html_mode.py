#!/usr/bin/env python3
"""HTML 分支：直接抓论文的网页版（arXiv /html、ar5iv、bioRxiv、任意论文页），
按**段落**替换其中的自然语言，公式（MathML）、图片、链接、脚注锚点原位不动。

为什么值得单独做一条分支：
  - **不需要 TeX**：公式是 MathML，浏览器原生渲染，省掉字体注入、3 遍探测编译、
    交叉引用收敛，几秒钟出中文页面；
  - **比 PDF 干净**：拿到的是 DOM 文本，不是从坐标反推的文本块，不存在「一块译文
    盖住半行」的问题；
  - **回填天然安全**：段落里的 `<math>` / `<cite>` / `<span>` 全部换成 ⟪Pnnnn⟫ 交给
    模型，模型只看到自然语言，标记回填时还原成原始 HTML——与 LaTeX 分支同一条原则。

但 HTML 版是出版社 / arXiv 用 LaTeXML 转的**降级产物**：参考文献可能整个丢掉
（`ltx_missing_citation`）、个别命令转不出来（`ltx_ERROR`）。所以 `fetch` 会统计这些
缺陷，`route.py` 据此决定「快但降级」还是「慢但保真」。

用法：
    python html_mode.py fetch   <链接 / ID / .html> --project ./项目      # 下载 + 清洗
    python html_mode.py extract ./项目                                   # 切段 → segments.json
    python html_mode.py apply   ./项目 [--pdf]                           # 回填 → out/ + 双语页
    python html_mode.py check   ./项目                                   # 体检

产物：
    src/index.html            原始抓取，只读
    zh/index.html             清洗后的基线（extract 从这读，apply 每次从它重建 out/）
    zh/assets/                本地化的 CSS / 图片
    out/index.html            纯中文版
    html/bilingual.html       中英对照版（每段中文 + 原文，可一键收起英文）
    out/<论文>_中文版.pdf     加 --pdf 时用 Chrome 无头打印（不装 TeX 也能出 PDF）
"""

from __future__ import annotations

import argparse
import bisect
import re
import shutil
import sys
import urllib.error
import urllib.request
from html import escape, unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

sys.path.insert(0, str(Path(__file__).parent))
from _common import (MARKER, MARKER_RE, die, load_json, meta_dir, ok, read_text,  # noqa: E402
                     save_json, text_key, warn, write_text)
import sources  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

MARKER_ID = "index.html"

# ---------------------------------------------------------------- 结构常量

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "param", "source", "track", "wbr"}

# 这些标签里的文字一律不动：公式、代码、引用键、脚本。
OPAQUE_TAGS = {"script", "style", "math", "svg", "pre", "code", "cite", "kbd", "samp",
               "textarea", "select", "button", "iframe", "video", "audio", "canvas",
               "form", "annotation", "annotation-xml", "template", "object", "title"}

# 类名命中即整块不动：参考文献、目录、页眉页脚、LaTeXML 转换报错。
OPAQUE_CLASSES = ("ltx_bibliography", "ltx_bibitem", "ltx_biblist", "ltx_toc",
                  "ltx_page_navbar", "ltx_page_logo", "ltx_page_footer",
                  "ltx_page_header", "ltx_error", "ltx_pagination",
                  "ds-skip-link", "arxiv-html-footer", "ltx_role_bibliography",
                  "ltx_personname", "infobox")

# LaTeXML 习惯把整段文字再包一层 `<span class="ltx_text" style="font-size:90%">`。
# 这层壳没有语义，但如果留着，整段话会被当成「一个受保护片段」整体跳过——
# 题注会被切成「while using 100 / less data. It also outperforms…」这样的碎句。
# 清洗时把这些纯展示用的 span 拆掉（只留标签、不留壳），语义 span 一律保留。
KEEP_SPAN_HINTS = ("ltx_font_", "ltx_ref", "ltx_tag", "ltx_note", "ltx_align_",
                   "ltx_bib", "ltx_pagination", "ltx_math", "ltx_ram_title",
                   "ltx_font_bold", "ltx_font_italic", "ltx_personname")

HEAD_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "figcaption",
              "td", "th", "dt", "dd", "blockquote", "div"}

# 自动生成的编号标签：确定性本地化，不交给模型（否则同一篇会混出「图 1 / Figure 1」）。
LABEL_WORDS = {
    "abstract": "摘要", "references": "参考文献", "bibliography": "参考文献",
    "acknowledgments": "致谢", "acknowledgements": "致谢", "acknowledgment": "致谢",
    "contents": "目录", "index terms": "索引词", "keywords": "关键词",
    "proof": "证明", "theorem": "定理", "lemma": "引理", "corollary": "推论",
    "proposition": "命题", "definition": "定义", "assumption": "假设",
    "remark": "注", "example": "例", "claim": "断言",
}
LABEL_PATTERNS = [
    (re.compile(r"^(?:Figure|Fig\.?)\s*([\dA-Z]+[a-z]?)$", re.I), r"图 \1"),
    (re.compile(r"^(?:Table|Tab\.?)\s*([\dA-Z]+[a-z]?)$", re.I), r"表 \1"),
    (re.compile(r"^(?:Algorithm|Alg\.?)\s*(\d+)$", re.I), r"算法 \1"),
    (re.compile(r"^(?:Listing|List\.?)\s*(\d+)$", re.I), r"代码 \1"),
    (re.compile(r"^(?:Equation|Eq\.?)\s*\(?\s*([\d.]+)\s*\)?$", re.I), r"式 (\1)"),
    (re.compile(r"^(?:Section|Sec\.?)\s*(\d+(?:\.\d+)*)$", re.I), r"第 \1 节"),
    (re.compile(r"^(?:Appendix|App\.?)\s*([A-Z]\d*)$"), r"附录 \1"),
]
LABEL_NOUNS = (("theorem", "定理"), ("lemma", "引理"), ("corollary", "推论"),
               ("proposition", "命题"), ("definition", "定义"),
               ("assumption", "假设"), ("remark", "注"), ("example", "例"))


def localize_label(text: str) -> str | None:
    """把「Figure 1」「Section 3.2」「Abstract」这类自动生成的标签换成中文。

    只认**整段就是标签**的文本：正文里的「Figure 1 shows…」不归这里管，交给模型。
    题注标签常把冒号包在同一段里（`Figure 1: `），要一起处理，否则匹配不上。
    返回 None 表示不是标签。
    """
    s = text.strip()
    if not s or len(s) > 40:
        return None
    colon = re.search(r"[:：]\s*$", s)
    core = s[:colon.start()].strip() if colon else s
    zh = _label_core(core)
    if zh is None:
        return None
    return zh + "：" if colon else zh


def _label_core(s: str) -> str | None:
    low = re.sub(r"\s+", " ", s.lower())
    if low in LABEL_WORDS:
        return LABEL_WORDS[low]
    for pat, repl in LABEL_PATTERNS:
        if pat.match(s):
            return pat.sub(repl, s)
    for en, zh in LABEL_NOUNS:
        m = re.match(rf"^{en}\s*([\d.]+)$", s, re.I)
        if m:
            return f"{zh} {m.group(1)}"
    return None


# ---------------------------------------------------------------- 扫描

class _Scanner(HTMLParser):
    """把 HTML 切成 token，并给出每个 token 在原文里的字符区间。

    HTMLParser 的 getpos() 给的是 (行, 列)，换算成绝对偏移后与原文精确对齐，
    所以回填可以只做字符串替换，不必重新序列化 DOM（不会丢属性、不会改引号风格）。
    """

    def __init__(self, raw: str):
        super().__init__(convert_charrefs=False)
        self.raw = raw
        self.line_start = [0]
        for m in re.finditer(r"\n", raw):
            self.line_start.append(m.end())
        self.tokens: list[dict] = []

    def _off(self) -> int:
        ln, col = self.getpos()
        return self.line_start[ln - 1] + col

    def _tag_end(self, s: int) -> int:
        e = self.raw.find(">", s)
        return len(self.raw) if e < 0 else e + 1

    def handle_starttag(self, tag, attrs):
        s = self._off()
        self.tokens.append({"kind": "start", "tag": tag, "attrs": dict(attrs),
                            "start": s, "end": self._tag_end(s)})

    def handle_startendtag(self, tag, attrs):
        s = self._off()
        self.tokens.append({"kind": "start", "tag": tag, "attrs": dict(attrs),
                            "start": s, "end": self._tag_end(s), "selfclose": True})

    def handle_endtag(self, tag):
        s = self._off()
        self.tokens.append({"kind": "end", "tag": tag, "attrs": {},
                            "start": s, "end": self._tag_end(s)})

    def handle_data(self, data):
        s = self._off()
        self.tokens.append({"kind": "data", "tag": None, "attrs": {},
                            "start": s, "end": s + len(data), "text": data})

    def handle_entityref(self, name):
        s = self._off()
        self.tokens.append({"kind": "data", "tag": None, "attrs": {},
                            "start": s, "end": s + len(name) + 2, "text": f"&{name};"})

    def handle_charref(self, name):
        s = self._off()
        self.tokens.append({"kind": "data", "tag": None, "attrs": {},
                            "start": s, "end": s + len(name) + 3, "text": f"&#{name};"})

    def handle_comment(self, data):
        s = self._off()
        self.tokens.append({"kind": "comment", "tag": None, "attrs": {},
                            "start": s, "end": self._tag_end(s)})

    def handle_decl(self, decl):
        s = self._off()
        self.tokens.append({"kind": "decl", "tag": None, "attrs": {},
                            "start": s, "end": self._tag_end(s)})

    def handle_pi(self, data):
        s = self._off()
        self.tokens.append({"kind": "pi", "tag": None, "attrs": {},
                            "start": s, "end": self._tag_end(s)})

    def unknown_decl(self, data):
        s = self._off()
        self.tokens.append({"kind": "decl", "tag": None, "attrs": {},
                            "start": s, "end": self._tag_end(s)})


def scan(raw: str) -> dict:
    """扫一遍文档，给出：文本片段、块级元素（含是否为叶子）、上下文。"""
    p = _Scanner(raw)
    p.feed(raw)

    runs: list[dict] = []
    cur: dict | None = None
    stack: list[tuple[str, str | None, str]] = []   # (tag, ctx, class)
    opaque_depth = 0
    blocks: list[dict] = []
    block_stack: list[dict] = []
    title_depth = 0

    def cur_ctx() -> str:
        for _tag, ctx, _cls in reversed(stack):
            if ctx:
                return ctx
        return "body"

    for idx, tk in enumerate(p.tokens):
        kind = tk["kind"]
        if kind == "start":
            tag = tk["tag"]
            cls = (tk["attrs"].get("class") or "").lower()
            is_opaque = tag in OPAQUE_TAGS or any(c in cls for c in OPAQUE_CLASSES)
            if is_opaque and not tk.get("selfclose"):
                opaque_depth += 1
            if tag == "h1" and "ltx_title_document" in cls:
                title_depth += 1
            ctx = None
            if tag in HEAD_TAGS:
                ctx = "head"
            elif tag == "figcaption" or "ltx_caption" in cls:
                ctx = "caption"
            elif tag in ("td", "th"):
                ctx = "cell"
            elif tag in ("li", "dt", "dd"):
                ctx = "item"
            elif "ltx_role_footnote" in cls:
                ctx = "footnote"
            if tag not in VOID and not tk.get("selfclose"):
                stack.append((tag, ctx, cls))
            cur = None
        elif kind == "end":
            tag = tk["tag"]
            cls = ""
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == tag:
                    cls = stack[i][2]
                    del stack[i:]
                    break
            if (tag in OPAQUE_TAGS or any(c in cls for c in OPAQUE_CLASSES)) and opaque_depth:
                opaque_depth -= 1
            if tag == "h1" and title_depth:
                title_depth -= 1
            cur = None
        elif kind == "data":
            if cur is not None and cur["end"] == tk["start"]:
                cur["end"] = tk["end"]
            else:
                cur = {"start": tk["start"], "end": tk["end"]}
                runs.append(cur)
            cur["opaque"] = opaque_depth > 0
            cur["ctx"] = cur_ctx()
            cur["title"] = title_depth > 0
            cur["ti"] = idx
            continue
        else:
            cur = None
        # 块级元素：记下内层区间，稍后判断是不是叶子
        if kind == "start" and tk["tag"] in BLOCK_TAGS and not tk.get("selfclose"):
            block_stack.append({"tag": tk["tag"], "ti": idx, "open": tk["end"],
                                "cls": (tk["attrs"].get("class") or "").lower()})
        elif kind == "end":
            for i in range(len(block_stack) - 1, -1, -1):
                if block_stack[i]["tag"] == tk["tag"]:
                    b = block_stack.pop()
                    blocks.append({"tag": b["tag"], "cls": b["cls"],
                                   "start": b["open"], "end": tk["start"],
                                   "ti0": b["ti"], "ti1": idx})
                    del block_stack[i:]
                    break

    for r in runs:
        r["source"] = raw[r["start"]:r["end"]]
        r["text"] = unescape(r["source"])
    blocks.sort(key=lambda b: (b["start"], -b["end"]))
    return {"runs": runs, "blocks": blocks, "tokens": p.tokens}


def _mark_leaves(blocks: list[dict]) -> None:
    """标出叶子块：内部没有其它块级元素。只有叶子块才作为翻译单元，
    否则 `li` 和它里面的 `p` 会被抽两遍。"""
    for b in blocks:
        b["leaf"] = True
    for b in blocks:
        for other in blocks:
            if other is b:
                continue
            if other["start"] == b["start"] or other["end"] == b["end"]:
                continue
            if b["start"] <= other["start"] and other["end"] <= b["end"]:
                b["leaf"] = False
                break


# ---------------------------------------------------------------- 抽取

EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
URLONLY = re.compile(r"^(?:https?://|www\.)\S+$")
NUMONLY = re.compile(r"^[\s\d.,;:%()\[\]\-–—/+×=<>°]+$")


def _match_close(tokens: list[dict], i: int, i1: int) -> int:
    """从 tokens[i]（一个开始标签）找到配对的结束标签下标；自闭合/未闭合时退回自身。"""
    tag = tokens[i]["tag"]
    if tag in VOID or tokens[i].get("selfclose"):
        return i
    depth = 1
    for k in range(i + 1, i1):
        tk = tokens[k]
        if tk["kind"] == "start" and tk["tag"] == tag:
            depth += 1
        elif tk["kind"] == "end" and tk["tag"] == tag:
            depth -= 1
            if depth == 0:
                return k
    return max(i, i1 - 1)


# 只起排版作用的行内标签：拆成「开标签 / 闭标签」两个标记，里面的文字照样翻。
# 否则 `<td><span class="ltx_font_bold">Optimization</span></td>` 整格会被当成
# 一个受保护片段，表头就永远是英文了。
PAIR_TAGS = {"a", "span", "em", "b", "i", "strong", "sup", "sub", "u", "s",
             "small", "mark", "q", "abbr", "ins", "del", "font"}
# 这几类 span 装的是自动编号标签（Figure 1 / Table 2）或人名：整块保留原样。
# 标签交给 apply 的确定性本地化，人名照抄（译成「达尼贾尔·哈夫纳」反而不好认）。
WHOLE_CLASS_HINTS = ("ltx_tag", "ltx_ref_tag", "ltx_personname")


def _is_pair(tk: dict) -> bool:
    if tk["tag"] not in PAIR_TAGS:
        return False
    cls = (tk["attrs"].get("class") or "").lower()
    return not any(h in cls for h in WHOLE_CLASS_HINTS)


def carve(raw: str, tokens: list[dict], i0: int, i1: int) -> tuple[str, list[str], list[list[int]]]:
    """把一个块的内层切成「自然语言 + ⟪Pnnnn⟫」。

    返回 (masked, protected, pairs)。pairs 记录哪两个标记是一对开/闭标签，
    回填时用来校验嵌套没被模型打乱（打乱会生成坏 HTML，宁可退原文）。
    """
    parts: list[str] = []
    protected: list[str] = []
    pairs: list[list[int]] = []
    stack: list[tuple[str, int, int]] = []   # (tag, 配对下标, 开标记编号)
    i = i0
    while i < i1:
        tk = tokens[i]
        if tk["kind"] == "data":
            buf = [tk["text"]]
            j = i + 1
            while j < i1 and tokens[j]["kind"] == "data":
                buf.append(tokens[j]["text"])
                j += 1
            parts.append(unescape("".join(buf)))
            i = j
            continue
        if tk["kind"] == "start":
            j = _match_close(tokens, i, i1)
            closed = (j > i and tokens[j]["kind"] == "end" and tokens[j]["tag"] == tk["tag"]
                      and not tk.get("selfclose"))
            if _is_pair(tk) and closed:
                protected.append(raw[tk["start"]:tk["end"]])
                parts.append(MARKER.format(len(protected)))
                stack.append((tk["tag"], j, len(protected)))
                i += 1
                continue
            protected.append(raw[tk["start"]:tokens[j]["end"]])
            parts.append(MARKER.format(len(protected)))
            i = j + 1
            continue
        if tk["kind"] == "end":
            if stack and stack[-1][0] == tk["tag"] and stack[-1][1] == i:
                _tag, _j, num = stack.pop()
                protected.append(raw[tk["start"]:tk["end"]])
                parts.append(MARKER.format(len(protected)))
                pairs.append([num, len(protected)])
            i += 1
            continue
        i += 1

    text = "".join(parts)
    text = re.sub(r"[ \t\r\n]+", " ", text)
    return text.strip(), protected, pairs


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z'’\-]*", MARKER_RE.sub(" ", text))


def _passes(text: str, ctx: str) -> bool:
    bare = MARKER_RE.sub("", text).strip()
    if not bare or not re.search(r"[A-Za-z]", bare):
        return False
    if URLONLY.match(bare) or EMAIL.fullmatch(bare) or NUMONLY.match(bare):
        return False
    if localize_label(bare) is not None:
        return False
    # 子图编号 (a) (b)：一个字母，不该翻
    if re.fullmatch(r"\(?[A-Za-z]\)?", bare):
        return False
    # LaTeX 参数泄漏（tabularray 转换失败后的 L[1]Q[l,m,wd=#1] 之类）
    if re.search(r"#\d", bare) or re.search(r"\\[A-Za-z]+", bare):
        return False
    words = _words(text)
    if not words:
        return False
    if ctx in ("body", "footnote"):
        return len(words) >= 2
    if len(bare) < 2:
        return False
    if len(words) == 1 and bare.isupper() and len(bare) <= 6:
        return False
    return True


def _block_ctx(block: dict, runs: list[dict], starts: list[int]) -> str:
    """块自身的语境：块内第一个非空文本片段的语境即可代表。"""
    for r in runs:
        if block["start"] <= r["start"] < block["end"]:
            return r["ctx"]
    return "body"


def extract(root: Path) -> dict:
    zh = root / "zh" / MARKER_ID
    if not zh.is_file():
        die(f"缺少 {zh}，请先跑 html_mode.py fetch")
    raw = read_text(zh)
    info = scan(raw)
    _mark_leaves(info["blocks"])
    tokens, runs = info["tokens"], info["runs"]
    tok_starts = [t["start"] for t in tokens]

    segs: list[dict] = []
    covered: list[tuple[int, int]] = []

    for b in info["blocks"]:
        if not b["leaf"]:
            continue
        i0 = bisect.bisect_left(tok_starts, b["start"])
        i1 = bisect.bisect_left(tok_starts, b["end"])
        masked, prot, pairs = carve(raw, tokens, i0, i1)
        ctx = _block_ctx(b, runs, tok_starts)
        if not _passes(masked, ctx):
            continue
        lead = raw[b["start"]:b["start"] + (len(raw[b["start"]:b["end"]])
                                               - len(raw[b["start"]:b["end"]].lstrip()))]
        segs.append({
            "id": f"{MARKER_ID}#{len(segs) + 1:04d}",
            "file": MARKER_ID,
            "start": b["start"],
            "end": b["end"],
            "source": raw[b["start"]:b["end"]],
            "masked": masked,
            "protected": prot,
            "pairs": pairs,
            "role": ctx,
            "ctx": ctx,
            "lead": lead,
            "level": "block",
        })
        covered.append((b["start"], b["end"]))

    # 落在叶子块之外的散文本（div 外直接挂的文字、caption 里嵌的 bare text）单独成段
    def inside(pos: int) -> bool:
        for s, e in covered:
            if s <= pos < e:
                return True
        return False

    for r in runs:
        if r["opaque"] or inside(r["start"]):
            continue
        text = re.sub(r"[ \t\r\n]+", " ", r["text"]).strip()
        if not _passes(text, r["ctx"]):
            continue
        segs.append({
            "id": f"{MARKER_ID}#{len(segs) + 1:04d}",
            "file": MARKER_ID,
            "start": r["start"],
            "end": r["end"],
            "source": r["source"],
            "masked": text,
            "protected": [],
            "pairs": [],
            "role": r["ctx"],
            "ctx": r["ctx"],
            "lead": r["source"][:len(r["source"]) - len(r["source"].lstrip())],
            "level": "run",
            **({"title": True} if r.get("title") else {}),
        })

    segs.sort(key=lambda s: s["start"])
    for i, s in enumerate(segs, 1):
        s["id"] = f"{MARKER_ID}#{i:04d}"

    save_json(meta_dir(root) / "segments.json", {
        "engine": "html",
        "file": MARKER_ID,
        "fingerprint": _fingerprint(raw),
        "segments": segs,
    })

    roles: dict[str, int] = {}
    marks = 0
    for s in segs:
        roles[s["role"]] = roles.get(s["role"], 0) + 1
        marks += len(s["protected"])
    chars = sum(len(s["masked"]) for s in segs)
    print(f"\n[{root.name}] 抽取（HTML，按段落）")
    print(f"  文件      {MARKER_ID}（{len(raw):,} 字符）")
    print(f"  可译片段  {len(segs)}（段落 {sum(1 for s in segs if s['level'] == 'block')} / "
          f"散文本 {sum(1 for s in segs if s['level'] == 'run')}）")
    print("  语境分布  " + "  ".join(f"{k} {v}" for k, v in sorted(roles.items())))
    print(f"  受保护片段 {marks}（公式、引用、加粗、链接、图片）")
    print(f"  源文字符  {chars:,}（按 --budget 18000 约 {(chars + 17999) // 18000} 批）")
    return {"segments": segs}


def _fingerprint(raw: str) -> str:
    import hashlib
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 回填

def unmask_html(translated: str, protected: list[str]) -> str:
    """标记还原成原始 HTML，模型写出来的文字一律转义（`&` `<` 不会破坏结构）。"""
    out: list[str] = []
    last = 0
    for m in MARKER_RE.finditer(translated):
        out.append(escape(translated[last:m.start()], quote=False))
        idx = int(m.group(1)) - 1
        out.append(protected[idx] if 0 <= idx < len(protected) else "")
        last = m.end()
    out.append(escape(translated[last:], quote=False))
    return "".join(out)


def pairs_ok(seq: list[int], pairs: list[list[int]]) -> bool:
    """成对标记必须仍然正确嵌套。模型若把开/闭标签调换顺序，会生成坏 HTML，
    这种情况宁可整段退原文，也不能写进成品。"""
    if not pairs:
        return True
    open_of = {o: c for o, c in pairs}
    close_of = {c: o for o, c in pairs}
    stack: list[int] = []
    for n in seq:
        if n in open_of:
            stack.append(open_of[n])
        elif n in close_of:
            if not stack or stack[-1] != n:
                return False
            stack.pop()
    return not stack


def apply(root: Path, bilingual: bool = True, pdf: bool = False) -> dict:
    zh_dir = root / "zh"
    zh = zh_dir / MARKER_ID
    if not zh.is_file():
        die(f"缺少 {zh}，请先跑 html_mode.py fetch + extract")
    seg_data = load_json(meta_dir(root) / "segments.json")
    if not seg_data or seg_data.get("engine") != "html":
        die("这不是 HTML 分支的项目（segments.json 不是 html 引擎），请用 apply.py")
    tr = load_json(meta_dir(root) / "translations.json", default={}) or {}
    keys = load_json(meta_dir(root) / "tr_keys.json", default={}) or {}

    raw = read_text(zh)
    if _fingerprint(raw) != seg_data.get("fingerprint"):
        die("segments.json 与 zh/index.html 对不上（基线被改过）。\n"
            "  请重跑：python html_mode.py extract <项目>   —— 译文按 id 保存，不会丢")

    subs: dict[int, tuple[int, str]] = {}
    applied = missing = same = stale = 0
    problems: list[dict] = []
    blocks: list[dict] = []

    for s in seg_data["segments"]:
        text = tr.get(s["id"])
        if text is None or not str(text).strip():
            missing += 1
            continue
        # 段 id 是按出现顺序编的，重新抽取会整体位移。指纹对不上说明这条译文
        # 属于旧的切分，套上去会把 A 段的中文写进 B 段。
        if keys.get(s["id"]) and keys[s["id"]] != text_key(s["masked"]):
            stale += 1
            continue
        text = str(text).strip()
        if text == s["masked"].strip():
            same += 1
            continue
        want = sorted(int(x) for x in MARKER_RE.findall(s["masked"]))
        got = sorted(int(x) for x in MARKER_RE.findall(text))
        if want != got:
            problems.append({"id": s["id"], "reason":
                             f"标记不一致（应 {len(want)} 个，实 {len(got)} 个）"})
            missing += 1
            continue
        if not pairs_ok([int(x) for x in MARKER_RE.findall(text)], s.get("pairs") or []):
            problems.append({"id": s["id"], "reason": "成对标记嵌套被打乱（加粗/链接会坏）"})
            missing += 1
            continue
        inner = s["lead"] + unmask_html(text, s["protected"])
        subs[s["start"]] = (s["end"], inner)
        applied += 1
        if s["level"] == "block":
            blocks.append({"start": s["start"], "end": s["end"]})

    # 标题同步到 <title>
    title_zh = None
    for s in seg_data["segments"]:
        if s.get("title") and tr.get(s["id"]):
            title_zh = MARKER_RE.sub("", str(tr[s["id"]])).strip()
            break

    out_dir = root / "out"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    shutil.copytree(zh_dir, out_dir)

    zh_doc = _rebuild(raw, subs, None)
    zh_doc = _localize_labels(zh_doc)
    if title_zh:
        zh_doc = _set_title(zh_doc, title_zh)
    zh_doc = _inject_page_style(zh_doc, bilingual=False)
    write_text(out_dir / MARKER_ID, zh_doc)

    if bilingual:
        bi = _rebuild(raw, subs, blocks)
        bi = _localize_labels(bi)
        if title_zh:
            bi = _set_title(bi, title_zh + " — 中英对照")
        bi = _inject_page_style(bi, bilingual=True)
        html_dir = root / "html"
        html_dir.mkdir(parents=True, exist_ok=True)
        write_text(html_dir / "bilingual.html", bi)
        if (out_dir / "assets").is_dir():
            dst = html_dir / "assets"
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(out_dir / "assets", dst)
        ok(f"中英对照页：{html_dir / 'bilingual.html'}")

    total = len(seg_data["segments"])
    report = {"total": total, "applied": applied, "missing": missing,
              "unchanged": same, "stale": stale, "problems": problems}
    save_json(meta_dir(root) / "html_apply.json", report)

    print(f"\n[{root.name}] 回填（HTML）")
    print(f"  片段      {total}")
    print(f"  已替换    {applied}")
    print(f"  缺译      {missing}（这些段保留英文）")
    if stale:
        print(f"  过期译文  {stale}（片段已被重新切分，需重译：python html_mode.py extract 之后重跑 pending.py）")
    if same:
        print(f"  与原样相同 {same}")
    if problems:
        print(f"  标记不匹配 {len(problems)}")
        for p in problems[:8]:
            print(f"    {p['id']:<24} {p['reason']}")
    ok(f"中文版：{out_dir / MARKER_ID}")

    if pdf:
        export_pdf(root)
    return report


def _set_title(doc: str, text: str) -> str:
    return re.sub(r"(<title[^>]*>).*?(</title>)",
                  lambda m: m.group(1) + escape(text) + m.group(2),
                  doc, count=1, flags=re.S)


def _rebuild(raw: str, subs: dict[int, tuple[int, str]],
             blocks: list[dict] | None) -> str:
    """按 subs 重建文档；给了 blocks 就再套一层「中文 + 原文」的块包装。"""
    spans: list[tuple[int, int, str]] = []
    if blocks:
        covered = [(b["start"], b["end"]) for b in blocks]
        for b in blocks:
            en = raw[b["start"]:b["end"]]
            zh_inner = subs[b["start"]][1]
            spans.append((b["start"], b["end"],
                          '<span class="arz-zh">' + zh_inner + '</span>'
                          '<span class="arz-en" lang="en">' + _strip_ids(en) + '</span>'))

        def in_block(pos: int) -> bool:
            return any(s <= pos < e for s, e in covered)

        for pos, (end, text) in subs.items():
            if not in_block(pos):
                spans.append((pos, end, text))
    else:
        spans = [(pos, end, text) for pos, (end, text) in subs.items()]

    spans.sort(key=lambda x: x[0])
    out, last = [], 0
    for s, e, text in spans:
        if s < last:
            continue
        out.append(raw[last:s])
        out.append(text)
        last = e
    out.append(raw[last:])
    return "".join(out)


def _strip_ids(fragment: str) -> str:
    """英文副本去掉 id：两栏共存时 id 重复会让页内锚点跳到错误的那一栏。"""
    return re.sub(r'\sid="[^"]*"', "", fragment)


def _localize_labels(doc: str) -> str:
    """把回填后仍然整段是英文编号标签的文本换成中文（Figure 1 → 图 1）。

    这些标签是 LaTeXML 自动生成的，抽取时被跳过（不进 segments），所以要在回填后
    单独扫一遍；顺带把紧随其后的半角冒号换成全角。
    """
    info = scan(doc)
    reps: list[tuple[int, int, str]] = []
    prev_label = False
    for r in info["runs"]:
        if r["opaque"]:
            continue
        text = r["text"]
        stripped = text.strip()
        if not stripped:
            continue
        zh = localize_label(stripped)
        if zh is not None:
            lead = text[:len(text) - len(text.lstrip())]
            trail = text[len(text.rstrip()):]
            reps.append((r["start"], r["end"], lead + escape(zh, quote=False) + trail))
            prev_label = True
            continue
        if prev_label and stripped[0] in ":：":
            # 「图 1: 题注」→「图 1：题注」
            i = text.index(stripped[0])
            reps.append((r["start"] + i, r["start"] + i + 1, "："))
        prev_label = False
    if not reps:
        return doc
    reps.sort(key=lambda x: x[0])
    out, last = [], 0
    for s, e, t in reps:
        out.append(doc[last:s])
        out.append(t)
        last = e
    out.append(doc[last:])
    return "".join(out)


PAGE_CSS = """
<style id="arz-style">
:root{--arz-en:#5b6570;--arz-line:#d0d7de}
body.arz{padding-bottom:3rem}
.arz-zh{display:inline}
body.arz-bi .arz-zh{display:block}
body.arz-bi .arz-en{display:block;color:var(--arz-en);font-size:.9em;line-height:1.65;
  border-left:3px solid var(--arz-line);padding-left:.7em;margin:.15em 0 .6em}
body.arz-bi.arz-zh-only .arz-en{display:none}
.arz-bar{position:fixed;right:14px;bottom:14px;z-index:99999;display:flex;gap:6px;
  align-items:center;font:13px/1.4 -apple-system,"Segoe UI",system-ui,sans-serif;
  background:#fff;border:1px solid var(--arz-line);border-radius:10px;padding:6px 8px;
  box-shadow:0 4px 18px rgba(0,0,0,.13)}
.arz-bar button{border:1px solid var(--arz-line);background:#f6f8fa;border-radius:7px;
  padding:3px 9px;cursor:pointer;font:inherit;color:#24292f}
.arz-bar button[aria-pressed="true"]{background:#0969da;border-color:#0969da;color:#fff}
.arz-bar span{color:#57606a;padding:3px 2px}
@media print{.arz-bar{display:none}}
/* 拆掉 ltx_text 外壳后补回两处观感：题注字号、项目符号与正文同行 */
.ltx_caption{font-size:90%}
li.ltx_item{display:flex;gap:.5em;align-items:baseline}
li.ltx_item>.ltx_tag{flex:0 0 auto}
</style>
"""

BAR = """
<div class="arz-bar" role="toolbar" aria-label="阅读选项">
  <span>对照</span>
  <button type="button" id="arz-m1" aria-pressed="true">中英</button>
  <button type="button" id="arz-m2" aria-pressed="false">仅中文</button>
  <button type="button" id="arz-fs">A+</button>
</div>
<script>
(function(){
  var b=document.body, m1=document.getElementById('arz-m1'),
      m2=document.getElementById('arz-m2'), fs=document.getElementById('arz-fs'),
      lv=0, sizes=[1,1.08,1.16,1.26];
  function setMode(zhOnly){
    b.classList.toggle('arz-zh-only', zhOnly);
    m1.setAttribute('aria-pressed', String(!zhOnly));
    m2.setAttribute('aria-pressed', String(zhOnly));
    try{ localStorage.setItem('arz-zh-only', zhOnly?'1':'0'); }catch(e){}
  }
  m1.onclick=function(){setMode(false)};
  m2.onclick=function(){setMode(true)};
  fs.onclick=function(){ lv=(lv+1)%sizes.length;
    document.documentElement.style.fontSize=(100*sizes[lv]).toFixed(0)+'%';
    fs.textContent='A'+'+'.repeat(lv+1); };
  try{ setMode(localStorage.getItem('arz-zh-only')==='1'); }catch(e){}
})();
</script>
"""


def _inject_page_style(doc: str, bilingual: bool) -> str:
    m = re.search(r"<body([^>]*)>", doc, re.I)
    cls = "arz arz-bi" if bilingual else "arz"
    if m:
        attrs = re.sub(r'\sclass\s*=\s*("[^"]*"|\'[^\']*\')', "", m.group(1))
        doc = doc[:m.start()] + f'<body{attrs} class="{cls}">' + doc[m.end():]
    else:
        doc = f'<body class="{cls}">' + doc
    doc = doc.replace("</head>", PAGE_CSS + "</head>", 1) if "</head>" in doc else PAGE_CSS + doc
    if bilingual and "</body>" in doc:
        doc = doc.replace("</body>", BAR + "</body>", 1)
    return doc


# ---------------------------------------------------------------- 体检

LATIN_RUN = re.compile(r"[A-Za-z][A-Za-z'’\-]*(?:[,\s]+[A-Za-z][A-Za-z'’\-]*){6,}")


def check(root: Path) -> dict:
    arz = meta_dir(root)
    seg_data = load_json(arz / "segments.json")
    if not seg_data or seg_data.get("engine") != "html":
        die("这不是 HTML 分支的项目")
    tr = load_json(arz / "translations.json", default={}) or {}
    out = root / "out" / MARKER_ID
    if not out.is_file():
        die("还没有 out/index.html，请先 html_mode.py apply")

    keys = load_json(arz / "tr_keys.json", default={}) or {}
    missing = [s["id"] for s in seg_data["segments"]
               if not str(tr.get(s["id"], "")).strip()
               or (keys.get(s["id"]) and keys[s["id"]] != text_key(s["masked"]))]
    info = scan(read_text(out))
    leftover = []
    for r in info["runs"]:
        if r["opaque"]:
            continue
        m = LATIN_RUN.search(r["text"])
        if m and len(re.findall(r"[A-Za-z]+", m.group(0))) >= 7:
            leftover.append({"ctx": r["ctx"], "text": r["text"].strip()[:90]})

    report = {"segments": len(seg_data["segments"]), "missing": missing,
              "leftover": leftover, "pass": not missing and not leftover}
    save_json(arz / "html_check.json", report)

    print(f"\n[{root.name}] 体检（HTML）")
    print(f"  片段      {report['segments']}")
    print(f"  缺译      {len(missing)}")
    print(f"  残留英文  {len(leftover)}（≥7 个连续英文词）")
    for it in leftover[:8]:
        print(f"    · [{it['ctx']}] {it['text']}")
    print("\n  结论      通过" if report["pass"] else "\n  结论      不通过")
    return report


# ---------------------------------------------------------------- 抓取

def http_get(url: str, timeout: int = 60, limit: int | None = None) -> tuple[str, str, bytes]:
    return sources.http_get(url, timeout, limit)


def _match_end(raw: str, tokens: list[dict], start: int, tag: str) -> int:
    depth, begun = 0, False
    for tk in tokens:
        if tk["kind"] == "start" and tk["tag"] == tag:
            if tk["start"] == start:
                begun = True
            if begun:
                depth += 1
        elif tk["kind"] == "end" and tk["tag"] == tag and begun:
            depth -= 1
            if depth == 0:
                return tk["end"]
    return len(raw)


def _article_span(raw: str, tokens: list[dict]) -> tuple[int, int]:
    """截出正文，扔掉出版社 / arXiv 的页头页脚、弹窗、导航。"""
    best: tuple[int, int] | None = None
    best_score = -1
    for tk in tokens:
        if tk["kind"] != "start":
            continue
        tag, attrs = tk["tag"], tk["attrs"]
        cls = (attrs.get("class") or "").lower()
        ident = attrs.get("id") or ""
        if "ltx_page_main" in cls or ident == "ltx_page_main":
            key, score = "ltx_page_main", 100
        elif tag == "article" and "ltx_document" in cls:
            key, score = "ltx_document", 90
        elif tag == "article":
            key, score = "article", 70
        elif tag == "main":
            key, score = "main", 60
        else:
            continue
        if score <= best_score:
            continue
        end = _match_end(raw, tokens, tk["start"], tag)
        if end > tk["start"]:
            best, best_score = (tk["start"], end), score
            if score >= 90:
                break
    if best:
        return best
    lo = raw.lower()
    m = re.search(r"<body[^>]*>", raw, re.I)
    return (m.end() if m else 0, lo.rfind("</body>") if "</body>" in lo else len(raw))


CHROME_CSS_HINTS = ("design-system", "typekit", "fonts.googleapis", "fonts.gstatic",
                    "cdn.jsdelivr", "bootstrapcdn", "fontawesome")


def _collect_assets(raw: str, base: str) -> tuple[list[dict], list[str]]:
    """找出论文自身的 CSS 与图片；出版社套壳的样式表（设计系统、字体服务）丢掉。"""
    items: list[dict] = []
    for m in re.finditer(r"<link\b[^>]*>", raw, re.I):
        tag = m.group(0)
        if not re.search(r'rel\s*=\s*["\']?stylesheet', tag, re.I):
            continue
        href = re.search(r'href\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        if not href:
            continue
        url = urljoin(base, href.group(1))
        if any(h in url.lower() for h in CHROME_CSS_HINTS):
            continue
        items.append({"url": url, "type": "css"})
    for m in re.finditer(r"<img\b[^>]*>", raw, re.I):
        tag = m.group(0)
        src = re.search(r'\bsrc\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        if src and not src.group(1).startswith("data:"):
            url = urljoin(base, src.group(1))
            items.append({"url": url, "type": "img", "attr": src.group(1)})
    inline = []
    for m in re.finditer(r"<style\b[^>]*>(.*?)</style>", raw, re.S | re.I):
        block = m.group(0)
        if any(h in block.lower() for h in ("typekit", "design-system")):
            continue
        inline.append(block)
    return items, inline


def _safe_name(url: str, used: set[str]) -> str:
    name = Path(urlparse(url).path).name or "asset"
    name = re.sub(r"[^\w.\-]", "_", name)
    if not Path(name).suffix:
        name += ".bin"
    stem, suf = Path(name).stem, Path(name).suffix
    i = 1
    while name in used:
        name = f"{stem}-{i}{suf}"
        i += 1
    used.add(name)
    return name


def _rewrite_css_urls(css: str, css_url: str) -> str:
    """CSS 里的相对 url() 补成绝对地址：离线时字体缺失会退化成后备字体，
    联网时还能拉到正确字形，不至于整页排版崩掉。"""
    def repl(m):
        q, u = m.group(1), m.group(2)
        if u.startswith(("data:", "http://", "https://", "#", "//")):
            return m.group(0)
        return f"url({q}{urljoin(css_url, u)}{q})"
    return re.sub(r"url\(\s*([\"']?)([^\"')]+)\1\s*\)", repl, css)


def _element_ranges(doc: str, tokens: list[dict], pred) -> list[tuple[int, int]]:
    """按谓词挑出整个元素的区间（含自身标签），用于整块删掉。"""
    out: list[tuple[int, int]] = []
    i, n = 0, len(tokens)
    while i < n:
        tk = tokens[i]
        if tk["kind"] == "start":
            cls = (tk["attrs"].get("class") or "").lower()
            if pred(tk["tag"], cls, tk["attrs"]):
                j = _match_close(tokens, i, n)
                if tokens[j]["kind"] == "end" and tokens[j]["tag"] == tk["tag"]:
                    out.append((tk["start"], tokens[j]["end"]))
                    i = j + 1
                    continue
        i += 1
    return out


def _strip_ranges(doc: str, ranges: list[tuple[int, int]]) -> str:
    for s, e in sorted(set(ranges), reverse=True):
        doc = doc[:s] + doc[e:]
    return doc


def _clean_doc(doc: str) -> str:
    """清洗基线：删掉出版社套壳（arXiv 的 license 横条、水印）与转换残渣，
    拆掉没有语义的 `<span class="ltx_text">` 外壳，让每段话恢复成连续文本。"""
    info = scan(doc)
    doc = _strip_ranges(doc, _element_ranges(
        doc, info["tokens"],
        lambda tag, cls, attrs: (attrs.get("id") or "").lower() in ("infobox", "watermark-tr")
        or "infobox" in cls or "watermark" in cls))

    # tabularray 一类命令转换失败后，会在页面上留下 `L[1]Q[l,m,wd=#1]` 这种
    # LaTeX 参数残渣。它所在的 ltx_para 整块删掉，比留着更干净。
    info = scan(doc)
    junk: list[tuple[int, int]] = []
    for (s, e) in _element_ranges(doc, info["tokens"],
                                  lambda tag, cls, attrs: "ltx_para" in cls):
        plain = re.sub(r"<[^>]+>", "", doc[s:e])
        if re.search(r"#\d", plain):
            junk.append((s, e))
    doc = _strip_ranges(doc, junk)

    info = scan(doc)
    unwrap: list[tuple[int, int]] = []
    for i, tk in enumerate(info["tokens"]):
        if tk["kind"] != "start" or tk["tag"] != "span":
            continue
        cls = (tk["attrs"].get("class") or "").lower()
        if any(h in cls for h in KEEP_SPAN_HINTS):
            continue
        j = _match_close(info["tokens"], i, len(info["tokens"]))
        if j == i or info["tokens"][j]["kind"] != "end":
            continue
        unwrap.append((tk["start"], tk["end"]))
        unwrap.append((info["tokens"][j]["start"], info["tokens"][j]["end"]))
    return _strip_ranges(doc, unwrap)


def fetch(source: str, project: Path) -> dict:
    info = sources.resolve(source)
    if not info["html_url"]:
        hint = ""
        if info["pdf_url"]:
            hint = (f"\n  这个来源没有可抓的 HTML，改用 PDF 分支：\n"
                    f"  python pdf_mode.py {info['pdf_url']} --project <项目> --compare")
        die(f"没有可抓的 HTML：{info['reason']}{hint}")

    project.mkdir(parents=True, exist_ok=True)
    src_dir, zh_dir = project / "src", project / "zh"
    src_dir.mkdir(exist_ok=True)
    zh_dir.mkdir(exist_ok=True)

    url = info["html_url"]
    local = Path(url)
    if local.is_file():
        raw, final, ctype = read_text(local), local.as_uri(), "text/html"
    else:
        print(f"  下载 {url}")
        try:
            final, ctype, blob = http_get(url)
        except urllib.error.HTTPError as exc:
            die(f"HTTP {exc.code}：{url}\n"
                f"  arXiv 的 HTML 只覆盖近年论文。换 LaTeX 分支（--mode latex），"
                f"或 PDF 分支：{info.get('pdf_url') or ''}")
        except Exception as exc:                       # noqa: BLE001
            die(f"下载失败：{exc}\n  网络不通时先把网页存到本地再跑：\n"
                f"  python html_mode.py fetch ./paper.html --project {project}")
        raw = blob.decode("utf-8", errors="replace")
        if "html" not in ctype.lower() and "<html" not in raw[:2000].lower():
            die(f"返回的不是 HTML（{ctype}）。这份文档请走 PDF 分支。")

    write_text(src_dir / MARKER_ID, raw)

    parsed = scan(raw)
    defects = {
        "missing_citation": raw.count("ltx_missing_citation"),
        "error_span": raw.count("ltx_ERROR"),
        "has_bibliography": bool(re.search(r"ltx_bibliograph|ltx_biblist", raw)),
    }

    a, b = _article_span(raw, parsed["tokens"])
    body = raw[a:b]
    if len(body) < 2000:
        warn(f"正文只有 {len(body)} 字符，可能截错元素了，建议目视检查 zh/{MARKER_ID}")

    assets, inline_styles = _collect_assets(raw, final)
    assets_dir = zh_dir / "assets"
    if assets_dir.exists():
        shutil.rmtree(assets_dir)
    assets_dir.mkdir(parents=True)
    used: set[str] = set()
    mapping: dict[str, str] = {}
    total_bytes = 0
    for item in assets:
        if len(mapping) >= 40 or total_bytes > 30 * 1024 * 1024:
            break
        try:
            f_url, f_type, blob = http_get(item["url"], timeout=45, limit=8 * 1024 * 1024)
        except Exception:                                  # noqa: BLE001
            continue
        if not blob:
            continue
        name = _safe_name(f_url, used)
        target = assets_dir / name
        if item["type"] == "css" or "css" in f_type.lower():
            try:
                target.write_text(_rewrite_css_urls(blob.decode("utf-8", errors="replace"), f_url),
                                  encoding="utf-8", newline="\n")
            except Exception:                              # noqa: BLE001
                continue
        else:
            try:
                target.write_bytes(blob)
            except Exception:                              # noqa: BLE001
                continue
        item["local"] = f"assets/{name}"
        if item.get("attr"):
            mapping[item["attr"]] = item["local"]
        total_bytes += len(blob)

    title_m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
    title = unescape(title_m.group(1).strip()) if title_m else (info.get("arxiv_id") or project.name)
    title = re.sub(r"\s+", " ", title)

    head = ['<!DOCTYPE html>', '<html lang="zh-CN">', "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1, shrink-to-fit=no">',
            f"<title>{escape(title)}</title>"]
    for item in assets:
        if item.get("local") and item["type"] == "css":
            head.append(f'<link rel="stylesheet" href="{item["local"]}">')
    head.extend(inline_styles)
    head.append("</head>")
    head.append('<body class="arz">')

    doc = "\n".join(head) + "\n" + body + "\n</body>\n</html>\n"

    for attr, local_path in mapping.items():
        doc = doc.replace(f'"{attr}"', f'"{local_path}"')
    doc = re.sub(r'<script\b[^>]*>.*?</script>', "", doc, flags=re.S | re.I)
    doc = re.sub(r'<span[^>]*class="[^"]*ltx_ERROR[^"]*"[^>]*>.*?</span>', "", doc,
                 flags=re.S | re.I)
    doc = re.sub(r"<noscript\b[^>]*>.*?</noscript>", "", doc, flags=re.S | re.I)
    doc = _clean_doc(doc)

    write_text(zh_dir / MARKER_ID, doc)
    save_json(meta_dir(project) / "html_src.json", {
        "engine": "html", "input": source, "kind": info["kind"], "url": final,
        "title": title, "chars": len(doc), "assets": len(mapping), "defects": defects,
    })

    print(f"\n[{project.name}] 抓取（HTML）")
    print(f"  来源      {info['kind']}  {final}")
    print(f"  标题      {title[:80]}")
    print(f"  正文      {len(body):,} 字符 → zh/{MARKER_ID}（整篇 {len(doc):,}）")
    print(f"  本地资源  {len(mapping)} 个（{total_bytes / 1024:.0f} KB）")
    if defects["error_span"] or defects["missing_citation"]:
        warn(f"这份 HTML 有转换缺陷：ltx_ERROR {defects['error_span']} 处 / "
             f"缺失引用 {defects['missing_citation']} 处 / 参考文献区块"
             f"{'有' if defects['has_bibliography'] else '缺失'}")
        warn("HTML 版是 LaTeXML 的降级产物。要原版式与完整参考文献，用 --mode latex 走源码重排。")
    ok("下一步：python html_mode.py extract <项目>")
    return {"chars": len(doc), "defects": defects}


# ---------------------------------------------------------------- 可选：打印 PDF

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
]


def find_chrome() -> str | None:
    for c in CHROME_CANDIDATES:
        if Path(c).is_file():
            return c
    return shutil.which("google-chrome") or shutil.which("chrome") or shutil.which("chromium")


def export_pdf(root: Path) -> None:
    """用 Chrome 无头模式把中文页打印成 PDF：不装 TeX 也能拿到一份可发人的成品。"""
    exe = find_chrome()
    page = root / "out" / MARKER_ID
    if not exe:
        warn("没找到 Chrome / Edge，跳过 PDF 导出（网页版已经可用）")
        return
    meta = load_json(meta_dir(root) / "html_src.json", default={}) or {}
    name = re.sub(r"[\\/:*?\"<>|]+", "_", (meta.get("title") or root.name))[:70]
    target = root / "out" / f"{name}_中文版.pdf"
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [exe, "--headless=new", "--disable-gpu", "--no-sandbox",
               f"--user-data-dir={tmp}", "--no-pdf-header-footer",
               "--virtual-time-budget=15000", f"--print-to-pdf={target}", page.as_uri()]
        try:
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=240)
        except Exception as exc:                           # noqa: BLE001
            warn(f"PDF 导出失败：{exc}")
            return
    if target.is_file() and target.stat().st_size > 10_000:
        ok(f"PDF：{target}（{target.stat().st_size / 1024 / 1024:.1f} MB）")
    else:
        warn("PDF 导出没成功（Chrome 版本过旧？）。网页版不受影响。")


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser(description="HTML 分支：抓网页版论文并回填中文")
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="下载 HTML 并清洗成基线")
    f.add_argument("source")
    f.add_argument("--project", required=True)

    e = sub.add_parser("extract", help="切段")
    e.add_argument("project")

    a = sub.add_parser("apply", help="回填译文，生成中文页与双语页")
    a.add_argument("project")
    a.add_argument("--no-bilingual", action="store_true")
    a.add_argument("--pdf", action="store_true", help="顺带用 Chrome 打印一份 PDF")

    c = sub.add_parser("check", help="体检")
    c.add_argument("project")

    args = ap.parse_args()
    if args.cmd == "fetch":
        fetch(args.source, Path(args.project).resolve())
    elif args.cmd == "extract":
        extract(Path(args.project).resolve())
    elif args.cmd == "apply":
        r = apply(Path(args.project).resolve(), not args.no_bilingual, args.pdf)
        return 0 if r["applied"] or not r["missing"] else 1
    elif args.cmd == "check":
        return 0 if check(Path(args.project).resolve())["pass"] else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
