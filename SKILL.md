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
version: 1.0.0
author: hellow-world-collab
visibility: "public"
license: MIT
agent_created: true
---

# 论文中译（保持原排版）

## 第 -1 步：环境自检（缺什么装什么，别让用户自己去折腾）

动手前先跑一遍，缺哪个补哪个，装完再往下走：

```bash
xelatex --version          # LaTeX 分支必需
pdftoppm -v                # 审计出图、对照页必需（poppler）
python -c "import fitz"    # 只有渲染 HTML 时才要 pymupdf
```

| 系统 | 装 XeLaTeX | 备注 |
|---|---|---|
| Windows | `winget install --id MiKTeX.MiKTeX -e`（没 winget 就 `choco install miktex`） | 装完把 `%LOCALAPPDATA%\Programs\MiKTeX\miktex\bin\x64` 补进 PATH；MiKTeX 首次编译自动拉宏包 |
| macOS | `brew install --cask basictex` | 再 `sudo tlmgr install xetex ctex`；完整版 `mactex-no-gui` 约 5 GB，先问用户要不要 |
| Debian / Ubuntu | `sudo apt install texlive-xetex texlive-lang-chinese texlive-latex-extra latexmk` | 中文字体另装 `fonts-noto-cjk` 或 `fonts-fandol` |
| Fedora | `sudo dnf install texlive-xetex texlive-collection-langchinese` | |
| Arch | `sudo pacman -S texlive-core texlive-langchinese` | |

`pdftoppm` 来自 poppler：Windows `winget install --id oschwartz10612.Poppler -e`，macOS `brew install poppler`，
Debian/Ubuntu `sudo apt install poppler-utils`。装完命令若仍找不到，重开一个 shell 或把 bin 目录加进 PATH。

Python 侧：渲染 HTML 要 `python -m pip install pymupdf`；PDF 分支**不要用系统的 3.13**——
`python scripts/pdf_mode.py <项目> --install` 会用 3.10–3.12 自建 `.venv-pdf`（几百 MB，只建一次）。

装完再跑一次 `xelatex --version` 确认出版本号，然后进第零步。确实是内网、没有包管理器这种情况，
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
python $S/fetch.py 2508.08241 --out ./2508.08241      # ① 取源码、认主文件
python $S/preflight.py ./2508.08241                    # ② 基线编译 + xetex 探测
python $S/inject_cjk.py ./2508.08241                   # ③ 注入中文字体（实证验证）
#            ↓ ④ 翻译（见下节，由模型完成）
python $S/apply.py ./2508.08241                        # ⑤ 回填
python $S/check_tr.py ./2508.08241                     # ⑤b 译文体检（缺译就停）
python $S/build.py ./2508.08241                        # ⑥ 编译
python $S/audit.py ./2508.08241 --png                  # ⑦ 审计
python $S/render_compare.py --root ./2508.08241 \      # ⑧ 双栏对照页（可选）
    --original .arz/original_arxiv.pdf --translated out/main.pdf \
    --out html/compare --single-file "论文_双栏对照.html"
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

```bash
python $S/pending.py ./2508.08241 --stats        # 看总进度
python $S/pending.py ./2508.08241 --limit 25     # 读一批（25 段约 3–5k 字）
python $S/pending.py ./2508.08241 --offset 25 --limit 25
python $S/pending.py ./2508.08241 --id tex/intro.tex#0007   # 单段详情含 protected
```

每读完一批，**立刻**写入 `translations.json`，不要攒到最后（中途断了会全丢）：

```bash
cd ./2508.08241 && python - <<'PY'
import json, io, os
p = '.arz/translations.json'
tr = json.load(io.open(p, encoding='utf-8')) if os.path.exists(p) else {}
tr.update({
  "tex/intro.tex#0001": "中文译文，⟪P0001⟫ 标记原样保留……",
})
json.dump(tr, io.open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('已写入', len(tr), '段')
PY
```

### 译文书写规则

1. **所有 `⟪Pnnnn⟫` 必须保留**，数量与编号不能变。位置可以按中文语序调整——`⟪P0002⟫` 是 `\cite{...}` 时，中文里放到句末读起来更顺，这是允许的。
2. **不许出现反斜杠**。译文里写 `\%`、`\textbf` 会被回填校验拒绝，整段退回原文。百分号直接写 `%`，加重语气靠句式而不靠命令。
3. **数字、单位、专有名词照抄**：`$27.38\%$` 这类整体在标记里，不要自己补；`Unitree G1`、`IsaacSim`、`PPO` 不译。
4. **术语查 `glossary.tsv`**（139 条，含"保留英文"标记）。表里没有的按同风格现定，顺手记进 `glossary.suggested.tsv`。
5. **图注要译**（caption 是正文）；图内标注不译（抽取时已跳过 `tikzpicture`/`pgfplots` 等环境）。
6. **不要扩写**。原文一句就是一句，不要补背景、不要合并相邻段——片段之间可能有结构符号，合并会让句子错位。

### 长文分批

一次 25 段左右，超过 5000 字容易在中途丢失标记。遇到 800 字以上的长段，单独处理。

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
用 `render_compare.py`：不动任何排版，两份 PDF 逐页渲成图，左右并排。

```bash
python $S/render_compare.py --root ./2509.09674 \
    --original .arz/original_arxiv.pdf \
    --translated out/main.pdf \
    --out html/compare \
    --single-file "论文_双栏对照.html"
```

产物：`html/compare/index.html` + `html/compare/pages/*.jpg`（懒加载，首屏约 100ms）；
`--single-file` 另出一份图片全内联的单文件版（约 11 MB，交给别人看用）。

要点：

- **原文用真 PDF**：`fetch.py` 只下源码，记得另存 `https://arxiv.org/pdf/<id>` 到 `.arz/original_arxiv.pdf`；
  找不到就退回 `base/main.pdf`（自己编译的基线，排版略异）。
- **锚点来自书签**：两份 PDF 的 `get_toc()` 条目顺序一致（同一份源码），按序号配对得到
  「原文第 p 页 ↔ 译文第 q 页」，联动滚动按锚点分段线性插值，而不是按比例——页数 24 vs 21
  也能对齐到同一节。两句书签数量对不上时自动退回等比例映射。
- 交互：联动/各自滚动、目录跳转（两栏一起跳）、双栏/原文/译文、缩放、交换左右、页码输入框、
  `[`/`]` 翻页、`L` 联动、`F`/`0` 复位。
- **全文搜索**：`Ctrl/⌘+F`（或顶栏「搜索」）。页面是图片，浏览器自带的查找搜不到正文，
  所以渲染时把 PDF 的文字层逐**词**抽进 `META`（`[y0, y1, [[x0, x1, 词], ...]]`，按页分组，
  45 页约 17.6k 个词 / 300 KB），前端做子串匹配，效果对齐浏览器：
  所有命中都上色（其余淡黄、当前橙色），命中处按词的横坐标画色块而不是整行横带，
  滚动条旁打命中刻度；`Enter`/`Shift+Enter` 上下条，`Esc` 关闭，右上角显示 `n/总数`。
  **单栏（原文/译文）只搜当前这一栏**，双栏两边都搜、切视图后命中自动重算，绝不把用户强行切回双栏。
  中英文都能搜。用 `rawdict` 而不是 `words` 取词：后者的 (块号, 行号) 在这类 PDF 里会跨行重复，
  同一组混进页面上下两端的词，行高能算到半页。
- **缩放按浏览器习惯**：`Ctrl/⌘ + 滚轮` 以指针为锚点（缩放前后指针下的内容不动），双击页面在
  100%↔220% 间切换，`+/-` 与按钮以视口中心为锚点，触屏可双指捏合；范围 40%–400%，100% = 适宽。
  放大后出现横向溢出时可按住拖动平移，两栏横向同步。
- 渲染参数：`--scale 2.4 --quality 82`（A4 → 1428×2020，放到 220% 仍锐）；单文件版 `--single-scale 1.7`
  （约 14 MB）。只想要小体积可退回 1.8 / 1.45。
- **双击判定是自己实现的**：靠两次 `click` 的间隔与位移，不依赖原生 `dblclick`（部分触控板/CDP 环境不派发）；
  拖动平移也要等位移超过 4 px 才 `preventDefault`，否则会吞掉双击。
- 页码差不是错：中文字号紧、行距小，21 页对应原文 24 页，看的是同一节。

## 编译前的译文体检

`build.py` 现在会先跑 `check_tr.py`，缺译就直接拦下（`--allow-partial` 可强行编译）：

```bash
python $S/check_tr.py ./2508.08241          # 单独跑：缺译 / 中文过少 / 成片英文
python $S/check_tr.py ./2508.08241 --json   # 给上层脚本消费
```

为什么要有这一步：`apply.py` 只校验标记，没提交译文的段会**原样保留英文**；LaTeX 编译又永远成功，
最后只能人工翻 20 页去发现「第 12 页那段怎么还是英文」。四类判据写在 `.arz/check_tr.json` 里：

| 类别 | 判据 | 处理 |
|---|---|---|
| 缺译 | `translations.json` 里没有这个 id | 补译 |
| 原样返回 | 译文 == 原文 masked（公式、纯符号段属正常） | 只提示不拦 |
| 中文过少 | 原文 ≥8 个英文词，译文汉字占比 <8% | 视为没译 |
| 成片英文 | 译文里仍有 ≥7 个连续英文单词 | `--strict` 才算失败，默认只提示 |

## 已知问题与处理

| 现象 | 原因 | 处理 |
|---|---|---|
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
