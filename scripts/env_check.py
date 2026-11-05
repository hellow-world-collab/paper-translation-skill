#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""环境自检：缺什么、装什么，一次说清楚。

    python scripts/env_check.py           # 人看（✓ / ✗ / ⚠ 三态）
    python scripts/env_check.py --json    # 机器读

退出码：0 = 核心依赖齐了；1 = 缺 xelatex，LaTeX 分支跑不了。
刻意不检查中文字体：各平台差异太大，`inject_cjk.py` 会自己挑并给哨兵验证。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import find_bin  # noqa: E402

HINTS = {
    "xelatex": {
        "win": "winget install --id MiKTeX.MiKTeX -e   然后重开终端（或把 %LOCALAPPDATA%\\Programs\\MiKTeX\\miktex\\bin\\x64 加进 PATH）",
        "mac": "brew install --cask basictex && sudo tlmgr install xetex ctex",
        "linux": "sudo apt install texlive-xetex texlive-lang-chinese texlive-latex-extra latexmk",
    },
    "pdftoppm": {
        "win": "winget install --id oschwartz10612.Poppler -e",
        "mac": "brew install poppler",
        "linux": "sudo apt install poppler-utils",
    },
    "pymupdf": {
        "win": "python -m pip install pymupdf",
        "mac": "python3 -m pip install pymupdf",
        "linux": "python3 -m pip install pymupdf",
    },
}


def platform_key() -> str:
    if sys.platform.startswith("win"):
        return "win"
    if sys.platform == "darwin":
        return "mac"
    return "linux"


def linux_hint(item: str) -> str:
    """按实际包管理器换命令，省得用户拿着 apt 命令在 Fedora 上试。"""
    if shutil.which("dnf"):
        return {"xelatex": "sudo dnf install texlive-xetex texlive-collection-langchinese",
                "pdftoppm": "sudo dnf install poppler-utils",
                "pymupdf": "python3 -m pip install pymupdf"}.get(item, HINTS[item]["linux"])
    if shutil.which("pacman"):
        return {"xelatex": "sudo pacman -S texlive-core texlive-langchinese",
                "pdftoppm": "sudo pacman -S poppler",
                "pymupdf": "python3 -m pip install pymupdf"}.get(item, HINTS[item]["linux"])
    return HINTS[item]["linux"]


def hint(item: str) -> str:
    key = platform_key()
    return linux_hint(item) if key == "linux" else HINTS[item][key]


def check_bin(name: str) -> str | None:
    try:
        return find_bin(name, required=False)
    except Exception:
        return None


def main() -> None:
    as_json = "--json" in sys.argv[1:]
    py = sys.version_info
    py_ok = (3, 10) <= (py.major, py.minor) < (3, 13)

    items = []
    xe = check_bin("xelatex")
    items.append({
        "name": "xelatex", "level": "required",
        "ok": bool(xe), "path": xe,
        "why": "编译中文 PDF（LaTeX 分支的核心）",
        "hint": None if xe else hint("xelatex"),
    })
    pop = check_bin("pdftoppm")
    items.append({
        "name": "pdftoppm", "level": "optional",
        "ok": bool(pop), "path": pop,
        "why": "审计时把页面导成图（--png），没有也能编译",
        "hint": None if pop else hint("pdftoppm"),
    })

    try:
        import pymupdf as fitz  # noqa: F401
        ver = getattr(fitz, "__version__", "?")
    except Exception:
        try:
            import fitz  # type: ignore # noqa: F401
            ver = getattr(fitz, "__version__", "?")
        except Exception:
            ver = None
    items.append({
        "name": "pymupdf", "level": "optional",
        "ok": ver is not None, "path": ver,
        "why": "渲染两个 HTML（对照页、阅读页）；不渲染就不用装",
        "hint": None if ver else hint("pymupdf"),
    })

    items.append({
        "name": "Python", "level": "info",
        "ok": py_ok, "path": f"{py.major}.{py.minor}.{py.micro}",
        "why": "3.10–3.12 只影响 PDF 分支（pdf2zh 要求 <3.13）；LaTeX 分支不限",
        "hint": None if py_ok else "PDF 分支改用 3.12 建环境：py -3.12 -m venv .venv-pdf（macOS/Linux 用 python3.12）",
    })

    missing = [i for i in items if i["level"] == "required" and not i["ok"]]
    if as_json:
        print(json.dumps({"ok": not missing, "items": items}, ensure_ascii=False, indent=2))
    else:
        mark = {"required": ("✓", "✗"), "optional": ("✓", "⚠"), "info": ("✓", "⚠")}
        print("环境自检")
        for i in items:
            good, bad = mark[i["level"]]
            sym = good if i["ok"] else bad
            where = i["path"] or "未找到"
            print(f"  {sym} {i['name']:<9} {where}")
            print(f"      └─ {i['why']}")
        bad = [i for i in items if not i["ok"] and i["level"] != "info"]
        if bad:
            print("\n缺的这样装：")
            for i in bad:
                print(f"  {i['name']}：{i['hint']}")
            print("\n装完重跑一次本脚本，出现 ✓ 再继续。")
        else:
            print("\n核心依赖齐了，可以开工。")
    sys.exit(1 if missing else 0)


if __name__ == "__main__":
    main()
