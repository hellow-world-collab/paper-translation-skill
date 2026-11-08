#!/usr/bin/env python3
"""翻译前的抽取覆盖率检查：找出「源文里是英文正文、却没进 segments.json」的地方。

为什么要有这一步：check_tr.py / build.py 只检查 translations.json 对 segments.json
的覆盖率。若 extract.py 把某段判成不译命令的参数（例如旧版的
\\noindent{\\textbf{标题}} {同一行紧接的长段}），这段根本不在 segments 里，
体检会报 100%、编译会 0 错误，最后 PDF 却留着整段英文，只能靠人翻页发现。

做法：segments.json 记录了每段在 zh/ 源 tex 里的 [start, end] 偏移。
把源文减去这些区间的并集，剩下的就是「抽取器跳过的部分」；在这些区间里
剥掉命令、公式、注释、标签与图片路径，凡还留下成片英文（≥3 词 / ≥20 字符，
表格内 2 词 / 8 字符），或出现没被抽走的 \\section{...} 章节标题，就是可疑漏译。
位置差集比「拿字符串去 segments 里搜」可靠得多，也能直接给行号。

用法：
    python check_coverage.py <项目目录>                 # 报告；有可疑漏译退出码 1
    python check_coverage.py <项目目录> --warn-only     # 只提示，不阻断流程
    python check_coverage.py <项目目录> --json          # 机器可读

放在 extract.py 之后、开始翻译之前跑。修好抽取规则后应重跑 extract.py --force
并确认本检查清零。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import die, load_json, meta_dir, save_json  # noqa: E402

SKILL_ROOT = Path(__file__).resolve().parent.parent

# 参数里不是正文的命令：连名字带参数一起抹掉，否则 label key、图片路径、
# cite key 会被当成英文词凑成「成片英文」。\textbf / \emph / \caption 等
# 参数属于正文，不能进这张表。
NONPROSE_CMDS = {
    "includegraphics", "label", "ref", "eqref", "autoref", "cref", "Cref",
    "cite", "citep", "citet", "citeauthor", "citeyear", "nocite", "url", "href",
    "input", "include", "includeonly", "usepackage", "RequirePackage",
    "documentclass", "bibliography", "bibliographystyle", "addbibresource",
    "newcommand", "renewcommand", "providecommand", "DeclareMathOperator",
    "def", "let", "newenvironment", "renewenvironment", "newtheorem",
    "lstinputlisting", "lstset", "lstnewenvironment", "graphicspath",
    "setlength", "addtolength", "hspace", "vspace", "rule", "color",
    "textcolor", "definecolor", "pagestyle", "thispagestyle", "setcounter",
    "addtocounter", "counterwithin", "geometry", "phantom", "vphantom",
    "hphantom", "cline", "cmidrule", "multirow", "multicolumn",
}
# 无参声明命令（noindent / centering / 字号 / hfill …）绝不能进 NONPROSE_CMDS：
# 它们后面的 {..} 是正文而不是参数。旧版 extract.py 就是这么把
# \noindent{\textbf{标题}} {长段} 整段吞掉的，本脚本不能再犯。
# 命令名会在 strip_tex 的 else 分支里被单独抹掉，参数文字原样保留。

# 正文里成片英文的判据：≥3 个英文词、≥20 字符。短粗体标题（Quantitative
# Prediction Analysis）只有 3 个词，判据太松会漏；再松就会把命令参数碎片算进来。
RUN = re.compile(
    r"[A-Za-z][A-Za-z'’\-]*(?:[\s,;:()/\u2013\u2014]+[A-Za-z][A-Za-z'’\-]*){2,}")
# 表格表头常常只有一两个词（\makecell[c]{Dynamic\\Obstacle}），
# 用正文判据会整个漏掉；表格区内放宽到 2 词 / 8 字符，并过滤 RVO、PPO 这类
# 全大写方法缩写和 3 字母以下的排版碎片。
TABLE_RUN = re.compile(
    r"[A-Za-z][A-Za-z'’\-]*(?:[\s,;:()/\u2013\u2014]+[A-Za-z][A-Za-z'’\-]*){1,}")
TABLE_BLOCK = re.compile(
    r"\\begin\s*\{(?:tabular\*?|tabularx|tabulary|longtable\*?|supertabular|"
    r"xtabular|xltabular|tblr)\}.*?\\end\s*\{[^}]+\}", re.S)
EN_WORD = re.compile(r"[A-Za-z]{2,}")

# 章节标题是「一个词」也绝对不能漏（\section{METHOD} / \section{EXPERIMENTS}）。
# 上面的 RUN 要 ≥3 个英文词，单词标题永远报不出来，只能单独认命令参数。
# 2603.09460 实测：这两个全大写标题没进 segments，体检 100%、编译 0 错误，
# 最后靠人眼翻 PDF 才发现「III. METHOD」还是英文。
HEADING_ARG = re.compile(
    r"(?:\\(?:sub)*section|\\chapter|\\part|\\paragraph|\\subparagraph)\*?"
    r"\s*(?:\[[^\]]*\])?\s*\{([^{}]{1,120})\}")
# 标题里只有命令（\section{\label{sec:x}}）时不是正文，别误报
HEADING_JUNK = re.compile(
    r"\\(?:ref|eqref|label|cite|citep|citet|includegraphics|input|autoref|cref)\b")

# 代码类环境整块不查（里面本来就允许大段英文）
CODE_ENVS = ("verbatim", "lstlisting", "minted", "filecontents", "comment",
             "Verbatim", "alltt", "tikzpicture", "pgfplots", "axis", "picture",
             "circuitikz", "algorithm", "algorithmic")
CODE_BLOCK = re.compile(
    r"\\begin\s*\{(" + "|".join(CODE_ENVS) + r")\}.*?\\end\s*\{\1\}", re.S)

MATH_PAIRS = [
    re.compile(r"\$\$.*?\$\$", re.S),
    re.compile(r"(?<!\\)\$(?:[^$\\]|\\.)*?\$", re.S),
    re.compile(r"\\\[.*?\\\]", re.S),
    re.compile(r"\\\(.*?\\\)", re.S),
    re.compile(r"\\begin\s*\{(?:equation\*?|align\*?|gather\*?|multline\*?|"
               r"flalign\*?|displaymath|math|array)\}.*?\\end\s*\{[^}]+\}", re.S),
]
COMMENT = re.compile(r"(?<!\\)%.*$", re.M)
CMD_HEAD = re.compile(r"\\([A-Za-z@]+|.)")


def read_balanced(s: str, i: int) -> int:
    """i 指向 { 或 [，返回配对右括号之后的位置（含）；处理嵌套。"""
    if i >= len(s) or s[i] not in "{[":
        return i + 1
    pair = {"{": "}", "[": "]"}[s[i]]
    depth = 0
    j = i
    while j < len(s):
        if s[j] == "\\" and j + 1 < len(s):
            j += 2
            continue
        if s[j] == s[i]:
            depth += 1
        elif s[j] == pair:
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return len(s)


def strip_tex(chunk: str) -> str:
    """把 chunk 里的命令名、非正文参数、公式、注释去掉，只留可能印到纸上的文字。"""
    s = COMMENT.sub(" ", chunk)
    s = CODE_BLOCK.sub(" ", s)
    for pat in MATH_PAIRS:
        s = pat.sub(" ", s)

    out: list[str] = []
    i = 0
    while i < len(s):
        if s[i] != "\\":
            out.append(s[i])
            i += 1
            continue
        m = CMD_HEAD.match(s, i)
        if not m:
            out.append(" ")
            i += 1
            continue
        name, j = m.group(1), m.end()
        if name in NONPROSE_CMDS:
            while j < len(s) and s[j] in " \t\n":
                j += 1
            while j < len(s) and s[j] in "{[":
                j = read_balanced(s, j)
                while j < len(s) and s[j] in " \t\n":
                    j += 1
            out.append(" ")
            i = j
        else:
            # 命令名抹掉，参数里的文字保留（\textbf{...} 是正文）
            out.append(" ")
            i = j
    return "".join(out)


def uncovered_spans(text: str, segs: list[dict]) -> list[tuple[int, int]]:
    ranges = sorted((int(s["start"]), int(s["end"])) for s in segs)
    merged: list[list[int]] = []
    for a, b in ranges:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    spans: list[tuple[int, int]] = []
    prev = 0
    for a, b in merged:
        if a > prev:
            spans.append((prev, a))
        prev = max(prev, b)
    if prev < len(text):
        spans.append((prev, len(text)))
    return spans


def scan(root: Path, min_chars: int = 20) -> dict:
    arz = meta_dir(root)
    data = load_json(arz / "segments.json")
    if not data:
        die("缺少 segments.json，请先跑 extract.py")

    by_file: dict[str, list[dict]] = {}
    for s in data["segments"]:
        by_file.setdefault(s["file"], []).append(s)

    # 一个片段都没抽到的 .tex 文件不会出现在 by_file，只按 by_file 遍历的话，
    # 整文件漏抽时连查都不会查它（\section{METHOD} 那次就踩了这个盲区）。
    # 这类文件只做结构性标题检查，不套「成片英文」判据，免得公式与宏定义误报。
    zh = root / "zh"
    zero_files = sorted(
        p.relative_to(zh).as_posix() for p in zh.rglob("*")
        if p.is_file() and p.suffix.lower() in {".tex", ".ltx"}
        and p.relative_to(zh).as_posix() not in by_file)

    findings: list[dict] = []
    total_uncovered = 0
    covered_chars = 0
    for rel in sorted(set(by_file) | set(zero_files)):
        segs = by_file.get(rel, [])
        path = root / "zh" / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        # 导言区全是模板宏、字体声明与断词样本（IEEE 的 op-tical net-works 等），
        # 扫它只会制造误报。正文才是漏译高发区。
        m_doc = re.search(r"\\begin\s*\{document\}", text)
        body_start = m_doc.start() if m_doc else 0
        covered_chars += sum(s["end"] - s["start"] for s in segs)
        table_ranges = [(m.start(), m.end()) for m in TABLE_BLOCK.finditer(text)]
        spans = [(max(a, body_start), b)
                 for a, b in uncovered_spans(text, segs) if b > body_start]
        for a, b in spans:
            chunk = text[a:b]
            total_uncovered += len(chunk)
            # 章节标题单独查：单个词（尤其整词大写）永远达不到成片判据，
            # 但漏掉它就是中文 PDF 里一个刺眼的英文大标题。
            for hm in HEADING_ARG.finditer(chunk):
                title = " ".join(hm.group(1).split())
                if not re.search(r"[A-Za-z]{3,}", title) or HEADING_JUNK.search(title):
                    continue
                hline = text.count("\n", 0, a + hm.start(1)) + 1
                findings.append({"file": rel, "line": hline, "chars": len(title),
                                 "text": title[:160], "kind": "heading"})
            if not segs:
                # 整文件零片段：只报结构性标题，别拿公式/宏定义里的英文碎片凑数
                continue
            cleaned = strip_tex(chunk)
            in_table = any(a < e and b > st for st, e in table_ranges)
            pat = TABLE_RUN if in_table else RUN
            need_words = 2 if in_table else 3
            floor = min(min_chars, 8) if in_table else min_chars
            for m in pat.finditer(cleaned):
                run = " ".join(m.group(0).split())
                words = EN_WORD.findall(run)
                if len(run) < floor or len(words) < need_words:
                    continue
                if in_table and not any(len(w) >= 4 and not w.isupper() for w in words):
                    continue
                # cleaned 丢掉了命令，m.start() 不是原文件位置；回到 chunk 里找首词定位行号
                first = run.split()[0].strip(",;:()")
                pos = chunk.find(first)
                at = a + pos if pos >= 0 else a
                line = text.count("\n", 0, at) + 1
                findings.append({"file": rel, "line": line, "chars": len(run),
                                 "text": run[:160]})

    # 同一段文字可能被多个 span 或命令切碎，按 (文件, 文本前 60 字) 去重
    seen: set[tuple[str, str]] = set()
    uniq: list[dict] = []
    for f in findings:
        key = (f["file"], f["text"][:60])
        if key not in seen:
            seen.add(key)
            uniq.append(f)
    uniq.sort(key=lambda x: (x["file"], x["line"]))

    report = {
        "files": len(by_file),
        "segments": len(data["segments"]),
        "covered_chars": covered_chars,
        "uncovered_chars": total_uncovered,
        "suspects": uniq,
        "suspect_chars": sum(f["chars"] for f in uniq),
        "pass": not uniq,
    }
    save_json(arz / "coverage.json", report)
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="翻译前检查抽取覆盖率（检出未抽取的英文正文）")
    ap.add_argument("project")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--warn-only", action="store_true", help="有可疑漏译也返回 0")
    ap.add_argument("--min-chars", type=int, default=20)
    args = ap.parse_args()

    root = Path(args.project).resolve()
    r = scan(root, args.min_chars)

    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if (r["pass"] or args.warn_only) else 1

    print(f"\n[{root.name}] 抽取覆盖率检查（翻译前）")
    print(f"  源文件       {r['files']} 个 / {r['segments']} 段")
    print(f"  已覆盖字符   {r['covered_chars']}")
    print(f"  未覆盖字符   {r['uncovered_chars']}（含命令、公式、标签等正常不译内容）")
    print(f"  可疑英文成片 {len(r['suspects'])} 处 / {r['suspect_chars']} 字符")
    for f in r["suspects"][:15]:
        print(f"    {f['file']}:{f['line']:<5} {f['chars']:4d}字  {f['text'][:100]}")
    if len(r["suspects"]) > 15:
        print(f"    …… 另有 {len(r['suspects']) - 15} 处（见 .arz/coverage.json）")

    if r["pass"]:
        print("\n  结论      通过：未发现被跳过的英文正文")
        return 0
    print("\n  结论      不通过：这些正文没进 segments.json，检查通过后不会有任何体检能发现它们")
    print("            修好 extract.py 后重跑：python extract.py <项目目录> --force")
    return 0 if args.warn_only else 1


if __name__ == "__main__":
    sys.exit(main())
