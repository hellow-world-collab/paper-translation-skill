#!/usr/bin/env python3
"""辅助工具：列出待翻译片段，供模型分批阅读。

按文件分组输出，每段带 ID、字数与 masked 内容。默认只列未翻译的。

用法：
    python pending.py <项目目录>                  # 列全部待译
    python pending.py <项目目录> --file tex/intro.tex
    python pending.py <项目目录> --limit 20 --offset 0
    python pending.py <项目目录> --id root.tex#0007   # 单段原文与保护内容
    python pending.py <项目目录> --stats
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import die, load_json, meta_dir


def main():
    ap = argparse.ArgumentParser(description="查看待翻译片段")
    ap.add_argument("project")
    ap.add_argument("--file", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--id", default=None, help="查看单段详情")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--all", action="store_true", help="包含已翻译的")
    args = ap.parse_args()

    root = Path(args.project).resolve()
    arz = meta_dir(root)
    data = load_json(arz / "segments.json")
    if not data:
        die("缺少 segments.json，请先跑 extract.py")
    tr = load_json(arz / "translations.json", default={}) or {}

    segs = data["segments"]
    if args.file:
        segs = [s for s in segs if s["file"] == args.file]

    if args.stats:
        by_file: dict[str, list[int]] = {}
        for s in segs:
            done = 1 if s["id"] in tr else 0
            by_file.setdefault(s["file"], [0, 0])
            by_file[s["file"]][0] += done
            by_file[s["file"]][1] += 1
        print(f"{'文件':<30}{'已译':>6}{'总数':>6}{'进度':>8}")
        for f, (done, total) in sorted(by_file.items()):
            print(f"{f:<30}{done:>6}{total:>6}{done / total:>7.0%}")
        all_done = sum(v[0] for v in by_file.values())
        all_total = sum(v[1] for v in by_file.values())
        print(f"{'合计':<30}{all_done:>6}{all_total:>6}{all_done / all_total:>7.0%}")
        return

    if args.id:
        hit = [s for s in data["segments"] if s["id"] == args.id]
        if not hit:
            die(f"没有 {args.id}")
        s = hit[0]
        print(f"ID       {s['id']}")
        print(f"文件     {s['file']}  [{s['start']}:{s['end']}]")
        print(f"原文     {s['source'][:400]}")
        print(f"\nmasked   {s['masked']}")
        print(f"\nprotected ({len(s['protected'])} 个)")
        for i, p in enumerate(s["protected"], 1):
            print(f"  P{i:04d} {p[:160]}")
        if s["id"] in tr:
            print(f"\n现有译文 {tr[s['id']]}")
        return

    pool = segs if args.all else [s for s in segs if s["id"] not in tr]
    if not pool:
        print("全部片段已翻译。")
        return

    total = len(pool)
    start = args.offset
    end = total if not args.limit else min(total, start + args.limit)
    print(f"# 待译 {total} 段（显示 {start + 1}-{end}）\n")

    last_file = None
    for s in pool[start:end]:
        if s["file"] != last_file:
            print(f"\n## {s['file']}")
            last_file = s["file"]
        print(f"\n[{s['id']}] {len(s['masked'])}字")
        print(s["masked"])

    if end < total:
        print(f"\n# 还有 {total - end} 段：--offset {end}")


if __name__ == "__main__":
    main()
