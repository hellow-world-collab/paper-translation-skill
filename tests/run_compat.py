#!/usr/bin/env python3
"""兼容性自检：把 tests/cases 里的最小工程各跑一遍，打印一张矩阵。

每个 case 只覆盖一类容易翻车的写法：文档类、\\input 多文件、参考文献后端、
代码清单、表格 / 算法 / 子图、字体包放在哪、00README 指定的主文件……

用法：
    python tests/run_compat.py                 # fetch → inject → extract
    python tests/run_compat.py --full          # 再灌假译文，apply → build → 查 PDF
    python tests/run_compat.py ieeetran        # 只跑一个 case
    python tests/run_compat.py --keep          # 保留产物（默认放临时目录，跑完删）

判定：
    main    主文件识别正确（readme-toplevel 这块专门有一例）
    cjk     字体方案里至少有一个能把中文写进 PDF（哨兵探针通过）
    seg     抽取到的可译片段数合理，且必须包含 expect 列表里的关键词
    full    编译 0 错误、PDF 有中文、参考文献没丢（有 .bib 的 case）

退出码 0 表示全部通过。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
CASES = HERE / "cases"

# 每个 case 必须被抽出来的关键词：证明「短标签 / 表格单元格 / 帧标题 / 算法题注」没漏
EXPECT = {
    "article-input": ["Sample efficiency", "Robustness"],       # description 列表条目名
    "ieeetran": ["legged robots", "Introduction"],
    "beamer": ["Motivation", "Method", "Key idea"],             # 帧标题 / block 标题
    "floats": ["Walk", "Ours", "Simulation.", "Training procedure"],  # 表头 / 子图 / 算法题注
    "revtex": ["Model"],
    "llncs": ["Method"],
    "acmart": ["Introduction"],
    "elsarticle": ["Introduction"],
    "biblatex": ["Introduction"],
    "minted": ["Introduction"],
    "preamble-type1": ["Introduction"],
    "readme-toplevel": ["Introduction"],
}
# 主文件必须识别成什么（其余 case 都是 main.tex）
EXPECT_MAIN = {"readme-toplevel": "main.tex"}
# 这些 case 的 PDF 里必须有「参考文献」；beamer / revtex 的模板本身就不打印这个标题
EXPECT_REFS = {"article-input", "biblatex", "elsarticle", "llncs", "acmart",
               "minted", "floats", "ieeetran", "preamble-type1"}


def run(cmd: list[str], timeout: int) -> tuple[int, str, float]:
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or ""), time.time() - t0
    except subprocess.TimeoutExpired as exc:
        return 124, (exc.output or "") + "\n[TIMEOUT]", time.time() - t0


def fake_translate(root: Path) -> int:
    """把每个片段的英文词替换成「译文」，标记原样保留——只为跑通编译。"""
    seg = json.loads((root / ".arz" / "segments.json").read_text(encoding="utf-8"))
    tr = {s["id"]: re.sub(r"[A-Za-z][A-Za-z\-]{1,}", "译文", s["masked"])
          for s in seg["segments"]}
    (root / ".arz" / "translations.json").write_text(
        json.dumps(tr, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(tr)


def pdf_check(root: Path) -> dict:
    sys.path.insert(0, str(SCRIPTS))
    from _common import find_bin  # noqa: E402

    from _common import pdf_pages  # noqa: E402

    pdf = root / "out" / "main.pdf"
    out = {"pages": 0, "cjk": 0, "refs": None, "undefined": None}
    if not pdf.is_file():
        return out
    out["pages"] = pdf_pages(pdf)
    pdftotext = find_bin("pdftotext", required=False)
    if not pdftotext:
        return out
    rc, text, _ = run([pdftotext, "-layout", str(pdf), "-"], 120)
    out["cjk"] = len(re.findall(r"[\u4e00-\u9fff]", text))
    has_bib = any((root / "src").rglob("*.bib"))
    out["refs"] = ("参考文献" in text) if has_bib else None
    out["undefined"] = "??" in text
    return out


def one_case(case: Path, work: Path, full: bool) -> dict:
    name = case.name
    rec: dict = {"case": name}
    proj = work / name
    if proj.exists():
        shutil.rmtree(proj)

    rc, out, _ = run([sys.executable, str(SCRIPTS / "fetch.py"), str(case), "--out", str(proj)], 180)
    m = re.search(r"主文件\s+(\S+?)（", out)
    rec["main"] = m.group(1) if m else None
    rec["main_ok"] = rec["main"] == EXPECT_MAIN.get(name, "main.tex")
    if rc != 0:
        rec["error"] = out[-600:]
        rec["ok"] = False
        return rec

    rc, out, sec = run([sys.executable, str(SCRIPTS / "inject_cjk.py"), str(proj)], 1800)
    rec["inject_sec"] = round(sec, 1)
    m = re.search(r"采用 (\w+)：", out)
    rec["recipe"] = m.group(1) if m else None
    rec["probe_ok"] = bool(re.search(r"中文已进 PDF", out))
    if rc != 0:
        rec["error"] = out[-900:]

    rc2, out2, _ = run([sys.executable, str(SCRIPTS / "extract.py"), str(proj)], 300)
    seg_file = proj / ".arz" / "segments.json"
    masked = ""
    if seg_file.is_file():
        data = json.loads(seg_file.read_text(encoding="utf-8"))
        rec["segments"] = len(data["segments"])
        masked = "\n".join(s["masked"] for s in data["segments"])
    else:
        rec["segments"] = 0
        rec["error"] = out2[-600:]

    need = EXPECT.get(name, [])
    rec["missing_words"] = [w for w in need if w not in masked]

    if full and rec["segments"]:
        fake_translate(proj)
        run([sys.executable, str(SCRIPTS / "apply.py"), str(proj)], 300)
        rc3, out3, _ = run([sys.executable, str(SCRIPTS / "build.py"), str(proj)], 1800)
        rec["build_rc"] = rc3
        rec["pdf"] = pdf_check(proj)
        err = re.search(r"错误\s+(\d+)", out3)
        rec["compile_errors"] = int(err.group(1)) if err else None
        if rc3 != 0:
            rec["error"] = out3[-900:]

    refs_ok = True
    if full and name in EXPECT_REFS:
        refs_ok = bool((rec.get("pdf") or {}).get("refs"))
    rec["ok"] = (rec.get("main_ok") and rec.get("probe_ok") and not rec.get("missing_words")
                 and rec.get("segments", 0) > 0 and not rec.get("error") and refs_ok)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description="LaTeX 兼容性自检")
    ap.add_argument("cases", nargs="*", help="只跑指定的 case（默认全部）")
    ap.add_argument("--full", action="store_true", help="额外跑 apply → build 并检查 PDF")
    ap.add_argument("--keep", action="store_true", help="保留产物目录")
    ap.add_argument("--out", default=None, help="产物目录（默认用临时目录）")
    args = ap.parse_args()

    if not CASES.is_dir():
        print(f"找不到 {CASES}", file=sys.stderr)
        return 2

    wanted = args.cases or sorted(p.name for p in CASES.iterdir() if p.is_dir())
    work = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="arxzhtex-compat-"))
    work.mkdir(parents=True, exist_ok=True)

    print(f"样本 {len(wanted)} 个，产物 {work}")
    print("每个样本要跑一次真实编译（自动挑选字体方案），全套约 5–10 分钟\n")

    results = []
    for name in wanted:
        case = CASES / name
        if not case.is_dir():
            print(f"  跳过 {name}：没有这个 case")
            continue
        print(f"  → {name}", end="", flush=True)
        rec = one_case(case, work, args.full)
        results.append(rec)
        flag = "ok " if rec["ok"] else "FAIL"
        print(f"   {flag}  main={rec.get('main')}  recipe={rec.get('recipe')}  "
              f"段={rec.get('segments')}" + (f"  缺={rec['missing_words']}" if rec.get("missing_words") else ""))

    print("\n" + "=" * 78)
    head = f"{'case':<16} {'main':<12} {'字体':<5} {'中文':<5} {'段数':>4}  {'编译':<5} 备注"
    print(head)
    print("-" * 78)
    for r in results:
        extra = ""
        if r.get("missing_words"):
            extra = "漏抽：" + "、".join(r["missing_words"])
        elif r.get("error"):
            extra = r["error"].strip().splitlines()[-1][:60] if r["error"].strip() else "失败"
        pdf = r.get("pdf") or {}
        print(f"{r['case']:<16} {str(r.get('main')):<12} {str(r.get('recipe')):<5} "
              f"{'✓' if r.get('probe_ok') else '✗':<5} {r.get('segments', 0):>4}  "
              f"{('✓' if not r.get('compile_errors') else str(r['compile_errors'])):<5} {extra}")
        if args.full and pdf:
            print(f"{'':<16} PDF {pdf.get('pages', 0)} 页 / 中文 {pdf.get('cjk', 0)} 字"
                  f" / 参考文献 {pdf.get('refs')} / 未定义引用 {pdf.get('undefined')}")
    print("=" * 78)

    bad = [r for r in results if not r["ok"]]
    (work / "matrix.json").write_text(json.dumps(results, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    if not args.keep:
        print(f"\n产物在 {work}（--keep 可保留；本次先留着方便排查）")
    else:
        print(f"\n产物已保留：{work}")
    if bad:
        print(f"\n{len(bad)} 个用例未通过：" + "、".join(r["case"] for r in bad))
        return 1
    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
