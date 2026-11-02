"""メインウィンドウ（wxPython）。VoiceOver/PC-Talker 前提。

アクセシビリティ設計:
- 全コントロールに名前を付与する。**macOS では `SetName` だけでは
  入力欄とリストの読み上げ名にならない**ので、`ui/ax_label.py` の
  `label_control` を使う（下地の NSView へ直接設定する）。
  さらに「ラベル自体に説明を埋める」方針（SetName だけでは
  読み上げられない SR があるため、StaticText ラベルにも明示文言を入れる）。
- 時間のかかる処理（スキャン・配置）は**開始と完了で短い音を鳴らす**。
  読み上げより先に鳴らし、成功と失敗で音を変える（`ui/sounds.py`）。
- キーボードのみで全操作完結。Tab/Shift+Tab で循環。
- 結果リストにフォーカスがある時だけ、修飾なしのキーを捌く（EVT_CHAR_HOOK）:
    スペース : 試聴 再生/停止トグル
    Enter    : 配置（PTSL 方式）
  機能のショートカットは**すべて Cmd 付きのメニューのアクセラレータ**
  （Cmd+C/E/S/F/T/R/D など。`_build_menu`）。修飾なしの文字キーは
  リストの頭文字への移動に残す。フォーカスが他にある時は通常処理
  （検索欄への空白入力や Enter 検索）を妨げない。キーの一覧は
  `ui/keys_dialog.py` の KEY_HELP と対で保つ。
- 結果リストは ListBox。1行に describe_ja() 全文を入れ、
  カーソル移動だけで全情報が読み上げられるようにする。
- 操作結果は画面下部のステータス（読み上げ用テキスト）に反映し、
  重要な変化（配置・スキャン完了）は announce=True で能動読み上げする。
- 時間のかかる処理（配置・スキャン）はワーカースレッドで実行し、
  UI と VoiceOver の応答を止めない。

注意: このモジュールは wx に依存する。ロジックは controller.py 側に
寄せてあるため、ここは「wx ウィジェットと controller の配線」に徹する。
"""

from __future__ import annotations

import threading

import wx

from core.i18n import APP_NAME, plural, sentences, tr
from core.models import SampleKind, SourceKind
from ui.ax_label import label_control
from ui.controller import NO_SAMPLE, AppController, SearchForm
from ui.edit_dialog import EditDialog
from ui.keys_dialog import KeysDialog
from ui.session_dialog import SessionDialog
from ui.source_dialog import SourceDialog
from ui.wx_keys import press_button
from ui import sounds

# 種別コンボの選択肢（表示ラベル, 値）
_KIND_CHOICES: list[tuple[str, SampleKind | None]] = [
    (tr("すべての種別", "All types"), None),
    (tr("ワンショットのみ", "One-shots only"), SampleKind.ONESHOT),
    (tr("ループのみ", "Loops only"), SampleKind.LOOP),
]

# チャンネル構成の絞り込み（表示ラベル, チャンネル数）
_CHANNEL_CHOICES: list[tuple[str, int | None]] = [
    (tr("すべての構成", "All channel formats"), None),
    (tr("モノラルのみ", "Mono only"), 1),
    (tr("ステレオのみ", "Stereo only"), 2),
]

# 「すべて」を表す先頭項目。読み取り先・パックの Choice で使う
_ALL_SOURCES = tr("すべての読み取り先", "All sources")
_ALL_PACKS = tr("すべてのパック", "All packs")

# 試聴の音量の選択肢（パーセント）。キー操作の刻みと合わせてある
_VOLUME_STEPS = [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]


class MainFrame(wx.Frame):
    """PTサンプルマネージャのメインウィンドウ。"""

    def __init__(self, controller: AppController) -> None:
        super().__init__(None, title=APP_NAME, size=(720, 600))
        self._controller = controller
        self._placing = False    # Pro Tools 配置の実行中フラグ（再入防止）
        self._scanning = False   # フォルダスキャンの実行中フラグ（再入防止）
        # Choice の並び順と ID/名前の対応。選択肢を作り直すたびに更新する
        self._sources: list = []
        self._packs: list[str] = []
        self._build_menu()
        self._build_ui()
        self._bind_events()
        self._initial_populate()
        # 起動時は検索欄にフォーカス
        self._search_text.SetFocus()

    # ------------------------------------------------------------------
    # UI 構築
    # ------------------------------------------------------------------
    def _build_menu(self) -> None:
        """メニューバーを作る。**終了の手段はここにしか無い。**

        メニューバーを設定しないと、macOS のアプリメニューに「終了」が
        入らず、**Cmd+Q が効かない**（実機で確認。メニューは Apple と
        アプリ名だけになる）。ウィンドウを閉じる以外に終わらせる手段が
        無くなるので、必ず用意する。

        `wx.ID_EXIT` は macOS では自動でアプリメニューへ移動し、
        Cmd+Q が割り当てられる。
        """
        menubar = wx.MenuBar()

        # 「操作」には結果リストのショートカットと同じ機能を並べる。
        # **キーを覚えていなくてもメニューから辿り着ける**ようにするため。
        #
        # **ショートカットには必ず修飾キーを付ける。** 修飾なしの文字キーを
        # 割り当てると、結果リストで頭文字を打って項目へ飛ぶ操作
        # （リストの標準機能）が使えなくなる。実機で「C や S で始まる
        # サンプルに飛べない」と指摘を受けた。
        # `\tCtrl+…` と書くと macOS では Cmd に読み替えられる。
        ops = wx.Menu()
        scan_item = ops.Append(
            wx.ID_ANY,
            tr("フォルダをスキャン", "Scan Folders") + "\tCtrl+R",
            tr("読み取り先の中身を登録する", "Add the contents of the sources"))
        source_item = ops.Append(
            wx.ID_ANY, tr("読み取り先を管理", "Manage Sources") + "\tCtrl+F",
            tr("サンプルを読み取るフォルダを追加・削除",
               "Add or remove the folders samples are read from"))
        session_item = ops.Append(
            wx.ID_ANY, tr("セッションの条件", "Session Settings") + "\tCtrl+S",
            tr("配置先のテンポ・キー・配置のしかた",
               "Tempo and key of the destination, and how samples are "
               "placed"))
        ops.AppendSeparator()
        target_item = ops.Append(
            wx.ID_ANY,
            tr("配置先のトラックを選ぶ", "Choose Destination Track") + "\tCtrl+T",
            tr("Pro Tools のどのトラックへ置くかを決める",
               "Decide which Pro Tools track samples go to"))
        place_item = ops.Append(
            wx.ID_ANY, tr("Pro Tools へ配置", "Place in Pro Tools"),
            tr("選択中のサンプルを配置する", "Place the selected sample"))
        preview_item = ops.Append(
            wx.ID_ANY, tr("試聴 再生/停止", "Preview Play/Stop"),
            tr("選択中のサンプルを試聴する", "Preview the selected sample"))
        favorite_item = ops.Append(
            wx.ID_ANY,
            tr("お気に入りに登録・解除",
               "Add to or Remove from Favorites") + "\tCtrl+D",
            tr("選択中のサンプルにお気に入りの印を付け外しする",
               "Mark or unmark the selected sample as a favorite"))
        edit_item = ops.Append(
            wx.ID_ANY, tr("タグ・種別を編集", "Edit Tags and Type") + "\tCtrl+E",
            tr("選択中のサンプルのタグ・カテゴリ・種別を編集する",
               "Edit the tags, category and type of the selected sample"))
        copy_item = ops.Append(
            wx.ID_ANY, tr("パスをコピー", "Copy Path") + "\tCtrl+C",
            tr("選択中のサンプルのファイルパスをクリップボードへ",
               "Copy the file path of the selected sample to the clipboard"))
        ops.AppendSeparator()
        # macOS では ID_EXIT は自動でアプリメニューへ移る（Cmd+Q が付く）
        quit_item = ops.Append(
            wx.ID_EXIT, tr("終了", "Quit") + "\tCtrl+Q",
            tr("PTサンプルマネージャを終了する", "Quit PT Sample Manager"))
        menubar.Append(ops, tr("操作", "Actions"))

        help_menu = wx.Menu()
        # **Cmd+/ は使わない。** Pro Tools のショートカットと重なって
        # 効かなかった（実機で確認）。Cmd+Shift+K なら重なりにくい
        keys_item = help_menu.Append(
            wx.ID_HELP, tr("キー操作の一覧", "Keyboard Commands") + "\tCtrl+Shift+K",
            tr("使えるキーを一覧で確認する", "See the list of available keys"))
        # **いつビルドしたものかを利用者が確かめられるようにする。**
        # 凍結ビルドではないので、見ただけでは中身の新しさが分からない。
        # **`wx.ID_ABOUT` は使わない。** macOS では自動でアプリメニューへ
        # 移されてしまい、ヘルプから辿れなくなる（実機で確認）
        version_item = help_menu.Append(
            wx.ID_ANY, tr("バージョン情報", "Version Information"),
            tr("ビルドの日時とコミットを読み上げる", "Read the build date and commit"))
        menubar.Append(help_menu, tr("ヘルプ", "Help"))

        self.SetMenuBar(menubar)
        self.Bind(wx.EVT_MENU, self._on_quit, quit_item)
        self.Bind(wx.EVT_MENU, self._on_show_keys, keys_item)
        self.Bind(wx.EVT_MENU, self._on_show_version, version_item)
        self.Bind(wx.EVT_MENU, self._on_scan, scan_item)
        self.Bind(wx.EVT_MENU, self._on_sources, source_item)
        self.Bind(wx.EVT_MENU, self._on_session, session_item)
        self.Bind(wx.EVT_MENU, self._on_import, place_item)
        self.Bind(wx.EVT_MENU, self._on_target_track, target_item)
        self.Bind(wx.EVT_MENU, self._on_preview, preview_item)
        self.Bind(wx.EVT_MENU, self._on_edit, edit_item)
        self.Bind(wx.EVT_MENU, self._on_copy, copy_item)
        self.Bind(wx.EVT_MENU, self._on_toggle_favorite, favorite_item)

    def _on_quit(self, _evt: wx.Event) -> None:
        """終了する。閉じる処理（試聴の停止）を通したいので Close を呼ぶ。"""
        self.Close()

    def _on_show_keys(self, _evt: wx.Event) -> None:
        """キー操作の一覧をダイアログで開く。

        **一度に読み上げるだけでは聞き逃す。** 以前はステータス欄へ全文を
        出していたが、項目が多く途中から聞き直せなかった。リストにして
        **上下矢印で 1 項目ずつたどれる**ようにしてある
        （`ui/keys_dialog.py`）。
        """
        dlg = KeysDialog(self)
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()
        self._set_status(tr("キー操作の一覧を閉じました", "Keyboard commands closed"))

    def _on_show_version(self, _evt: wx.Event) -> None:
        """ビルドの日時とコミットを読み上げる。

        **ダイアログで出す。** ステータス欄の更新と能動アナウンスだけでは、
        メニューから実行したときに読み上げられなかった（実機で確認）。
        メニューが閉じるときのフォーカス移動とアナウンスがぶつかるため。
        ダイアログならフォーカスがそこへ移るので確実に読まれる。

        **動いているコードは常に最新**（ランチャがソースを直接実行する）。
        ビルドが古い場合は「検証が済んでいない」と伝える（詳細は
        `core/build_info.py`）。
        """
        text = self._controller.describe_build_ja()
        # あとから確認できるようステータスにも残す（読み上げはダイアログ側）
        self._set_status(text)
        wx.MessageBox(text, tr("バージョン情報", "Version Information"),
                      wx.OK | wx.ICON_INFORMATION, self)

    def _build_ui(self) -> None:
        panel = wx.Panel(self)
        panel.SetName(tr("メインパネル", "Main panel"))
        outer = wx.BoxSizer(wx.VERTICAL)

        # --- 検索条件エリア -------------------------------------------
        # 各入力には StaticText ラベルを必ず添え、ラベル文言に説明を埋める
        search_box = wx.StaticBoxSizer(
            wx.VERTICAL, panel, label=tr("検索条件", "Search Conditions")
        )

        # テキスト検索
        search_box.Add(
            self._labeled(
                panel,
                tr("ファイル名で検索（部分一致）", "Search by file name (partial match)"),
                self._make_text_ctrl(panel, tr("ファイル名検索", "File name search")),
                attr="_search_text",
            ),
            flag=wx.EXPAND | wx.ALL,
            border=4,
        )

        # 種別
        self._kind_choice = wx.Choice(
            panel, choices=[c[0] for c in _KIND_CHOICES]
        )
        label_control(self._kind_choice, tr("種別の絞り込み", "Type filter"))
        self._kind_choice.SetSelection(0)
        search_box.Add(
            self._labeled_existing(panel, tr("種別", "Type"), self._kind_choice),
            flag=wx.EXPAND | wx.ALL,
            border=4,
        )

        # チャンネル構成（モノラル / ステレオ）
        self._channel_choice = wx.Choice(
            panel, choices=[c[0] for c in _CHANNEL_CHOICES]
        )
        label_control(self._channel_choice, tr("チャンネル構成の絞り込み",
                                               "Channel format filter"))
        self._channel_choice.SetSelection(0)
        search_box.Add(
            self._labeled_existing(
                panel, tr("チャンネル構成", "Channel format"), self._channel_choice),
            flag=wx.EXPAND | wx.ALL,
            border=4,
        )

        # 読み取り先フォルダ
        self._source_choice = wx.Choice(panel, choices=[_ALL_SOURCES])
        label_control(
            self._source_choice,
            tr("読み取り先フォルダの絞り込み。コマンドと数字ですぐ切り替えられる。コマンドとFで追加や削除ができる",
               "Source folder filter. Command and a number switch it right "
               "away. Command-F adds or removes sources"))
        self._source_choice.SetSelection(0)
        search_box.Add(
            self._labeled_existing(panel, tr("読み取り先",
                                             "Source"), self._source_choice),
            flag=wx.EXPAND | wx.ALL,
            border=4,
        )

        # パック（Splice）
        self._pack_choice = wx.Choice(panel, choices=[_ALL_PACKS])
        label_control(
            self._pack_choice,
            tr("パックの絞り込み。Splice の読み取り先でのみ使える",
               "Pack filter. Available only for Splice sources"))
        self._pack_choice.SetSelection(0)
        search_box.Add(
            self._labeled_existing(panel, tr("パック",
                                             "Pack"), self._pack_choice),
            flag=wx.EXPAND | wx.ALL,
            border=4,
        )

        # お気に入りだけを出す。**よく使う素材をすぐ呼び出せるように**
        self._favorite_only = wx.CheckBox(
            panel, label=tr("お気に入りだけを表示", "Show favorites only"))
        label_control(
            self._favorite_only,
            tr("お気に入りだけに絞り込む。コマンドとDで、選択中のサンプルを登録・解除できる",
               "Show only favorites. Command-D adds or removes the "
               "selected sample"))
        search_box.Add(self._favorite_only, flag=wx.EXPAND | wx.ALL, border=4)

        # カテゴリ
        search_box.Add(
            self._labeled(
                panel,
                tr("カテゴリ名（完全一致、空欄で全件）",
                   "Category name (exact match; empty for all)"),
                self._make_text_ctrl(panel, tr("カテゴリ検索", "Category search")),
                attr="_category_text",
            ),
            flag=wx.EXPAND | wx.ALL,
            border=4,
        )

        # タグ
        search_box.Add(
            self._labeled(
                panel,
                tr("タグ（カンマ区切り、すべて一致）",
                   "Tags (separated by commas; all must match)"),
                self._make_text_ctrl(panel, tr("タグ検索", "Tag search")),
                attr="_tags_text",
            ),
            flag=wx.EXPAND | wx.ALL,
            border=4,
        )

        # 検索ボタン
        self._search_btn = wx.Button(panel, label=tr("検索を実行", "Search"))
        self._search_btn.SetName(tr("検索を実行するボタン",
                                    "Button that runs the search"))
        search_box.Add(self._search_btn, flag=wx.ALL, border=4)

        outer.Add(search_box, flag=wx.EXPAND | wx.ALL, border=8)

        # --- 結果リスト -----------------------------------------------
        result_label = wx.StaticText(panel, label=tr("検索結果一覧",
                                                     "Search results"))
        outer.Add(result_label, flag=wx.LEFT | wx.TOP, border=8)
        self._result_list = wx.ListBox(panel, style=wx.LB_SINGLE)
        _list_name = tr("検索結果一覧。上下矢印で項目を移動、文字キーでその頭文字へ移動、スペースで試聴、エンターで配置。"
                        "コマンドキーとCでパスをコピー、Eで編集、Sでセッションの条件、Fで読み取り先、Tで配置先トラック",
                        "Search results. Up and Down arrows move between "
                        "items, a letter key jumps to that letter, Space "
                        "previews, Return places. Command with C copies the "
                        "path, E edits, S opens Session Settings, F opens "
                        "Manage Sources, T chooses the destination track")
        # macOS では ListBox も SetName が読み上げ名にならない
        # （TextCtrl と同じ。詳細は ui/ax_label.py）
        label_control(self._result_list, _list_name)
        outer.Add(
            self._result_list, proportion=1,
            flag=wx.EXPAND | wx.ALL, border=8,
        )

        # --- 試聴の音量 -----------------------------------------------
        # **キー（コマンドと上下矢印）でも変えられる**が、Tab で辿って
        # 選べる場所も用意する。いまいくつなのかを確かめる手段にもなる
        self._volume_choice = wx.Choice(
            panel, choices=[tr(f"{v} パーセント",
                               f"{v} percent") for v in _VOLUME_STEPS])
        label_control(
            self._volume_choice,
            tr("試聴の音量。コマンドキーと上下矢印でも変えられる",
               "Preview volume. Command with the Up and Down arrows also "
               "changes it"))
        outer.Add(
            self._labeled_existing(panel, tr("試聴の音量",
                                             "Preview "
                                             "volume"), self._volume_choice),
            flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=8)
        self._sync_volume_choice()

        # --- 操作ボタン -----------------------------------------------
        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        self._preview_btn = wx.Button(panel, label=tr("試聴 再生/停止",
                                                      "Preview Play/Stop"))
        self._preview_btn.SetName(tr("選択中のサンプルを試聴 再生または停止",
                                     "Play or stop the preview of the "
                                     "selected sample"))
        self._import_btn = wx.Button(
            panel, label=tr("Pro Tools へ配置", "Place in Pro Tools"))
        self._import_btn.SetName(
            tr("選択中のサンプルを Pro Tools へ配置する。置き先は、コマンドとTで指定したトラックか、Pro Tools "
               "で選択中のトラック",
               "Place the selected sample in Pro Tools. It goes to the "
               "track chosen with Command-T, or the track selected in Pro "
               "Tools")
        )
        self._copy_btn = wx.Button(panel, label=tr("パスをコピー", "Copy Path"))
        self._copy_btn.SetName(
            tr("選択中のサンプルのファイルパスをクリップボードにコピー",
               "Copy the file path of the selected sample to the clipboard")
        )
        self._edit_btn = wx.Button(panel, label=tr("タグ・種別を編集",
                                                   "Edit Tags and Type"))
        self._edit_btn.SetName(
            tr("選択中のサンプルのタグ、カテゴリ、種別を編集する",
               "Edit the tags, category and type of the selected sample")
        )
        self._scan_btn = wx.Button(panel, label=tr("フォルダをスキャン",
                                                   "Scan Folders"))
        # **フォルダを選ばせるのは、読み取り先がひとつも無いときだけ**
        # （`_on_scan`）。以前の名前は「フォルダを選択し」のままだった
        self._scan_btn.SetName(
            tr("登録した読み取り先の中のオーディオファイルをライブラリに登録する。読み取り先がまだ無いときは、フォルダを選ぶ",
               "Add the audio files in your sources to the library. If "
               "there are no sources yet, you choose a folder")
        )
        self._session_btn = wx.Button(panel, label=tr("セッションの条件",
                                                      "Session Settings"))
        self._session_btn.SetName(
            tr("配置先セッションのテンポとキーを設定する。設定すると、配置のときに素材を移調・尺変更してから置く",
               "Set the tempo and key of the destination session. When set, "
               "samples are transposed and fitted before they are placed")
        )
        self._source_btn = wx.Button(panel, label=tr("読み取り先を管理",
                                                     "Manage Sources"))
        self._source_btn.SetName(
            tr("サンプルを読み取るフォルダを追加・削除する。Splice として登録するとパックごとに絞り込める",
               "Add or remove the folders samples are read from. Adding a "
               "folder as Splice lets you filter by pack")
        )
        for b in (
            self._preview_btn, self._import_btn, self._copy_btn,
            self._edit_btn, self._scan_btn, self._session_btn,
            self._source_btn,
        ):
            btn_row.Add(b, flag=wx.ALL, border=4)
        outer.Add(btn_row, flag=wx.ALL, border=4)

        # --- ステータス（読み上げ用） ---------------------------------
        self._status = wx.StaticText(panel, label=tr("準備完了", "Ready"))
        label_control(self._status, tr("状態表示", "Status"))
        outer.Add(self._status, flag=wx.EXPAND | wx.ALL, border=8)

        panel.SetSizer(outer)
        self._panel = panel

    def _make_text_ctrl(self, parent: wx.Window, name: str) -> wx.TextCtrl:
        """Enter で検索が走るよう TE_PROCESS_ENTER 付きの入力欄を作る。

        **macOS では `SetName` が入力欄の読み上げ名にならない。**
        実機のアクセシビリティ情報を見ると AXTitle も AXDescription も
        空のままで、VoiceOver には「テキストフィールド」としか伝わらず、
        タブで移動しても何の欄か分からなかった。

        `label_control` が下地の NSView へ直接名前を設定する
        （詳細は `ui/ax_label.py`）。検証は
        アクセシビリティの階層を書き出してできる。
        """
        ctrl = wx.TextCtrl(parent, style=wx.TE_PROCESS_ENTER)
        label_control(ctrl, name)
        return ctrl

    def _labeled(
        self, parent: wx.Window, label: str, ctrl: wx.Window, attr: str
    ) -> wx.BoxSizer:
        """ラベル + 入力を縦に積み、ctrl を self.<attr> に登録する。"""
        setattr(self, attr, ctrl)
        return self._labeled_existing(parent, label, ctrl)

    def _labeled_existing(
        self, parent: wx.Window, label: str, ctrl: wx.Window
    ) -> wx.BoxSizer:
        box = wx.BoxSizer(wx.VERTICAL)
        st = wx.StaticText(parent, label=label)
        box.Add(st, flag=wx.BOTTOM, border=2)
        box.Add(ctrl, flag=wx.EXPAND)
        return box

    # ------------------------------------------------------------------
    # イベント結線
    # ------------------------------------------------------------------
    def _bind_events(self) -> None:
        self._search_btn.Bind(wx.EVT_BUTTON, self._on_search)
        # 各テキスト欄で Enter → 検索
        for ctrl in (
            self._search_text, self._category_text, self._tags_text
        ):
            ctrl.Bind(wx.EVT_TEXT_ENTER, self._on_search)
        self._preview_btn.Bind(wx.EVT_BUTTON, self._on_preview)
        self._import_btn.Bind(wx.EVT_BUTTON, self._on_import)
        self._copy_btn.Bind(wx.EVT_BUTTON, self._on_copy)
        self._edit_btn.Bind(wx.EVT_BUTTON, self._on_edit)
        self._scan_btn.Bind(wx.EVT_BUTTON, self._on_scan)
        self._session_btn.Bind(wx.EVT_BUTTON, self._on_session)
        self._source_btn.Bind(wx.EVT_BUTTON, self._on_sources)
        # 読み取り先を変えたらパックの選択肢を入れ替える
        self._source_choice.Bind(wx.EVT_CHOICE, self._on_source_changed)
        # お気に入りの絞り込みは、切り替えたらそのまま検索し直す
        self._favorite_only.Bind(wx.EVT_CHECKBOX, self._on_favorite_only)
        self._volume_choice.Bind(wx.EVT_CHOICE, self._on_volume_choice)
        # **種別やチャンネル構成を変えたら、パックの選択肢も入れ替える。**
        # 条件に合うサンプルが無いパックを選択肢に残すと、選んでも 0 件で
        # 探せない
        self._kind_choice.Bind(wx.EVT_CHOICE, self._on_filter_changed)
        self._channel_choice.Bind(wx.EVT_CHOICE, self._on_filter_changed)
        # リストでの選択移動時は状態更新（describe_ja を読み上げ）
        self._result_list.Bind(wx.EVT_LISTBOX, self._on_select)
        # キー処理はパネルレベルの EVT_CHAR_HOOK で一括（per-button KEY_DOWN は
        # 使わない。IME 競合や取りこぼしを避ける既知の方針）。
        # グローバルなアクセラレータは使わず、結果リストにフォーカスが
        # ある時だけショートカットを発動する（検索欄への入力を奪わない）。
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        # 終了時に試聴を確実に停止する
        self.Bind(wx.EVT_CLOSE, self._on_close)

    # ------------------------------------------------------------------
    # ハンドラ
    # ------------------------------------------------------------------
    def _current_form(self) -> SearchForm:
        _, kind = _KIND_CHOICES[self._kind_choice.GetSelection()]
        _, channels = _CHANNEL_CHOICES[self._channel_choice.GetSelection()]
        return SearchForm(
            text=self._search_text.GetValue(),
            kind=kind,
            category=self._category_text.GetValue(),
            tags_csv=self._tags_text.GetValue(),
            channels=channels,
            source_id=self._selected_source_id(),
            pack=self._selected_pack(),
            favorite_only=self._favorite_only.GetValue(),
        )

    # -- 読み取り先とパックの選択肢 ------------------------------------
    def _selected_source(self):
        """選択中の読み取り先。先頭（すべて）なら None。"""
        index = self._source_choice.GetSelection()
        if index <= 0 or index > len(self._sources):
            return None
        return self._sources[index - 1]

    def _selected_source_id(self) -> int | None:
        """選択中の読み取り先 ID。先頭（すべて）なら None。"""
        source = self._selected_source()
        return source.id if source is not None else None

    def _selected_pack(self) -> str:
        """選択中のパック名。先頭（すべて）なら空文字。"""
        index = self._pack_choice.GetSelection()
        if index <= 0 or index > len(self._packs):
            return ""
        return self._packs[index - 1]

    def _reload_sources(self) -> None:
        """読み取り先の選択肢を作り直す。選択は可能なら保つ。"""
        keep = self._selected_source_id()
        self._sources = self._controller.list_sources()
        labels = [_ALL_SOURCES] + [
            tr(f"{s.name}（パックあり）", f"{s.name} (has packs)")
            if s.kind is SourceKind.SPLICE else s.name
            for s in self._sources
        ]
        self._source_choice.Set(labels)
        index = next(
            (i for i, s in enumerate(self._sources, 1) if s.id == keep), 0)
        self._source_choice.SetSelection(index)
        self._reload_packs()

    def _reload_packs(self) -> str:
        """パックの選択肢を作り直す。

        **読み取り先を選んでいればその中のパックだけを出す。**
        パックが無ければ選択肢は「すべてのパック」だけになる。

        **いまの検索条件に合うパックだけを出す。** 種別を「ループのみ」に
        しているのにワンショットしか無いパックが並ぶと、選んでも 0 件に
        なって探せない（実機で 527 件中 341 件がそうなっていた）。

        戻り値: 選んでいたパックが選択肢から外れたときの説明（保てたら
        空文字）。**黙って「すべてのパック」へ戻さない。** 絞り込みが
        外れたことに気づけないと、急に増えた検索結果の理由が分からない。
        """
        keep = self._selected_pack()
        counted = self._controller.list_packs_with_counts(
            self._selected_source_id(), self._current_form())
        self._packs = [name for name, _ in counted]
        # 件数を添える。Splice は 1 ファイル単位で落とすので
        # 1 件しか入っていないパックが大量にできる
        self._pack_choice.Set(
            [_ALL_PACKS] + [tr(f"{name}（{n}件）",
                               f"{name} ({n})") for name, n in counted])
        index = next(
            (i for i, p in enumerate(self._packs, 1) if p == keep), 0)
        self._pack_choice.SetSelection(index)
        self._pack_choice.Enable(bool(self._packs))
        if keep and index == 0:
            return tr(f"選んでいたパック「{keep}」は条件に合わないため、絞り込みを外しました",
                      f"The pack you chose, “{keep}”, does not match the "
                      "conditions, so the pack filter was cleared")
        return ""

    def _jump_to_source(self, index: int) -> None:
        """読み取り先を番号で切り替えて、そのまま検索し直す。

        0 は「すべての読み取り先」。**切り替えたら結果まで出す**ので、
        コマンドキーと数字ひとつで目的の素材群に辿り着ける。
        """
        if index >= self._source_choice.GetCount():
            # 選択肢の数には先頭の「すべての読み取り先」が入っている。
            # そのまま「いま N 件」と言うと、登録した数より 1 つ多く聞こえる
            registered = self._source_choice.GetCount() - 1
            if registered:
                tail = tr(f"登録した読み取り先は {registered} 件で、"
                          f"コマンドと2から{registered + 1}までです。"
                          "コマンドと1はすべての読み取り先です",
                          f"You have {plural(registered, 'source')}: "
                          "Command-2 and up, in the order you added them. "
                          "Command-1 is all sources")
            else:
                tail = tr("読み取り先はまだ登録されていません。コマンドとFで登録できます",
                          "No sources have been added yet. Add one with "
                          "Command-F")
            self._set_status(
                tr(f"コマンドと{index + 1}に当たる読み取り先はありません。{tail}",
                   f"There is no source for Command-{index + 1}. {tail}"),
                announce=True, sound="failure")
            return
        self._source_choice.SetSelection(index)
        self._reload_packs()
        name = self._source_choice.GetStringSelection()
        rows = self._refresh_results()
        summary = self._controller.search_result_summary_ja()
        self._set_status(
            tr(f"{name} に切り替えました。{summary}", f"Switched to {name}. {summary}"),
            announce=True, sound="success" if rows else "failure")

    def _focus_packs(self) -> None:
        """パックの選択肢へ移動する。

        パックは数が多く番号では選べないので、**そこへ飛べるようにする。**
        あとは矢印で選ぶ。
        """
        if not self._packs:
            self._set_status(
                tr("いまの読み取り先にはパックがありません",
                   "This source has no packs"), announce=True)
            return
        self._pack_choice.SetFocus()
        self._set_status(
            tr(f"パックの絞り込みへ移動しました。{len(self._packs)} 件あります。"
               "上下矢印で選んでください",
               "Moved to the pack filter. "
               f"{plural(len(self._packs), 'pack')} available. Choose with "
               "the Up and Down arrows"),
            announce=True)

    def _on_source_changed(self, _evt: wx.Event) -> None:
        """読み取り先を変えたらパックの選択肢を入れ替える。"""
        self._reload_packs()
        count = len(self._packs)
        name = self._source_choice.GetStringSelection()
        if count:
            self._set_status(tr(f"{name} に切り替えました。パック {count} 件",
                                f"Switched to {name}. "
                                f"{plural(count, 'pack')}"))
        else:
            self._set_status(tr(f"{name} に切り替えました", f"Switched to {name}"))

    def _refresh_results(self) -> list[str]:
        """いまの検索条件で一覧を作り直す。先頭を選んだ状態にして行を返す。"""
        rows = self._controller.search(self._current_form())
        self._result_list.Set(rows)
        if rows:
            self._result_list.SetSelection(0)
        return rows

    def _on_search(self, _evt: wx.Event) -> None:
        rows = self._refresh_results()
        # 見つかったかどうかを音でも伝える。0 件は失敗の音にして、
        # 読み上げを待たずに「条件を変える必要がある」と分かるようにする
        self._set_status(
            self._controller.search_result_summary_ja(),
            sound="success" if rows else "failure")

    def _on_select(self, _evt: wx.Event) -> None:
        idx = self._result_list.GetSelection()
        sample = self._controller.get_sample(idx)
        if sample is not None:
            self._set_status(sample.describe_ja())

    def _on_char_hook(self, evt: wx.KeyEvent) -> None:
        """パネルレベルのキー処理。

        - コマンドと数字・P・上下矢印（読み取り先・パック・音量）
        - ボタンの上の Enter（そのボタンを押す）
        - 結果リストにフォーカスがあり修飾キーなしの時のみ:
          Enter=配置 / スペース=試聴

        それ以外は evt.Skip() で通常処理（テキスト欄への空白入力や
        Enter による検索、リストの頭文字への移動など）を妨げない。
        """
        focused = wx.Window.FindFocus()

        # **読み取り先はコマンドキーと数字ですぐ切り替える。**
        # Tab で選択肢まで移動して矢印で選ぶのは手数が多い。
        # 1 は「すべての読み取り先」、2 以降が登録した順
        if evt.GetModifiers() == wx.MOD_CONTROL:
            key = evt.GetKeyCode()
            if ord("1") <= key <= ord("9"):
                self._jump_to_source(key - ord("1"))
                return
            if key == ord("P"):
                self._focus_packs()
                return
            # **試聴の音量は矢印で。** 一覧を辿りながら調整できるよう、
            # 手をリストから離さずに済ませる
            if key in (wx.WXK_UP, wx.WXK_DOWN):
                # **文字入力欄では標準の動きを奪わない**（コマンドと
                # 上下矢印は文頭・文末へのカーソル移動）
                if isinstance(focused, wx.TextCtrl):
                    evt.Skip()
                    return
                self._set_status(
                    self._controller.nudge_preview_volume(key == wx.WXK_UP),
                    announce=True)
                self._sync_volume_choice()
                return

        # **ボタンは Enter でも押せるようにする。** macOS の wx では
        # スペースでしか押せず、リターンキーで押せないボタンがある
        # （既定ボタン以外）。VoiceOver で辿ってきて押せないと戸惑うので、
        # ここでクリックに変換する（共通処理は ui/wx_keys.py）
        if (isinstance(focused, wx.Button)
                and evt.GetModifiers() == wx.MOD_NONE
                and evt.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER)):
            press_button(focused)
            return

        # **修飾なしの文字キーは横取りしない。** 結果リストでは頭文字を
        # 打って項目へ飛べる（リストの標準機能）。ここで C や S を
        # 拾ってしまうと、その文字で始まるサンプルへ飛べなくなる。
        # 機能のショートカットはメニューのアクセラレータ（Cmd 付き）に
        # 任せてある（`_build_menu`）。
        if focused is self._result_list and evt.GetModifiers() == wx.MOD_NONE:
            key = evt.GetKeyCode()
            if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
                self._on_import(evt)
                return  # 消費（既定の改行処理はさせない）
            if key == wx.WXK_SPACE:
                self._on_preview(evt)
                return
        evt.Skip()

    def _on_preview(self, _evt: wx.Event) -> None:
        idx = self._result_list.GetSelection()
        if idx == wx.NOT_FOUND:
            self._set_status(NO_SAMPLE)
            return
        self._set_status(self._controller.preview_toggle(idx))

    def _on_import(self, _evt: wx.Event) -> None:
        """選択サンプルを Pro Tools の選択トラックへ配置する（PTSL 主経路）。

        PTSL 通信は数秒かかり得るため、ワーカースレッドで実行して UI と
        VoiceOver の応答を止めない。押下時点の Sample を渡す（配置中に
        再検索されても index ずれの影響を受けないように）。
        """
        if self._placing:
            self._set_status(tr("配置を実行中です。完了をお待ちください。",
                                "Placement is in progress. Please wait for "
                                "it to finish."),
                             announce=True)
            return
        sample = self._controller.get_sample(self._result_list.GetSelection())
        if sample is None:
            self._set_status(NO_SAMPLE, announce=True)
            return
        self._placing = True
        # **ボタンは無効化しない。** フォーカスのあるボタンを無効化すると
        # フォーカスがよそへ移りかねない（セッション条件の試聴ボタンと
        # 同じ方針。ui/session_dialog.py）。多重実行は _placing で防ぎ、
        # 押し直されたら「実行中です」と読む
        # 配置は数秒かかり得るため、開始を先にアナウンスして無反応を避ける。
        # 短い音も鳴らして「受け付けた」ことを即座に伝える
        self._set_status(tr(f"Pro Tools へ配置しています… {sample.filename}",
                            f"Placing in Pro Tools… {sample.filename}"),
                         announce=True, sound="start")

        def worker() -> None:
            try:
                message = self._controller.place_sample_in_pro_tools(sample)
            except Exception as exc:  # 想定外でも UI は必ず復帰させる
                message = tr(f"[エラー] 配置中に予期しないエラー: {exc}",
                             f"[Error] Unexpected error while placing: {exc}")
            wx.CallAfter(self._on_import_done, message)

        threading.Thread(target=worker, daemon=True).start()

    def _on_import_done(self, message: str) -> None:
        if not self:  # ウィンドウ破棄後に届いた完了通知は無視
            return
        self._placing = False
        # 成否で音を変える。文言の先頭（[情報]/[警告]/[エラー]）から判断する
        self._set_status(message, announce=True, sound="auto")

    def _on_toggle_favorite(self, _evt: wx.Event) -> None:
        """選択中のサンプルをお気に入りに登録・解除する。

        **数千件の中から前に使った素材を探し直すのは手間。**
        印を付けておけば「お気に入りだけを表示」で呼び出せる。
        """
        index = self._result_list.GetSelection()
        message, row = self._controller.toggle_favorite(index)
        if row is None:
            self._set_status(message, announce=True)
            return

        sample = self._controller.get_sample(index)
        if (self._favorite_only.GetValue()
                and sample is not None and not sample.favorite):
            # **「お気に入りだけを表示」中に外した行は、その場で消す。**
            # 残しておくと、お気に入りだけの一覧に外れたものが混ざり、
            # 「解除できていない」ように見える
            self._controller.drop_result(index)
            self._result_list.Delete(index)
            count = self._result_list.GetCount()
            if count:
                self._result_list.SetSelection(min(index, count - 1))
            message += tr("。一覧から取り除きました", ". It was removed from the list")
        else:
            self._result_list.SetString(index, row)
            self._result_list.SetSelection(index)
        self._set_status(message, announce=True)

    def _sync_volume_choice(self) -> None:
        """音量の選択肢を、いまの値に合わせる。

        キーで変えたときも表示が食い違わないようにする。
        """
        percent = int(round(self._controller.preview_volume * 100))
        nearest = min(_VOLUME_STEPS, key=lambda v: abs(v - percent))
        self._volume_choice.SetSelection(_VOLUME_STEPS.index(nearest))

    def _on_volume_choice(self, _evt: wx.Event) -> None:
        """選択肢で音量を変える。"""
        index = self._volume_choice.GetSelection()
        if index < 0:
            return
        self._set_status(
            self._controller.set_preview_volume(_VOLUME_STEPS[index] / 100.0),
            announce=True)

    def _on_favorite_only(self, _evt: wx.Event) -> None:
        """絞り込みを切り替えたら、そのまま検索し直す。

        チェックしてから検索を押す手間を省く。
        パックの選択肢も条件に合わせて入れ替え、選んでいたパックが
        外れたときは検索結果と合わせて読み上げる（気づかないうちに
        全パックの検索へ広がると、件数が急に増えた理由が分からない）。
        """
        dropped = self._reload_packs()
        self._on_search(None)
        if dropped:
            summary = self._controller.search_result_summary_ja()
            self._set_status(
                tr(f"{dropped}。{summary}", f"{dropped}. {summary}"),
                announce=True)

    def _on_filter_changed(self, _evt: wx.Event) -> None:
        """種別やチャンネル構成を変えたら、パックの選択肢を入れ替える。

        **条件に合わないパックを残さない。** 選んでも 0 件になるものが
        並んでいると、探せているのか分からなくなる。
        件数も知らせる（選択肢が減ったことに気づけるように）。
        """
        before = len(self._packs)
        dropped = self._reload_packs()
        count = len(self._packs)
        if count == before and not dropped:
            return
        head = (tr(f"条件に合うパックは {count} 件になりました",
                   f"Packs that match the conditions: {count}") if count
                else tr("この条件に合うパックはありません",
                        "No packs match these conditions"))
        self._set_status(tr(f"{head}。{dropped}",
                            f"{head}. {dropped}") if dropped else head,
                         announce=True)

    def _on_edit(self, _evt: wx.Event) -> None:
        """選択サンプルのタグ・カテゴリ・種別を編集するダイアログを開く。"""
        idx = self._result_list.GetSelection()
        sample = self._controller.get_sample(idx)
        if sample is None:
            self._set_status(NO_SAMPLE, announce=True)
            return
        dlg = EditDialog(self, sample)
        try:
            if dlg.ShowModal() != wx.ID_OK:
                self._set_status(tr("編集をキャンセルしました", "Editing canceled"))
                return
            row = self._controller.apply_edits(
                idx, dlg.tags_csv, dlg.category, dlg.kind)
        finally:
            dlg.Destroy()
        if row is None:
            self._set_status(tr("保存できませんでした", "Could not save"), announce=True)
            return
        # 該当行だけ書き換え、選択とフォーカスは結果リストに保つ
        self._result_list.SetString(idx, row)
        self._result_list.SetSelection(idx)
        self._set_status(tr(f"保存しました。{row}", f"Saved. {row}"), announce=True)

    def _on_session(self, _evt: wx.Event) -> None:
        """配置先セッションの条件（テンポ・キー）を設定する。

        **Pro Tools からテンポとキーは取れない**（PTSL に問い合わせる手段が
        無い）ため、ここで入力してもらう。設定しておくと、配置のたびに
        素材を移調・尺変更してから置く。
        """
        # 選択中の素材から読めた BPM とキーを渡す。
        # **キーが未設定なら素材のキーが初期値になる**（入力の手間が減る）
        sample = self._controller.get_sample(self._result_list.GetSelection())
        music = (self._controller.sample_music(sample)
                 if sample is not None else None)
        dlg = SessionDialog(
            self,
            self._controller.session_settings,
            self._controller.describe_render_backend(),
            music,
            detect_tempo=self._controller.detect_session_tempo,
            # **置く前に耳で確かめられるようにする。** その場の入力値で
            # 加工して鳴らす（選択中のサンプルを使う）。
            # 鳴っているときに押せば止まる。加工（重い）をワーカー
            # スレッドで行えるよう、準備と再生を分けて渡す
            preview_prepare=lambda settings:
                self._controller.prepare_preview_with_settings(
                    sample, settings),
            preview_play=self._controller.play_prepared_preview,
            preview_stop_if_active=self._controller.stop_preview_if_active,
            preview_sample=sample.filename if sample is not None else "",
        )
        try:
            if dlg.ShowModal() != wx.ID_OK:
                self._set_status(tr("セッション条件の設定をキャンセルしました",
                                    "Session settings canceled"))
                return
            message = self._controller.update_session_settings(dlg.settings)
        finally:
            # **どの閉じ方でも試聴を止める。** Enter・Esc・キャンセル
            # ボタン・ウィンドウの閉じるボタン、どこから閉じても
            # 加工した音を鳴りっぱなしにしない
            try:
                self._controller.preview_stop()
            except Exception:
                pass
            dlg.Destroy()
        self._set_status(message, announce=True)

    _KEEP_PT_SELECTION = tr("Pro Tools で選んでいるトラック（指定しない）",
                            "The track selected in Pro Tools (no fixed "
                            "destination)")

    def _on_target_track(self, _evt: wx.Event) -> None:
        """配置先のトラックを選ぶ。

        **画面を見ずに Pro Tools でトラックを選ぶのは難しい。**
        選び損ねると「オーディオトラックが選ばれていません」で配置が
        失敗するので、ここで決めておけば Pro Tools 側の選択に
        左右されなくなる。
        """
        names, error = self._controller.list_audio_tracks()
        if error:
            self._set_status(error, announce=True, sound="auto")
            return

        choices = [self._KEEP_PT_SELECTION] + names
        current = self._controller.target_track
        index = names.index(current) + 1 if current in names else 0

        dlg = wx.SingleChoiceDialog(
            self,
            tr("配置先のトラックを選んでください", "Choose the destination track"),
            tr("配置先の指定", "Destination Track"), choices)
        try:
            dlg.SetSelection(index)
            if dlg.ShowModal() != wx.ID_OK:
                self._set_status(tr("配置先の指定をやめました",
                                    "Destination track unchanged"))
                return
            picked = dlg.GetStringSelection()
        finally:
            dlg.Destroy()

        chosen = "" if picked == self._KEEP_PT_SELECTION else picked
        self._set_status(self._controller.set_target_track(chosen),
                         announce=True)

    def _on_sources(self, _evt: wx.Event) -> None:
        """読み取り先フォルダを管理するダイアログを開く。

        Splice のライブラリと自前の素材フォルダを分けて登録しておくと、
        検索のときに切り替えて表示できる。
        """
        dlg = SourceDialog(self, self._controller)
        try:
            dlg.ShowModal()
            message = dlg.last_message
            changed = dlg.changed
        finally:
            dlg.Destroy()
        self._reload_sources()
        count = len(self._sources)
        parts = [message]
        if count:
            parts.append(tr(f"読み取り先は {count} 件です。", plural(count, "source")))
        if changed:
            # **外した読み取り先のサンプルを一覧に残さない。** 残ったまま
            # お気に入りや編集をすると、DB に無いサンプルを相手にして
            # 「追加しました」と嘘を読み上げる
            self._refresh_results()
            parts.append(self._controller.search_result_summary_ja())
        self._set_status(sentences(parts)
                         or tr("読み取り先の管理を閉じました", "Manage Sources closed"),
                         announce=True)

    def _on_scan(self, _evt: wx.Event) -> None:
        """フォルダをスキャンする。

        **登録済みの読み取り先があれば、フォルダ選択画面を出さない。**
        再スキャンのたびにフォルダを選び直すのは手間なので、

          - 読み取り先を選んでいる  → そのフォルダだけを走査
          - 「すべての読み取り先」  → 登録済みを**全部**走査
          - ひとつも登録がない      → そのときだけフォルダ選択を出す

        という順で決める。
        """
        if self._scanning:
            self._set_status(tr("スキャンを実行中です。完了をお待ちください。",
                                "A scan is in progress. Please wait for it "
                                "to finish."),
                             announce=True)
            return

        source = self._selected_source()
        if source is not None:
            targets = [source]
        elif self._sources:
            # 登録済みを全部。**選び直させない**
            targets = list(self._sources)
        else:
            folder = self._ask_folder()
            if folder is None:
                return
            targets = [None]

        # ボタンは無効化しない（理由は _on_import と同じ。_scanning で防ぐ）
        self._scanning = True
        if targets == [None]:
            where = folder
        elif len(targets) == 1:
            where = tr(f"「{targets[0].name}」", f"“{targets[0].name}”")
        else:
            where = tr(f"登録済みの読み取り先 {len(targets)} 件",
                       f"{plural(len(targets), 'added source')}")
        self._set_status(tr(f"スキャンしています… {where}", f"Scanning… {where}"),
                         announce=True, sound="start")

        def run_scan() -> str:
            try:
                if targets == [None]:
                    return self._controller.scan_isolated(
                        folder, None).describe_ja()
                return self._controller.scan_sources(targets)
            except Exception as exc:  # スキャン失敗でも UI は必ず復帰させる
                return tr(f"[エラー] スキャンに失敗しました: {exc}",
                          f"[Error] The scan failed: {exc}")

        if self._controller.db_path == ":memory:":
            # インメモリ DB は別スレッドから触れないため同期実行（テスト用）
            self._on_scan_done(run_scan())
            return

        threading.Thread(
            target=lambda: wx.CallAfter(self._on_scan_done, run_scan()),
            daemon=True,
        ).start()

    def _ask_folder(self) -> str | None:
        """フォルダ選択ダイアログ。選ばなければ None。

        **読み取り先がひとつも登録されていないときだけ使う。**
        """
        dlg = wx.DirDialog(
            self,
            message=tr("スキャンするサンプルフォルダを選択", "Choose a sample folder to scan"),
            style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST,
        )
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return None
            return dlg.GetPath()
        finally:
            dlg.Destroy()

    def _on_scan_done(self, message: str) -> None:
        if not self:  # ウィンドウ破棄後に届いた完了通知は無視
            return
        self._scanning = False
        # スキャンで新しいパックが増えている可能性がある
        self._reload_packs()
        # 現在の検索条件で一覧を更新してから、件数も含めて読み上げる
        self._refresh_results()
        self._set_status(
            sentences([message, self._controller.search_result_summary_ja()]),
            announce=True, sound="auto",
        )

    def _on_copy(self, _evt: wx.Event) -> None:
        # **文字入力欄では標準のコピーを奪わない。** メニューの Cmd+C は
        # フォーカスがどこでも届いてしまうため、検索欄で選択した文字を
        # コピーしたつもりがサンプルのパスに化ける。入力欄にいるときは
        # その欄の選択文字をコピーする
        focused = wx.Window.FindFocus()
        if isinstance(focused, wx.TextCtrl):
            if focused.CanCopy():
                focused.Copy()
                self._set_status(tr("選択した文字をコピーしました",
                                    "Copied the selected text"))
            else:
                self._set_status(tr("コピーする文字が選択されていません",
                                    "No text is selected to copy"))
            return

        idx = self._result_list.GetSelection()
        path = self._controller.get_path_for_copy(idx)
        if path is None:
            self._set_status(NO_SAMPLE)
            return
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(path))
            wx.TheClipboard.Close()
            self._set_status(tr(f"パスをコピーしました: {path}",
                                f"Copied the path: {path}"))
        else:
            self._set_status(tr("クリップボードを開けませんでした",
                                "Could not open the clipboard"))

    def _on_close(self, evt: wx.CloseEvent) -> None:
        """終了時に試聴を止めてから閉じる（再生の残留防止）。

        **後片付けで止まらないようにする。** 試聴の停止が固まっても
        ウィンドウは閉じる（閉じられないほうが困る）。
        """
        try:
            self._controller.preview_stop()
        except Exception:
            pass
        # **テンポとキーを次回へ持ち越さない。** 別のセッションを開いたまま
        # 前回のテンポで配置すると、素材が違う倍率で伸縮される
        try:
            self._controller.clear_session_tempo()
        except Exception:
            pass
        self.Destroy()

    # ------------------------------------------------------------------
    # 起動時の初期表示
    # ------------------------------------------------------------------
    def _initial_populate(self) -> None:
        """起動直後に登録済みサンプルを全件表示する。

        検索操作をしなくても、起動→Tab→矢印キーですぐ一覧を
        閲覧できるようにするため。未登録ならスキャンへの導線を伝える。
        """
        self._reload_sources()
        rows = self._controller.search(SearchForm())
        self._result_list.Set(rows)
        if rows:
            self._result_list.SetSelection(0)
            self._set_status(
                tr(f"起動しました。登録サンプル{len(rows)}件を表示しています。",
                   f"Started. Showing {plural(len(rows), 'sample')}.")
            )
        else:
            # 読み取り先を先に登録する流れを案内する（マニュアルと同じ順）。
            # 登録しておくと、読み取り先での絞り込みと再スキャンが使える
            self._set_status(
                tr("起動しました。サンプルが未登録です。コマンドとFで読み取り先のフォルダを登録してから、"
                   "コマンドとRでスキャンしてください。",
                   "Started. There are no samples yet. Add a source folder "
                   "with Command-F, then scan with Command-R.")
            )

    def _set_status(self, text: str, announce: bool = False,
                    sound: str | None = None) -> None:
        """ステータス文言を更新する。

        announce=True のときは、フォーカス移動を伴わない重要な状態変化
        （配置結果など）を能動的に SR へアナウンスする。通常の選択移動等は
        wx のフォーカス読み上げに任せ、二重読みを避けるため announce=False。

        sound には "success" / "failure" / "start" を渡す。**読み上げより
        先に鳴らす**（音が読み上げに重なると聞き取りにくいため）。
        `"auto"` を渡すと文言の先頭（[エラー]/[警告]）から判断する。
        """
        self._status.SetLabel(text)
        if sound == "auto":
            sounds.play_for_message(text)
        elif sound:
            sounds.play(sound)
        if announce:
            from ui.announce import announce as sr_announce

            sr_announce(text)
