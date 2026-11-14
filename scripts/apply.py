#!/usr/bin/env python3
"""第五步：把译文回填到源码，并校验结构未被破坏。

校验三件事，任一不通过就保留原文并记录问题：
  1. 标记数量与编号必须与抽取时一致（防止模型吞掉公式）
  2. 去掉标记后不得残留反斜杠（防止模型生成 TeX 语法）
  3. 译文非空，且不能是原样返回的英文占位

用法：
    python apply.py <项目目录>
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import (MARKER_RE, die, load_json, meta_dir, ok, read_text,
                     save_json, warn, write_text)


def check_segment(seg: dict, translated: str) -> str | None:
    """返回错误说明，None 表示通过。"""
    if not translated or not translated.strip():
        return "译文为空"

    want = sorted(int(x) for x in MARKER_RE.findall(seg["masked"]))
    got = sorted(int(x) for x in MARKER_RE.findall(translated))

    if want != got:
        missing = set(want) - set(got)
        extra = set(got) - set(want)
        bits = []
        if missing:
            bits.append("丢失 " + ",".join(f"P{i:04d}" for i in sorted(missing)))
        if extra:
            bits.append("多出 " + ",".join(f"P{i:04d}" for i in sorted(extra)))
        if not bits:
            bits.append(f"数量 {len(got)} ≠ {len(want)}")
        return "标记异常：" + "；".join(bits)

    bare = MARKER_RE.sub("", translated)
    if "\\" in bare:
        snippet = bare[max(0, bare.find("\\") - 12): bare.find("\\") + 24]
        return f"译文含反斜杠（模型生成了 TeX）：…{snippet}…"

    if "⟪" in bare or "⟫" in bare:
        return "译文残留未闭合的标记符号"

    return None


# 中文题注一般比英文长 1–2 行。很多老模板在 \caption 之后、\end{figure}
# 之前用一个负 \vskip 把浮动体「收紧」，英文短题注时刚好；译成中文后这段
# 负间距会把紧随的正文拉上来压住题注（1803.10122 首页实测）。
# 只处理 figure/table 浮动体末尾、\end{center} 之后紧接的负 \vskip，
# 把它置零；其它位置的负间距（作者块、公式上下）一律不动。
# 正则只匹配带负号的写法，替换成 0 后天然幂等。
FLOAT_TAIL_VSKIP = re.compile(
    r"(\\end\{(?:center|flushleft|flushright)\}\s*\\vskip\s*)"
    r"-[0-9]*\.?[0-9]+\s*(in|pt|cm|mm|em|ex|bp)"
    r"(?=\s*\\end\{(?:figure|table)\*?\})"
)


def relax_float_tail(text: str) -> tuple[str, int]:
    count = 0

    def repl(m):
        nonlocal count
        count += 1
        return m.group(1) + "0" + m.group(2)

    return FLOAT_TAIL_VSKIP.sub(repl, text), count


def unmask(translated: str, protected: list[str]) -> str:
    def repl(m):
        idx = int(m.group(1)) - 1
        val = protected[idx] if 0 <= idx < len(protected) else m.group(0)
        # \abstract{% 这类写法里 % 原本在行尾、只为吃掉换行；译文回填成一行后
        # 会把后面整段中文连同右花括号一起注释掉，TeX 报
        # "File ended while scanning use of \abstract"（2601.21998 实测）。
        # 保护值以未转义的 % 结尾时补回换行。
        if val.endswith("%") and not val.endswith("\\%"):
            val += "\n"
        # 保护内容若以「字母控制序列」结尾，而译文紧接着一个 ASCII 字母，
        # TeX 会把两者读成同一个未定义控制序列：⟪P⟫ 里是 \midrule，
        # 译文写成 ⟪P⟫DINOv2 → \midruleDINOv2（2506.09985 实测报错）。
        # 只在标记边界补一个空格，不动其它位置。
        nxt = translated[m.end():m.end() + 1]
        if (nxt and re.match(r"[A-Za-z]", nxt)
                and re.search(r"\\[A-Za-z@]+\*?$", val)):
            val += " "
        return val

    # 只裁模型译文自带的首尾空白，不要在回填之后再 .strip()：
    # 保护内容可能是 `\ `（反斜杠+空格，表格里 Avg.\ \\ 的常见写法），
    # 尾部空格被裁掉就退化成裸反斜杠，与后面的 \\ 连成 \\\，
    # 表格行不再结束 → Misplaced \noalign + 连锁 99 个 Missing }
    # （2607.08639 实测）。
    out = MARKER_RE.sub(repl, translated.strip())
    # xeCJK 把中文字符的 catcode 设为 11，因此 \命令 后紧跟中文会被并成
    # 一个未定义的控制序列（实测 \boldmath这 直接报错）。必须在交界处补空格。
    out = re.sub(r"(\\[A-Za-z@]+\*?)(?=[\u4e00-\u9fff])", r"\1 ", out)
    return out


def main():
    ap = argparse.ArgumentParser(description="回填译文")
    ap.add_argument("project")
    ap.add_argument("--dry-run", action="store_true", help="只校验不写回")
    args = ap.parse_args()

    root = Path(args.project).resolve()
    arz = meta_dir(root)
    seg_data = load_json(arz / "segments.json")
    if not seg_data:
        die("缺少 segments.json，请先跑 extract.py")

    tr_path = arz / "translations.json"
    translations = load_json(tr_path, default={})
    if not translations:
        die(f"还没有译文：请把译文写入 {tr_path}\n"
            "格式：{\"root.tex#0001\": \"中文译文（保留 ⟪Pnnnn⟫ 标记）\", ...}")

    segs = seg_data["segments"]
    # zh/ 是「已注入字体、未翻译」的基线，必须保持干净，否则重跑 extract 会
    # 把上一轮的中文当成源码。每次回填都从 zh/ 重建 out/，保证幂等。
    zh = root / "zh"
    out_dir = root / "out"
    if not args.dry_run:
        if out_dir.exists():
            shutil.rmtree(out_dir)
        shutil.copytree(zh, out_dir)
    work = zh if args.dry_run else out_dir

    by_file: dict[str, list[dict]] = {}
    for seg in segs:
        by_file.setdefault(seg["file"], []).append(seg)

    # 偏移量自检：segments.json 记录的是抽取那一刻 zh/ 里的字符位置。如果之后又跑过
    # inject_cjk（注入块变长/变短），整段位置会平移，回填会把中文塞进英文单词中间。
    stale = []
    for rel, items in by_file.items():
        path = work / rel
        if not path.is_file():
            continue
        text = read_text(path)
        for seg in items:
            if text[seg["start"]:seg["end"]] != seg["source"]:
                stale.append(seg["id"])
    if stale:
        die(f"segments.json 与当前 zh/ 对不上：{len(stale)} 段位置偏移（例如 {stale[0]}）。\n"
            "这通常是重新跑过 inject_cjk.py 或改过类文件造成的。\n"
            f"请先重新抽取，再回填：\n"
            f"  python {Path(__file__).resolve().parent / 'extract.py'} {root} --force\n"
            "译文按 id 保存，重新抽取不会丢。")

    applied = skipped = unchanged = relaxed_total = 0
    issues: list[dict] = []

    for rel, items in by_file.items():
        path = work / rel
        if not path.is_file():
            warn(f"{rel} 不存在，跳过")
            continue
        text = read_text(path)
        original = text

        # 从后往前替换，前面的位置不受影响
        for seg in sorted(items, key=lambda s: s["start"], reverse=True):
            translated = translations.get(seg["id"])
            if translated is None:
                skipped += 1
                continue
            if translated.strip() == seg["masked"].strip():
                unchanged += 1
                continue

            err = check_segment(seg, translated)
            if err:
                issues.append({"id": seg["id"], "file": rel, "reason": err,
                               "masked": seg["masked"][:120]})
                skipped += 1
                continue

            replacement = unmask(translated, seg["protected"])
            text = text[: seg["start"]] + replacement + text[seg["end"]:]
            applied += 1

        text, relaxed = relax_float_tail(text)
        relaxed_total += relaxed

        if text != original and not args.dry_run:
            write_text(path, text)

    total = len(segs)
    print(f"\n[{root.name}] 回填结果")
    print(f"  总片段    {total}")
    print(f"  已替换    {applied}")
    print(f"  未提供译文 {skipped - len(issues)}")
    print(f"  校验失败  {len(issues)}")
    if unchanged:
        print(f"  与原样相同 {unchanged}（视为不译）")
    if relaxed_total:
        print(f"  浮动体负间距置零 {relaxed_total} 处（中文题注变长时防止正文压住题注）")

    if issues:
        print("\n  校验未通过（已保留原文，需人工处理）：")
        for it in issues[:12]:
            print(f"    {it['id']:<24} {it['reason']}")
            print(f"      {it['masked'][:100]}")
        if len(issues) > 12:
            print(f"    …… 另有 {len(issues) - 12} 条")

    save_json(arz / "apply.json", {
        "total": total,
        "applied": applied,
        "skipped": skipped - len(issues),
        "failed": len(issues),
        "issues": issues,
    })

    if args.dry_run:
        print("\n（dry-run，未写回文件）")
    elif applied:
        print(f"\n下一步：python build.py {root}")
    else:
        print("\n没有任何片段被替换，请检查 translations.json 的键名是否与片段 ID 一致。")


if __name__ == "__main__":
    main()
