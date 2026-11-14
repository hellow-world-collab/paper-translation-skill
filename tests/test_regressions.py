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
from inject_cjk import CLASS_LOCALIZE, resolve_inject_target  # noqa: E402
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
check("unicode-math 以 mathbf=sym 加载（修 \\mathbf{\\Omega} 空白）",
      "usepackage[mathbf=sym]{unicode-math}" in norm)
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

print("\n⑥ 2610.31617 实战：纯 \\input 兼容壳与 hyperref 元数据")
d6 = Path(tempfile.mkdtemp(prefix="arz106-proj-"))
(d6 / "src").mkdir()
(d6 / "src" / "main_highlighted.tex").write_text(
    "% !TeX root = main_highlighted.tex\n\\input{main.tex}\n", encoding="utf-8")
(d6 / "src" / "main.tex").write_text(
    "\\documentclass{IEEEtran}\n\\begin{document}\nHello world.\n\\end{document}\n",
    encoding="utf-8")
target, note = resolve_inject_target(d6 / "src", "main_highlighted.tex")
check("纯 \\input 壳解析出真正主文件", target == "main.tex", str(target))
check("普通主文件仍返回自身、无提示",
      resolve_inject_target(d6 / "src", "main.tex") == ("main.tex", None))
check("解析结果说明编译入口不变", bool(note) and "编译入口不变" in note, str(note))

fake6 = (
    "\\documentclass{article}\n"
    "\\hypersetup{pdfauthor={}, pdftitle={DiffDreamer: Coupling World Models "
    "with Differentiable Simulation for Agile Quadrotor Control}}\n"
    "\\begin{document}\n"
    "Intro paragraph with several English words for the extractor to cover.\n"
    "This sentence is definitely untranslated prose left behind on purpose.\n"
    "\\end{document}\n")
d6b = Path(tempfile.mkdtemp(prefix="arz106-cov-"))
(d6b / "zh").mkdir()
(d6b / ".arz").mkdir()
(d6b / "zh" / "x.tex").write_text(fake6, encoding="utf-8")
a6 = fake6.index("Intro paragraph")
b6 = fake6.index("\n", a6)
(d6b / ".arz" / "segments.json").write_text(json.dumps({"segments": [{
    "id": "x.tex#0001", "file": "x.tex", "start": a6, "end": b6,
    "source": fake6[a6:b6], "masked": fake6[a6:b6], "protected": [],
}]}, ensure_ascii=False), encoding="utf-8")
t6 = " | ".join(f["text"] for f in scan(d6b)["suspects"])
check("pdftitle 元数据不再误报", "Coupling World Models" not in t6, t6[:110])
check("真正未覆盖的正文仍能报出", "definitely untranslated" in t6, t6[:140])

# ── 1803.10122《World Models》实战新增 ────────────────────────────────
from apply import relax_float_tail                       # noqa: E402
from check_coverage import NONPROSE_CMDS                # noqa: E402

check("ICML 机构名 \\icmlaffiliation 进非正文命令表",
      "icmlaffiliation" in NONPROSE_CMDS)
icml_sty = (r"\centerline{\large\bf Abstract}" + "\n"
            + r"\def\fnum@figure{Figure \thefigure}")
loc = icml_sty
for pat, rep in CLASS_LOCALIZE:
    loc = pat.sub(rep, loc)
check("ICML 硬编码 Abstract 被本地化", "摘要" in loc and "Abstract" not in loc, loc[:90])
check("Figure \\thefigure 带空格也能本地化", "图~" in loc, loc[:90])
float_src = r"\end{center}" + "\n" + r"\vskip -0.45in" + "\n" + r"\end{figure}"
float_out, hit = relax_float_tail(float_src)
check("浮动体尾部负 vskip 被置零",
      hit == 1 and "0in" in float_out and "-0.45in" not in float_out, float_out[:90])
check("负 vskip 处理幂等且不误伤其它位置",
      relax_float_tail(float_out)[1] == 0
      and relax_float_tail(r"\vskip -0.45in" + "\ntext")[1] == 0)

# ── 9 篇「世界模型 → WAM」批量实战新增修复 ─────────────────────────────
from apply import unmask                                 # noqa: E402
from build import normalize_xetex                       # noqa: E402
from check_tr import looks_like_name_list               # noqa: E402
from extract import DECLARATION_COMMANDS                # noqa: E402
from inject_cjk import XETEX_INCOMPATIBLE               # noqa: E402

print("\n⑦ 2301.08243 / 2601.21998：XeLaTeX 不兼容宏包与数学宏")
check("axessibility / accsupp 进摘除清单",
      XETEX_INCOMPATIBLE == {"axessibility", "accsupp"})
inj = src.replace("\\\\", "\\")
check("主文件 + input 树 + zh 三处都用了同一摘除集合",
      inj.count("set(removed) | XETEX_INCOMPATIBLE") == 2
      and inj.count("set(removed_zh) | XETEX_INCOMPATIBLE") == 1)
check("\\boldsymbol 在 unicode-math 下改走 \\symbf",
      "let\\boldsymbol\\symbf" in inj)

print("\n⑧ 2501.03575 / 2602.15922：NVIDIA 模板与类文件标签")
check("termes 配方显式加载 fontspec 三件套",
      "usepackage{fontspec}" in inj
      and "setmainfont{TeX Gyre Termes}" in inj
      and "setmonofont{TeX Gyre Cursor}" in inj)
check("\\setmainfont 与 \\setmathfont 前都清空 defaultfontfeatures",
      inj.count("\\defaultfontfeatures{}") >= 2)
nv_cls = ("{\\large\\bfseries\\headingfont Abstract}\n"
          "\\DisableLigatures{encoding = *, family = *}\n")
nv_loc = nv_cls
for pat, rep in CLASS_LOCALIZE:
    nv_loc = pat.sub(rep, nv_loc)
check("NVIDIA 硬编码 Abstract 本地化为「摘要」",
      "摘要" in nv_loc and "Abstract" not in nv_loc, nv_loc[:90])
check("microtype \\DisableLigatures 整行注释掉",
      any(l.lstrip().startswith("%") and "DisableLigatures" in l
          for l in nv_loc.splitlines()))

print("\n⑨ 2506.09985 / 2602.15922：抽取与回填的边界情况")
check("字号／字形声明命令进 DECLARATION_COMMANDS",
      all(c in DECLARATION_COMMANDS
          for c in ("scriptsize", "footnotesize", "bfseries", "centering")))
d_segs = one_file(
    r"\centering\scriptsize{The left arm picks up the red cube and places it on the plate.}" + "\n")
check("\\centering\\scriptsize{成句任务指令} 被抽成正文",
      len(d_segs) == 1 and "The left arm picks up" in d_segs[0].masked,
      d_segs[0].masked[:80] if d_segs else "0 段")
p_segs = one_file(
    r"\prompt{The video captures a robot arm picking up the cup and moving it to the shelf.}" + "\n")
check("未知自定义命令的成句参数被递归抽取",
      len(p_segs) == 1 and "The video captures" in p_segs[0].masked
      and "\\prompt" not in p_segs[0].masked,
      p_segs[0].masked[:80] if p_segs else "0 段")
check("\\abstract{% 结尾补换行，不吞中文",
      unmask("⟪P0001⟫这是摘要正文", [r"\abstract{%"]).startswith("\\abstract{%\n这是摘要"))
check("\\midrule 与 DINOv2 之间补空格",
      unmask("⟪P0001⟫DINOv2 特征", [r"\midrule"]).startswith("\\midrule DINOv2"))
check("回填结果不整体 strip，保护值 `\\ ` 不被吃掉",
      unmask("⟪P0001⟫测试", [r"\ "]) == "\\ 测试")

print("\n⑩ 2501.03575 / 2602.15922：编译前 XeLaTeX 归一化")
dn = Path(tempfile.mkdtemp(prefix="arz109-"))
doc = ("\\documentclass{article}\n\\pdfoutput=1\n"
       "\\usepackage{microtype}\n\\begin{document}\n正文\n\\end{document}\n")
# 故意同时造 LF 与 CRLF：_common.read_text 不做通用换行转换，Windows 下
# 若正则不收 \r，CRLF 那份会静默漏改 \pdfoutput=1。
(dn / "lf.tex").write_bytes(doc.encode("utf-8"))
(dn / "crlf.tex").write_bytes(doc.replace("\n", "\r\n").encode("utf-8"))
touched = normalize_xetex(dn)
check("LF / CRLF 两份都被改写", sorted(touched) == ["crlf.tex", "lf.tex"], str(touched))
after_lf = (dn / "lf.tex").read_bytes().decode("utf-8")
after_crlf = (dn / "crlf.tex").read_bytes().decode("utf-8")
check("两种换行下 \\pdfoutput=1 都被注释",
      "\n% arxiv-zh-tex: \\pdfoutput" in after_lf
      and "% arxiv-zh-tex: \\pdfoutput" in after_crlf,
      after_crlf.splitlines()[1][:70])
check("microtype 守卫插在 \\begin{document} 之前",
      after_lf.index("protrusion=false,expansion=false") < after_lf.index("\\begin{document}"))
check("归一化幂等（第二遍不再改文件）", normalize_xetex(dn) == [])

print("\n⑪ 2506.09985 / 2607.08639：贡献者名单不再误报漏译")
names_src = ("Alice Smith, Bob Jones, Carol Lee, David Wu, Eva Chen, Frank Lin, "
             "and the PerceptionLM team")
check("人名名单豁免", looks_like_name_list(names_src, "Alice Smith 等人与 PerceptionLM 团队"))
check("普通英文正文不豁免",
      not looks_like_name_list(
          "The model is trained with several settings, then evaluated, "
          "and finally compared, and reported here.", "模型在多种设置下训练并评估比较。"))
check("名单段译文无汉字时不豁免",
      not looks_like_name_list(names_src, "Alice Smith, Bob Jones et al."))

print("\n" + ("全部通过" if all(RESULTS) else f"{RESULTS.count(False)} 项失败"),
      f"({sum(RESULTS)}/{len(RESULTS)})")
sys.exit(0 if all(RESULTS) else 1)
