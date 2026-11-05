#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打技能市场的上架包——把仓库压成平台能接受的两级目录。

平台口径是「根目录 / 二级目录 / 文件」（https://open.workbuddy.cn/docs/skill），
而仓库里 `assets/katex/fonts/*.woff2` 是四层、`tests/cases/*/*` 五六层，
直接 zip 整个目录会被「目录层级超限」挡回来。这里做三件事：

1. **KaTeX 压平**：字体内联成 CSS 的 data URI，只留 `assets/katex.min.css`、
   `assets/katex.min.js`、`assets/auto-render.min.js` 三个文件，
   不再有 `fonts/`、`contrib/` 子目录；
2. **丢掉只对仓库有意义的资源**：docs 截图、tests 样本、README、图标母版；
3. **打完自检**：层级、体积、frontmatter 必填项，不合格直接报错，
   免得传到平台上才被驳回。

用法：
    python scripts/package_market.py
    python scripts/package_market.py --out ~/Desktop/arxiv-zh-tex-1.0.0.zip
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import re
import sys
import zipfile
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
ASSETS = SKILL_ROOT / "assets"
KATEX = ASSETS / "katex"

MAX_BYTES = 3 * 1024 * 1024
MAX_DEPTH = 2  # 相对技能根目录的目录层数；文件本身不算

SKIP_DIRS = {
    ".git", ".github", "docs", "tests", "dist", "build",
    "__pycache__", ".pytest_cache", ".venv-pdf", ".venv", ".idea", ".vscode",
}
SKIP_FILES = {
    "_skillhub_meta.json", ".DS_Store", "Thumbs.db",
    ".gitignore", ".gitattributes", "README.md",
}
# 这几张图只服务 README 与市场详情页，装到用户机器上没用；根目录的 _icon.png 要留
SKIP_ASSETS = {"icon.png", "icon-256.png", "icon-dark.png", "social-preview.png"}

REQUIRED = [
    "name", "display_name", "display_name_en", "description",
    "description_zh", "description_en", "version", "author",
]


def fail(msg: str) -> None:
    print("✗ " + msg, file=sys.stderr)
    sys.exit(1)


def frontmatter() -> dict[str, str]:
    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    if not text.startswith("---"):
        fail("SKILL.md 开头没有 YAML frontmatter")
    head = text.split("---", 2)[1]
    data: dict[str, str] = {}
    for line in head.splitlines():
        if ":" in line and not line[:1].isspace() and not line.startswith("-"):
            k, v = line.split(":", 1)
            data[k.strip()] = v.strip().strip('"').strip("'")
    missing = [k for k in REQUIRED if not data.get(k)]
    if missing:
        fail("frontmatter 缺必填字段：" + "、".join(missing))
    return data


def flat_katex() -> dict[str, bytes]:
    """把 KaTeX 收成三个平铺文件，CSS 里的字体换成 data URI。"""
    css = (KATEX / "katex.min.css").read_text(encoding="utf-8")
    bad: list[str] = []

    def repl(m: re.Match) -> str:
        block = m.group(0)
        hit = re.search(r"fonts/([^)\"']+?\.woff2)", block)
        if not hit:
            bad.append(block[:70])
            return block
        font = KATEX / "fonts" / hit.group(1)
        if not font.is_file():
            bad.append(hit.group(1))
            return block
        b64 = base64.b64encode(font.read_bytes()).decode("ascii")
        return re.sub(r"src:[^}]+",
                      'src:url(data:font/woff2;base64,%s) format("woff2")' % b64, block)

    css = re.sub(r"@font-face\{[^}]*\}", repl, css)
    if bad:
        fail("KaTeX 字体缺失：%s" % "、".join(bad[:3]))
    if "url(fonts/" in css:
        fail("KaTeX CSS 里仍有外部字体引用，压平失败")

    return {
        "katex.min.css": css.encode("utf-8"),
        "katex.min.js": (KATEX / "katex.min.js").read_bytes(),
        "auto-render.min.js": (KATEX / "contrib" / "auto-render.min.js").read_bytes(),
    }


def collect() -> dict[tuple[str, ...], bytes]:
    files: dict[tuple[str, ...], bytes] = {}
    for p in sorted(SKILL_ROOT.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(SKILL_ROOT)
        if any(part in SKIP_DIRS for part in rel.parts[:-1]):
            continue
        if rel.name in SKIP_FILES or p.suffix in (".pyc", ".pyo"):
            continue
        if rel.parts[:2] == ("assets", "katex"):
            continue  # 由 flat_katex() 统一处理
        if rel.parts[0] == "assets" and rel.name in SKIP_ASSETS:
            continue
        files[rel.parts] = p.read_bytes()
    for name, data in flat_katex().items():
        files[("assets", name)] = data
    return files


def main() -> None:
    meta = frontmatter()
    top = meta["name"]
    ap = argparse.ArgumentParser(description="打技能市场上架包（两级目录）")
    ap.add_argument("--out", default=str(
        SKILL_ROOT / "dist" / ("%s-skill-%s.zip" % (top, meta["version"]))))
    args = ap.parse_args()
    out = Path(args.out).expanduser().resolve()

    files = collect()

    too_deep = [f"{top}/" + "/".join(parts) for parts in files if len(parts) > MAX_DEPTH]
    if too_deep:
        fail("仍有 %d 个文件超过两级目录：\n  %s" % (len(too_deep), "\n  ".join(too_deep[:5])))

    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for parts in sorted(files):
            z.writestr("/".join((top,) + parts), files[parts],
                       compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)

    raw = out.read_bytes()
    if len(raw) > MAX_BYTES:
        fail("包体积 %.2f MB 超过 3 MB，需要再删资源" % (len(raw) / 1024 / 1024))

    dirs = sorted({"/".join((top,) + parts[:-1]) if len(parts) > 1 else top for parts in files})
    print("✓ %s" % out)
    print("  %d 个文件 / %.2f MB / 最深 %d 层 / sha256 %s" % (
        len(files), len(raw) / 1024 / 1024,
        max(len(p) + 1 for p in files), hashlib.sha256(raw).hexdigest()[:16]))
    print("  目录：%s" % "、".join(dirs))
    print("  版本：%s（更新内容时记得先改 SKILL.md 的 version）" % meta["version"])


if __name__ == "__main__":
    main()
