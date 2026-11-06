---
name: arxiv-zh-tex
display_name: arXiv 论文中译
display_name_en: arXiv Paper Chinese Translator
description: 把论文翻成中文并尽量保持原排版。有 LaTeX 源码（arXiv 链接/ID、.tex 目录、压缩包）时走源码重排，公式、图表、引用、参考文献原位不动；只有 PDF（期刊版、扫描件、arXiv 未给源码）时自动切到 PDF 分支（pdf2zh），逐块覆盖文字层、保留版式与公式。两种分支都能再产出「左原文 / 右译文」的双栏对照 HTML。
description_zh: 把 arXiv 论文翻成中文且尽量不动原排版：有 LaTeX 源码走源码重排，公式、图表、引用原位保留；只有 PDF 走版式保留翻译；另出可搜索的双栏对照页。需本机装 XeLaTeX。
description_en: Translate arXiv papers into Chinese while keeping the original layout, via LaTeX source rebuild or layout-preserving PDF translation, plus a searchable side-by-side reader. Requires a local XeLaTeX install.
examples_zh:
  - "把 arXiv 2509.09674 翻成中文，公式和排版别动"
  - "这篇 IEEE 双栏论文只有 PDF，帮我出个中文版"
  - "翻完给我一个左原文右译文、能搜索的双栏对照页"
examples_en:
  - "Translate arXiv 2509.09674 into Chinese without breaking the layout"
  - "This IEEE paper only comes as a PDF — give me a Chinese version"
  - "Then produce a side-by-side reader with working search"
category: productivity
version: 1.0.3
author: hellow-world-collab
visibility: "public"
license: MIT
agent_created: true
---

# 论文中译（保持原排版）

## 第 -1 步：环境自检（缺什么装什么，别让用户自己去折腾）

动手前先跑一次：

```bash
python ~/.workbuddy/skills/arxiv-zh-tex/scripts/env_check.py    # 加 --json 给脚本读
```

它逐个打勾 `xelatex` / `pdftoppm` / `pymupdf` / Python 版本，缺的连安装命令一起打印。
**不要跳过这一步直接开编译**：Windows 上 MiKTeX 通常不在 PATH，`env_check.py` 会去常见安装位置找，
比手敲 `xelatex --version` 可靠；退出码 1 表示缺 xelatex。

缺 XeLaTeX 时按系统装（其余依赖的安装命令脚本会自己给出）：

| 系统 | 命令 | 备注 |
|---|---|---|
| Windows | `winget install --id MiKTeX.MiKTeX -e`（没 winget 就 `choco install miktex`） | 装完把 `%LOCALAPPDATA%\Programs\MiKTeX\miktex\bin\x64` 补进 PATH；首次编译自动拉宏包 |
| macOS | `brew install --cask basictex` | 再 `sudo tlmgr install xetex ctex`；完整版 `mactex-no-gui` 约 5 GB，先问用户要不要 |
| Debian / Ubuntu | `sudo apt install texlive-xetex texlive-lang-chinese texlive-latex-extra latexmk` | 中文字体另装 `fonts-noto-cjk` 或 `fonts-fandol` |
| Fedora | `sudo dnf install texlive-xetex texlive-collection-langchinese` | |
| Arch | `sudo pacman -S texlive-core texlive-langchinese` | |

`pdftoppm` 来自 poppler：Windows `winget install --id oschwartz10612.Poppler -e`，macOS `brew install poppler`，
Debian/Ubuntu `sudo apt install poppler-utils`。装完命令仍找不到，重开一个 shell 或把 bin 目录加进 PATH。

Python 侧：渲染 HTML 要 `python -m pip install pymupdf`；PDF 分支**不要用系统的 3.13**——
`python scripts/pdf_mode.py <项目> --install` 会用 3.10–3.12 自建 `.venv-pdf`（几百 MB，只建一次）。

装完重跑 `env_check.py`，出现 ✓ 再进第零步。确实是内网、没有包管理器这种情况，
给用户官网链接和一句手动步骤即可，不要卡在这里反复重试。

## 第零步：先分流——有源码走源码，没源码走 PDF

> **技能目录**：下文统一用 `~/.workbuddy/skills/arxiv-zh-tex`，这是本地 `git clone` 的默认位置。
> 从技能市场安装时目录名可能不同（例如带 skill id），先 `ls ~/.workbuddy/skills` 确认实际目录，
> 把它替换掉下文路径即可。不要因为路径不存在就判定技能没装好。

```bash
S=~/.workbuddy/skills/arxiv-zh-tex/scripts
python $S/route.py <arXiv ID / 链接 / PDF / .tex 目录 / 压缩包>   # 只判断，打印下一步命令
```

| 输入 | 分支 | 产物 | 说明 |
|---|---|---|---|
| arXiv ID、abs/pdf 链接、`.tex` 目录、`.zip/.tar.gz` | **LaTeX**（主链） | 重新编译的中文 PDF | 质量上限最高：公式、编号、交叉引用都是真 LaTeX |
| 只有 `.pdf`：期刊排版、扫描件、arXiv 没放源码 | **PDF** | `<论文>_中文版.pdf` + `_双语版.pdf` | 版式 1:1 保留，公式是原图；译文质量取决于翻译后端 |

`route.py --mode latex|pdf` 可以强制指定；`--mode auto`（默认）按上表判断。
两条分支的最后都能接 `render_compare.py` 出双栏对照页，阅读体验一致。

**自动退路**：arXiv 有条目但作者只交了 PDF（`e-print` 返回 404 / "no source available"）时，
`fetch.py` 会失败并直接把 `pdf_mode.py` 的完整命令打出来——不要停在那里问用户，
按提示转到 PDF 分支继续。用户显式说「用 PDF 分支」时，即使有源码也照办（`--mode pdf`）。

## 核心原则（LaTeX 分支）

**模型永远不生成 TeX 语法。** 源码被切成若干自然语言片段，公式、命令、引用、数值被替换成 `⟪P0001⟫` 这样的标记交给模型；回填时只替换片段内的文字，标记还原成原文。这样公式和结构从根本上不可能被改坏。

产出是**纯中文版**（不是中英对照）。图表内不可编辑的英文一律不译。

### 抽取覆盖哪些写法

| 位置 | 译？ | 说明 |
|---|---|---|
| 正文、章节标题 | ✅ | `\section{Introduction}` 这种单词标题也收 |
| 图表题注、子图题注 | ✅ | 单词题注（`Simulation.`）也收 |
| 表格单元格 | ✅ | 按单元格切开，`Ours` / `Walk` 也收 |
| `\item[术语]` | ✅ | description 列表的条目名，之前整条丢 |
| beamer 帧标题 / block 标题 | ✅ | `\begin{frame}{标题}` |
| 算法标题 | ✅ | 只挖 `\caption`，伪代码一个字不动 |
| 公式、图内文字、伪代码、`\verb` | ❌ | 整体保护 |
| 参考文献条目 | ❌ | `.bbl` / `thebibliography` 不动 |

判定看「语境 + 词数」：正文要至少两个英文词；题注 / 单元格 / 术语只要一个
（首字母大写或全小写、≥3 个字母），全大写缩写（PPO / GPU）仍然跳过。
语境随命令参数递归下传、随环境切换（`tabular` → `cell`），实现在 `extract.Token.ctx`。

## LaTeX 分支（有源码）

```bash
python $S/fetch.py 2508.08241 --out ./2508.08241       # ① 取源码、认主文件
python $S/preflight.py ./2508.08241                    # ② 基线编译 + xetex 探测
#   已知环境 xelatex 可用时可加 --skip-xetex，省一轮 3 遍的探测编译
python $S/inject_cjk.py ./2508.08241                   # ③ 注入中文字体（实证验证）
python $S/extract.py ./2508.08241                      # ③b 抽取可译片段
python $S/check_coverage.py ./2508.08241               # ③c 翻译前查未抽取正文（必须清零）
#            ↓ ④ 翻译（见下节，按字符预算分批 + submit.py 提交）
python $S/apply.py ./2508.08241                        # ⑤ 回填
python $S/check_tr.py ./2508.08241                     # ⑤b 译文体检（缺译/未抽取就停）
python $S/build.py ./2508.08241                        # ⑥ 编译（交叉引用收敛即停）
python $S/audit.py ./2508.08241 --png                  # ⑦ 审计
python $S/render_compare.py --root ./2508.08241 \      # ⑧ 双栏对照页（可选）
    --original .arz/original_arxiv.pdf --translated out/main.pdf \
    --out html/compare
# 只有需要「一个文件发给别人」时才加：--single-file "论文_双栏对照.html"
```

目录约定：

| 路径 | 含义 |
|---|---|
| `src/` | 原始源码，只读 |
| `zh/` | 已注入字体、**未翻译**的基线，必须保持干净 |
| `out/` | `zh/` + 译文，每次 `apply` 重建，可随时丢弃 |
| `.arz/` | manifest / preflight / segments / translations / build / audit |

`zh/` 一定不能写译文。`extract` 从 `zh/` 读，一旦污染，下一轮会把中文当原文再抽一遍。

## 翻译环节

**按字符量分批，不按段数。** 实测 2607.15036 里「25 段」可能是 710 字，也可能是
18,209 字，差 25 倍；按段数切会多出一倍轮次，还容易撞上下文上限。
`--budget 18000` 把 295 段切成 **5 批**（旧法 12 批）。

```bash
python $S/pending.py ./2508.08241 --stats --budget 18000   # 总进度与预计批数
python $S/pending.py ./2508.08241 --budget 18000           # 读一批（约 1.8 万字符）
python $S/pending.py ./2508.08241 --id tex/intro.tex#0007  # 单段详情含 protected
```

读完一批，把译文写成**纯数据 TSV**（一行一段：`id<TAB>译文`；不要换行、不要反斜杠、
`⟪Pnnnn⟫` 原样保留），然后一次提交：

```bash
# batch.tsv
# vop_rl.tex#0001	学习在拥挤环境中敏捷导航……⟪P0001⟫
# vop_rl.tex#0002	……
python $S/submit.py ./2508.08241 batch.tsv       # 也可用 .json：{"段 id": "译文"}
python $S/submit.py ./2508.08241 batch.tsv --dry-run
```

`submit.py` 复用 `apply.check_segment` 的校验规则，并检查 `glossary.tsv` 第 4 列的
「禁用译法」硬约束（如 `sim-to-real` 不许写「仿真到真机」）；**整批校验通过才原子写入**，
有一段坏就一段都不写，不会留下半批污染。输出里直接给出剩余段数、字符数与预计批数。

每批都从队头取：**不要用 `--offset`**（下标是待译队列，译完一批会跳段）；
`--limit/--offset` 只作为旧法保留。中途断了直接重跑 `pending.py --budget 18000`
就会从没译的第一段继续。

### 译文书写规则

1. **所有 `⟪Pnnnn⟫` 必须保留**，数量与编号不能变。位置可以按中文语序调整——`⟪P0002⟫` 是 `\cite{...}` 时，中文里放到句末读起来更顺，这是允许的。
2. **不许出现反斜杠**。译文里写 `\%`、`\textbf` 会被回填校验拒绝，整段退回原文。百分号直接写 `%`，加重语气靠句式而不靠命令。
3. **数字、单位、专有名词照抄**：`$27.38\%$` 这类整体在标记里，不要自己补；`Unitree G1`、`IsaacSim`、`PPO` 不译。
4. **术语查 `glossary.tsv`**（139 条，含"保留英文"标记）。表里没有的按同风格现定，顺手记进 `glossary.suggested.tsv`。
5. **图注要译**（caption 是正文）；图内标注不译（抽取时已跳过 `tikzpicture`/`pgfplots` 等环境）。
6. **不要扩写**。原文一句就是一句，不要补背景、不要合并相邻段——片段之间可能有结构符号，合并会让句子错位。

### 长文分批

`--budget 18000` 是实测值：2607.15036 单批 1.8 万字符能一次译完且不丢标记，
295 段共 5 批，每批字符量稳定在 1.3–1.8 万。超过 2000 字符的长段会独占一批，
脚本不会把段切开。若某篇公式标记特别密集，把预算降到 12000；不要退回「固定 25 段」。

## PDF 分支（只有 PDF）

期刊 PDF、扫描件、arXiv 没给源码的论文走这条。底层是 PDFMathTranslate（`pdf2zh`）：
解析文本块 → 逐块翻译 → 用译文覆盖原文字层，图片与公式区域保持原坐标。
仓库自带 `scripts/pdf_translate.py` 直接调它，**不依赖 pdf-formula-translate 技能**；
那个技能只在本文件的翻译器缺失时才作为退路。

```bash
python $S/pdf_mode.py 论文.pdf --project ./论文 --compare          # 最常用
python $S/pdf_mode.py https://arxiv.org/pdf/2509.09674 --project ./x --compare
python $S/pdf_mode.py 论文.pdf --project ./x --check               # 只体检，不翻译
python $S/pdf_mode.py 论文.pdf --project ./x --install             # 首次：建 .venv-pdf 并装 pdf2zh
```

产物：`<论文>_原文.pdf`、`<论文>_中文版.pdf`、`<论文>_双语版.pdf`、`html/compare/`（若加 `--compare`）。

必须知道的五件事：

1. **Python 只能用 3.10–3.12**。`pdf2zh` 的元数据是 `requires-python <3.13`，本机默认 3.13 会报
   `No matching distribution found`。脚本会自动找 `.venv-pdf` 或 `py -0p` 里 3.10–3.12 的解释器。
2. **版本钉死两件套**：`pdf2zh==1.9.11` + `tencentcloud-sdk-python-tmt==3.0.1478`。
   PyPI 上没有 1.9.12；而 tmt 的 3.1.x 只剩 10 KB 的空壳（没有 `TextTranslateRequest`），
   装上会让 `import pdf2zh` 直接 ImportError，`--install` 已自动带这两个版本。
3. **首次运行要联网**：ONNX 版式模型（约 40 MB）+ 中文字体，再调翻译后端。
   脚本默认 `HF_ENDPOINT=https://hf-mirror.com`、默认后端 `bing`（google 在部分网络下 502，
   失败会自动再试 bing）；付费后端（deepl/openai/…）要自备 key，脚本不会偷偷扣费。
4. **字体别再走 HF**：`NOTO_FONT_PATH` 指向本机中文字体（脚本自动挑 `msyh.ttc`/`simhei.ttf`）；
   不设的话 pdf2zh 在 `download_remote_fonts` 处抛 `cannot unpack non-iterable NoneType object`。
5. **扫描件翻不了**：没有文字层的 PDF 一个文本块都抽不出来，`audit.py` 会报"多页几乎没有中文"。
   这种要先 OCR，不在本技能范围内。

### 这条链上踩过的坑（都已修）

| 现象 | 根因 | 修法 |
|---|---|---|
| `AttributeError: 'NoneType' object has no attribute 'predict'` | pdf2zh 1.9.11 的 `high_level.translate()` **自己不加载版式模型**（CLI 里是外面加载好传进去的） | `translate_pdf.py` 显式 `model=OnnxModel.load_available()` |
| 输入 PDF 翻译完就消失 | `translate()` 会把「位于系统临时目录的输入」读完就 `unlink`（"Cleaned temp file"） | `pdf_mode.py` 只喂项目目录里的 `*_原文.pdf`，绝不传 `/tmp` 下的路径 |
| `pip install` 报 502 / No matching distribution | 本机代理对 PyPI 不通；Python 3.13 装不上 pdf2zh | 先默认源、失败自动换清华镜像；解释器只认 3.10–3.12 |

审计会自动分流：项目里存在 `.arz/pdf_mode.json` 且没有 `segments.json` 时，`audit.py` 只做
「每页中文覆盖率」检查，不查标记。

## 附加产出：中英对照 HTML（旧版，默认不用）

> 这是最早的**段落级重排**版本，把 PDF 拆段后重新排版，不是「原页面对照」。
> 实际交付请用下面的 `render_compare.py`；这一版保留给「想要可重排网页」的场景。

PDF 保排版，HTML 保可读：原文/译文逐段对齐、公式用 KaTeX 渲染、插图从源码里的
PDF 矢量图高清重绘、表格从编译结果裁切、引用可点开跳到文末条目。

```bash
python $S/render_html.py ./2509.09674                 # → html/index.html（+ html/assets/）
python $S/render_html.py ./2509.09674 --standalone    # 另出单文件版，图片与 KaTeX 全内联
```

依赖 `pymupdf`（`pip install pymupdf`）；KaTeX 随 Skill 打包，离线可用（仓库里在 `assets/katex/`，
市场包因为只收两级目录，摊平成 `assets/katex.min.css` 等文件，脚本两种布局都认）。

页面能力：目录侧栏（滚动高亮）、阅读进度条、对照/仅译文/仅原文三档（快捷键 `1`/`2`/`3`）、
字号调节、深浅色、可点引用、打印样式。三种模式的宽度都做过限制，长公式不会撑破栏宽。

数据只取自 `.arz/segments.json`、`translations.json` 和 `.aux`/`.bbl`——**翻译阶段不需要额外工作**，
已经有了译文就能出页面。

## 附加产出：双栏页面对照 HTML（左原文 / 右译文）

上面那个是**段落级重排**；如果用户要的是「左边原版 PDF 的样子、右边中文 PDF 的样子」，
用 `render_compare.py`：不动任何排版，两份 PDF 原样逐页并排（页面由浏览器里的 pdf.js
现渲，不是预烘的图片；详见下面「渲染架构」）。

```bash
python $S/render_compare.py --root ./2509.09674 \
    --original .arz/original_arxiv.pdf \
    --translated out/main.pdf \
    --out html/compare
# 只有需要「一个文件发给别人」时才加：--single-file "论文_双栏对照.html"
```

产物：`html/compare/index.html` + `pdf-en.js` / `pdf-zh.js`（base64 后的两份 PDF，
按需 `<script>` 载入）。目录版双击 `index.html` 就能开（`file://` 下用 Blob worker，
已验证），**三档视图（双栏 / 原文 / 译文）、搜索、划选复制、任意缩放全都在**。

单文件版只是把这三份内容全内联进一个 HTML，功能完全相同，唯一好处是不用带目录、
可直接发微信/邮件。代价是体积约等于两份 PDF 之和 × 4/3：2509.00215 的 PDF 各约
5 MB → 13.7 MB；2607.15036 的 PDF 各 16.7 MB → **47.4 MB**。所以**默认不生成**，
每篇省约 1 分钟生成时间和一次浏览器验收。

**渲染架构（v1.0.2 起）**：页面不是预烘的图片，而是把 PDF 交给浏览器里的 pdf.js 现渲。
canvas 的位图尺寸按「CSS 宽度 × devicePixelRatio」实时算，缩放一次重绘一次，
所以任何比例都是 1:1 物理像素——和 Chrome 内置 PDF 阅读器同一条路线。
- 默认只渲视口附近的页（IntersectionObserver + 800px 余量），离远了把 canvas 位图释放，
  43 页也不会把内存吃穿；单页位图上限 12 Mpx，4× 缩放 + 高分屏时按需降采样。
- 缩放防抖 130ms，重绘期间先沿用旧位图（浏览器自己插值），落定后立刻变锐。
- **文字可划选、可复制**：canvas 上只有像素，所以每页渲完再叠一层透明 textLayer
  （pdf.js `getTextContent` 的每个 item 一个 span）。span 的 top 用 `baseline − ascent×字号`
  换算，横向按 PDF 记录的字宽做 `scaleX` 校正，选区和字形重合；只给 span 开 `pointer-events`，
  在空白处双击放大 / 拖动平移不受影响，按在字上则是划选（双击选词）。
  离屏页的文字层随 canvas 一起释放。
- `--scale / --quality / --format` 已废弃并被忽略（旧脚本传了也不会报错）。
- 引擎坏掉时页面会弹一条可见的告警（而不是白屏），提示换浏览器或直接看 PDF。
- 取舍：pdf.js 引擎（320 KB）与 worker（1.09 MB）随包内联，`file://` 双击可用；
  worker 走 Blob URL，因为 file:// 下 `new Worker('pdf.worker.min.js')` 会被判跨源。
  代价是 index.html 从 400 KB 涨到 2～3 MB（随 META 大小浮动），但换来任意缩放不糊、正文可复制。

要点：

- **原文用真 PDF**：`fetch.py` 只下源码，记得另存 `https://arxiv.org/pdf/<id>` 到 `.arz/original_arxiv.pdf`；
  找不到就退回 `base/main.pdf`（自己编译的基线，排版略异）。
- **锚点来自书签**：两份 PDF 的 `get_toc()` 条目顺序一致（同一份源码），按序号配对得到
  「原文第 p 页 ↔ 译文第 q 页」，联动滚动按锚点分段线性插值，而不是按比例——页数 24 vs 21
  也能对齐到同一节。两句书签数量对不上时自动退回等比例映射。
- 交互：联动/各自滚动、目录跳转（两栏一起跳）、双栏/原文/译文、缩放、交换左右、页码输入框、
  `[`/`]` 翻页、`L` 联动、`F`/`0` 复位。
- **全文搜索**：`Ctrl/⌘+F`（或顶栏「搜索」）。页面是 canvas，浏览器自带的查找只能搜到
  已渲染页的文字层，所以渲染时把 PDF 的文字层逐**词**抽进 `META`
  （`[y0, y1, [[x0, x1, 词, [字x0…]], ...]]`，按页分组；45 页约 1.76 万个词，
  META 约 120 万字符——逐字符坐标比只存词框大一倍多，随论文浮动），
  前端做子串匹配，效果对齐浏览器：
  所有命中都上色（其余淡黄、当前橙色），命中处按**真实字形边界**画色块而不是整行横带，
  滚动条旁打命中刻度；`Enter`/`Shift+Enter` 上下条，`Esc` 关闭，右上角显示 `n/总数`。
  **单栏（原文/译文）只搜当前这一栏**，双栏两边都搜、切视图后命中自动重算，绝不把用户强行切回双栏。
  中英文都能搜。用 `rawdict` 而不是 `words` 取词：后者的 (块号, 行号) 在这类 PDF 里会跨行重复，
  同一组混进页面上下两端的词，行高能算到半页。
- **命中框精确到字**：每个词额外存词内**逐字符**的起点 x（`[x0, x1, 词, [字x0…]]`）。
  只存词框的话，前端只能按「字符数平均」内插，混排词（`SimpleVLA-RL:`、中文夹 `VLA`）
  会偏半个字——中文整行没空格、一个词就是一行，偏得最明显。存字符坐标后直接取字形边界。
- **缩放按浏览器习惯**：`Ctrl/⌘ + 滚轮` 以指针为锚点（缩放前后指针下的内容不动），双击页面在
  100%↔220% 间切换，`+/-` 与按钮以视口中心为锚点，触屏可双指捏合；范围 40%–400%，100% = 适宽。
  放大后出现横向溢出时可按住拖动平移，两栏横向同步。
- 体积：目录版 = `index.html`（约 2～3 MB，含引擎）+ `pdf-en.js` / `pdf-zh.js`（base64 的两份 PDF，
  按需载入）；单文件版把两份 PDF 也内联，体积 ≈ 两份 PDF 之和 × 4/3——2509.00215 约 13.7 MB，
  2607.15036 的 PDF 各 16.7 MB → **47.4 MB**，因此默认不生成。
- **双击判定是自己实现的**：靠两次 `click` 的间隔与位移，不依赖原生 `dblclick`（部分触控板/CDP 环境不派发）；
  拖动平移也要等位移超过 4 px 才 `preventDefault`，否则会吞掉双击。
  点在文字上时不触发双击放大——那一下留给浏览器的选词（与桌面 PDF 阅读器一致）。
- 页码差不是错：中文字号紧、行距小，21 页对应原文 24 页，看的是同一节。

## 翻译前 / 编译前的两道检查

**① 翻译前先跑 `check_coverage.py`，必须清零。** `check_tr.py` 只比对 translations 与
segments，对「根本没被抽到的正文」天然报 100% 通过——2607.15036 就吃过这个亏：
8 个粗体小标题 + 3 段正文 + 3 个 `\makecell` 表头留在中文 PDF 里是英文，体检全绿、
编译 0 错误，最后靠人翻页才发现。本脚本用 segments 记录的 `[start,end]` 偏移，
把源文减去已覆盖区间，在剩下的区间里剥掉命令、公式、注释、标签后找成片英文：

```bash
python $S/check_coverage.py ./2508.08241            # 有可疑漏译退出码 1
python $S/check_coverage.py ./2508.08241 --warn-only
```

实测：修复前的 2607.15036 报 **25 处 / 2018 字符**（全是真漏译），修好抽取器重抽后
**0 处**。报告写在 `.arz/coverage.json`。

**② 编译前 `check_tr.py` 兜底**（`build.py` 会自动跑，`--allow-partial` 可强行编译）：

```bash
python $S/check_tr.py ./2508.08241          # 缺译 / 中文过少 / 成片英文 / 未抽取正文
python $S/check_tr.py ./2508.08241 --json   # 给上层脚本消费
```

`apply.py` 只校验标记，没提交译文的段会**原样保留英文**；LaTeX 编译又永远成功。
五类判据写在 `.arz/check_tr.json` 里：

| 类别 | 判据 | 处理 |
|---|---|---|
| 缺译 | `translations.json` 里没有这个 id | 补译 |
| 原样返回 | 译文 == 原文 masked（公式、纯符号段属正常） | 只提示不拦 |
| 中文过少 | 原文 ≥8 个英文词，译文汉字占比 <8% | 视为没译 |
| 成片英文 | 译文里仍有 ≥7 个连续英文单词 | `--strict` 才算失败，默认只提示 |
| 未抽取正文 | 源文成片英文落在 segments 区间之外（复用 `check_coverage`） | **拦编译**；修 extract.py 后 `--force` 重抽 |

## 已知问题与处理

| 现象 | 原因 | 处理 |
|---|---|---|
| ~~对照页看着比直接开 PDF 糊~~ | 旧版页面是预渲染位图（最多 1836 px），放大或高分屏下必然被拉伸；先换 WebP 3.0× 只到 PDF 的 81%，治不了根 | **v1.0.2 已改架构**：页面改由 pdf.js 现渲，位图尺寸 = CSS 宽 × dpr，缩放即重绘；实测 dpr=2 下 50%/100%/200% 的 canvas 与显示像素比恒为 2.00，260% 时 1.85（受单页 12 Mpx 上限保护，仍远好于旧版拉伸） |
| ~~正文选不中、复制出来是图~~ | canvas 上只有像素 | 每页渲完叠一层透明 textLayer（见「渲染架构」一节）；只给 span 开 `pointer-events`，空白处双击放大照旧 |
| 搜索高亮在中文侧偏半个字 | 一个词只有首尾坐标时，前端按「字符数平均」内插；中文整行没空格、一个词就是一行，混排处必然偏 | `page_words` 存词内**逐字符**起点 x，命中框直接取字形边界；中文侧 8 个混排词实测旧算法偏 2.2–21.7 px（关键词行最明显），新算法 0 px |
| 控制台 `NetworkError: importScripts ... blob:null/...`，渲染变慢 | `file://` 的 origin 是 `null`，pdf.js 判 `workerSrc` 跨源后又包一层 CDN wrapper，在 blob worker 里必然失败，随后静默退回主线程渲染 | 不走 `workerSrc`，直接 `new PDFWorker({port: new Worker(blobUrl)})`；另存 `workerSrc` 作兜底。用 `window.__pdfMode` 可确认走的是 `worker` 还是 `main` |
| 编译 0 错误但 PDF 无中文 | `newtxtext`/`txfonts` 等 Type1 包触发 METAFONT 回退，中文静默消失 | `inject_cjk` 会自动检测并换成 OpenType（TeX Gyre Termes + unicode-math）；若仍失败换 Fandol |
| 同样是 0 错误 0 缺字，但汉字全丢 | 文档类经 `XCharter.fontspec` 设了 `\defaultfontfeatures{Extension=.otf}`，`SimSun` 被解析成 `SimSun.otf` → nullfont | 注入块在 `xeCJK` 之后加 `\defaultfontfeatures{}`，清空继承特性（已自动） |
| `Transform components aren't all known` 刷屏 | 同上 | 同上 |
| Fandol 编译通过但中文进不了 PDF | 本机 MiKTeX 的 Fandol 无 ToUnicode | 用 SimSun；脚本的哨兵检测会拦住这种情况 |
| **摘要整段没译** | 有些模板（如 `thuc3i.cls`）把 `\begin{abstract}` 写在 `\begin{document}` **之前** | `extract` 已补 preamble 里的 abstract 扫描 |
| **章节标题没译，只有部分章节变中文** | "至少两个英文词"规则把 `\section{Introduction}`、`\section{Experiments}` 这类单词标题判为杂项丢弃 | `extract` 对 sectioning 命令的参数放宽到 1 个词（`HEADING_RE`） |
| **PDF 里出现整段 Python 代码被"翻译"** | 作者自建 `\lstnewenvironment{normalcode}`，名字不在 `OPAQUE_ENVS` | `extract` 自动扫描 `\lstnewenvironment{...}` 并登记为不译环境 |
| **题注前缀还是英文**（`Figure 1 \|`、`Table 2 \|`、`Listing 1 \|`，正文却是中文） | 模板把 `figurename=Figure` 写进 `\captionsetup{}`，caption 宏包每次 `\caption` 重新套用，`\AtBeginDocument` 里的 `\renewcommand` 压不住；`\DeclareCaptionType{listing}[Listing]` 定义的是 `\listingname` 而非 `\lstlistingname` | `localize_class_files()` 直接改 cls 里的 `figurename=`/`tablename=`；注入块同时补 `figurename`/`tablename`/`listingname`/`lstlistingname` 四个 `\renewcommand`（均已自动） |
| `Contents` / `References` 仍是英文 | babel 的 `\captionsenglish` 会在 `\begin{document}` 时覆盖 `\contentsname`、`\refname` | 注入块用 `\AtBeginDocument{}` 延后设置；硬编码在 cls 里的（如 `\textbf{Abstract $\vert$}`）由 `localize_class_files()` 替换 |
| 表格右侧撞出版心 | 中文表头比英文宽 | `build.py --fit-tables`（列数 ≥7 时自动加 `\small`）；或手动按具体宽度调 |
| 100 errors 上限、文档截断 | 作者把 `$\s$`、`$\a$` 当变量名，XeTeX 下重映射成文本重音 | 见 `rules/packages.yaml`；这类要改源码，脚本不自动动 |
| PDF 分支报 `cannot unpack non-iterable NoneType`（`download_remote_fonts`） | pdf2zh 默认去 HF 拉 Noto CJK 字体，国内拿不到就返回 None | `pdf_mode.py` 自动把 `NOTO_FONT_PATH` 指向本机 `msyh.ttc`/`simhei.ttf` |
| PDF 分支报 `'NoneType' object has no attribute 'predict'` | pdf2zh 1.9.11 的 `high_level.translate()` 自己不加载版式模型（CLI 里是外面加载好传进去的） | `translate_pdf.py` 显式传 `model=OnnxModel.load_available()` |
| 输入 PDF 翻完就消失 | `translate()` 会把「位于系统临时目录的输入」读完就 `unlink` | `pdf_mode.py` 只喂项目目录里的 `*_原文.pdf`，绝不传 `/tmp` 下的路径 |
| 装完 pdf2zh 仍 `ImportError: cannot import name 'TextTranslateRequest'` | `tencentcloud-sdk-python-tmt` 3.1.x 只剩 10 KB 空壳 | 钉 `tencentcloud-sdk-python-tmt==3.0.1478`（`--install` 自动带） |
| PDF 分支 `pip install pdf2zh` 报 `No matching distribution` | 本机 Python 3.13，而 pdf2zh 的 `requires-python` 是 `>=3.10,<3.13` | 用 3.12 建独立环境：`py -3.12 -m venv .venv-pdf`；`pdf_mode.py` 会自动找它 |
| 装 `pdf2zh==1.9.12` 报找不到版本 | 这个版本在 PyPI 上**不存在**（参考技能写错了） | 用 `pdf2zh==1.9.11`，最后一个支持 3.12 的版本 |
| `--precise` / ultrafast 没任何变化 | pdf2zh 1.9.x 没有这个开关，传进去会被 `**kwarg` 静默吞掉 | 别指望它；要加速就调 `-t` 线程数 |
| `\boldmath这` 未定义 | xeCJK 下中文字符 catcode=11，命令与中文粘连成新控制序列 | `apply.py` 自动在命令后补空格，无需手工 |
| 术语前后不一致 | 分批翻译导致漂移 | `audit.py` 会列出台词命中率，按提示统一 |
| HTML：表格只裁到一半 | 组间距超过聚类阈值；表格单元格本身也是文本块，不能拿来当边界 | `crop_float` 改成「正文段落定外框 + 横线收边 + 相邻题注封顶」 |
| HTML：表格裁成了目录页 | 目录里的「5.3 Real-World Experiments」与题注同文 | 命中的文本块必须以 `Table n` / `Figure n` 开头 |
| HTML：目录条目夹带整段正文 | 抽取器把 `\section{X}` 和紧随的第一段并成一个段落 | `render_html` 按标记把正文拆回独立段落，不丢内容 |
| HTML：公式整条显示成红色 | KaTeX 不认 `\label`、`\nonumber` | `_math()` 先清洗这些命令再交给 KaTeX |
| HTML：子图题注漏成正文、主图题注取错 | 一个浮动体内有多个段落/多个 `\caption` | 区间内段落全部消费；题注取**最后一个** `\caption` |
| 表头、子图题注、`\item[术语]` 明明是正文却没译 | 抽取要求「至少两个英文词」，`Walk`、`Simulation.`、`Ours` 全被当杂项丢掉 | `extract` 按语境放宽：caption / cell / item / heading 一个词也收（`Token.ctx`） |
| beamer 帧标题不译 | `\begin{frame}{标题}` 的标题被当环境参数整体保护 | `TITLE_ENVS` 把 frame/block/alertblock/exampleblock 的必选参数当正文 |
| 算法标题仍是 `Algorithm 1: Training procedure` | `algorithm` 整个环境在不译集合里 | `carve_caption()` 只把 `\caption` 的参数挖出来，其余原样保护 |
| biblatex 论文参考文献整段消失、正文里是 `[smith2020]` | 只跑 bibtex，而 biblatex 的 `.bbl` 必须 biber 生成；且旧逻辑只在「首轮零错误」时才跑 | `pick_bib_tool()` 认 biblatex → 跑 biber；bib 步骤改到首轮编译之后**无条件**执行 |
| TeX Live 下 minted 报 `You must invoke LaTeX with the -shell-escape flag` | MiKTeX 默认允许 write18，本机察觉不到，换机器就炸 | `_common.xetex_flags()` 扫到 minted/pythontex/svg 才加 `-shell-escape`（只对可信源码） |
| `Listing 1:` 还是英文 | minted 浮动体用的是 `\listingscaption`，既不是 `\lstlistingname` 也不是 `\listingname` | 注入块三个名字都 renew |
| IEEEtran 的 `Abstract—`、`Index Terms—` 不译 | 它们读 `\abstractname`、`\IEEEkeywordsname` | 注入块补 abstractname / IEEEkeywordsname / bibname / appendixname / proofname / algorithmcfname 等（都带 `\ifdefined` 兜底） |
| 字体包写在 `\input{preamble}` 里时，A1 方案报上千条错 | 只扫主文件的 `\usepackage`，没发现 newtxmath，照样加 unicode-math | `detect_type1_tree()` 扫全部 tex/sty/cls；`strip_in_tree()` 连被 include 的文件一起摘 |
| arXiv 包里 `00README.json` 写了主文件却不认 | 只匹配 `"main"` 这个键名，不认 arXiv 的 `usage: toplevel` | `readme_toplevel()` 解析 `sources[]`，取 `usage=toplevel` 的 filename |
| PDF 分支报「找不到 pdf-formula-translate 技能」 | 旧版把外部技能目录当硬依赖，clone 仓库的人一定踩 | 仓库自带 `scripts/pdf_translate.py`，优先用它；外部技能只作退路 |
| 显示「0 错误」，PDF 里却缺符号、缺公式 | `-file-line-error` 下 TeX 不再打印行首 `!`，旧的 `^!` 计数全部漏掉 | `_common.error_lines()` 同时认 `f.tex:12:` 形式；preflight / inject / build 统一改用它 |
| caption 宏包报 `Can be used only in preamble` | `\captionsetup` 被放进了 `\AtBeginDocument` | 移到 `\begin{document}` 之前的顶层 |
| 附录里的 `\bm{\omega}` 变成空白 | A1 方案摘掉 `bm` 后没给替代 | 注入 `\providecommand{\bm}[1]{\symbf{#1}}` |
| 表格里的 ✓ 全是空格 | `amssymb` 被摘掉后 `\checkmark` 落到拉丁字体，没有 U+2713 | 在 `\AtBeginDocument` 里改用 pifont 的 `\ding{51}`（`\checkmark` 要到这时才定义） |
| `Abstract:` / `Keywords:` 仍是英文 | 类文件写死的是带冒号的版本，正则只匹配不带冒号的 | `CLASS_LOCALIZE` 补 `\textbf{Abstract:}` / `\textbf{Keywords:}` |
| `\resizebox{...}{...}{\begin{tabular}...}` 包住的整张表一个字都不译 | `resizebox` 列在 `OPAQUE_COMMANDS` 里，整块被保护 | 从 OPAQUE 移除，交给 `TEXT_COMMANDS` + `MIXED_ARGS`（长度参数仍不译） |
| 表头 `\textbf{Environment}` 不译 | 递归进命令参数时用的是函数参数 `ctx`，把 cell 丢回 body | 改用当前环境 `sub_ctx = cur()` |
| `\item Vectors:` 这类单词条目名不译 | 只给 `\item[...]` 放宽到 1 个词，普通 `\item` 仍按正文两个词 | 段首紧跟 `\item` 时按 item 语境；另外把 `no/yes/on/off` 加入短词白名单 |
| 重跑 `inject_cjk` 后回填，中文被塞进英文单词中间、摘要只剩半句 | `segments.json` 记的是字符偏移，注入块长度一变全部平移；旧逻辑看到文件就复用 | segments 记 `zh/` 指纹，对不上自动重抽；`apply.py` 再逐段核对 `source`，不一致直接报错而不是硬写 |
| `audit.py --png` 崩在 `too many values to unpack` | `residue` 改成 dict 之后，导出图片仍按二元组解包 | 改取 `r["page"]` |
| `Algorithm 1` 仍是英文 | caption 宏包在加载时就把浮动体名字抓走了，改 `\algorithmname` 不生效 | `\captionsetup[algorithm]{name=算法}` |
| 编译 0 错误、体检 100%，PDF 里却整段英文，粗体小标题也还是英文 | `\noindent{\textbf{标题}} {同一行紧接的长段}` 里的 `noindent` 被列进 `OPAQUE_COMMANDS`，参数解析又把两个分组一并吞掉，标题与正文都不进 segments；2607.15036 实测漏 8 个小标题 + 3 段正文（集中在 p11/p12），`audit.py` 的「英文成片」告警才把它揪出来 | 新增 `DECLARATION_COMMANDS`：`noindent` / `indent` / `centering` / 字号声明等无参命令只保护命令名，紧随的 `{..}` 分组按正文递归抽取。该文片段 295→297、覆盖率 85%→87%，A/B 去掉标记编号后原有可译句 0 丢失；以 masked 原样回填与英文基线逐字节一致 |
| 中文 PDF 的表格表头还是 `Static Cylinder` / `Static Rectangular` | `makecell` 不在可译命令表，`\makecell[c]{...}` 整个单元格被保护 | 把 `makecell` / `thead` 加进 `TEXT_COMMANDS`；可选参数 `[c]` 仍由 `opts_end` 保护。2607.15036 片段 297→303，`check_coverage` 清零 |
| 一篇论文要十几轮翻译、每轮还要手写 Python 字典字面量 | 按「25 段」分批，实测单批源文 710～18,209 字符、差 25 倍；译文又得完整重打一遍 | `pending.py --budget 18000` 按字符切批（295 段 12 批→5 批，每批 1.3–1.8 万字符）；`submit.py` 直读 TSV/JSON、整批原子校验写入 |
| 体检 100%、编译 0 错误却漏译，最后靠人眼翻 PDF 才发现 | `check_tr` 只比对 translations 与 segments，看不见「根本没被抽到的正文」 | 新增 `check_coverage.py`：用 segments 的 `[start,end]` 做位置差集；`check_tr` / `build` 同步接入并拦编译。修复前报 25 处 / 2018 字符，修复后 0 处 |
| `sim-to-real` 被译成「仿真到真机」，只能事后写脚本全局替换 | `glossary.tsv` 只是给模型看的建议，提交时没有强制力 | 加第 4 列「禁用译法」，`submit.py` 当硬约束检查，违例整批拒绝 |
| `audit` 报 `pose` 命中 18 次，逐条查几乎全是误报 | 术语正则只有尾部 `\b`，`pose` 命中了 pro**pose** / im**pose** / pur**pose** | `term_consistency()` 改用前后负向环视 `(?<![A-Za-z0-9])` / `(?![A-Za-z0-9])`。**不能补前置 `\b`**：中文属于 `\w`，「鲁棒的sim-to-real」里「的」与 `s` 之间没有词边界，会被整个漏掉。实测 `pose` 误报消失、`sim-to-real` 命中率 50%→100% |
| 每篇都生成 47 MB 的单文件对照页 | `--single-file` 写进了示例命令，其实本机看目录版就够 | 示例去掉 `--single-file`、默认不生成；需要发给别人时才加。目录版的三档视图、搜索、划选、缩放一样不少 |

### 分批翻译别用 offset

`pending.py --offset N` 的 N 是**待译队列**的下标，不是段落编号。译完一批后再带 offset，会跳过中间若干段。要么每批只用 `--limit N` 从队头取，要么每批前先 `--stats` 核对。

### 改了抽取规则之后怎么续

规则改动会让段落编号整体位移。不要重译，按 `source` 文本回迁：

```bash
cp .arz/segments.json .arz/segments.old.json   # 抽之前先备份
python $S/extract.py . --force
python - <<'PY'
import json, io, collections
old = json.load(io.open('.arz/segments.old.json', encoding='utf-8'))['segments']
new = json.load(io.open('.arz/segments.json', encoding='utf-8'))['segments']
tr  = json.load(io.open('.arz/translations.json', encoding='utf-8'))
buckets = collections.defaultdict(list)
for s in old:
    buckets[(s['file'], s['source'])].append(tr.get(s['id']))
out, miss = {}, []
for s in new:
    b = buckets[(s['file'], s['source'])]
    v = None
    while b and v is None:
        v = b.pop(0)
    if v: out[s['id']] = v
    else: miss.append(s['id'])
json.dump(out, io.open('.arz/translations.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('迁移', len(out), '待补', len(miss), miss)
PY
```

## 审计怎么读

`audit.py` 输出四类信号，优先级从高到低：

1. **源码层未译片段** —— 必须清零。有英文原文却提交了无中文译文，说明漏译。
2. **整页无中文** —— 纯图页正常；若该页有正文说明字体失效。
3. **术语异常** —— `✗` 是命中率低于 25%，通常是整段漏译或译名跑偏；`⚠` 是 25–60%，多为同义词混用。
4. **英文成片（≥8 行/页）** —— 图注密集的页面会误报，看样本判断。

`--png` 会把首页和可疑页导成图，供视觉核验排版（是否重影、表格是否溢出、图注是否被裁）。

## 环境自检

改过脚本、或换到新机器，先跑一遍再干活（`tests/` 只在 GitHub 仓库里，市场包为控制体积和目录层级没有收录）：

```bash
python ~/.workbuddy/skills/arxiv-zh-tex/tests/run_compat.py          # 取源码 + 挑字体 + 抽取
python ~/.workbuddy/skills/arxiv-zh-tex/tests/run_compat.py --full   # 再灌假译文 → 编译 → 查 PDF
```

12 个最小工程各针对一类易翻车写法：标准类 + `\input`、IEEEtran、revtex4-2、elsarticle、
llncs、acmart、beamer、minted、natbib+bibtex、biblatex+biber、preamble 里的 Type1、
`00README.json` 指定主文件。全绿说明主文件识别、字体注入、抽取规则、参考文献后端都还正常；
某项红了直接看它打印的「漏抽」或最后一段日志。产物默认留在临时目录，`--out` 可指定位置。

## 边界

- 没有源码的论文不再直接失败：`route.py` 会判成 PDF 分支，交给 `pdf_mode.py`（pdf2zh）。
  但 PDF 分支要求有文字层；扫描件需要先 OCR。
- 不翻参考文献条目（`thebibliography` 与 `.bbl` 整体跳过）、不翻图内文字（含 `tikz`/`pgfplots`
  节点的文字）、不翻伪代码（`algorithm` / `algorithmic` 体，仅标题译）。
- 不自动改作者的重音宏、自定义类、参考文献格式——这些改动风险高于收益，遇到就报给用户。
- 中文字体里没有的符号（①②、特殊箭头、emoji）会 `Missing character` 留白；`inject_cjk` 会打印条数，
  真在意就换字体栈或改源码。
- 目标语言固定中文（简体）。要做其它语言需改 `glossary.tsv` 与字体栈。
