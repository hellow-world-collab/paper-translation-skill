#!/usr/bin/env python3
"""PDF 分支：调用 pdf-formula-translate 技能，把「只有 PDF」的论文也接到同一条产线上。

为什么要有这一层：
  · 本技能主链要 LaTeX 源码；期刊 PDF、扫描版、arXiv 没给源码的论文都吃不到。
  · `pdf-formula-translate` 基于 PDFMathTranslate，逐块覆盖文字层，能保住公式与版式，
    但它是独立技能、依赖 Python 3.10–3.12（pdf2zh 明确 <3.13），需要一个适配层。

它做的事：
  1. 找到 pdf-formula-translate 技能目录；
  2. 挑一个能 `import pdf2zh` 的解释器（默认用本技能的 .venv-pdf）；
  3. 调它的 translate_pdf.py，产出 *-mono.pdf / *-dual.pdf；
  4. 统一改名成 `<论文>_中文版.pdf` / `<论文>_双语版.pdf`，原文留一份 `<论文>_原文.pdf`；
  5. 可选：直接调用 render_compare.py 生成三栏对照页（PDF 分支也能有）。

用法：
    python pdf_mode.py 论文.pdf --project ./2509.09674 --compare
    python pdf_mode.py https://arxiv.org/pdf/2509.09674 --project ./2509.09674 --service bing
    python pdf_mode.py 论文.pdf --check            # 只体检：技能在不在、解释器能不能用
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import die, meta_dir, ok, run, save_json, warn  # noqa: E402

SKILL_NAME = "pdf-formula-translate"
PINNED = "pdf2zh==1.9.11"          # 1.9.12 不存在；1.9.11 是当前 <3.13 的最后一版
# PyPI 上 tencentcloud-sdk-python-tmt 最新版（3.1.x）只剩 10 KB 的壳，没有
# TextTranslateRequest，pdf2zh 一 import 就 ImportError。必须钉在 3.0.x 最后一版。
TMT_PINNED = "tencentcloud-sdk-python-tmt==3.0.1478"
PIP_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
HF_MIRROR = "https://hf-mirror.com"
MINOR_OK = {(3, 10), (3, 11), (3, 12)}


# --------------------------------------------------------------------------- #
# 定位
# --------------------------------------------------------------------------- #
def find_pdf_skill() -> Path | None:
    cands = []
    env = os.environ.get("PDF_FORMULA_TRANSLATE_HOME")
    if env:
        cands.append(Path(env))
    home = Path.home() / ".workbuddy" / "skills" / SKILL_NAME
    cands.append(home)
    cands.append(Path(__file__).resolve().parents[2] / SKILL_NAME)   # skills/ 同级
    for c in cands:
        if (c / "scripts" / "translate_pdf.py").is_file():
            return c
    return None


def venv_python() -> Path:
    root = Path(__file__).resolve().parents[1] / ".venv-pdf"
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def usable(py: str | Path) -> tuple[bool, str]:
    """解释器能 import pdf2zh 才算可用。"""
    try:
        p = subprocess.run(
            [str(py), "-c", "import sys,pdf2zh;print(sys.version.split()[0])"],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as e:  # noqa: BLE001
        return False, f"启动失败：{e}"
    if p.returncode == 0:
        # pdf2zh 导入时会往 stdout 打一行 "not in git repo"，只取最后一行版本号
        ver = [ln.strip() for ln in p.stdout.strip().splitlines() if ln.strip()]
        return True, (ver[-1] if ver else "?")
    err = (p.stderr or "").strip().splitlines()
    return False, err[-1] if err else "import pdf2zh 失败"


def pick_cjk_font() -> Path | None:
    """挑一个本机中文字体给 pdf2zh 用。

    pdf2zh 默认从 HuggingFace 拉 Noto Sans CJK，国内经常拿不到，报
    `TypeError: cannot unpack non-iterable NoneType object`（download_remote_fonts 返回 None）。
    它支持用环境变量 NOTO_FONT_PATH 指定本地字体，绕开这一步。
    """
    cands = [
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/Deng.ttf"),
        Path("C:/Windows/Fonts/simsun.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
    ]
    for c in cands:
        if c.is_file():
            return c
    return None


def pip_install(py: Path, pkgs: list[str]) -> tuple[int, str]:
    """先走默认源，失败（常见是代理 502 / 无外网）再换清华镜像。"""
    rc, out = run([str(py), "-m", "pip", "install", *pkgs], timeout=1800)
    if rc != 0:
        warn("默认源失败，改走清华镜像重试")
        rc, out2 = run([str(py), "-m", "pip", "install", "-i", PIP_MIRROR, *pkgs], timeout=1800)
        out = out + "\n" + out2
    return rc, out


def base_pythons() -> list[Path]:
    """列出本机 3.10–3.12 的解释器（用来建 venv）。"""
    out: list[Path] = []
    if os.name == "nt":
        try:
            p = subprocess.run(["py", "-0p"], capture_output=True, text=True, timeout=30)
            for line in p.stdout.splitlines():
                if "-V:3.1" not in line:
                    continue
                ver = line.split("-V:")[1].split()[0]
                if tuple(int(x) for x in ver.split(".")[:2]) in MINOR_OK:
                    tail = line.split("Python", 1)[-1].strip()
                    if tail:
                        out.append(Path(tail))
        except Exception:  # noqa: BLE001
            pass
    for c in (
        Path("C:/Users/%s/AppData/Local/Programs/Python/Python312/python.exe" % os.environ.get("USERNAME", "")),
        Path("C:/Python312/python.exe"),
    ):
        if c.is_file():
            out.append(c)
    # 去重且保序
    seen, uniq = set(), []
    for c in out:
        k = str(c).lower()
        if k not in seen:
            seen.add(k); uniq.append(c)
    return uniq


def discover_python(explicit: str | None) -> Path | None:
    """依次尝试：显式指定 → 环境变量 → 技能自带 venv → py -0p 里的 3.10–3.12。"""
    tried: list[str] = []
    cands: list[Path] = []
    if explicit:
        cands.append(Path(explicit))
    if os.environ.get("ARXIV_ZH_PDF_PYTHON"):
        cands.append(Path(os.environ["ARXIV_ZH_PDF_PYTHON"]))
    if venv_python().is_file():
        cands.append(venv_python())

    # Windows 上用 py 启动器列出安装的解释器
    if os.name == "nt":
        try:
            p = subprocess.run(["py", "-0p"], capture_output=True, text=True, timeout=30)
            for line in p.stdout.splitlines():
                if "-V:3.1" not in line:
                    continue
                ver = line.split("-V:")[1].split()[0]
                minor = tuple(int(x) for x in ver.split(".")[:2])
                if minor in MINOR_OK:
                    tail = line.split("Python", 1)[-1].strip()
                    if tail:
                        cands.append(Path(tail))
        except Exception:  # noqa: BLE001
            pass

    for c in cands:
        if not Path(c).is_file():
            continue
        good, info = usable(c)
        if good:
            print(f"    解释器：{c}  (Python {info})")
            return Path(c)
        tried.append(f"{c}: {info}")
    if tried:
        print("    试过但不可用：")
        for t in tried:
            print(f"      - {t}")
    return None


# --------------------------------------------------------------------------- #
# 下载远程 PDF
# --------------------------------------------------------------------------- #
def fetch_pdf(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"    下载 PDF：{url}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 arxiv-zh-tex/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    if dest.stat().st_size < 20_000:
        die(f"下载到的文件太小（{dest.stat().st_size} 字节），可能不是 PDF")
    return dest


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="用 pdf-formula-translate 处理只有 PDF 的论文")
    ap.add_argument("input", help="PDF 路径或 http(s) 链接")
    ap.add_argument("--project", required=True, help="项目目录（产物统一放这里）")
    ap.add_argument("--service", default="bing",
                    help="翻译后端，默认 bing（国内可达）；google 在部分网络下 502")
    ap.add_argument("--lang-in", default="en")
    ap.add_argument("--lang-out", default="zh")
    ap.add_argument("--pages", default="", help="只翻指定页，1 起，如 1,3-5")
    ap.add_argument("--thread", type=int, default=4)
    ap.add_argument("--python", default=None, help="指定 Python 解释器（需能 import pdf2zh）")
    ap.add_argument("--compare", action="store_true", help="产出后直接生成三栏对照页")
    ap.add_argument("--title", default="")
    ap.add_argument("--check", action="store_true", help="只体检，不翻译")
    ap.add_argument("--install", action="store_true", help=f"在 .venv-pdf 里装 {PINNED}")
    ap.add_argument("--keep-temp", action="store_true")
    args = ap.parse_args()

    # 子进程（pdf2zh）里的中文输出默认按 GBK 编码，父进程按 UTF-8 解码会乱码
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    # 版式模型（DocLayout-YOLO）与字体走 HuggingFace，国内直连基本拿不到
    os.environ.setdefault("HF_ENDPOINT", HF_MIRROR)
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    if not os.environ.get("NOTO_FONT_PATH"):
        f = pick_cjk_font()
        if f:
            os.environ["NOTO_FONT_PATH"] = str(f)

    project = Path(args.project).expanduser().resolve()
    project.mkdir(parents=True, exist_ok=True)

    # ---- 1. 技能 ----
    skill = find_pdf_skill()
    if not skill:
        die(f"找不到 {SKILL_NAME} 技能。装一下：搜索并安装「公式版式保留PDF中英互译」，"
            f"或设置 PDF_FORMULA_TRANSLATE_HOME 指向它。")
    ok(f"PDF 技能：{skill}")

    # ---- 2. 安装（可选） ----
    if args.install:
        py = venv_python()
        if not py.is_file():
            bases = base_pythons()
            if not bases:
                die("本机没有 3.10–3.12 的解释器，建不了 venv。装一个 Python 3.12 再来。")
            print(f"    用 {bases[0]} 建 .venv-pdf（pdf2zh 不支持 3.13）")
            rc0, out0 = run([str(bases[0]), "-m", "venv", str(py.parent.parent)], timeout=600)
            if rc0 != 0:
                print(out0[-500:]); die("建 venv 失败")
        print(f"    安装 {PINNED} + {TMT_PINNED} → {py.parent.parent}")
        rc, out = pip_install(py, [PINNED, TMT_PINNED])
        print(out[-800:])
        if rc != 0:
            die("安装失败")
        if not args.check:
            ok("环境就绪。去掉 --install 再跑一次即可翻译。")
            return 0

    # ---- 3. 解释器 ----
    py = discover_python(args.python)
    if not py:
        die(f"没有可用的 Python（需要 3.10–3.12，且已装 {PINNED}）。\n"
            f"    本机默认 3.13 装不了 pdf2zh（requires-python <3.13）。\n"
            f"    修法：用 3.12 建 venv 后执行 pdf_mode.py ... --install")
    if args.check:
        print(f"    字体：{os.environ.get('NOTO_FONT_PATH') or '未找到本机中文字体（会去 HF 下载）'}")
        print(f"    HF 镜像：{os.environ.get('HF_ENDPOINT')}")
        ok("体检通过：技能、解释器、pdf2zh 都在。")
        return 0

    # ---- 4. 输入 ----
    src = args.input.strip()
    if src.lower().startswith(("http://", "https://")):
        stem = src.rstrip("/").split("/")[-1] or "paper"
        stem = stem.replace(".pdf", "")
        pdf = fetch_pdf(src, project / f"{stem}.pdf")
    else:
        pdf = Path(src).expanduser().resolve()
        if not pdf.is_file():
            die(f"PDF 不存在：{pdf}")
    ok(f"原文：{pdf.name}（{pdf.stat().st_size / 1048576:.1f} MB）")

    # 统一留一份「原文.pdf」，对照页要它
    original = project / f"{pdf.stem}_原文.pdf"
    if pdf != original:
        if not original.exists() or original.stat().st_mtime < pdf.stat().st_mtime:
            shutil.copy2(pdf, original)

    # ---- 5. 翻译 ----
    out_dir = project / "pdf_out"
    out_dir.mkdir(exist_ok=True)
    # 必须喂项目目录里的副本：pdf2zh 会把「位于系统临时目录的输入 PDF」读完就删掉
    # （translate() 里的 "Cleaned temp file" 分支），直接传 /tmp 下的路径会被吃掉。
    cmd = [str(py), str(skill / "scripts" / "translate_pdf.py"), str(original),
           "-o", str(out_dir), "-li", args.lang_in, "-lo", args.lang_out,
           "-s", args.service, "-t", str(args.thread), "--skip-install"]
    if args.pages:
        cmd += ["--pages", args.pages]
    print(f"\n[{project.name}] PDF 分支翻译（{args.service}）")
    rc, out = run(cmd, timeout=3600)
    if rc != 0 and args.service == "google":
        # 国内最常见的失败就是 google 端点不通，自动换 bing 再试一次
        warn("google 后端失败，自动改用 bing 重试")
        cmd[cmd.index("-s") + 1] = "bing"
        args.service = "bing"
        rc, out = run(cmd, timeout=3600)
    tail = "\n".join(out.strip().splitlines()[-12:])
    if rc != 0:
        print(tail)
        die("翻译失败。常见原因：网络到翻译后端不通（换 --service bing）、"
            "PDF 是扫描件没有文字层、或 pdf2zh 版本不对")
    print(tail)

    # ---- 6. 收产物 ----
    mono = sorted(out_dir.glob("*-mono.pdf"))
    dual = sorted(out_dir.glob("*-dual.pdf"))
    if not mono:
        die(f"没有找到 *-mono.pdf，检查 {out_dir}")
    zh = project / f"{pdf.stem}_中文版.pdf"
    shutil.copy2(mono[-1], zh)
    ok(f"中文版：{zh.name}（{zh.stat().st_size / 1048576:.1f} MB）")
    bi = None
    if dual:
        bi = project / f"{pdf.stem}_双语版.pdf"
        shutil.copy2(dual[-1], bi)
        ok(f"双语版：{bi.name}")

    # ---- 7. 元数据 ----
    save_json(meta_dir(project) / "pdf_mode.json", {
        "mode": "pdf",
        "skill": str(skill),
        "python": str(py),
        "engine": PINNED,
        "service": args.service,
        "source_pdf": str(pdf),
        "original": str(original),
        "translated": str(zh),
        "dual": str(bi) if bi else None,
        "pages": args.pages or "all",
    })
    if not args.keep_temp:
        shutil.rmtree(out_dir, ignore_errors=True)

    # ---- 8. 对照页 ----
    if args.compare:
        rc2, out2 = run([
            sys.executable, str(Path(__file__).parent / "render_compare.py"),
            "--root", str(project),
            "--original", str(original),
            "--translated", str(zh),
            "--out", str(project / "html" / "compare"),
            "--title", args.title or pdf.stem,
            "--sub", "PDF 分支 · 左原文 / 右译文",
            "--single-file", str(project / f"{pdf.stem}_双栏对照.html"),
        ], timeout=1800)
        print(out2.strip()[-600:])
        if rc2 != 0:
            warn("对照页生成失败，PDF 本身没受影响")

    print("\n下一步（可选）：")
    print(f"    python audit.py \"{project}\"      # PDF 分支的审计只看覆盖率，不查标记")
    return 0


if __name__ == "__main__":
    sys.exit(main())
