#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""1.0.4 回归用例：把本次修掉的四类问题各钉一条，跑一次全部验一遍。

    python tests/test_regressions.py

只做单元 / 最小工程级验证，不需要 TeX 编译；真实论文链路另见 run_compat.py。
四类问题来自 2603.09460v2 的实战：
  ① ieeeconf.cls 上 \contentsname / \refname 裸 renew 报 2 个错
  ② \section{METHOD} / \section{EXPERIMENTS} 整词大写标题漏抽，且覆盖率检查不报
  ③ 类文件写死的 Fig. / TABLE / Abstract / Appendix / References 没本地化
  ④ 术语表被当硬约束，按语境更贴切的译法反而被拦
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "scripts"))

from check_coverage import scan                     # noqa: E402
from extract import extract_file, looks_like_prose  # noqa: E402
from inject_cjk import CLASS_LOCALIZE               # noqa: E402
from submit import advice_rows                     # noqa: E402

RESULTS: list[bool] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append(bool(passed))
    print(("  PASS  " if passed else "  FAIL  ") + name
          + (f"  · {detail}" if detail else ""))


def one_file(text: str) -> list:
    p = Path(tempfile.mkdtemp(prefix="arz104-")) / "t.tex"
    p.write_text(text, encoding="utf-8")
    return extract_file(p, "t.tex", in_body_only=False)


print("\n① 整词大写章节标题（heading 放行、cell 仍过滤）")
for title in ("METHOD", "EXPERIMENTS", "PPO", "Introduction", "RELATED WORK"):
    segs = one_file(f"\\section{{{title}}}\n")
    check(f"\\section{{{title}}} 被抽取", len(segs) == 1, f"{len(segs)} 段")

cells = one_file("\\begin{tabular}{cc}\nPPO & RVO \\\\\n\\end{tabular}\n")
check("表格 cell 仍过滤全大写缩写",
      not any("PPO" in s.masked or "RVO" in s.masked for s in cells),
      f"{len(cells)} 段")
check("正文里单个全大写词仍跳过", not looks_like_prose("PPO", 1))
check("标题语境放行 METHOD", looks_like_prose("METHOD", 1, True))

print("\n② check_coverage 能报出漏掉的单词标题")
body = ("\\documentclass{article}\n\\begin{document}\n"
        "\\section{METHOD}\n\\section{EXPERIMENTS}\n"
        "Ordinary English prose that should also be extracted as a paragraph.\n"
        "\\end{document}\n")
d = Path(tempfile.mkdtemp(prefix="arz104-proj-"))
(d / "zh").mkdir()
(d / ".arz").mkdir()
(d / "zh" / "x.tex").write_text(body, encoding="utf-8")
(d / ".arz" / "segments.json").write_text('{"segments": []}', encoding="utf-8")
r = scan(d)
texts = [f["text"] for f in r["suspects"]]
check("未抽取时报 METHOD / EXPERIMENTS",
      any("METHOD" in t for t in texts) and any("EXPERIMENTS" in t for t in texts),
      f"{len(r['suspects'])} 处可疑")

segs = [{"id": s.id, "file": s.file, "start": s.start, "end": s.end}
        for s in extract_file(d / "zh" / "x.tex", "x.tex")]
(d / ".arz" / "segments.json").write_text(
    json.dumps({"segments": segs}, ensure_ascii=False), encoding="utf-8")
r2 = scan(d)
check("抽取后覆盖率清零", r2["pass"],
      f"剩 {len(r2['suspects'])} 处：{[f['text'][:36] for f in r2['suspects'][:3]]}")

print("\n③ 类文件写死的英文标签")
cls = r"""
\def\fnum@figure{Fig.~\thefigure}
\def\fnum@table{TABLE~\thetable}
\bfseries\textit{Abstract}---
\@IEEEappendixsavesection*{Appendix \* #1}
\@IEEEappendixsavesection*{Appendix}
\addcontentsline{toc}{section}{Appendix: #1}
\addcontentsline{toc}{section}{Appendix}
\def\thebibliography#1{\section*{References}
\addcontentsline{toc}{section}{References}
"""
new = cls
for pat, rep in CLASS_LOCALIZE:
    new = pat.sub(rep, new)
for word in ("Fig.~", "TABLE~", "Abstract}", "Appendix", "References"):
    check(f"残留已清零：{word}", word not in new)
for word in ("图~\\thefigure", "表~\\thetable", "摘要}---", "附录", "参考文献"):
    check(f"中文标签到位：{word}", word in new)

print("\n④ 注入块兜底与术语表降级")
src = (ROOT / "scripts" / "inject_cjk.py").read_text(encoding="utf-8")
norm = src.replace("\\\\", "\\")  # 源码里是 Python 字符串字面量，反斜杠写成了两个
check("\\providecommand 兜底两行",
      "providecommand{\\contentsname}{目录}\\renewcommand" in norm
      and "providecommand{\\refname}{参考文献}\\renewcommand" in norm)
check("不再有裸 \\renewcommand{\\contentsname}",
      'lines.append("  \\\\renewcommand{\\\\contentsname}{目录}")' not in src)
check("pifont 被条件加载（表格对勾不再是死代码）",
      "IfFileExists{pifont.sty}" in norm)
check("术语建议行可读", len(advice_rows()) >= 1,
      f"{len(advice_rows())} 条")

# 整批提交里出现「建议避开」写法时仍应通过（dry-run 不写盘）
b = Path(tempfile.mkdtemp(prefix="arz104-sub-"))
(b / ".arz").mkdir()
(b / ".arz" / "segments.json").write_text(json.dumps({"segments": [{
    "id": "x.tex#0001", "file": "x.tex", "start": 0, "end": 12,
    "source": "sim-to-real", "masked": "sim-to-real", "protected": [],
}]}, ensure_ascii=False), encoding="utf-8")
(b / ".arz" / "translations.json").write_text("{}", encoding="utf-8")
(b / "b.tsv").write_text("x.tex#0001\t从仿真到真机的迁移\n", encoding="utf-8")
p = subprocess.run([sys.executable, str(ROOT / "scripts" / "submit.py"),
                    str(b), str(b / "b.tsv"), "--dry-run"],
                   capture_output=True, text=True, encoding="utf-8")
out = (p.stdout or "") + (p.stderr or "")
check("术语偏离不拦截（dry-run 返回 0）", p.returncode == 0,
      out.strip().splitlines()[-1] if out.strip() else "")
check("输出标明仅提示不拦截", "仅提示，不拦截" in out)

print("\n⑤ 2611.11605 实战：覆盖率检查的三类误报")
fake = (
    "\\documentclass{article}\n\\begin{document}\n"
    "Intro paragraph with several English words for the extractor to cover.\n"
    "\\author{%\n"
    "\\IEEEauthorblockN{Yunyi Gao, Yixing Shi, and Sameer Alam}\n"
    "\\IEEEauthorblockA{School of Artificial Intelligence and Robotics, "
    "Xiamen University Malaysia}\n"
    "}\n"
    "% \\section{Limitations and Future Work}\n"
    "\\begin{tabular}{lcc ccccc cccc}\n\\toprule\n\\end{tabular}\n"
    "\\section{METHOD}\n\\end{document}\n")
d5 = Path(tempfile.mkdtemp(prefix="arz105-proj-"))
(d5 / "zh").mkdir()
(d5 / ".arz").mkdir()
(d5 / "zh" / "x.tex").write_text(fake, encoding="utf-8")
a5 = fake.index("Intro paragraph")
b5 = fake.index("\n", a5)
(d5 / ".arz" / "segments.json").write_text(json.dumps({"segments": [{
    "id": "x.tex#0001", "file": "x.tex", "start": a5, "end": b5,
    "source": fake[a5:b5], "masked": fake[a5:b5], "protected": [],
}]}, ensure_ascii=False), encoding="utf-8")
t5 = " | ".join(f["text"] for f in scan(d5)["suspects"])
check("注释掉的 section 不再误报", "Limitations" not in t5, t5[:90])
check("author 人名与机构不再误报",
      "Yunyi Gao" not in t5 and "Xiamen University" not in t5, t5[:90])
check("tabular 列格式不再误报", "lcc ccccc" not in t5, t5[:90])
check("真标题 METHOD 仍能报出", "METHOD" in t5, t5[:90])

print("\n" + ("全部通过" if all(RESULTS) else f"{RESULTS.count(False)} 项失败"),
      f"({sum(RESULTS)}/{len(RESULTS)})")
sys.exit(0 if all(RESULTS) else 1)
