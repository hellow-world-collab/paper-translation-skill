#!/usr/bin/env python3
"""批量提交译文：从一份 TSV / JSON 读整批，全批校验通过后才合并进 translations.json。

替代「每批手写一个 Python 字典字面量并运行」的旧做法。模型只需产出纯数据：

    # batch.tsv —— 一行一段，用 TAB 分隔；不要写反斜杠、不要换行、保留 ⟪Pnnnn⟫
    vop_rl.tex#0001	学习在拥挤环境中敏捷导航……⟪P0001⟫
    vop_rl.tex#0002	……

    python submit.py <项目目录> batch.tsv
    python submit.py <项目目录> batch.json      # 也可：{"段 id": "译文", ...}

校验项（与 apply.py 完全同源，这里过了 apply 就一定过）：
  1. 段 id 存在、不重复、整批一次性原子写入（有错一段都不写，避免半批污染）
  2. ⟪Pnnnn⟫ 编号与数量一致、无反斜杠、无未闭合标记 —— 直接复用 apply.check_segment
  3. glossary.tsv 第 4 列的「禁用译法」硬约束（如 sim-to-real 不许写「仿真到真机」）
  4. 中文占比过低的段只提醒、不拦截（公式段与短标签本来就无汉字）

用法：
    python submit.py <项目目录> batch.tsv
    python submit.py <项目目录> batch.tsv --dry-run
    python submit.py <项目目录> batch.json --budget 18000
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import MARKER_RE, cjk_ratio, die, load_json, meta_dir  # noqa: E402
from apply import check_segment  # noqa: E402

SKILL_ROOT = Path(__file__).resolve().parent.parent
GLOSSARY = SKILL_ROOT / "glossary.tsv"
BUDGET_DEFAULT = 18000


def banned_rows() -> list[tuple[str, str, list[str]]]:
    """读 glossary.tsv 第 4 列（可选）：禁用译法，多个用 | 分隔。

    返回 [(原文术语, 规定译名, [禁用写法, ...]), ...]。
    老条目没有第 4 列时自动跳过，向后兼容。
    """
    out: list[tuple[str, str, list[str]]] = []
    if not GLOSSARY.is_file():
        return out
    for line in GLOSSARY.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 4 or not parts[3].strip():
            continue
        bad = [x.strip() for x in parts[3].split("|") if x.strip()]
        if bad:
            out.append((parts[0].strip(), parts[1].strip(), bad))
    return out


def read_batch(path: Path) -> dict[str, str]:
    if not path.is_file():
        die(f"批次文件不存在：{path}")
    raw = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        d = json.loads(raw)
        if not isinstance(d, dict):
            die("JSON 批次文件必须是 {段 id: 译文} 对象")
        return {str(k): str(v) for k, v in d.items()}

    items: dict[str, str] = {}
    for i, line in enumerate(raw.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            die(f"第 {i} 行不是「id<TAB>译文」格式：{line[:80]}")
        sid, tr = parts[0].strip(), parts[1]
        if sid in items:
            die(f"第 {i} 行重复提交 {sid}")
        items[sid] = tr
    return items


def main() -> int:
    ap = argparse.ArgumentParser(description="批量提交并校验一批译文")
    ap.add_argument("project")
    ap.add_argument("batch", help="TSV 或 JSON 批次文件")
    ap.add_argument("--dry-run", action="store_true", help="只校验不写入")
    ap.add_argument("--budget", type=int, default=BUDGET_DEFAULT,
                    help="剩余批数估算用的字符预算，默认 18000")
    args = ap.parse_args()

    root = Path(args.project).resolve()
    arz = meta_dir(root)
    data = load_json(arz / "segments.json")
    if not data:
        die("缺少 segments.json，请先跑 extract.py")
    segs = {s["id"]: s for s in data["segments"]}
    tr = load_json(arz / "translations.json", default={}) or {}
    batch = read_batch(Path(args.batch))
    if not batch:
        die("批次文件是空的")

    banned = banned_rows()
    errors: list[tuple[str, str]] = []
    thin: list[str] = []
    n_banned = 0

    for sid, text in batch.items():
        if sid not in segs:
            errors.append((sid, "段 id 不存在（请先跑 pending.py 取本批 id）"))
            continue
        err = check_segment(segs[sid], text)
        if err:
            errors.append((sid, err))
            continue
        for en, zh, bads in banned:
            for bad in bads:
                if bad in text:
                    errors.append((sid, f"术语硬约束：{en} 规定写作「{zh}」，"
                                        f"译文出现禁用写法「{bad}」"))
                    n_banned += 1
        bare = MARKER_RE.sub("", text)
        src_words = len(re.findall(r"[A-Za-z]{2,}", MARKER_RE.sub("", segs[sid]["masked"])))
        if src_words >= 8 and cjk_ratio(bare) < 0.08:
            thin.append(sid)

    print(f"\n[{root.name}] 提交 {len(batch)} 段（来源 {Path(args.batch).name}）")
    print(f"  校验失败      {len(errors)} 处" if errors else "  校验失败      0 处")
    print(f"  其中术语违规  {n_banned} 处（硬约束表 {len(banned)} 条规则）")
    print(f"  中文过少提醒  {len(thin)} 段" + (f"（如 {thin[0]}）" if thin else ""))

    if errors:
        print("\n  整批未写入，请先修好这些段：")
        for sid, why in errors[:15]:
            print(f"    {sid:<26} {why}")
        if len(errors) > 15:
            print(f"    …… 另有 {len(errors) - 15} 条")
        return 1

    if not args.dry_run:
        tr.update(batch)
        # 原子写：中途被打断也不会留下半个 translations.json
        tmp = arz / "translations.json.tmp"
        tmp.write_text(json.dumps(tr, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(arz / "translations.json")

    # 试运行也按「若写入后」报进度，方便提交前先看还剩几批
    effective = dict(tr)
    effective.update(batch)
    done = sum(1 for s in data["segments"] if s["id"] in effective)
    total = len(data["segments"])
    left_chars = sum(len(s["masked"]) for s in data["segments"]
                     if s["id"] not in effective)
    batches = (left_chars + args.budget - 1) // args.budget if args.budget else 0
    print(f"\n  {'试运行（未写入）' if args.dry_run else '已写入'}；"
          f"累计 {done}/{total}（{done / total:.0%}）")
    print(f"  剩余 {total - done} 段 / {left_chars} 字符，"
          f"按 --budget {args.budget} 约 {batches} 批")
    if total - done:
        print(f"  下一批：python pending.py {root} --budget {args.budget}")
    else:
        print("  全部译完，下一步：python apply.py <项目目录>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
