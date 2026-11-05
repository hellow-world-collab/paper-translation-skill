# 2508.08241 · BeyondMimic

用这套流程跑通的第一篇，作为回归基准。

| 项 | 值 |
|---|---|
| 模板 | Science 风格（`science.cls`，跨栏摘要） |
| 原版引擎 | pdflatex，59 页，0 错误 |
| XeLaTeX 兼容 | 完全兼容（59 页，0 错误） |
| 字体方案 | A1：TeX Gyre Termes + unicode-math + SimSun |
| 摘除的包 | newtxtext, newtxmath, txfonts, mathptmx |
| 切分段数 | 251（root.tex 175 + tex/*.tex 76） |
| 源码覆盖率 | 87% |

## 坑位

1. **Type1 字体**：`newtxtext/newtxmath` 在 XeLaTeX 下让中文静默消失，必须换 OpenType。见 `rules/packages.yaml#type1-fonts`。
2. **9 列表格溢出 19pt**：执行器折合惯量表（`tab:actuator_inertia`），中文表头比英文宽。加 `\small` 解决，或 `build.py --fit-tables`。
3. **标题在自定义宏里**：`\def\scititle{...}`，`extract` 只认 `\title{}`，需在 `zh/root.tex` 里手工译。
4. **图内文字不译**：图 4（GRF 曲线）、图 5（t-SNE）、图 6/7 的标注都是位图或 pgfplots 文本，保持原样。

## 已验证结果

- 手工全译版：54 页，0 错误，0 溢出，0 未定义引用（2026-11-04 完成，见 `translate/2508.08241/`）。
- 脚本流程：8 段真实译文回填后 59 页、0 错误，中文正常渲染。
