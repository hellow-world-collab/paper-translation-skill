#!/usr/bin/env python3
"""第二步：基线编译 + 缺包自动补装 + XeLaTeX 兼容性探测。

只做体检，不改任何内容。产出 preflight.json，后续步骤据此决定策略。

用法：
    python preflight.py <项目目录> [--skip-xetex]
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import (die, find_bin, load_json, meta_dir, ok, pdf_pages,
                     read_text, run, save_json, warn, xetex_flags)

MISSING_FILE = re.compile(
    r"(?:File|file)\s+[`'\"]([^`'\"]+?\.(?:sty|cls|def|fd|clo|cfg|bbx|cbx))['\"]\s+not found"
)
TOO_MANY_ERRORS = "That makes 100 errors"
UNDEFINED_CITE = re.compile(r"(?:Citation|Reference)\s+[`'\"]([^`'\"]+)['\"]\s+undefined")

# 这些包在 XeLaTeX 下没有意义或必然冲突，探测时直接忽略错误
XETEX_INCOMPATIBLE = {
    "newtxtext", "newtxmath", "newtxsf", "txfonts", "pxfonts",
    "mathptmx", "times", "helvet", "courier", "ae", "aecompl",
    "inputenc", "fontenc",
}


def count_errors(log: str) -> int:
    """统计真实错误数。行首的单行 '!' 才算，避免把内容里的感叹号算进去。"""
    return len(re.findall(r"^!", log, flags=re.M))


def error_samples(log: str, limit: int = 6) -> list[str]:
    samples = []
    lines = log.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("!"):
            ctx = " / ".join(x.strip() for x in lines[i:i + 3] if x.strip())
            samples.append(ctx[:220])
            if len(samples) >= limit:
                break
    return samples


def missing_packages(log: str) -> list[str]:
    names = []
    for m in MISSING_FILE.finditer(log):
        name = Path(m.group(1)).stem
        if name not in names:
            names.append(name)
    return names


def install_package(name: str) -> bool:
    """MiKTeX 用 mpm，TeX Live 用 tlmgr。"""
    mpm = find_bin("mpm", required=False)
    if mpm:
        rc, out = run([mpm, f"--install={name}"], timeout=300)
        if rc == 0:
            return True
        warn(f"mpm 安装 {name} 失败：{out.strip().splitlines()[-1][:120] if out.strip() else ''}")
    tlmgr = find_bin("tlmgr", required=False)
    if tlmgr:
        rc, _ = run([tlmgr, "install", name], timeout=300)
        return rc == 0
    return False


def compile_once(work: Path, main: str, engine: str, rounds: int = 3,
                 auto_install: bool = True) -> dict:
    """跑一轮完整编译（engine → bibtex → engine ×2）。返回统计。"""
    binary = find_bin(engine)
    flags = xetex_flags(work) if engine == "xelatex" else ["-interaction=nonstopmode", "-file-line-error"]
    log_name = f"{Path(main).stem}.log"
    installed: list[str] = []
    first_log = ""

    for attempt in range(14):
        rc, out = run([binary, *flags, main], cwd=work, timeout=900)
        log_path = work / log_name
        log = read_text(log_path) if log_path.is_file() else out
        if attempt == 0:
            first_log = log

        missing = missing_packages(log)
        if not missing or not auto_install:
            break
        progressed = False
        for pkg in missing:
            if pkg in installed:
                continue
            warn(f"缺包 {pkg}，尝试安装")
            if install_package(pkg):
                installed.append(pkg)
                progressed = True
        if not progressed:
            break

    # 参考文献：有 .bib 且没有 .bbl 时跑 bibtex
    bib_files = list(work.rglob("*.bib"))
    bbl_ok = bool(list(work.rglob("*.bbl")))
    bib_run = False
    if bib_files and not bbl_ok:
        bibtex = find_bin("bibtex", required=False)
        if bibtex:
            rc_b, out_b = run([bibtex, Path(main).stem], cwd=work, timeout=180)
            bib_run = rc_b == 0
            if not bib_run:
                warn(f"bibtex 失败：{out_b.strip().splitlines()[-1][:140] if out_b.strip() else ''}")

    for _ in range(rounds - 1):
        run([binary, *flags, main], cwd=work, timeout=900)

    log_path = work / log_name
    log = read_text(log_path) if log_path.is_file() else out
    pdf = work / (Path(main).stem + ".pdf")
    pages = pdf_pages(pdf) if pdf.is_file() else 0

    return {
        "engine": engine,
        "exit_code": rc,
        "errors": count_errors(log),
        "pages": pages,
        "pdf_bytes": pdf.stat().st_size if pdf.is_file() else 0,
        "undefined": sorted(set(UNDEFINED_CITE.findall(log)))[:10],
        "truncated": TOO_MANY_ERRORS in log,
        "missing_packages": missing_packages(log),
        "auto_installed": installed,
        "bibtex_run": bib_run,
        "overfull": len(re.findall(r"^Overfull", log, flags=re.M)),
        "samples": error_samples(log),
        "log": str(log_path),
    }


def main():
    ap = argparse.ArgumentParser(description="基线编译与兼容性体检")
    ap.add_argument("project")
    ap.add_argument("--skip-xetex", action="store_true", help="跳过 XeLaTeX 探测")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    root = Path(args.project).resolve()
    arz = meta_dir(root)
    manifest = load_json(arz / "manifest.json")
    if not manifest:
        die(f"{root} 里没有 manifest.json，请先跑 fetch.py")
    src = root / manifest["src"]
    main = manifest["main"]
    out = arz / "preflight.json"

    if out.is_file() and not args.force:
        data = load_json(out)
        ok(f"复用已有体检结果：{out}")
        print_summary(data)
        return

    print(f"\n[{root.name}] 主文件 {main}")

    # ① 基线：原始引擎编译
    base = root / "base"
    if base.exists():
        shutil.rmtree(base)
    shutil.copytree(src, base)
    ok("复制源码到 base/")
    print(f"\n① 基线编译（{manifest['engine_hint']}）")
    baseline = compile_once(base, main, manifest["engine_hint"])
    print(f"   错误 {baseline['errors']}  页数 {baseline['pages']}  "
          f"溢出 {baseline['overfull']}  自动装包 {len(baseline['auto_installed'])}")
    if baseline["truncated"]:
        warn("编译撞上 100 错误上限，文档被截断——基线本身就不干净")
    if baseline["errors"]:
        warn("基线有错误，先解决源码问题再翻译：")
        for s in baseline["samples"][:4]:
            print("     " + s)

    # ② XeLaTeX 兼容性探测：不注入中文，纯看引擎能否跑通
    xetex = None
    if not args.skip_xetex and baseline["pages"]:
        xe = root / "probe-xe"
        if xe.exists():
            shutil.rmtree(xe)
        shutil.copytree(src, xe)
        print("\n② XeLaTeX 兼容性探测")
        xetex = compile_once(xe, main, "xelatex")
        print(f"   错误 {xetex['errors']}  页数 {xetex['pages']}")

    verdict = "unknown"
    if xetex:
        if xetex["errors"] == 0 and xetex["pages"] >= baseline["pages"] * 0.95:
            verdict = "xetex_ready"
        elif xetex["pages"] >= 1:
            verdict = "xetex_needs_repair"
        else:
            verdict = "xetex_broken"

    data = {
        "project": str(root),
        "main": main,
        "baseline": baseline,
        "xetex_probe": xetex,
        "verdict": verdict,
        "notes": judge(baseline, xetex, verdict),
    }
    save_json(out, data)
    print_summary(data)
    print(f"\n下一步：python inject_cjk.py {root}")


def judge(baseline: dict, xetex: dict | None, verdict: str) -> list[str]:
    notes = []
    if baseline["errors"]:
        notes.append("基线编译本身有错误，翻译前应先修复，否则无法判断中文引入的问题。")
    if baseline["truncated"]:
        notes.append("基线撞 100 错误上限。常见原因是作者把重音命令当变量名（$\\s$、$\\a$），"
                     "mapping 到 XeTeX 后会连环报错。见 rules/packages.yaml。")
    if xetex and xetex["errors"] == 0 and verdict == "xetex_needs_repair":
        notes.append("XeLaTeX 零错误但页数明显少于基线，说明有内容被静默丢弃，需人工看差异页。")
    if verdict == "xetex_broken":
        notes.append("XeLaTeX 无法产出 PDF。检查是否用了仅 pdfTeX 支持的宏包（pstricks、"
                     "minted 的 shell-escape 依赖、Type1 字体集）。")
    if baseline["auto_installed"]:
        notes.append(f"自动安装的宏包：{', '.join(baseline['auto_installed'])}")
    return notes


def print_summary(data: dict):
    b = data["baseline"]
    x = data.get("xetex_probe")
    print("\n" + "─" * 56)
    print(f"  结论      {data['verdict']}")
    print(f"  基线      {b['errors']} 错误 / {b['pages']} 页 / {b['overfull']} 溢出")
    if x:
        print(f"  XeLaTeX   {x['errors']} 错误 / {x['pages']} 页")
    for note in data.get("notes", []):
        print(f"  · {note}")
    print("─" * 56)


if __name__ == "__main__":
    main()
