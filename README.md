# arxiv-zh-tex

把 arXiv 论文翻成中文，并且**尽量不动原排版**——公式、图表、编号、交叉引用、参考文献都留在原位。

一个 WorkBuddy / Claude Skill 形态的工程化流水线：输入是 arXiv ID、`.tex` 目录或一份 PDF，
输出是中文 PDF，外加一个「左原文 / 右译文」的页面对照网页。

## 两条分支

| 输入 | 分支 | 产物 | 质量 |
|---|---|---|---|
| arXiv ID / abs、pdf 链接 / `.tex` 目录 / `.zip` `.tar.gz` | **LaTeX**（主链） | 重新编译的中文 PDF | 公式、编号、交叉引用都是真 LaTeX，上限最高 |
| 只有 `.pdf`：期刊排版、arXiv 没放源码 | **PDF**（pdf2zh） | `<论文>_中文版.pdf` | 版式 1:1 保留，公式是原图 |

`route.py` 负责判断走哪条；`--mode latex|pdf` 可以强制指定。
两条分支最后都能接 `render_compare.py` 出双栏对照页。

> **PDF 分支不是 OCR。** 它读的是 PDF 的文字层（矢量文本 + 位置），逐块翻译后按原 bbox
> 覆盖回去。扫描件（纯图片 PDF）没有文字层，必须先做 OCR——不在本技能范围内。

## 核心原则

**模型永远不生成 TeX 语法。** 源码被切成自然语言片段，公式、命令、引用、数值替换成
`⟪P0001⟫` 这样的标记交给模型；回填时只替换片段内的文字，标记还原成原文。
公式和结构从根本上不可能被改坏。

产出是**纯中文版**（不是中英混排）。图里烧死的英文、代码清单、参考文献条目一律不译。

## 目录结构

```
SKILL.md                 技能主文档（流程、命令、踩坑表）
glossary.tsv             术语表：先到先得的译名锁定
scripts/
  route.py               第零步：判断 LaTeX / PDF 分支
  fetch.py               取源码（e-print）、认主文件 → manifest.json
  preflight.py           基线编译 + xetex 探测
  inject_cjk.py          注入中文字体（带哨兵验证）
  extract.py             切段、抽标记 → segments.json
  apply.py               回填译文 → 新的 .tex
  check_tr.py            译文体检（缺译 / 中文过少 / 成片英文）
  build.py               编译（内部先跑 check_tr）
  audit.py               产物审计（页数、中文覆盖率、图表数量、参考文献）
  render_html.py         段落级重排网页（旧版，可选）
  render_compare.py      页面对照页（左原文图 / 右译文图）
  pdf_mode.py            PDF 分支：建 .venv-pdf、跑 pdf2zh、接对照页
  pending.py             待译片段清单
  _common.py            路径、原子写、JSON 读写
assets/
  compare_template.html  对照页模板
  katex/                 KaTeX（离线渲染公式用）
rules/
  packages.yaml          宏包级 / 源码级坑与自动修法
  papers/                单篇论文的特殊处理记录
```

## 依赖

- **LaTeX 分支**：XeLaTeX（TeX Live / MiKTeX 均可）、`pdftoppm`（审计出图用）。
  中文字体：Windows 用 SimSun/微软雅黑，Linux 用 Fandol 或 Noto CJK。
- **PDF 分支**：Python **3.10–3.12**（`pdf2zh` 的元数据写了 `<3.13`）+ `pymupdf`。
  首次跑 `pdf_mode.py ... --install` 会自动建 `.venv-pdf` 并装好钉死版本：
  `pdf2zh==1.9.11`、`tencentcloud-sdk-python-tmt==3.0.1478`。
- 核心链路（fetch → preflight → inject → apply → build → audit）**只用标准库**，
  第三方依赖只有渲染两个 HTML 时才需要 `pymupdf`。

## 快速开始

```bash
S=~/.workbuddy/skills/arxiv-zh-tex/scripts

# 0) 先分流
python $S/route.py 2508.08241                    # 打印判定结果和下一步命令

# 1) LaTeX 分支
python $S/fetch.py      2508.08241 --out ./2508.08241
python $S/preflight.py  ./2508.08241
python $S/inject_cjk.py ./2508.08241
#      ↓ 翻译（由模型按段完成，写 translations.json）
python $S/apply.py      ./2508.08241
python $S/check_tr.py   ./2508.08241
python $S/build.py      ./2508.08241
python $S/audit.py      ./2508.08241 --png
python $S/render_compare.py --root ./2508.08241 \
    --original .arz/original_arxiv.pdf --translated out/main.pdf \
    --out html/compare --single-file "论文_双栏对照.html"

# 2) PDF 分支（没有源码 / 用户指定）
python $S/pdf_mode.py 论文.pdf --project ./论文 --compare
python $S/pdf_mode.py https://arxiv.org/pdf/2509.09674 --project ./x --compare
python $S/pdf_mode.py 论文.pdf --project ./x --check      # 只体检，不翻译
```

## 对照页

`render_compare.py` 把两份 PDF 逐页渲成图，左右并排；页数不一致是正常的
（中文字距紧、行距小，21 页对 24 页），靠书签锚点让「同一节」对齐而不是「同一页」。

- **联动 / 各自滚动**：拖动任意一栏，另一栏按锚点插值跟位
- **浏览器式缩放**：`Ctrl/⌘ + 滚轮` 以指针为锚点、双击 100%↔220%、双指捏合，40%–400%
- **浏览器式搜索**：`Ctrl/⌘+F`。页面是图片，浏览器自带查找搜不到正文，所以渲染时把
  PDF 文字层的每个词连同横坐标抽进 `META`，前端做子串匹配——所有命中都上色（当前橙色、
  其余淡黄），按词画框而不是整行横带，滚动条旁有命中刻度。**单栏视图只搜当前这一栏**，
  双栏两边都搜，切视图命中自动重算。
- 目录跳转、页码输入、`[`/`]` 翻页、`L` 联动、`F`/`0` 复位、`⇄` 交换左右

## 已验证

| 论文 | 分支 | 结果 |
|---|---|---|
| 2509.09674 *SimpleVLA-RL* | LaTeX | 24 页原文 → 21 页中文，5 图 / 7 表 / 1 代码框全部在位 |
| 2509.09674 | PDF（pdf2zh） | 版式、图、公式原位，正文全中文 |
| 2508.08241 *BeyondMimic* | LaTeX | 双栏 + 大量 tikz，字体与题注本地化通过 |

## 已知限制

- **扫描件**：没有文字层，PDF 分支抽不出块 → 先 OCR。
- **图内文字**：烧进位图的英文不译（要译得重画图）。
- **页数不一致**：正常现象，看的是同一节，不是同一页。
- **PDF 分支字体**：`pdf2zh` 默认去 HF 拉 Noto CJK，国内会失败；脚本自动改指本机中文字体
  并把 `HF_ENDPOINT` 指到镜像。
- **Type1 字体包**：`newtxtext`/`txfonts` 等在 XeLaTeX 下会让中文**静默消失**
  （0 错误、页数正常、一个字都没有）。`inject_cjk.py` 会自动换成 OpenType 等价物。
