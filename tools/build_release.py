"""配布物（zip）を作る。確かめる → 組み立てる → 並べる → zip → 中身の検証、の順。

    PYTHONPATH=. .venv/bin/python tools/build_release.py
        dist/PTサンプルマネージャ_<版>.zip を作る
    PYTHONPATH=. .venv/bin/python tools/build_release.py --dirty
        試し（書きかけの変更があっても止めない。コミットに + が付く）
    PYTHONPATH=. .venv/bin/python tools/build_release.py --app-only
        .app の組み立てと確認だけ（zip は作らない）

配布版は **PyInstaller で固めた .app**（python も wx も中に入る。
受け取った人は python を入れなくてよい）。

### 組み立てに使う python

**python.org の python3.14（/Library/Frameworks/Python.framework）で
組み立てる。** 開発用の `.venv` は Homebrew の python なので、固めると
Homebrew の OpenSSL・SQLite などのライブラリが入り、それぞれのライセンスを
別に集めることになる。python.org の python はそれらを自分の中に持っていて、
Python の LICENSE.txt にまとめて書いてある。

その python の部品（PyInstaller・wx・pyobjc など）を使い、足りない grpcio
だけを足した専用の環境を `build/release_venv/` に作る
（`--system-site-packages`。python.org の python 自体には何も入れない）。
無ければ最初に作る（grpcio を PyPI から取ってくる）。

工程（どこかで落ちたら zip を作らない）:

  1. 書きかけの変更  同梱するソースと、配布物の印（コミット）をずらさない
  2. テスト          tests/（開発用の .venv で。音と読み上げは止める）
  3. マニュアル      tools/doc_parts.py --check と check_html.py
  4. .app の組み立て PyInstaller（tools/release.spec）。build/release/ に作る
  5. .app の確認     中の実行ファイルで --selftest。署名が ad-hoc か
  6. 同梱部品のライセンス  PyInstaller の目録（toc）と各部品の RECORD から作る
  7. 並べる          はじめにお読みください・マニュアル・ライセンス・.app
  8. zip             ditto（.app の中のシンボリックリンクと署名を壊さない）
  9. zip の検証      展開して署名と --selftest、並び、txt の形、個人の情報

**署名は ad-hoc。** 手元の証明書で署名すると、
証明書の名前が配布物に入る。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.i18n import ENV_NAME, SUPPORTED  # noqa: E402
from core.version import BUNDLE_NAME, ENGLISH_NAME, VERSION  # noqa: E402
from tools.release_parts import find_part  # noqa: E402

APP_NAME = BUNDLE_NAME
EXE_NAME = "SampleManager"
BUILD = ROOT / "build" / "release"
DIST = ROOT / "dist"
SPEC = ROOT / "tools" / "release.spec"
APP = BUILD / "dist" / f"{APP_NAME}.app"
WORK = BUILD / "work"
STAGE = BUILD / "stage"
# コメントと説明文を除いた写しの置き場所（tools/release.spec が作る）
CLEAN = BUILD / "clean"
FOLDER = f"{APP_NAME}_{VERSION}"
ZIP = DIST / f"{FOLDER}.zip"
DOCS = ROOT / "docs"
DEV_PYTHON = ROOT / ".venv" / "bin" / "python"

# 組み立てに使う python（上の説明）と、足す部品
BASE_PYTHON = Path(
    "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14")
RELEASE_VENV = ROOT / "build" / "release_venv"
RELEASE_PYTHON = RELEASE_VENV / "bin" / "python"
# python.org の python に部品があれば、それをそのまま使う（grpcio だけ足す）。
# ほかの部品は、確かめていない版を黙って入れない（無ければ止める）
EXTRA_PACKAGES = ("grpcio==1.76.0",)

# 配る文書（配布物の中の名前 → 元のファイル）。テストもこれを読む
DOC_FILES = {
    "はじめにお読みください.txt": DOCS / "readme.txt",
    "マニュアル.html": DOCS / "manual.html",
    "マニュアル.txt": DOCS / "manual.txt",
    "ライセンス.txt": ROOT / "LICENSE.txt",
    "README (English).txt": DOCS / "readme_en.txt",
    "Manual (English).html": DOCS / "manual_en.html",
    "Manual (English).txt": DOCS / "manual_en.txt",
}
THIRD_PARTY = "同梱部品のライセンス.txt"
# 配布物の直下に並べるもの（はじめにお読みくださいの説明と合わせる。ずれたら止める）
TOP_FILES = (*DOC_FILES, THIRD_PARTY)
TOP_APPS = (f"{APP_NAME}.app",)

# 検査の検索語（個人の情報・関係の無い名前）は**ソースに書かない**（検索語そのものが
# 個人の情報になる）。作者の手元の release_private.json にだけ置く。無ければ、
# その検査はしない（公開しているソースから組み立てるとき）
PRIVATE_FILE = ROOT / "release_private.json"
_WORD_LISTS = ("personal", "personal_text", "words")


def read_private(path: Path) -> dict:
    """検索語を読む。ファイルが無ければ空（公開しているソースから組み立てるとき）。

    **壊れているときは止める。** 読めないまま空にすると、検査が全部黙って素通りして
    「確かめた」と表示される。語は小文字にそろえる（どの検査も大文字小文字を問わない）。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise SystemExit(f"[エラー] {path.name} を読めません（{exc}）") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"[エラー] {path.name} の形が違います")
    for key in _WORD_LISTS:
        words = data.get(key)
        if not (words and isinstance(words, list)
                and all(w and isinstance(w, str) for w in words)):
            raise SystemExit(f"[エラー] {path.name} の {key} が語の並びではありません")
        data[key] = tuple(w.lower() for w in words)
    return data


_PRIVATE = read_private(PRIVATE_FILE)
# 配布物に入っていてはいけないもの（作者の名前など）。**全部のファイルで探す**
PERSONAL = _PRIVATE.get("personal", ())
# 自分たちで書いた文字のファイルでだけ探すもの（メールアドレスの印と手元のパス）。
# python 本体や標準ライブラリの中には、関係のない人のアドレスの例が入っている。
# 同梱した部品のライセンスの文書にも、その部品の作者のアドレスがある
# （numpy の libdivide など。条件どおり載せるもので、作者の情報ではない）
PERSONAL_TEXT = _PRIVATE.get("personal_text", ())
# 自分たちで書いた文字のファイルと、コンパイルしたコードに入っていてはいけない言葉
# （ほかのプロジェクトの名前・手元のメモ）
PRIVATE_WORDS = _PRIVATE.get("words", ())
TEXT_SUFFIXES = (".py", ".txt", ".html", ".md", ".plist", ".strings", ".sh",
                 ".json")
# 自分たちのコードのパッケージ（写しから組み立てて、コンパイルしたものを調べる）
OWN_PACKAGES = ("core", "ui", "integration", "audio")
OWN_INSIDE_APP = "siblings"

RESULTS: list[tuple[str, bool, str]] = []


def find_words(text: str, words) -> list[str]:
    """文字列の中にある検索語（大文字小文字を問わない。語は小文字にしてある）。"""
    lowered = text.lower()
    return [word for word in words if word in lowered]


def clean_source(text: str) -> str:
    """同梱する .py から、**コメントと説明文（docstring）を除く**（動きは変えない）。

    .app の中の .py は平文のまま入り、コンパイルしたコードにも説明文は文字列として残る。
    開発の経過を配布物に出さない。ast で読んで書き直すとコメントは消える。説明文を消すと
    中身が空になるところは pass にする。
    """
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body.pop(0)
                if not body and not isinstance(node, ast.Module):
                    body.append(ast.Pass())
    return ast.unparse(tree) + "\n"


def step(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    tail = f" — {detail}" if detail else ""
    print(f"{'OK' if ok else 'NG'}  {name}{tail}", flush=True)
    return ok


def run(args: list, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([str(a) for a in args], capture_output=True,
                          text=True, **kw)


def last_line(done: subprocess.CompletedProcess) -> str:
    for text in (done.stderr, done.stdout):
        lines = (text or "").strip().splitlines()
        if lines:
            return lines[-1]
    return "出力なし"


def silent_env() -> dict:
    """音も読み上げも出さない（ビルドの最中に VoiceOver が読み出さないように）。"""
    return dict(os.environ, SM_SILENT="1", SM_NO_ANNOUNCE="1",
                PYTHONPATH=str(ROOT))


# --------------------------------------------------------------------------
# 1〜3. 確かめる
# --------------------------------------------------------------------------

def git_lines(*args: str) -> str:
    return run(["git", *args], cwd=ROOT).stdout.strip()


def git_commit() -> str:
    commit = git_lines("rev-parse", "--short", "HEAD") or "?"
    dirty = git_lines("status", "--porcelain")
    return commit + ("+" if dirty else "")


def check_clean(allow_dirty: bool) -> bool:
    name = "1. 書きかけの変更"
    dirty = [line for line in git_lines("status", "--porcelain").splitlines()
             if line.strip()]
    if dirty and not allow_dirty:
        return step(name, False,
                    f"{len(dirty)} 件（コミットしてから作る。"
                    f"試すなら --dirty）: {dirty[0]}")
    detail = "なし" if not dirty else f"{len(dirty)} 件あるが --dirty で続ける"
    return step(name, True, detail)


def run_tests() -> bool:
    done = run([DEV_PYTHON, "-m", "pytest", "tests/", "-q"], cwd=ROOT,
               env=silent_env())
    return step("2. テスト", done.returncode == 0, last_line(done))


def check_docs(allow_dirty: bool) -> bool:
    name = "3. マニュアル"
    tool = ROOT / "tools" / "doc_parts.py"
    parts = run([DEV_PYTHON, tool, "--check"], cwd=ROOT, env=silent_env())
    if parts.returncode != 0:
        if not allow_dirty:
            return step(name, False, "コードから作る部分が古い。"
                        "tools/doc_parts.py で作り直してコミットする")
        parts = run([DEV_PYTHON, tool], cwd=ROOT, env=silent_env())
        if parts.returncode != 0:
            return step(name, False, last_line(parts))
    html = run([DEV_PYTHON, ROOT / "tools" / "check_html.py",
                *[src for doc, src in DOC_FILES.items()
                  if doc.endswith(".html")]], cwd=ROOT)
    if html.returncode != 0:
        return step(name, False, "check_html: " + last_line(html))
    return step(name, True, "コードと合っている。構造の検査も通過")


# --------------------------------------------------------------------------
# 4〜5. 組み立てて確かめる
# --------------------------------------------------------------------------

def ensure_release_python() -> tuple[bool, str]:
    """組み立てに使う環境（build/release_venv）を用意する。無ければ作る。"""
    if not BASE_PYTHON.is_file():
        return False, f"python.org の python3.14 がありません: {BASE_PYTHON}"
    if not RELEASE_PYTHON.is_file():
        made = run([BASE_PYTHON, "-m", "venv", "--system-site-packages",
                    RELEASE_VENV])
        if made.returncode != 0:
            return False, "組み立て用の環境を作れません: " + last_line(made)
    probe = ("import grpc, PyInstaller, wx, objc, Foundation, just_playback, "
             "soundfile")
    if run([RELEASE_PYTHON, "-c", probe]).returncode != 0:
        got = run([RELEASE_PYTHON, "-m", "pip", "install", *EXTRA_PACKAGES])
        if got.returncode != 0:
            return False, "grpcio を入れられません: " + last_line(got)
        check = run([RELEASE_PYTHON, "-c", probe])
        if check.returncode != 0:
            return False, "組み立てに要る部品が足りません: " + last_line(check)
    version = run([RELEASE_PYTHON, "-c",
                   "import sys; print(sys.version.split()[0])"]).stdout
    return True, f"python {version.strip()}"


def build_app() -> bool:
    name = "4. .app の組み立て"
    ok, detail = ensure_release_python()
    if not ok:
        return step(name, False, detail)
    if BUILD.exists():
        shutil.rmtree(BUILD)
    env = dict(os.environ, SM_BUILD_DATE=time.strftime("%Y-%m-%d %H:%M"),
               SM_GIT_COMMIT=git_commit(), PYTHONDONTWRITEBYTECODE="1")
    done = run([RELEASE_PYTHON, "-m", "PyInstaller", "--noconfirm", "--clean",
                "--log-level", "WARN", "--distpath", BUILD / "dist",
                "--workpath", WORK, SPEC], cwd=ROOT, env=env)
    if done.returncode != 0 or not APP.is_dir():
        return step(name, False, last_line(done))
    size = sum(p.stat().st_size for p in APP.rglob("*")
               if p.is_file() and not p.is_symlink())
    return step(name, True, f"{size / 1e6:.0f} MB、{detail}、"
                f"コミット {env['SM_GIT_COMMIT']}")


def selftest(app: Path, lang: str = "ja") -> tuple[bool, str]:
    """中の実行ファイルで --selftest。

    **借りている部品は .app の中のものを読むこと**まで確かめる
    （組み立てる環境の部品を読んで通ると、同梱の写しが壊れていても気づけない）。
    """
    exe = app / "Contents" / "MacOS" / EXE_NAME
    try:
        done = run([exe, "--selftest"], timeout=180,
                   env=dict(silent_env(), **{ENV_NAME: lang}))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    try:
        report = json.loads((done.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False, last_line(done)
    if (done.returncode != 0 or not report.get("ok")
            or not report.get("frozen")
            or report.get("version") != VERSION
            or report.get("transport") != "grpc"
            or not Path(report.get("parts", "")).resolve().is_relative_to(
                app.resolve())):
        return False, json.dumps(report, ensure_ascii=False)
    return True, (f"版 {report['version']}、"
                  f"Pro Tools への接続 {report['transport']}、"
                  f"加工の道具 {report['render_backend']}、"
                  f"python {report['python']}")


def signature_problems(app: Path) -> list[str]:
    """署名が壊れていないか、ad-hoc か（証明書の名前が入っていないか）。"""
    problems = []
    verify = run(["codesign", "--verify", "--deep", "--strict", app])
    if verify.returncode != 0:
        problems.append("署名の検証: " + last_line(verify))
    info = run(["codesign", "-dvv", app]).stderr
    if "Signature=adhoc" not in info:
        problems.append("ad-hoc の署名ではない")
    if "Authority=" in info:
        problems.append("証明書の名前が入っている")
    return problems


# 組み立て用の環境の中で動かす（PyInstaller の読み取り部品を使うため）
_COMPILED_PROBE = r'''
import json, sys, types
from pathlib import Path
from PyInstaller.archive.readers import ZlibArchiveReader

work = Path(sys.argv[1])
words, own = json.loads(sys.argv[2]), json.loads(sys.argv[3])
def consts(values):
    # 文字列だけの並びは、コンパイラが 1 つのタプルや frozenset にまとめる
    for c in values:
        if isinstance(c, str):
            yield c
        elif isinstance(c, (tuple, frozenset)):
            yield from consts(c)
        elif isinstance(c, types.CodeType):
            yield from strings(c)
def strings(code):
    yield from code.co_names
    yield from code.co_varnames
    yield from consts(code.co_consts)
hits = []
seen = 0
for pyz in sorted(work.rglob("PYZ-*.pyz")):
    reader = ZlibArchiveReader(str(pyz))
    for name in sorted(reader.toc):
        if name.split(".")[0] not in own:
            continue
        code = reader.extract(name)
        if not isinstance(code, types.CodeType):
            continue
        seen += 1
        text = "\n".join(strings(code)).lower()
        found = [w for w in words if w.lower() in text]
        if found:
            hits.append(f"{name}（{'・'.join(found)}）")
if not seen:
    hits.append("自分たちのコードが PYZ に見つからない")
print(json.dumps(hits, ensure_ascii=False))
'''


def compiled_hits() -> list[str]:
    """.app に入れた自分たちのコンパイル済みのコードに、関係の無い名前や個人の情報が
    無いか。PYZ は圧縮されていて、grep では見えない。

    入口の app.py は PYZ に入らない（実行ファイルの中の PKG に入る）ので、
    コンパイルする前の写しを調べる。
    """
    words = list(PRIVATE_WORDS) + list(PERSONAL)
    if not words:
        return []
    done = run([RELEASE_PYTHON, "-c", _COMPILED_PROBE, WORK,
                json.dumps(words), json.dumps(OWN_PACKAGES)])
    if done.returncode != 0:
        return ["読めない: " + last_line(done)]
    hits = json.loads(done.stdout.strip().splitlines()[-1])
    entry = CLEAN / "src" / "app.py"
    if not entry.is_file():
        return hits + [f"写しが見つからない: {entry.name}"]
    found = find_words(entry.read_text(encoding="utf-8"), words)
    if found:
        hits.append(f"app（{'・'.join(found)}）")
    return hits


def check_app() -> bool:
    ok, detail = selftest(APP)
    if not ok:
        return step("5. .app の確認", False, f"--selftest: {detail}")
    ok, english = selftest(APP, "en")
    if not ok:
        return step("5. .app の確認", False, f"--selftest（英語）: {english}")
    if not all((APP / "Contents" / "Resources" / f"{lang}.lproj"
                / "InfoPlist.strings").is_file() for lang in SUPPORTED):
        return step("5. .app の確認", False, "Finder の名前の訳（lproj）が無い")
    hits = compiled_hits()
    if hits:
        return step("5. .app の確認", False, "コンパイルしたコードに関係の無い名前"
                    f"や個人の情報: {'、'.join(hits[:5])}")
    problems = signature_problems(APP)
    if problems:
        return step("5. .app の確認", False, "、".join(problems))
    scanned = ("コンパイルしたコードに関係の無い名前は無い" if PERSONAL
               else "検索語が無いので、関係の無い名前は調べていない")
    return step("5. .app の確認", True,
                f"{detail}。英語でも起動。署名は ad-hoc。{scanned}")


# --------------------------------------------------------------------------
# 6. 同梱部品のライセンス（実際に入ったファイルから）
# --------------------------------------------------------------------------

# 組み立て用の環境の中で動かす（そちらの部品の RECORD を読むため）。
# 結果は JSON で受け取る
_LICENSE_PROBE = r'''
import ast, importlib.metadata as metadata, json, re, sys
from pathlib import Path

work, own = Path(sys.argv[1]), [Path(p).resolve() for p in sys.argv[2:]]
DOC_SUFFIXES = ("", ".txt", ".md", ".rst")
LICENSE_NAME = re.compile(r"^(LICEN[CS]E|COPYING|NOTICE|AUTHORS)", re.I)

found = set()
def walk(node):
    if isinstance(node, (list, tuple)):
        if len(node) == 3 and all(isinstance(x, str) for x in node):
            p = Path(node[1])
            if p.is_absolute() and p.is_file():
                found.add(p.resolve())
            return
        for child in node:
            walk(child)
for toc in sorted(work.glob("*.toc")):
    try:
        walk(ast.literal_eval(toc.read_text(encoding="utf-8")))
    except (ValueError, SyntaxError):
        pass

owner = {}
for dist in metadata.distributions():
    for rel in dist.files or []:
        try:
            owner[Path(dist.locate_file(rel)).resolve()] = dist
        except (OSError, ValueError):
            pass

def texts(dist):
    out = []
    for rel in dist.files or []:
        parts = Path(str(rel)).parts
        if not parts or not parts[0].endswith(".dist-info"):
            continue
        name = parts[-1]
        if (LICENSE_NAME.match(name)
                and Path(name).suffix.lower() in DOC_SUFFIXES):
            try:
                t = Path(dist.locate_file(rel)).read_text(
                    encoding="utf-8", errors="replace")
            except OSError:
                continue
            out.append([str(rel), t.replace("\f", "\n")])
    return out

def label(dist):
    meta = dist.metadata
    lab = meta.get("License-Expression") or ""
    if not lab:
        t = (meta.get("License") or "").strip()
        lab = t if t and len(t) < 120 else ""
    if not lab:
        lab = "、".join(c.split("::")[-1].strip()
                       for c in meta.get_all("Classifier") or []
                       if c.startswith("License ::"))
    return lab or "（部品の文書を参照）"

def project(dist):
    meta = dist.metadata
    urls = [meta.get("Home-page") or ""] + [
        u.split(",", 1)[-1].strip() for u in meta.get_all("Project-URL") or []]
    return next((u.rstrip("/").lower() for u in urls if "github.com" in u),
                (urls[0] or "").rstrip("/").lower())

base = Path(sys.base_prefix).resolve()
site = [Path(p).resolve() for p in sys.path if p.endswith("site-packages")]
import PyInstaller
pyi = Path(PyInstaller.__file__).resolve().parent
used, unknown, python_files = {}, [], 0
for path in sorted(found):
    # 部品の持ち主を先に見る。組み立て用の環境（build/release_venv）は
    # プロジェクトの中にあるので、先に「自分たちのもの」で弾くと、
    # そこに入れた grpcio が一覧から黙って落ちる（実際に落ちた）
    dist = owner.get(path)
    if dist is not None:
        used[dist.metadata["Name"]] = dist
        continue
    if any(path.is_relative_to(r) for r in own):
        continue
    if path.is_relative_to(pyi):
        used["pyinstaller"] = metadata.distribution("pyinstaller")
        continue
    if (path.is_relative_to(base)
            and not any(path.is_relative_to(s) for s in site)):
        python_files += 1
        continue
    unknown.append(str(path))
used.setdefault("pyinstaller", metadata.distribution("pyinstaller"))
version = f"python{sys.version_info.major}.{sys.version_info.minor}"
lic = base / "lib" / version / "LICENSE.txt"
print(json.dumps({
    "unknown": unknown,
    "python_files": python_files,
    "python_version": sys.version.split()[0],
    "python_license": lic.read_text(encoding="utf-8", errors="replace"),
    "parts": [{"name": n, "version": used[n].version,
               "label": label(used[n]), "project": project(used[n]),
               "texts": texts(used[n])}
              for n in sorted(used, key=str.lower)],
}, ensure_ascii=False))
'''

_PYINSTALLER_NOTE = [
    "PyInstaller のブートローダー（.app の中の実行ファイル）と、"
    "起動時に使う部品が入っています。",
    "PyInstaller の条件は、PyInstaller でまとめたプログラムを、"
    "どのような条件でも配ってよいとしています"
    "（下の文書の「Bootloader Exception」）。",
    "",
]


def _license_head(data: dict) -> list[str]:
    parts = data["parts"]
    lines = [
        f"{APP_NAME} {VERSION} に同梱している部品のライセンス",
        "",
        "この .app には、次の部品が入っています"
        "（PyInstaller でまとめたとき、実際に入ったファイルから作った一覧です）。",
        f"This file lists the parts made by others inside {ENGLISH_NAME} "
        f"{VERSION}.",
        "The full text of each license follows, part by part. The notes above "
        "them are in Japanese.",
        f"部品は {len(parts) + 1} 個です。"
        "それぞれの条件の全文を、この下に部品ごとに載せます。",
        "",
        f"・Python {data['python_version']}（Python Software Foundation "
        "License。標準ライブラリと、Python に含まれる部品）",
    ]
    lines += [f"・{p['name']} {p['version']}（{p['label']}）" for p in parts]
    lines += [
        "",
        "このアプリ自身（作者 Takahashi）のライセンスは「ライセンス.txt」に"
        "あります。",
        "アプリの中には、作者が作った別の道具の部品"
        "（素材の名前の解析・加工・Pro Tools のセッション情報の読み取り）も"
        "入っています。これも「ライセンス.txt」の条件で配ります。",
        "",
        "=" * 70, f"Python {data['python_version']}", "=" * 70, "",
        data["python_license"].strip(), "",
    ]
    return lines


def collect_licenses() -> tuple[bool, str, str]:
    """(成否, 文書, 詳細)。

    **どの部品にも当たらないファイルがあれば止める**（黙って一覧から落とさない）。
    """
    part = find_part()
    own = [ROOT] + ([part] if part else [])
    done = run([RELEASE_PYTHON, "-c", _LICENSE_PROBE, WORK / SPEC.stem, *own])
    if done.returncode != 0:
        return False, "", "目録を読めない: " + last_line(done)
    data = json.loads(done.stdout.strip().splitlines()[-1])
    if data["unknown"]:
        return False, "", (f"持ち主の分からないファイルが "
                           f"{len(data['unknown'])} 件: {data['unknown'][0]}")
    parts = data["parts"]
    if not parts:
        return False, "", "PyInstaller の目録が読めない"
    lines = _license_head(data)
    missing, borrowed = [], []
    for part in parts:
        texts = part["texts"]
        title = f"{part['name']} {part['version']}（{part['label']}）"
        lines += ["=" * 70, title, "=" * 70, ""]
        if part["name"].lower() == "pyinstaller":
            lines += _PYINSTALLER_NOTE
        if not texts:
            # 部品の配布物に文書が無いとき: 同じプロジェクトの、同じ条件の
            # 部品の文書を載せる（MIT は全文を添える条件のため）
            sibling = next(
                (p for p in parts if p is not part
                 and p["project"] == part["project"]
                 and p["label"] == part["label"] and p["texts"]), None)
            if sibling is None:
                missing.append(part["name"])
                lines += [
                    "この部品の配布物にはライセンスの文書が入っていません。"
                    f"条件は「{part['label']}」です。",
                    f"部品の案内: {part['project']}", ""]
            else:
                borrowed.append(part["name"])
                texts = sibling["texts"]
                lines += [
                    "この部品の配布物にはライセンスの文書が入っていないので、"
                    f"同じプロジェクト（{part['project']}）の同じ条件の部品 "
                    f"{sibling['name']} の文書を載せます。", ""]
        for rel, text in texts:
            lines += [f"--- {rel} ---", "", text.strip(), ""]
    detail = (f"部品 {len(parts) + 1} 個（Python の標準ライブラリなど "
              f"{data['python_files']} ファイル）")
    if borrowed:
        detail += f"。同じプロジェクトの文書を載せたもの: {'、'.join(borrowed)}"
    if missing:
        return False, "", (detail + "。ライセンスの文書が見つからないもの: "
                           + "、".join(missing))
    return True, "\n".join(lines) + "\n", detail


# --------------------------------------------------------------------------
# 7〜9. 並べて zip にし、確かめる
# --------------------------------------------------------------------------

def write_txt(path: Path, text: str) -> None:
    """配る txt は BOM 付き UTF-8・CRLF（古い日本語のエディタで化けない）。"""
    path.write_text(text.replace("\r\n", "\n"), encoding="utf-8-sig",
                    newline="\r\n")


def doc_text(src: Path) -> str:
    """配る文書の中身（BOM を除き、版を差し込む）。"""
    return src.read_text(encoding="utf-8-sig").replace("{VERSION}", VERSION)


def stage_files(third_party: str) -> bool:
    name = "7. 並べる"
    if STAGE.exists():
        shutil.rmtree(STAGE)
    folder = STAGE / FOLDER
    folder.mkdir(parents=True)
    copy = run(["ditto", APP, folder / APP.name])
    if copy.returncode != 0:
        return step(name, False, last_line(copy))
    for doc, src in DOC_FILES.items():
        if doc.endswith(".txt"):
            write_txt(folder / doc, doc_text(src))
        else:
            shutil.copy2(src, folder / doc)
    write_txt(folder / THIRD_PARTY, third_party)
    names = sorted(p.name for p in folder.iterdir())
    want = sorted(TOP_FILES + TOP_APPS)
    if names != want:
        return step(name, False, f"並びが説明と違う: {names}")
    return step(name, True,
                f"ファイル {len(TOP_FILES)} つとアプリ {len(TOP_APPS)} つ")


def make_zip() -> bool:
    DIST.mkdir(exist_ok=True)
    if ZIP.exists():
        ZIP.unlink()
    # --norsrc --noextattr: 拡張属性（隔離の印など）を __MACOSX に入れない。
    # 署名は .app の中の CodeResources にある
    done = run(["ditto", "-c", "-k", "--norsrc", "--noextattr",
                "--keepParent", STAGE / FOLDER, ZIP])
    if done.returncode != 0 or not ZIP.is_file():
        return step("8. zip", False, last_line(done))
    return step("8. zip", True,
                f"{ZIP.name}（{ZIP.stat().st_size / 1e6:.1f} MB）")


def _own_text(folder: Path, path: Path) -> bool:
    """自分たちで書いた文字のファイルか（メールアドレスの印も探す対象）。

    直下の文書（同梱部品のライセンスを除く）と、.app の中の同梱した部品の .py、
    Info.plist、名前の訳（*.lproj）。python 本体や部品のファイルは含めない。
    """
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return False
    rel = path.relative_to(folder)
    if len(rel.parts) == 1:
        return rel.name != THIRD_PARTY
    return (OWN_INSIDE_APP in rel.parts
            or rel.parts[1:] == ("Contents", "Info.plist")
            or (rel.parts[1:3] == ("Contents", "Resources")
                and rel.parent.name.endswith(".lproj")))


def personal_hits(folder: Path) -> list[str]:
    hits = []
    for path in sorted(folder.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        data = path.read_bytes().lower()
        words = PERSONAL
        if _own_text(folder, path):
            words = PERSONAL + PERSONAL_TEXT
        for word in words:
            if word.encode() in data:
                hits.append(f"{path.relative_to(folder)}（{word}）")
                break
    return hits


def private_hits(folder: Path) -> list[str]:
    """自分たちで書いた文字のファイルに、ほかのプロジェクトの名前・手元のメモが無いか。"""
    hits = []
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder).as_posix()
        named = find_words(rel, PRIVATE_WORDS + PERSONAL)   # 名前も見る
        if named:
            hits.append(f"{rel}（名前に {'・'.join(named)}）")
            continue
        if (path.is_symlink() or not path.is_file()
                or not _own_text(folder, path)):
            continue
        found = find_words(path.read_text(encoding="utf-8", errors="replace"),
                           PRIVATE_WORDS)
        if found:
            hits.append(f"{rel}（{'・'.join(found)}）")
    return hits


def txt_problems(folder: Path) -> list[str]:
    problems = []
    for name in TOP_FILES:
        if not name.endswith(".txt"):
            continue
        data = (folder / name).read_bytes()
        if not data.startswith(b"\xef\xbb\xbf"):
            problems.append(f"{name} に BOM が無い")
        if b"\n" in data.replace(b"\r\n", b""):
            problems.append(f"{name} に CRLF でない改行がある")
        if re.search(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]", data):
            problems.append(f"{name} に制御文字がある")
    return problems


def _zip_layout() -> tuple[list[str], list[str]]:
    """(問題, UTF-8 の印が無い日本語の名前)。

    **ditto の zip は、日本語の名前に UTF-8 の印（0x800）を付けない。**
    名前は UTF-8 なので、そのとおりに読む（Mac の「アーカイブユーティリティ」も
    UTF-8 として読むので化けない）。
    """
    with zipfile.ZipFile(ZIP, metadata_encoding="utf-8") as zf:
        bad = zf.testzip()
        if bad is not None:
            return [f"壊れたファイル: {bad}"], []
        names = zf.namelist()
        tops = sorted({n.split("/", 2)[1] for n in names
                       if n.count("/") >= 1 and n.split("/", 2)[1]})
        roots = {n.split("/", 1)[0] for n in names}
        unflagged = [i.filename for i in zf.infolist()
                     if not i.filename.isascii()
                     and not i.flag_bits & 0x800]
    problems = []
    if roots != {FOLDER}:
        problems.append(f"最上位のフォルダが 1 つでない: {sorted(roots)}")
    if tops != sorted(TOP_FILES + TOP_APPS):
        problems.append(f"直下の並びが説明と違う: {tops}")
    junk = ("__MACOSX", ".DS_Store", "__pycache__")
    if any(j in n for n in names for j in junk):
        problems.append("__MACOSX・.DS_Store・__pycache__ のどれかが入っている")
    return problems, unflagged


def verify_zip() -> bool:
    name = "9. zip の検証"
    problems, unflagged = _zip_layout()
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        done = run(["ditto", "-x", "-k", ZIP, out])
        if done.returncode != 0:
            return step(name, False, "展開できない: " + last_line(done))
        folder = out / FOLDER
        app = folder / f"{APP_NAME}.app"
        problems += signature_problems(app)
        # 言語の違いは工程 5 で確かめた。ここは展開で壊れていないかを見る
        ok, detail = selftest(app)
        if not ok:
            problems.append(f"展開した .app の --selftest: {detail}")
        # --selftest のあとも署名が壊れていないこと（.app の中に書いていないこと）
        problems += [f"--selftest のあと: {p}"
                     for p in signature_problems(app)]
        problems += txt_problems(folder)
        hits = personal_hits(folder)
        if hits:
            problems.append(f"個人の情報が入っている: {'、'.join(hits[:5])}")
        hits = private_hits(folder)
        if hits:
            problems.append(f"関係の無い名前が入っている: {'、'.join(hits[:5])}")
        same = run(["diff", "-rq", STAGE / FOLDER, folder])
        if same.returncode != 0:
            problems.append("展開したものが並べたものと違う: " + last_line(same))
    if problems:
        return step(name, False, "、".join(problems))
    note = ""
    if unflagged:
        note = (f"。日本語の名前に UTF-8 の印が無いもの {len(unflagged)} 件"
                "（Mac の展開では化けない）")
    digest = hashlib.sha256(ZIP.read_bytes()).hexdigest()
    words = ("個人の情報・関係の無い名前" if PERSONAL
             else "（検索語が無いので、個人の情報と関係の無い名前は調べていない）")
    return step(name, True, f"展開して署名・--selftest・並び・txt・{words}を"
                f"確かめた{note}。SHA-256 {digest[:16]}…")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="配布物（zip）を作る")
    ap.add_argument("--dirty", action="store_true",
                    help="書きかけの変更があっても止めない（試し）")
    ap.add_argument("--app-only", action="store_true",
                    help=".app の組み立てと確認だけ")
    args = ap.parse_args(argv)
    print(f"配布物: {FOLDER}（{ROOT}）", flush=True)
    print()
    if args.app_only:
        ok = build_app() and check_app()
        print()
        head = ".app を作りました" if ok else ".app を作れませんでした"
        print(f"{head}: {APP}")
        return 0 if ok else 1
    ok = check_clean(args.dirty) and run_tests() and check_docs(args.dirty)
    ok = ok and build_app() and check_app()
    third_party = ""
    if ok:
        good, third_party, detail = collect_licenses()
        ok = step("6. 同梱部品のライセンス", good, detail)
    ok = ok and stage_files(third_party) and make_zip() and verify_zip()
    print()
    if not ok:
        failed = [name for name, good, _ in RESULTS if not good]
        print(f"配布物を作れませんでした。通らなかった工程: {'、'.join(failed)}")
        if ZIP.exists() and failed:
            ZIP.unlink()          # 検証に通らなかった zip は残さない
        return 1
    print(f"配布物ができました: {ZIP}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
