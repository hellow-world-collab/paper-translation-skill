"""arxiv-zh-tex 公共模块：二进制探测、进程调用、项目目录约定。

设计约束：
- Windows 上 MiKTeX 通常不在 PATH，必须自动探测常见安装位置。
- TeX 日志是 UTF-8，但 Windows 控制台默认 GBK，一律按字节读再解码。
- 所有中间产物放在 <project>/.arz/ 下，不污染论文源码目录。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# 受保护片段的标记。选用 ⟪⟫ 是因为它几乎不会出现在论文正文里，
# 且模型分词时会被当作独立符号，不会与相邻文字粘连。
MARKER = "⟪P{:04d}⟫"
MARKER_RE = re.compile(r"⟪P(\d{4,})⟫")

META_DIR = ".arz"

_WIN_TEX_DIRS = [
    r"C:\Users\{user}\AppData\Local\Programs\MiKTeX\miktex\bin\x64",
    r"C:\Program Files\MiKTeX\miktex\bin\x64",
    r"C:\Program Files (x86)\MiKTeX\miktex\bin",
    r"C:\texlive\2024\bin\windows",
    r"C:\texlive\2025\bin\windows",
    r"C:\texlive\2023\bin\windows",
]


def _win_tex_candidates(name: str):
    user = os.environ.get("USERNAME", "")
    for pattern in _WIN_TEX_DIRS:
        yield Path(pattern.format(user=user)) / f"{name}.exe"
    # TeX Live / MiKTeX 也可能装在其它盘
    for drive in "CDEF":
        yield Path(f"{drive}:/texlive/2025/bin/windows/{name}.exe")


def find_bin(name: str, required: bool = True) -> str | None:
    """优先 PATH，其次 Windows 常见安装位置。"""
    found = shutil.which(name)
    if found:
        return found
    if sys.platform.startswith("win"):
        for candidate in _win_tex_candidates(name):
            if candidate.is_file():
                return str(candidate)
    if required:
        raise SystemExit(
            f"找不到 {name}。请确认已安装 MiKTeX 或 TeX Live，并把它加入 PATH。"
        )
    return None


def tex_env() -> dict:
    """TeX 进程环境：避免长行被截断，关闭交互。"""
    env = os.environ.copy()
    env["max_print_line"] = "4000"
    env["error_line"] = "254"
    env["half_error_line"] = "238"
    # 让 MiKTeX 缺包时自动安装，不弹窗等待
    env["MIKTEX_AUTOINSTALL"] = "1"
    env.setdefault("TEXMFVAR", env.get("TEXMFVAR", ""))
    return env


def run(cmd, cwd=None, timeout=600, check=False) -> tuple[int, str]:
    """执行命令并返回 (退出码, 合并输出)。输出统一按 UTF-8 解码。"""
    if isinstance(cmd, str):
        cmd = [cmd]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            env=tex_env(),
        )
        out = proc.stdout.decode("utf-8", errors="replace")
        rc = proc.returncode
    except subprocess.TimeoutExpired as exc:
        out = (exc.output or b"").decode("utf-8", errors="replace")
        rc = 124
    if check and rc != 0:
        raise SystemExit(f"命令失败（{rc}）：{' '.join(cmd)}\n{out[-3000:]}")
    return rc, out


def meta_dir(root: Path) -> Path:
    d = Path(root) / META_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_json(path: Path, default=None):
    path = Path(path)
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        # 半截 JSON 多半是上一次写入被中断。宁可停下，也不要静默当成空文件重来。
        raise SystemExit(
            f"{path} 不是合法 JSON（{exc.lineno} 行 {exc.colno} 列：{exc.msg}）。\n"
            f"    先看备份：{path.with_suffix('.bak.json')}，"
            f"或手工修好后重跑；不要直接删。"
        ) from exc


def save_json(path: Path, data) -> None:
    """原子写：先写 .tmp 再 os.replace。

    translations.json 是几十轮分批累积出来的，直接覆盖写时若进程被中断
    （Ctrl-C、断电、上下文被掐），文件会变成半截 JSON，几十段译文就没了。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


def read_text(path: Path) -> str:
    """论文源码可能是 UTF-8、Latin-1 或带 BOM 的 UTF-8。"""
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def write_text(path: Path, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8", newline="\n")


def cjk_ratio(text: str) -> float:
    """中文字符占非空白字符的比例，用于检测中文是否真的进了 PDF。"""
    stripped = re.sub(r"\s+", "", text)
    if not stripped:
        return 0.0
    cjk = len(re.findall(r"[\u4e00-\u9fff]", stripped))
    return cjk / len(stripped)


def pdf_pages(pdf: Path) -> int:
    rc, out = run([find_bin("pdfinfo", required=False) or "pdfinfo", str(pdf)])
    if rc == 0:
        m = re.search(r"Pages:\s+(\d+)", out)
        if m:
            return int(m.group(1))
    # pdfinfo 不在时退化为数 PDF 对象
    data = Path(pdf).read_bytes()
    return max(data.count(b"/Type /Page") - data.count(b"/Type /Pages"),
               data.count(b"/Type/Page") - data.count(b"/Type/Pages"), 0)


def pdf_text(pdf: Path, first: int | None = None, last: int | None = None) -> str:
    cmd = [find_bin("pdftotext", required=False) or "pdftotext", "-layout"]
    if first:
        cmd += ["-f", str(first)]
    if last:
        cmd += ["-l", str(last)]
    cmd += [str(pdf), "-"]
    rc, out = run(cmd)
    return out if rc == 0 else ""


def die(msg: str, code: int = 1):
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def ok(msg: str):
    print(f"  [ok] {msg}")


def warn(msg: str):
    print(f"  [warn] {msg}")
