"""エントリポイント。

DB を準備し、AppController を組み立てて MainFrame を起動する。

実行（macOS）:
    cd <このフォルダ>
    .venv/bin/python app.py

（app.py のあるディレクトリが自動で sys.path に入るため、
  PYTHONPATH の指定は不要。どのカレントディレクトリからでも起動できる）

DB は OS のユーザーデータ領域に置く（pathlib で明示的に分岐）。

起動の引数:

| 引数 | 意味 |
| --- | --- |
| （なし） | 普通に起動する |
| `--selftest` | 窓を出さずに組み立てて、部品がそろっているかを 1 行の JSON で言う（配布版を作るときの確認） |

### 配布版（PyInstaller で固めた .app。2026-10-24 から）

配布版では、ログの行き先と部品の場所をここで決める（`_frozen_setup`）。
借りている部品（ptcore）は .app の中の `siblings/placement_core/` に
同梱してあり、その場所を教える。
**.app の中に `__pycache__` を書かない**（同梱した .py を読むと書こうとし、
署名が壊れて Finder から開いても無反応になる）。
"""

from __future__ import annotations

import sys
from pathlib import Path

from core.i18n import APP_NAME, is_english, tr

# PyInstaller で固めた配布版として動いているか
FROZEN = bool(getattr(sys, "frozen", False))

# ログの行き先（手元の起動用スクリプトと同じ場所）
LOG_PATH = Path.home() / "Library" / "Logs" / "SampleManager.log"

if FROZEN:
    sys.dont_write_bytecode = True


def _default_db_path() -> Path:
    """OS ごとに適切な DB 保存先を返す。

    - macOS: ~/Library/Application Support/SampleManager/library.db
    - Windows: %LOCALAPPDATA%/SampleManager/library.db
    - その他: ~/.sample_manager/library.db
    """
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support" / "SampleManager"
    elif sys.platform == "win32":
        import os

        local = os.environ.get("LOCALAPPDATA")
        base = (Path(local) / "SampleManager" if local
                else home / "SampleManager")
    else:
        base = home / ".sample_manager"
    base.mkdir(parents=True, exist_ok=True)
    return base / "library.db"


# 加工の道具（ffmpeg・rubberband）を探す場所。Homebrew の既定の置き場所
TOOL_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


def _extend_tool_path(dirs=TOOL_DIRS) -> list[str]:
    """PATH に Homebrew の場所を足す。足した場所を返す。

    **Finder から開いたアプリの PATH には Homebrew の場所が入っていない**
    （`/usr/bin:/bin:/usr/sbin:/sbin` だけ）。ffmpeg と rubberband は
    `shutil.which` で探すので、入っているのに「入っていない」と判断され、
    移調・尺変更とチャンネル数の変換が止まっていた（ターミナルから
    起動すると PATH を引き継ぐので気づきにくい）。

    実在する場所だけを、後ろに足す（利用者が PATH で決めた順を崩さない）。
    """
    import os

    current = os.environ.get("PATH", "")
    parts = [p for p in current.split(os.pathsep) if p]
    added = [d for d in dirs if d not in parts and os.path.isdir(d)]
    if added:
        os.environ["PATH"] = os.pathsep.join(parts + added)
    return added


def _bundle_root() -> Path:
    """配布版の .app の場所（…/PTサンプルマネージャ.app）。

    実行ファイルは `.app/Contents/MacOS/SampleManager` にある。
    """
    return Path(sys.executable).resolve().parents[2]


def _bundled_parts() -> Path:
    """配布版で、同梱した部品（siblings/ など）が置いてある場所。"""
    return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))


def _frozen_setup(log: bool = True) -> None:
    """配布版だけ。起動の準備をここでする。

    - 借りている部品の場所を教える（`PLACEMENT_CORE_PATH`。
      integration の部品が import されるより前に決める）
    - バージョン情報が読む Info.plist の場所を教える（`SM_APP_BUNDLE`）
    - 標準出力とエラーをログへ（Finder から開くと見えないため）
    """
    import os
    import time

    from core.parts import PART_NAME, PARTS_ENV

    # **外から渡された値を残さない。** 配布版は必ず自分の中の部品を使う
    # （残すと --selftest が .app の外の部品を読んで合格してしまう）
    os.environ[PARTS_ENV] = str(_bundled_parts() / "siblings" / PART_NAME)
    os.environ["SM_APP_BUNDLE"] = str(_bundle_root())
    if not log:
        return
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        stream = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
    except OSError:
        return
    sys.stdout = sys.stderr = stream
    from core.version import VERSION

    print(tr(f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} 起動（配布版 {VERSION}）",
             f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} started "
             f"(packaged version {VERSION})"), flush=True)


def selftest() -> int:
    """`--selftest`: 窓を出さずに組み立てて、部品がそろっているかを確かめる。

    **音も読み上げも出さない。利用者のライブラリと設定にも触らない**
    （一時フォルダを使う）。配布版を作るとき（tools/build_release.py）に、
    組み上げた .app の中の実行ファイルで動かす。関数の中で遅れて読む
    部品（試聴・加工・Pro Tools への接続）は、配布版で取りこぼすと
    その機能だけが黙って止まるので、ここで読み込めるかを確かめる。

    結果は 1 行の JSON。そろっていれば終了コード 0。
    """
    import json
    import os
    import shutil
    import tempfile

    os.environ["SM_SILENT"] = "1"
    if FROZEN:
        _frozen_setup(log=False)
    _extend_tool_path()

    problems: list[str] = []
    for name in ("wx", "objc", "Foundation", "AppKit", "just_playback",
                 "soundfile", "grpc"):
        try:
            __import__(name)
        except Exception as exc:
            problems.append(tr(f"{name}（{type(exc).__name__}: "
                               f"{str(exc)[:120]}）",
                               f"{name} ({type(exc).__name__}: "
                               f"{str(exc)[:120]})"))

    from core.parts import parts_path
    from integration import render_service
    from integration.ptsl_spot_client import SpotPTSLClient, transport

    parts = render_service._load()
    if parts is None:
        problems.append(tr("加工の部品（splicename・prep・dsp）",
                           "the audio processing parts (splicename, prep, "
                           "dsp)"))
    for name in ("placement", "timeline", "audiofile"):
        try:
            __import__(name)          # _load が ptcore を sys.path に足している
        except Exception as exc:
            problems.append(tr(f"借りている部品 {name}（{exc}）",
                               f"the borrowed part {name} ({exc})"))
    try:
        SpotPTSLClient()._load()      # 配置の確認に使う sessioninfo
    except Exception as exc:
        problems.append(tr(f"配置の確認の部品（{exc}）",
                           f"the part that checks placement ({exc})"))
    if transport() != "grpc":
        problems.append(tr("Pro Tools への直接の接続（grpcio）",
                           "the direct connection to Pro Tools (grpcio)"))
    backend = parts[2].choose_backend().name if parts else ""

    import wx

    from core.library import Library
    from core.session_settings import SessionSettings
    from core.version import VERSION
    from ui.controller import AppController
    from ui.main_frame import MainFrame

    workdir = Path(tempfile.mkdtemp(prefix="sm_selftest_"))
    try:
        app = wx.App(False)
        library = Library(workdir / "library.db")
        frame = MainFrame(AppController(library, None, SessionSettings(),
                                        workdir / "session_settings.json"))
        if frame.GetMenuBar() is None:
            problems.append(tr("メニュー", "the menu"))
        frame.Destroy()
        library.close()
        del app
    except Exception as exc:
        problems.append(tr(f"窓の組み立て（{type(exc).__name__}: {exc}）",
                           f"building the window ({type(exc).__name__}: "
                           f"{exc})"))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print(json.dumps({
        "ok": not problems,
        "problems": problems,
        "version": VERSION,
        "frozen": FROZEN,
        "transport": transport(),
        "parts": parts_path(),
        # 加工の道具は利用者が入れるもの。無くても失敗にはしない（報告だけ）
        "render_backend": backend or tr("なし", "none"),
        "python": sys.version.split()[0],
    }, ensure_ascii=False), flush=True)
    return 0 if not problems else 1


def _make_player():
    """試聴プレイヤーを作る。作れなければ None（起動は止めない）。

    **just_playback の例外は RuntimeError とは限らない。** 音の出る装置が
    見つからないと `MiniaudioError`（Exception の直下）を投げる。
    RuntimeError だけを受けていたので、その場合アプリが起動しなかった。
    """
    from audio.player import PreviewPlayer

    try:
        return PreviewPlayer()
    except Exception:
        return None


def _set_app_name(name: str) -> None:
    """メニューバーに出るアプリ名を変える。

    **`python app.py` で起動すると、macOS はアプリ名を「Python」と表示する。**
    実行しているのが Python.app 本体で、`PTサンプルマネージャ.app` の Info.plist
    （`CFBundleDisplayName`）は使われないため。

    NSBundle の情報辞書を**起動時に書き換える**と、アプリメニューの表示名が
    変わる。凍結ビルド（py2app）にしない方針なので、この方法を採る。

    `wx.App` を作る前に呼ぶこと。あとからでは間に合わない。
    失敗しても起動は止めない（名前が「Python」のままになるだけ）。
    """
    if sys.platform != "darwin":
        return
    try:
        from Foundation import NSBundle
    except ImportError:
        return
    try:
        bundle = NSBundle.mainBundle()
        if bundle is None:
            return
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        if info is not None:
            info["CFBundleName"] = name
            info["CFBundleDisplayName"] = name
    except Exception:
        pass


def _localize_app_menu(name: str) -> None:
    """アプリメニューの項目名を日本語にする。

    wx が作る macOS のアプリメニューは「Quit …」「Hide …」のように
    英語のまま出る（wx に日本語の訳語が入っていないため）。
    **読み上げで英語が混ざると分かりにくい**ので、実際の NSMenu を
    書き換える。

    メニューが組み上がったあと（フレームを表示したあと）に呼ぶこと。
    失敗しても英語のままになるだけなので、例外は投げない。
    """
    # 英語ではそのままでよい（wx が作る英語の項目を使う）
    if sys.platform != "darwin" or is_english():
        return
    try:
        from AppKit import NSApp
    except ImportError:
        return
    try:
        main_menu = NSApp().mainMenu()
        if main_menu is None or main_menu.numberOfItems() == 0:
            return
        app_menu = main_menu.itemAtIndex_(0).submenu()
        if app_menu is None:
            return

        # 前方一致で置き換える。項目の並びは macOS の版で変わり得るため
        rules = (
            ("Quit", f"{name}を終了"),
            ("Hide Others", "ほかを隠す"),
            ("Hide", f"{name}を隠す"),
            ("Show All", "すべてを表示"),
            ("Services", "サービス"),
            ("About", f"{name}について"),
            ("Preferences", "設定…"),
        )
        _retitle_items(app_menu, rules)
        _localize_window_menu(main_menu)
    except Exception:
        pass


def _retitle_items(menu, rules) -> None:
    """メニューの項目名を、前方一致の規則で置き換える。"""
    for i in range(menu.numberOfItems()):
        item = menu.itemAtIndex_(i)
        title = str(item.title() or "")
        for prefix, replacement in rules:
            if title.startswith(prefix):
                item.setTitle_(replacement)
                break


# wx が macOS で自動で足す「Window」メニューの訳（macOS の標準の言い方）
WINDOW_MENU_TITLE = "ウインドウ"
WINDOW_MENU_RULES = (
    ("Minimize", "しまう"),
    ("Zoom", "拡大／縮小"),
    ("Bring All to Front", "すべてを手前に移動"),
)


def _localize_window_menu(main_menu) -> bool:
    """wx が足す「Window」メニューを日本語にする。直したら True。

    **このメニューだけ英語のまま残っていた。** wx は macOS で
    「Window」メニュー（Minimize・Zoom・Bring All to Front）を自動で
    足すが、日本語の訳語が無い。起動した配布版のメニューバーを
    System Events で調べて分かった（Apple・PTサンプルマネージャ・操作・
    ヘルプ・Window）。アプリメニューと同じく、読み上げに英語が
    混ざらないよう書き換える。
    """
    for i in range(main_menu.numberOfItems()):
        item = main_menu.itemAtIndex_(i)
        sub = item.submenu()
        if sub is None:
            continue
        if str(item.title() or "") != "Window" and \
                str(sub.title() or "") != "Window":
            continue
        item.setTitle_(WINDOW_MENU_TITLE)
        sub.setTitle_(WINDOW_MENU_TITLE)
        _retitle_items(sub, WINDOW_MENU_RULES)
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--selftest"]:
        return selftest()
    if FROZEN:
        _frozen_setup()

    import wx

    # 加工の道具を探す前に。Finder から開くと Homebrew の場所が PATH に無い
    _extend_tool_path()
    # wx.App より先に。メニューバーのアプリ名に効く
    _set_app_name(APP_NAME)

    from core.library import Library
    from core.session_settings import SessionSettings
    from ui.controller import AppController
    from ui.main_frame import MainFrame

    db_path = _default_db_path()
    library = Library(db_path)

    # 試聴プレイヤーは just_playback 未導入でも、音の出る装置が無くても
    # 起動を止めない
    player = _make_player()

    # 配置先セッションの条件（テンポ・キー）。DB と同じ場所に置く
    settings_path = db_path.parent / "session_settings.json"
    settings = SessionSettings.load(settings_path)

    # 加工したファイルもデータ領域へ。一時ディレクトリだと
    # 取り込む前に掃除される可能性がある
    try:
        from integration import render_service

        render_service.set_work_dir(str(db_path.parent / "rendered"))
    except ImportError:
        pass

    controller = AppController(library, player, settings, settings_path)

    app = wx.App(False)
    # macOS のアプリメニューは wx が「Quit <表示名>」の形で作る。
    # 表示名を教えないと「Quit App」になるので、**MainFrame より先に**渡す
    app.SetAppName(APP_NAME)
    app.SetAppDisplayName(APP_NAME)
    # wx 内蔵の訳語を使う（Quit → 終了、Hide → 隠す など）。
    # 訳が無い環境でも英語のまま動くだけなので、失敗は無視してよい
    try:
        app.locale = wx.Locale(wx.LANGUAGE_ENGLISH if is_english()
                               else wx.LANGUAGE_JAPANESE)
    except Exception:
        pass

    frame = MainFrame(controller)
    frame.Show()
    # メニューが組み上がってから。英語のまま残る項目を日本語にする
    _localize_app_menu(APP_NAME)
    app.MainLoop()

    library.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
