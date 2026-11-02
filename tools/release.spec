# -*- mode: python ; coding: utf-8 -*-
# PTサンプルマネージャ（配布版）— PyInstaller の設定
#
# ★直接は使わない。tools/build_release.py が、組み立て・確認・ライセンスの収集・zip まで行う。
#   PYTHONPATH=. .venv/bin/python tools/build_release.py
#
# 出力: build/release/dist/PTサンプルマネージャ.app（windowed / onedir / arm64）
# - 開発言語は en（Info.plist の既定の説明も英語）。日本語は resources/ja.lproj から出る。
#   画面の言語の決め方（core/i18n.py）と同じく、日本語も英語も無い Mac では英語になる
# - 中の実行ファイルは ASCII 名（SampleManager）。.app の名前は読み上げで分かるように日本語
# - 署名は ad-hoc（PyInstaller の既定）。**手元の証明書では署名しない**（証明書の名前が配布物に入る）
# - 借りる部品（ptcore の .py）は .app の中の siblings/ に、中立の名前のフォルダで置く（core/parts.py）。
#   integration/ はファイルの場所を指定して（sys.path に足して）読む作りのため、ファイルのまま置く。
#   **コメントと説明文を除いた写し**を入れる（平文で残るので、開発の経過を出さない）
# - ファイルのまま置いたものは PyInstaller が中を調べないので、それらが import するもの（標準ライブラリ）を
#   ここで拾って hiddenimports に足す（拾い忘れると、配布版でだけその機能が黙って止まる）
# - **py-ptsl と protobuf は入れない。** Pro Tools へは integration/ptsl_grpc.py が grpcio で直接つなぐ
#   （py-ptsl を使うのは控えの旧経路 integration/ptsl_client.py だけ）
import ast
import importlib.util
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent
sys.path.insert(0, str(ROOT))

from core.build_info import FLAVOR_DISTRIBUTION  # noqa: E402
from core.i18n import SUPPORTED  # noqa: E402
from core.version import BUNDLE_NAME, VERSION  # noqa: E402
from tools.build_release import CLEAN, OWN_PACKAGES, clean_source  # noqa: E402
from tools.release_parts import part_files  # noqa: E402

# このプロジェクトと借り先の中の名前（hiddenimports には入れない）
LOCAL_TOPS = {"core", "ui", "integration", "tools", "ptcore", "audio", "app", "tests",
              "audiofile", "dsp", "library", "placement", "prep", "sessioninfo", "splicename",
              "timeline"}


def imports_of(path: Path) -> set:
    """ファイルが import する名前（関数の中のものも）。相対 import は除く。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


def importable(name: str) -> bool:
    try:
        return importlib.util.find_spec(name.split(".")[0]) is not None
    except (ImportError, ValueError):
        return False


def write_clean(path: Path, out: Path) -> None:
    """コメントと説明文を除いた写しを書く。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(clean_source(path.read_text(encoding="utf-8")), encoding="utf-8")


# 日本語と英語の名前・許可の説明（Contents/Resources/*.lproj。Mac の言語で Finder の名前が変わる）
datas = [(str(ROOT / "resources" / f"{lang}.lproj" / "InfoPlist.strings"), f"{lang}.lproj")
         for lang in SUPPORTED]
loose = []
for path, rel in part_files():
    write_clean(path, CLEAN / "siblings" / rel)
    datas.append((str(CLEAN / "siblings" / rel), str((Path("siblings") / rel).parent)))
    loose.append(path)

# 試聴の just_playback の _ma_playback（cffi で作った拡張）は、読み込まれるときに C の側から _cffi_backend を
# import する。PyInstaller には見えないので足す（無いと配布版でだけ試聴が使えない）
hidden = {"_cffi_backend"}
for path in loose:
    for name in imports_of(path):
        if name.split(".")[0] in LOCAL_TOPS or not importable(name):
            continue        # 自分たちの部品と、フラットに読むときだけの名前（audiofile など）
        hidden.add(name)

# 自分たちのコードも、**説明文とコメントを除いた写し**から組み立てる。
# コンパイルしたコード（.app の中の PYZ）にも説明文は文字列として残り、取り出せば読めるため
SRC = CLEAN / "src"
for path in [ROOT / "app.py"] + [p for d in OWN_PACKAGES for p in sorted((ROOT / d).glob("*.py"))]:
    write_clean(path, SRC / path.relative_to(ROOT))

a = Analysis(
    [str(SRC / "app.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    # 関数の中で遅れて import するもの（integration.render_service など）を取りこぼさないよう、丸ごと入れる
    hiddenimports=sorted(hidden) + collect_submodules("core") + collect_submodules("ui")
    + collect_submodules("integration") + collect_submodules("audio"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # ptsl（py-ptsl）と google.protobuf は控えの旧経路でしか使わない（上の説明）
    excludes=["tkinter", "PyInstaller", "pytest", "IPython", "ptsl", "google.protobuf",
              "setuptools", "pkg_resources"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SampleManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="arm64",
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="SampleManager",
)
app = BUNDLE(
    coll,
    name=f"{BUNDLE_NAME}.app",
    icon=None,
    bundle_identifier="local.takahashi.samplemanager",
    version=VERSION,
    info_plist={
        # **Finder は、この表示名が .app の本当の名前と同じときだけ訳を使う**（Apple の仕様）
        "CFBundleName": BUNDLE_NAME,
        "CFBundleDisplayName": BUNDLE_NAME,
        "CFBundleDevelopmentRegion": "en",
        "CFBundleLocalizations": list(SUPPORTED),
        "LSHasLocalizedDisplayName": True,
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "15.0",
        "LSArchitecturePriority": ["arm64"],
        "LSRequiresNativeExecution": True,
        # バージョン情報で読み上げる（tools/build_release.py が環境変数で渡す。core/build_info.py が読む）
        "SMBuildDate": os.environ.get("SM_BUILD_DATE", ""),
        "SMGitCommit": os.environ.get("SM_GIT_COMMIT", ""),
        "SMFlavor": FLAVOR_DISTRIBUTION,
        # 許可の説明の既定は英語。日本語は resources/ja.lproj/InfoPlist.strings にある
        "NSAppleEventsUsageDescription": (
            "Used only as a fallback: asks VoiceOver through System Events to speak "
            "when the app cannot ask directly."),
        "NSDesktopFolderUsageDescription": "Used to load samples placed on the Desktop.",
        "NSDocumentsFolderUsageDescription": "Used to load samples in the Documents folder.",
        "NSDownloadsFolderUsageDescription": "Used to load downloaded samples.",
        "NSRemovableVolumesUsageDescription": "Used to load samples on external drives.",
        "NSNetworkVolumesUsageDescription": "Used to load samples in network folders.",
    },
)
