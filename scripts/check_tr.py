#!/usr/bin/env python3
"""译文体检：编译前先查「翻全了没有」，别把半中半英的 PDF 交出去。

为什么单独一步：
  apply.py 只校验标记与反斜杠，缺译的段会被静默跳过、原样保留英文；
  编译又永远成功（LaTeX 不在乎中英混排），最后只能靠人翻 20 页去发现。
  这一步把「哪些段其实没翻」提前摆出来。

查五类问题：
  1. 缺译        segments.json 里有、translations.json 里没有
  2. 疑似原样返回 译文与原文 masked 完全相同（公式段正常，正文段异常）
  3. 中文过少    正文段的汉字占比 < 8%，基本等于没翻
  4. 成片英文    译文里仍有 ≥ 7 个连续英文单词（常见于模型漏抄一截）
  5. 未抽取正文  源文里是英文正文、却没进 segments.json —— 前四类都查不到它，
                 由 check_coverage.py 的位置差集给出（旧版漏 8 个小标题 + 3 段正文）

用法：
    python check_tr.py <项目目录>              # 体检，有问题退出码 1
    python check_tr.py <项目目录> --strict      # 连「成片英文」也算失败
    python check_tr.py <项目目录> --json        # 机器可读
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import MARKER_RE, cjk_ratio, die, load_json, meta_dir, save_json  # noqa: E402

# 连续 7 个以上英文单词（允许中间的逗号、连字符）。中英混排时模型常把半句话原样留下。
LATIN_RUN = re.compile(r"[A-Za-z][A-Za-z'’\-]*(?:[,\s]+[A-Za-z][A-Za-z'’\-]*){6,}")
# 正文段允许的「没翻」下限：低于这个汉字占比就当没译
MIN_CJK = 0.08
# 致谢 / 作者名单整段几乎都是人名与机构名，中文占比天然 < 8%，
# 但译文是完整的（2506.09985 的致谢段实测 30 余个人名、汉字占比 4%）。
# 「首字母大写且至少两个词」的专名，用来判断是不是这类名单。
PROPER_NAME = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b")


def looks_like_name_list(source: str, translated: str) -> bool:
    """纯人名/机构名单：不能按「中文过少」判成漏译。"""
    names = PROPER_NAME.findall(source)
    if len(names) < 5 or source.count(",") < 3:
        return False
    letters = len(re.findall(r"[A-Za-z]", source))
    covered = sum(len(n) for n in names) / max(1, letters)
    # 仍要求译文里有汉字，且与原句不同（原样返回在调用处已经单独判过）
    return covered >= 0.5 and bool(re.search(r"[\u4e00-\u9fff]", translated))


def bare(text: str) -> str:
    return MARKER_RE.sub("", text or "")


def run_len(s: str) -> int:
    return len(re.findall(r"[A-Za-z][A-Za-z'’\-]*", s))


def scan(root: Path, strict: bool = False) -> dict:
    """体检主体，供 check_tr.py 与 build.py 共用。"""
    arz = meta_dir(root)
    data = load_json(arz / "segments.json")
    if not data:
        die("缺少 segments.json，请先跑 extract.py")
    tr = load_json(arz / "translations.json", default={}) or {}

    segs = data["segments"]
    missing: list[dict] = []
    same: list[dict] = []
    thin: list[dict] = []
    latin: list[dict] = []

    for s in segs:
        t = tr.get(s["id"])
        if t is None or not str(t).strip():
            missing.append({"id": s["id"], "file": s["file"], "role": s["role"],
                            "src": s["source"][:90]})
            continue
        t = str(t)
        b = bare(t)
        body = bare(s["masked"])
        words_src = run_len(body)

        if b.strip() == body.strip():
            same.append({"id": s["id"], "file": s["file"], "role": s["role"],
                         "src": s["source"][:90]})
            continue

        # 公式/纯符号段没有汉字是正常的，只对含 8 个以上英文词的段要求中文
        if (s["role"] != "head" and words_src >= 8 and cjk_ratio(b) < MIN_CJK
                and not looks_like_name_list(body, b)):
            thin.append({"id": s["id"], "file": s["file"], "cjk": round(cjk_ratio(b), 3),
                         "src": s["source"][:90], "tr": b[:90]})

        # 贡献者 / 作者名单里连续英文人名不是「漏抄一段」，别误报成片英文
        longest = ""
        if not looks_like_name_list(body, b):
            for m in LATIN_RUN.finditer(b):
                if len(m.group(0)) > len(longest):
                    longest = m.group(0)
        if longest and run_len(longest) >= 7:
            latin.append({"id": s["id"], "file": s["file"], "words": run_len(longest),
                          "run": longest[:100], "role": s["role"]})

    # 第 5 类：源文里根本没被 extract.py 抽到的英文正文。体检只比对
    # translations 与 segments，对这类漏译天然是 100% 通过；交给位置差集检查。
    from check_coverage import scan as scan_coverage
    uncovered = scan_coverage(root)["suspects"]

    total = len(segs)
    done = total - len(missing)
    report = {
        "total": total,
        "translated": done,
        "missing": missing,
        "identical": same,
        "low_cjk": thin,
        "latin_runs": latin,
        "uncovered": uncovered,
        "pass": not (missing or thin or uncovered or (strict and latin)),
    }
    save_json(arz / "check_tr.json", report)
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="译文完整性体检")
    ap.add_argument("project")
    ap.add_argument("--strict", action="store_true", help="成片英文也算失败")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    root = Path(args.project).resolve()
    report = scan(root, args.strict)
    missing, same = report["missing"], report["identical"]
    thin, latin = report["low_cjk"], report["latin_runs"]
    uncovered = report["uncovered"]
    total, done = report["total"], report["translated"]

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["pass"] else 1

    def block(title: str, items: list[dict], fmt) -> None:
        if not items:
            return
        print(f"\n  {title}（{len(items)}）")
        for it in items[:12]:
            print("    " + fmt(it))
        if len(items) > 12:
            print(f"    …… 另有 {len(items) - 12} 条")

    print(f"\n[{root.name}] 译文体检")
    print(f"  片段      {total}")
    print(f"  已译      {done}（{done / total:.0%}）" if total else "  片段      0")
    print(f"  缺译      {len(missing)}")
    print(f"  原样返回  {len(same)}（公式、纯符号段属正常）")
    print(f"  中文过少  {len(thin)}")
    print(f"  成片英文  {len(latin)}")
    print(f"  未抽取正文 {len(uncovered)}（源文里有、segments 里没有）")

    block("源文里没被抽取的英文正文（覆盖率体检查不到，需修 extract.py）", uncovered,
          lambda it: f"{it['file']}:{it['line']:<5} {it['text'][:70]}")
    block("缺译（这些段会原样保留英文）", missing,
          lambda it: f"{it['id']:<28} {it['file']}  {it['src']}")
    block("中文过少（几乎等于没翻）", thin,
          lambda it: f"{it['id']:<28} 汉字占比 {it['cjk']:.0%}  {it['tr']}")
    block("成片英文（可能是漏抄一段）", latin,
          lambda it: f"{it['id']:<28} {it['words']} 词：{it['run']}")

    if report["pass"]:
        print("\n  结论      通过")
        return 0
    print("\n  结论      不通过（先补齐再 build，否则 PDF 会是半中半英）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
