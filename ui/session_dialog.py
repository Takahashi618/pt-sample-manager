"""配置先セッションの条件を入力するダイアログ。VoiceOver/PC-Talker 前提。

**Pro Tools からテンポとキーは取れない。** PTSL に問い合わせる手段が
無いため、ここで一度入力してもらう。入力しておくと、配置のたびに
素材がセッションへ合わせられる（移調とタイムストレッチ）。

アクセシビリティ設計は edit_dialog と同方針:
- 各入力に StaticText ラベルを併記し、SetName にも説明を入れる。
- ボタンは標準の OK / キャンセル（Enter=OK、Esc=キャンセル）。
- 入力が不正なら OK で閉じず、理由を読み上げてその欄へ戻す。
"""

from __future__ import annotations

import threading
from dataclasses import replace

import wx

from core.i18n import tr
from ui.ax_label import label_control

from core.session_settings import (
    COMMON_RATES,
    KEY_NONE,
    KEY_QUALITIES,
    KEY_ROOTS,
    SessionSettings,
    compose_key,
    describe_key_ja,
    parse_bpm,
    parse_copies,
    parse_every_bars,
    parse_key,
    parse_position,
    split_key,
)
from ui import announce as _announce_module
from ui.wx_keys import handle_dialog_enter_escape


class SessionDialog(wx.Dialog):
    """テンポ・キー・サンプルレート・自動加工の有無を編集する。

    OK で閉じた後、`settings` プロパティから値を取り出して
    `AppController.update_session_settings` に渡す。
    """

    def __init__(self, parent: wx.Window, settings: SessionSettings,
                 backend_note: str = "", sample_music=None,
                 detect_tempo=None, preview_prepare=None,
                 preview_play=None, preview_stop_if_active=None,
                 preview_sample: str | None = None) -> None:
        """preview_prepare / preview_play は対で渡す。

        - preview_prepare(settings): 加工までの準備。**ワーカースレッド
          から呼ばれる**（数秒かかるため）
        - preview_play(prepared): 準備の結果を鳴らす。UI スレッドで呼ぶ
        - preview_stop_if_active(): 鳴っていれば止めて True を返す
        - preview_sample: 試聴するサンプルの名前。**空文字なら選ばれて
          いない**（押されたら、加工を始めずにその旨を言う）。None は
          「分からない」で、従来どおり準備に任せる
        """
        super().__init__(parent, title=tr("配置先セッションの条件",
                                          "Destination Session Settings"))
        self._error = ""
        self._detect_tempo = detect_tempo
        self._preview_prepare = preview_prepare
        self._preview_play = preview_play
        self._preview_stop_if_active = preview_stop_if_active
        self._preview_sample = preview_sample
        self._can_preview = (preview_prepare is not None
                             and preview_play is not None)
        self._previewing = False   # 加工の実行中フラグ（連打での多重防止）
        # **編集しない設定（試聴の音量など）を引き継ぐための元の値。**
        # settings プロパティはこれを土台に差分だけ上書きして返す
        self._given = settings
        outer = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(self, label=tr("素材をセッションに合わせて加工してから配置します。"
                                             "空欄にすると、その項目では合わせません。",
                                             "Samples are processed to "
                                             "match the session before they "
                                             "are placed. Leave an item "
                                             "empty to skip matching it."))
        intro.SetName(tr("説明", "Description"))
        outer.Add(intro, flag=wx.ALL, border=8)

        # 選択中の素材から読めた情報。**設定の手がかりになる**ので先に出す
        music_note = ""
        if sample_music is not None and getattr(sample_music, "known", False):
            music_note = sample_music.describe_ja()
        if music_note:
            hint = wx.StaticText(self, label=tr(f"選択中の素材: {music_note}",
                                                "Selected sample: "
                                                f"{music_note}"))
            hint.SetName(tr(f"選択中の素材の情報。{music_note}",
                            f"Selected sample information. {music_note}"))
            outer.Add(hint, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=8)

        # テンポ
        self._bpm = wx.TextCtrl(
            self, value=f"{settings.bpm:g}" if settings.has_tempo else "")
        label_control(self._bpm, tr("セッションのテンポ。数値。空欄で尺を変えない",
                                    "Session tempo. A number. Leave empty "
                                    "to keep the length"))
        outer.Add(
            self._labeled(tr("セッションのテンポ（空欄で尺を変えない）",
                             "Session tempo (empty keeps the length)"),
                          self._bpm),
            flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=8)

        # **手入力だと間違えたまま気づけない。**
        # 実機で 160 BPM のセッションに 130 と入っていたことがあり、
        # 素材が違う倍率で伸縮されてメトロノームと合わなくなっていた
        if detect_tempo is not None:
            self._fetch_btn = wx.Button(
                self, label=tr("Pro Tools からテンポを取得",
                               "Get Tempo from Pro Tools"))
            label_control(
                self._fetch_btn,
                tr("いま開いているセッションのテンポを読み取って上の欄に入れる",
                   "Read the tempo of the open session into the field above"))
            self._fetch_btn.Bind(wx.EVT_BUTTON, self._on_fetch_tempo)
            outer.Add(self._fetch_btn,
                      flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=8)

        # 取得結果・試聴結果・キーの変更を書く場所。**常に作る**
        # （どのボタンが有るかに関わらず、状態を書き出す先が要る）
        self._fetch_note = wx.StaticText(self, label="")
        label_control(self._fetch_note, tr("設定の状態", "Settings status"))
        outer.Add(self._fetch_note,
                  flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM,
                  border=8)

        # キー。**未設定なら選択中の素材のキーを初期値にする。**
        # セッションのキーが決まっていないとき、素材のキーが出発点になる
        key_value = settings.key
        if not key_value and sample_music is not None:
            key_value = getattr(sample_music, "key", "") or ""
        root, minor = split_key(key_value)

        # **文字で打たせない。** 「Am」「C#maj」の書式を覚えるのも、
        # 打ち間違いに気づくのも、画面を見ずには難しい。
        # 選択肢なら上下矢印で選べて、読み上げも確実になる
        self._key_root = wx.Choice(
            self, choices=[KEY_NONE] + list(KEY_ROOTS))
        label_control(
            self._key_root,
            tr("セッションのキーの音。上下矢印で選ぶ。左右矢印でメジャーとマイナーを切り替える",
               "Session key note. Choose with the Up and Down arrows. The "
               "Left and Right arrows switch between major and minor"))
        self._key_root.SetSelection(
            KEY_ROOTS.index(root) + 1 if root in KEY_ROOTS else 0)
        outer.Add(
            self._labeled(tr("セッションのキー（上下矢印で選ぶ）",
                             "Session key (choose with the Up and Down "
                             "arrows)"),
                          self._key_root),
            flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=8)

        self._key_quality = wx.Choice(
            self, choices=[label for label, _ in KEY_QUALITIES])
        label_control(
            self._key_quality,
            tr("メジャーかマイナーか。上下矢印で選ぶ。キーの音の欄で左右矢印でも切り替えられる",
               "Major or minor. Choose with the Up and Down arrows. The "
               "Left and Right arrows in the key note field also switch it"))
        self._key_quality.SetSelection(1 if minor else 0)
        outer.Add(
            self._labeled(tr("メジャー・マイナー（左右矢印でも切り替え）",
                             "Major or minor (Left and Right arrows also "
                             "switch)"),
                          self._key_quality),
            flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=8)

        # サンプルレート
        self._rates = list(COMMON_RATES)
        self._rate = wx.Choice(
            self, choices=[f"{r} Hz" for r in self._rates])
        label_control(self._rate, tr("セッションのサンプルレートの選択",
                                     "Session sample rate selection"))
        try:
            self._rate.SetSelection(self._rates.index(settings.sample_rate))
        except ValueError:
            self._rate.SetSelection(0)
        outer.Add(
            self._labeled(tr("セッションのサンプルレート",
                             "Session sample rate"), self._rate),
            flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=8)

        # 自動加工
        self._auto = wx.CheckBox(self, label=tr("配置のときに自動で加工する",
                                                "Process automatically "
                                                "when placing"))
        label_control(self._auto, tr("配置のときに自動で加工するかどうか",
                                     "Whether to process automatically "
                                     "when placing"))
        self._auto.SetValue(settings.auto_render)
        outer.Add(self._auto,
                  flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=8)

        # --- 配置のしかた ---------------------------------------------
        place_box = wx.StaticBoxSizer(
            wx.VERTICAL, self, label=tr("配置のしかた", "How to Place"))

        self._at_cursor = wx.CheckBox(
            self, label=tr("編集カーソルの位置に置く", "Place at the edit cursor"))
        label_control(
            self._at_cursor,
            tr("編集カーソルの位置に置くかどうか。外すと下の小節・拍で位置を指定する",
               "Whether to place at the edit cursor. If off, the position "
               "below sets the bar and beat"))
        self._at_cursor.SetValue(settings.at_cursor)
        place_box.Add(self._at_cursor, flag=wx.EXPAND | wx.ALL, border=4)

        # **既定でオン。** 画面を見ずにカーソルを拍の上へ置くのは難しく、
        # 外れた位置に置くとメトロノームと合わない（実機で確認:
        # 拍の半分ずれると 187 ミリ秒）
        self._snap = wx.CheckBox(self, label=tr("カーソルの位置を拍に合わせる",
                                                "Snap the cursor position "
                                                "to the beat"))
        label_control(
            self._snap,
            tr("カーソルが拍から外れていたら、いちばん近い拍に合わせて置く。外すとカーソルの位置にそのまま置く",
               "If the cursor is off the beat, place at the nearest beat. "
               "If off, place exactly at the cursor"))
        self._snap.SetValue(settings.snap_to_beat)
        place_box.Add(self._snap, flag=wx.EXPAND | wx.ALL, border=4)

        self._position = wx.TextCtrl(self, value=settings.position)
        label_control(
            self._position,
            tr("配置する位置。小節、拍、ティックを縦棒で区切る。例 17|1|000",
               "Position to place at. Separate bar, beat and tick with "
               "vertical bars. Example: 17|1|000"))
        place_box.Add(
            self._labeled(tr("位置（例 17|1|000）",
                             "Position (example: 17|1|000)"), self._position),
            flag=wx.EXPAND | wx.ALL, border=4)

        self._copies = wx.TextCtrl(self, value=str(settings.copies))
        label_control(self._copies, tr("並べる個数。1 なら 1 個だけ置く",
                                       "Number of copies. 1 places a "
                                       "single copy"))
        place_box.Add(
            self._labeled(tr("並べる個数", "Number of copies"), self._copies),
            flag=wx.EXPAND | wx.ALL, border=4)

        self._every_bars = wx.TextCtrl(self, value=f"{settings.every_bars:g}")
        label_control(
            self._every_bars,
            tr("並べる間隔の小節数。テンポの設定が要る",
               "Interval between copies, in bars. Needs a tempo"))
        place_box.Add(
            self._labeled(tr("間隔（小節。テンポの設定が要る）",
                             "Interval (bars; needs a tempo)"),
                          self._every_bars),
            flag=wx.EXPAND | wx.ALL, border=4)

        outer.Add(place_box,
                  flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=8)

        # **置く前に耳で確かめる。** テンポやキーを変えても、配置して
        # みるまで結果が分からないのは怖い。同じ加工を通して鳴らす
        if self._can_preview:
            self._preview_btn = wx.Button(
                self, label=tr("この条件で試聴 再生/停止",
                               "Preview with These Settings Play/Stop"))
            label_control(
                self._preview_btn,
                tr("いま入力している条件で選択中のサンプルを加工して鳴らす。鳴っているときは止める。"
                   "コントロールキーとシフトとLでも操作できる。加工に数秒かかることがある",
                   "Process the selected sample with these settings and "
                   "play it. If it is playing, stop it. Control-Shift-L "
                   "does the same. Processing can take a few seconds"))
            self._preview_btn.Bind(wx.EVT_BUTTON, self._on_preview)
            outer.Add(self._preview_btn,
                      flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=8)

        if backend_note:
            note = wx.StaticText(self, label=backend_note)
            note.SetName(tr("加工に使う道具の説明", "The tool used for processing"))
            outer.Add(note, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=8)

        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        outer.Add(buttons, flag=wx.ALL | wx.ALIGN_RIGHT, border=8)

        self.SetSizerAndFit(outer)
        self.SetMinSize((460, -1))
        self._bpm.SetFocus()

        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)
        # **Enter で決定、Esc で戻る。** 選択肢（Choice）にフォーカスが
        # あると Enter が既定ボタンへ届かないことがある（実機で
        # 「決定できない」と指摘を受けた）ため、EVT_CHAR_HOOK で確実に
        # 拾う。Affirmative/EscapeId は wx_keys が押すボタンを探すのに使う
        self.SetAffirmativeId(wx.ID_OK)
        self.SetEscapeId(wx.ID_CANCEL)
        # キーの左右矢印・試聴のトグル・決定・取り消しをここで一括して捌く
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)

    def _labeled(self, label: str, ctrl: wx.Window) -> wx.BoxSizer:
        box = wx.BoxSizer(wx.VERTICAL)
        box.Add(wx.StaticText(self, label=label), flag=wx.BOTTOM, border=2)
        box.Add(ctrl, flag=wx.EXPAND)
        return box

    def _on_preview(self, _evt: wx.Event) -> None:
        """いま入力している条件で試聴する（鳴っていれば止める）。

        **入力が不正なままでは鳴らさない。** 打ち間違えたテンポで
        加工した音を聞いても意味がないので、先に確かめる。

        加工（ffmpeg）は数秒かかることがあるため、**準備はワーカー
        スレッドで行い、UI と VoiceOver の応答を止めない**（配置と
        同じ方針）。再生の開始は UI スレッドへ戻ってから行う。
        """
        if self._previewing:
            # 加工の実行中。連打で多重に走らせない。**黙って無視しない**
            # （押したのに何も起きないと、効いていないと思われる）
            message = tr("加工しています。しばらくお待ちください", "Processing. Please wait")
            self._fetch_note.SetLabel(message)
            _announce_module.announce(message)
            return
        # 鳴っているなら止めるだけ。**「加工しています」とは言わない**
        # （止めたいだけの人に待機を指示すると混乱する）
        if (self._preview_stop_if_active is not None
                and self._preview_stop_if_active()):
            message = tr("[情報] 試聴を止めました", "[Info] Preview stopped")
            self._fetch_note.SetLabel(message)
            _announce_module.announce(message)
            return

        # **鳴らすものが無いのに「加工しています」と言わない。** 以前は
        # 待つように言ったあとで「選択されていません」と読んでいた
        if self._preview_sample == "":
            message = tr("[警告] 試聴するサンプルが選ばれていません。一覧でサンプルを選んでから、もう一度開いてください",
                         "[Warning] No sample is selected to preview. "
                         "Choose a sample in the list, then open this again")
            self._fetch_note.SetLabel(message)
            _announce_module.announce(message)
            return

        bpm, bpm_error = parse_bpm(self._bpm.GetValue())
        if bpm_error:
            self._reject(bpm_error, self._bpm)
            return

        # コントロールの読み取りは UI スレッドで済ませてから渡す
        settings = self.settings
        if settings.can_render:
            # 加工は数秒かかる。**先に受け付けたことを伝える**
            # （加工しないときは一瞬で鳴るので何も言わない）
            self._fetch_note.SetLabel(tr("加工しています…", "Processing…"))
            _announce_module.announce(tr("加工しています。しばらくお待ちください",
                                         "Processing. Please wait"))
        # **ボタンは無効化しない。** フォーカスのあるボタンを無効化すると
        # フォーカスがよそへ移りかねず、加工が終わって Enter を押したとき
        # 別のボタン（キャンセル等）を押してしまう。多重実行は上のフラグで防ぐ
        self._previewing = True

        def worker() -> None:
            try:
                prepared = self._preview_prepare(settings)
                error = ""
            except Exception as exc:  # 想定外でも UI は必ず復帰させる
                prepared = None
                error = tr(f"[エラー] 試聴の準備に失敗しました（{exc}）。",
                           f"[Error] Could not prepare the preview ({exc}).")
            wx.CallAfter(self._on_preview_ready, prepared, error)

        threading.Thread(target=worker, daemon=True).start()

    def _on_preview_ready(self, prepared, error: str) -> None:
        """準備が終わったら UI スレッドで鳴らして結果を伝える。"""
        if not self:  # ダイアログが先に閉じられていたら何もしない
            return
        self._previewing = False
        message = error or self._preview_play(prepared)
        self._fetch_note.SetLabel(message)
        _announce_module.announce(message)

    def _on_char_hook(self, evt: wx.KeyEvent) -> None:
        """ダイアログ内のキー操作をまとめて捌く。

        - **エンター**: ボタンの上ならそのボタンを押す。それ以外は決定
        - **エスケープ**: 何も変えずに戻る
        - **コントロールとシフトとL**: 試聴の再生・停止
        - キーの欄で**左右矢印**: メジャーとマイナーの切り替え

        エンターとエスケープの共通処理は `ui/wx_keys.py`（選択肢に
        フォーカスがあると既定ボタンへ届かないことがあるため、ここで
        確実に拾う）。試聴の停止は閉じ方によらず呼び出し側が行う
        （`main_frame._on_session`）。
        """
        key = evt.GetKeyCode()
        mods = evt.GetModifiers()

        # 試聴のトグル。ここに至るまでに 2 度ぶつかっている:
        #   スペース単独        ボタンやチェックの操作を奪う
        #   コマンドとスペース  Spotlight がシステム側で横取りする
        #   コマンドとL         利用者が別のショートカットに使っていた
        # **`MOD_RAW_CONTROL` が macOS の Ctrl。** `MOD_CONTROL` は
        # macOS では Cmd に読み替えられるので、ここでは使えない
        if (mods == (wx.MOD_RAW_CONTROL | wx.MOD_SHIFT)
                and key == ord("L")):
            if self._can_preview:
                self._on_preview(None)
            return

        if mods == wx.MOD_NONE and wx.Window.FindFocus() is self._key_root:
            # 左でメジャー、右でマイナー（並びと同じ向き）
            if key in (wx.WXK_LEFT, wx.WXK_RIGHT):
                self._key_quality.SetSelection(
                    0 if key == wx.WXK_LEFT else 1)
                self._announce_key()
                return

        if handle_dialog_enter_escape(self, evt):
            return

        evt.Skip()

    def _announce_key(self) -> None:
        """いまのキーを読み上げる。"""
        text = describe_key_ja(self._current_key())
        self._fetch_note.SetLabel(tr(f"キー: {text}", f"Key: {text}"))
        _announce_module.announce(tr(f"キー {text}", f"Key {text}"))

    def _current_key(self) -> str:
        """選択中のキーを設定用の文字列にする（"Am" / "A" / ""）。"""
        index = self._key_root.GetSelection()
        if index <= 0:
            return ""
        root = KEY_ROOTS[index - 1]
        return compose_key(root, self._key_quality.GetSelection() == 1)

    def _on_fetch_tempo(self, _evt: wx.Event) -> None:
        """Pro Tools から実際のテンポを読み取ってテンポ欄に入れる。

        **手で入れた値が実際と違うと、素材が違う倍率で伸縮される。**
        結果は文言でも伝える（読み上げのため）。
        """
        tempo, error = self._detect_tempo()
        if error:
            self._fetch_note.SetLabel(error)
            wx.MessageBox(error, tr("テンポを取得できません", "Cannot Get the Tempo"),
                          wx.OK | wx.ICON_WARNING, self)
            return
        self._bpm.SetValue(f"{tempo:g}")
        message = tr(f"[情報] セッションのテンポは {tempo:g} でした。欄に入れました。",
                     f"[Info] The session tempo is {tempo:g}. It was entered "
                     "in the field.")
        self._fetch_note.SetLabel(message)
        self._bpm.SetFocus()
        wx.MessageBox(message, tr("テンポを取得しました", "Tempo Read"),
                      wx.OK | wx.ICON_INFORMATION, self)

    # ------------------------------------------------------------------
    # 検証
    # ------------------------------------------------------------------
    def _on_ok(self, _evt: wx.Event) -> None:
        """OK で閉じる。検証を通ってから閉じる。

        Enter からもボタンからもここへ来る（`_on_char_hook` が Enter を
        OK ボタンの押下に変換する）。
        """
        if self._validate():
            self.EndModal(wx.ID_OK)

    def _validate(self) -> bool:
        """入力を確かめる。問題なければ True。

        **不正なまま閉じない。** 打ち間違えたテンポで配置すると、
        気づかないうちに全部の素材が違う尺で置かれてしまう。
        """
        _, bpm_error = parse_bpm(self._bpm.GetValue())
        if bpm_error:
            self._reject(bpm_error, self._bpm)
            return False
        _, key_error = parse_key(self._current_key())
        if key_error:
            self._reject(key_error, self._key_root)
            return False

        # 位置はカーソルに置かないときだけ確かめる
        if not self._at_cursor.GetValue():
            _, position_error = parse_position(self._position.GetValue())
            if position_error:
                self._reject(position_error, self._position)
                return False

        copies, copies_error = parse_copies(self._copies.GetValue())
        if copies_error:
            self._reject(copies_error, self._copies)
            return False
        _, bars_error = parse_every_bars(self._every_bars.GetValue())
        if bars_error:
            self._reject(bars_error, self._every_bars)
            return False

        # 並べるにはテンポが要る（間隔を小節で数えるため）
        bpm, _ = parse_bpm(self._bpm.GetValue())
        if copies > 1 and bpm is None:
            self._reject(
                tr("2 個以上並べるにはセッションのテンポが要ります。テンポを入力するか、個数を 1 にしてください。",
                   "Placing 2 or more copies needs the session tempo. "
                   "Enter a tempo, or set the number of copies to 1."),
                self._bpm)
            return False

        self._error = ""
        return True

    def _reject(self, message: str, focus_on: wx.Window) -> None:
        self._error = message
        wx.MessageBox(message, tr("入力を確認してください", "Check Your Entry"),
                      wx.OK | wx.ICON_WARNING, self)
        focus_on.SetFocus()

    @property
    def error(self) -> str:
        """直近の検証エラー。テストと読み上げに使う。"""
        return self._error

    # ------------------------------------------------------------------
    # 入力値の取り出し（OK 後に呼ぶ）
    # ------------------------------------------------------------------
    @property
    def settings(self) -> SessionSettings:
        """入力値を SessionSettings にまとめる。

        **このダイアログで編集しない項目（試聴の音量など）は、渡された
        設定の値をそのまま引き継ぐ。** まっさらに作って返すと、触って
        いない項目が既定値へ戻る（実機で、開いて OK しただけで試聴の
        音量が 70% に戻っていた）。`replace` を使えば、あとから
        SessionSettings に項目が増えても同じ事故が起きない。
        """
        bpm, _ = parse_bpm(self._bpm.GetValue())
        key, _ = parse_key(self._current_key())
        index = self._rate.GetSelection()
        rate = self._rates[index] if 0 <= index < len(self._rates) \
            else self._rates[0]
        position, position_error = parse_position(self._position.GetValue())
        copies, _ = parse_copies(self._copies.GetValue())
        every_bars, _ = parse_every_bars(self._every_bars.GetValue())
        return replace(
            self._given,
            bpm=bpm,
            key=key,
            sample_rate=rate,
            auto_render=self._auto.GetValue(),
            at_cursor=self._at_cursor.GetValue(),
            snap_to_beat=self._snap.GetValue(),
            position="1|1|000" if position_error else position,
            copies=copies,
            every_bars=every_bars,
        )
