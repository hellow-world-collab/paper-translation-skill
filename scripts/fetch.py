#!/usr/bin/env python3
"""第一步：取回论文源码，识别主文件，产出 manifest.json。

用法：
    python fetch.py 2508.08241 --out ./paper
    python fetch.py https://arxiv.org/abs/2508.08241 --out ./paper
    python fetch.py ./some-latex-dir --out ./paper
    python fetch.py ./paper.tar.gz --out ./paper
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import die, load_json, meta_dir, ok, read_text, save_json, warn

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) arxiv-zh-tex/1.0"
ARXIV_ID = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")

TEX_SUFFIX = {".tex", ".ltx"}
BIB_SUFFIX = {".bib"}
FIG_SUFFIX = {".pdf", ".png", ".jpg", ".jpeg", ".eps", ".ps", ".svg"}

# 主文件命名偏好，从高到低
MAIN_NAME_HINTS = [
    "main", "root", "paper", "ms", "manuscript", "article",
    "neurips", "iclr", "cvpr", "aaai", "acl", "icml",
]


def normalize_source(source: str) -> tuple[str, str | None]:
    """把各种写法归一成 arXiv ID。返回 (id, 版本)。"""
    m = ARXIV_ID.search(source)
    if m and ("arxiv" in source.lower() or source.strip() == m.group(0)):
        return m.group(1), m.group(2)
    return source, None


def download_arxiv(arxiv_id: str, dest: Path, version: str | None = None) -> Path:
    """下载 e-print 包。先直连，失败后依次尝试本机代理。"""
    ident = arxiv_id + (version or "")
    urls = [
        f"https://arxiv.org/e-print/{ident}",
        f"https://export.arxiv.org/e-print/{ident}",
    ]
    proxies = [None]
    # 直连失败时试常见本地代理端口（Clash / V2Ray 默认口）
    for port in ("7897", "7890", "10809", "1080"):
        proxies.append(f"http://127.0.0.1:{port}")

    last_err = ""
    for url in urls:
        for proxy in proxies:
            opener = urllib.request.build_opener()
            if proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy})
                )
            # 显式清掉环境变量代理对直连的干扰
            if proxy is None:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({})
                )
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            try:
                with opener.open(req, timeout=180) as resp:
                    data = resp.read()
                if len(data) < 1024:
                    last_err = f"{url} 返回内容过小（{len(data)} 字节）"
                    continue
                dest.write_bytes(data)
                ok(f"下载 {len(data) / 1e6:.2f} MB ← {url}"
                   + (f"（经代理 {proxy}）" if proxy else "（直连）"))
                return dest
            except Exception as exc:  # noqa: BLE001
                last_err = f"{url} {'经 ' + proxy if proxy else '直连'}：{exc}"
                continue
    die(
        "下载失败，最后一次错误：\n  "
        + last_err
        + "\n\n排查建议：手动下载 https://arxiv.org/e-print/"
        + ident
        + " 后，用本地文件模式：python fetch.py <文件路径> --out <目录>"
        + "\n\n如果这篇确实没有源码（作者只交了 PDF，e-print 返回 404 / no source），"
        + "改走 PDF 分支：\n  python "
        + str(Path(__file__).with_name("pdf_mode.py"))
        + " https://arxiv.org/pdf/"
        + ident
        + " --project <项目目录> --compare"
    )


def unpack(archive: Path, src: Path) -> None:
    """解压 arXiv 包。可能是 tar.gz，也可能是单文件 gzip。"""
    if src.exists():
        shutil.rmtree(src)
    src.mkdir(parents=True)

    if tarfile.is_tarfile(archive):
        with tarfile.open(archive) as tf:
            for member in tf.getmembers():
                # 防目录穿越
                target = (src / member.name).resolve()
                if not str(target).startswith(str(src.resolve())):
                    warn(f"跳过可疑路径：{member.name}")
                    continue
                tf.extract(member, src, filter="data") if hasattr(tarfile, "data_filter") else tf.extract(member, src)
        ok("解压 tar 包")
        return

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(src)
        ok("解压 zip 包")
        return

    # 单文件 gzip：解出来通常就是一个 .tex
    import gzip

    raw = gzip.decompress(archive.read_bytes())
    head = raw[:400].decode("latin-1", errors="replace")
    if "%PDF" in head:
        die(
            "这个 arXiv 条目只有 PDF、没有 LaTeX 源码（作者提交的是 PDF-only）。\n"
            "本流程需要源码才能保持原排版。遇到这种论文只能走 PDF 重排方案。"
        )
    name = "main.tex"
    m = re.search(r"\\documentclass", head)
    (src / name).write_bytes(raw)
    ok(f"解清单文件 gzip → {name}（单文件 tex）")
    if not m:
        warn("解出的内容不像 LaTeX 源码，请人工确认")


def collect_files(src: Path) -> dict:
    tex, bib, fig = [], [], []
    for path in src.rglob("*"):
        if not path.is_file() or ".arz" in path.parts:
            continue
        suffix = path.suffix.lower()
        rel = path.relative_to(src).as_posix()
        if suffix in TEX_SUFFIX:
            tex.append(rel)
        elif suffix in BIB_SUFFIX:
            bib.append(rel)
        elif suffix in FIG_SUFFIX:
            fig.append(rel)
    return {"tex": sorted(tex), "bib": sorted(bib), "figures": sorted(fig)}


def read_arxiv_readme(src: Path) -> str:
    """arXiv 的 00README.json / 00README 可能指定主文件或要忽略的文件。"""
    for name in ("00README.json", "00README.yaml", "00README"):
        p = src / name
        if p.is_file():
            return read_text(p)
    return ""


def pick_main(src: Path, tex_files: list[str]) -> tuple[str, str]:
    """识别主文件，返回 (相对路径, 判定依据)。"""
    readme = read_arxiv_readme(src)

    # arXiv 新格式会在 00README.json 里写主文件
    m = re.search(r'"main(?:_file|file|tex)?"\s*:\s*"([^"]+\.(?:tex|ltx))"', readme)
    if m and (src / m.group(1)).is_file():
        return m.group(1), "00README 指定"

    candidates = []
    for rel in tex_files:
        text = read_text(src / rel)
        has_class = re.search(r"\\documentclass", text) is not None
        has_body = re.search(r"\\begin\s*\{document\}", text) is not None
        if has_class and has_body:
            candidates.append((rel, len(text)))

    if not candidates:
        # 有些项目用 \input 拼装，主文件没有 \documentclass，退一步找体量最大的
        if tex_files:
            biggest = max(tex_files, key=lambda r: (src / r).stat().st_size)
            return biggest, "无 \\documentclass，取最大 tex（需人工确认）"
        die("压缩包内没有 .tex 文件，无法继续。")

    if len(candidates) == 1:
        return candidates[0][0], "唯一含 documentclass 的 tex"

    def score(item):
        rel, size = item
        stem = Path(rel).stem.lower()
        depth = rel.count("/")
        hinted = next(
            (len(MAIN_NAME_HINTS) - i for i, h in enumerate(MAIN_NAME_HINTS)
             if stem == h or stem.startswith(h)),
            0,
        )
        # 排除明显是补充材料的主文件
        supp = -1 if re.search(r"supp|append|si\b|sm\b", stem) else 0
        return (hinted, -depth, size, supp)

    best = max(candidates, key=score)
    others = [c[0] for c in candidates if c[0] != best[0]]
    return best[0], f"多个候选，按命名与体量选中；其余：{', '.join(others[:4])}"


def detect_engine_hint(main_text: str) -> str:
    if re.search(r"\\usepackage(\[[^\]]*\])?\{fontspec\}", main_text):
        return "xelatex"
    if re.search(r"\\usepackage(\[[^\]]*\])?\{(?:ifxetex|iftex)\}", main_text):
        return "xelatex"
    if re.search(r"\\documentclass(\[[^\]]*\])?\{ctex", main_text):
        return "xelatex"
    return "pdflatex"


def main():
    ap = argparse.ArgumentParser(description="取回论文源码并识别主文件")
    ap.add_argument("source", help="arXiv ID / 链接 / 本地目录 / 压缩包")
    ap.add_argument("--out", default=None, help="项目目录，默认 ./<arxiv-id>")
    ap.add_argument("--force", action="store_true", help="重新下载，忽略已有结果")
    args = ap.parse_args()

    arxiv_id, version = normalize_source(args.source)
    out = Path(args.out or re.sub(r"[^\w.-]", "_", arxiv_id)).resolve()
    out.mkdir(parents=True, exist_ok=True)
    arz = meta_dir(out)
    manifest_path = arz / "manifest.json"
    src = out / "src"

    existing = load_json(manifest_path)
    if existing and not args.force and src.is_dir():
        ok(f"复用已有源码：{src}（主文件 {existing.get('main')}）")
        print(json.dumps(existing, ensure_ascii=False, indent=2))
        return

    source_path = Path(args.source)
    if source_path.is_dir():
        if src.exists():
            shutil.rmtree(src)
        shutil.copytree(source_path, src)
        kind = "directory"
        ok(f"复制本地目录 → {src}")
    elif source_path.is_file():
        unpack(source_path, src)
        kind = "archive"
    else:
        archive = arz / "source.tar.gz"
        download_arxiv(arxiv_id, archive, version)
        unpack(archive, src)
        kind = "arxiv"

    files = collect_files(src)
    if not files["tex"]:
        die("源码里没有 .tex 文件。")

    main_rel, why = pick_main(src, files["tex"])
    main_text = read_text(src / main_rel)

    manifest = {
        "source": args.source,
        "arxiv_id": arxiv_id if kind in ("arxiv", "archive") else None,
        "kind": kind,
        "src": "src",
        "main": main_rel,
        "main_reason": why,
        "engine_hint": detect_engine_hint(main_text),
        "tex_files": files["tex"],
        "bib_files": files["bib"],
        "figures": files["figures"],
        "has_bbl": any(p.suffix.lower() == ".bbl" for p in src.rglob("*")),
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }
    save_json(manifest_path, manifest)

    print(f"\n项目目录  {out}")
    print(f"主文件    {main_rel}（{why}）")
    print(f"tex 文件  {len(files['tex'])}   bib {len(files['bib'])}   图 {len(files['figures'])}")
    print(f"编译提示  {manifest['engine_hint']}   已有 bbl: {manifest['has_bbl']}")
    print(f"\n下一步：python preflight.py {out}")


if __name__ == "__main__":
    main()
