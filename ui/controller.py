"""アプリケーションコントローラ（非GUI）。

GUI（wx）とドメイン（core/audio）の間を仲介する。
wx に一切依存しないため、検索条件の組み立て・結果整形・状態管理を
GUI 起動なしでユニットテストできる。

責務:
- 検索フォーム入力（文字列）を core.library.search の引数へ変換
- 検索結果を「ListBox に出す行文字列」と「Sample 本体」に対応づけて保持
- 選択中サンプルに対する試聴・パスコピー・読込指示の仲介
- 配置先セッションの条件（テンポ・キー）の保持と、配置前の加工の指示
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from core.i18n import COMMA, is_english, plural, tr
from audio.player import PreviewPlayer
from core.library import Library
from core.models import Sample, SampleKind, Source, SourceKind
from core.scanner import ScanResult, looks_like_splice, scan_folder
from core.session_settings import (
    VOLUME_STEP,
    SessionSettings,
    describe_volume_ja,
    parse_volume,
)
from core.version import BUNDLE_NAME


# 何度も出てくる文言
NO_SAMPLE = tr("サンプルが選択されていません", "No sample is selected")
_NO_SESSION = tr("[警告] セッションが開かれていません。Pro Tools でセッションを開いてください。",
                 "[Warning] No session is open. Open a session in Pro Tools.")
_NO_PREVIEW = tr("この環境では試聴できません", "Preview is not available here")


def _cannot_load_ptsl(exc: Exception) -> str:
    return tr(f"[エラー] PTSL 連携を読み込めません（{exc}）。",
              f"[Error] Cannot load the Pro Tools connection ({exc}).")


def _cannot_connect(exc: Exception) -> str:
    return tr(f"[エラー] Pro Tools に接続できません（{exc}）。Pro Tools が起動しているか確認してください。",
              f"[Error] Cannot connect to Pro Tools ({exc}). Check that Pro "
              "Tools is running.")


def _cannot_load_render(exc: Exception) -> str:
    return tr(f"[エラー] 加工の部品を読み込めません（{exc}）。",
              f"[Error] Cannot load the audio processing parts ({exc}).")


def _file_missing(name: str) -> str:
    return tr(f"[エラー] ファイルが見つかりません: {name}。移動または削除されていないか確認してください。",
              f"[Error] The file was not found: {name}. Check whether it was "
              "moved or deleted.")


@dataclass
class SearchForm:
    """検索フォームの入力値。空文字/None は「条件なし」を意味する。"""

    text: str = ""
    kind: SampleKind | None = None
    category: str = ""
    tags_csv: str = ""  # カンマ区切りのタグ文字列
    channels: int | None = None   # 1 = モノラル、2 = ステレオ
    source_id: int | None = None  # 読み取り先フォルダ
    pack: str = ""                # Splice のパック名
    favorite_only: bool = False   # お気に入りだけを出す

    def to_kwargs(self) -> dict:
        """Library.search に渡す kwargs へ正規化する。"""
        tags = [t.strip() for t in self.tags_csv.split(",") if t.strip()]
        return {
            "text": self.text.strip() or None,
            "kind": self.kind,
            "category": self.category.strip() or None,
            "tags": tags or None,
            "channels": self.channels,
            "source_id": self.source_id,
            "pack": self.pack.strip() or None,
            "favorite_only": self.favorite_only,
        }


@dataclass
class PreparedPreview:
    """試聴の準備（加工まで）の結果。`play_prepared_preview` に渡す。

    加工は数秒かかることがあるため、準備だけをワーカースレッドで
    済ませられるように再生と分けてある。
    """

    path: str = ""
    filename: str = ""
    note: str = ""        # 加工内容などの補足（読み上げに添える）
    error: str = ""       # [警告]/[エラー] の文言。空なら成功

    @property
    def ok(self) -> bool:
        return not self.error


def normalize_source_root(root: str) -> str:
    """読み取り先のパスを比べられる形にする。

    `~/Samples` は展開し、末尾の区切りや `..` は畳む。**同じフォルダを
    書き方の違いで二重に登録しない**ため。実体（シンボリックリンクの先）
    までは追わない（リンク越しのパスをそのまま使いたいため）。
    """
    text = str(root).strip()
    if not text:
        return ""
    return os.path.normpath(os.path.expanduser(text))


class AppController:
    """GUI から呼ばれる操作の集約点。"""

    def __init__(
        self,
        library: Library,
        player: PreviewPlayer | None = None,
        settings: SessionSettings | None = None,
        settings_path: Path | None = None,
    ) -> None:
        self._library = library
        self._player = player  # None なら試聴非対応環境（テスト等）
        self._results: list[Sample] = []
        # 配置先セッションの条件。未指定なら「加工しない」既定値
        self._settings = settings or SessionSettings()
        self._settings_path = settings_path
        # 配置先トラックの指定。空なら Pro Tools で選ばれているものを使う。
        # **画面を見ずに Pro Tools でトラックを選ぶのは難しい**ので、
        # アプリ側から指定できるようにしてある
        self._target_track = ""

    # ------------------------------------------------------------------
    # セッション条件（移調・タイムストレッチの前提）
    # ------------------------------------------------------------------
    @property
    def session_settings(self) -> SessionSettings:
        return self._settings

    def update_session_settings(self, settings: SessionSettings) -> str:
        """セッション条件を差し替えて保存し、読み上げ文言を返す。"""
        self._settings = settings
        if self._settings_path is not None:
            settings.save(self._settings_path)
        return tr(f"セッション条件を設定しました。{settings.describe_ja()}",
                  f"Session settings saved. {settings.describe_ja()}")

    # ------------------------------------------------------------------
    # 配置先トラック
    # ------------------------------------------------------------------
    @property
    def target_track(self) -> str:
        """配置先に指定しているトラック名。空なら Pro Tools の選択に従う。"""
        return self._target_track

    def set_target_track(self, name: str) -> str:
        """配置先トラックを決める。読み上げ文言を返す。

        空文字を渡すと指定を外し、Pro Tools で選ばれているトラックに戻す。
        """
        self._target_track = name.strip()
        if not self._target_track:
            return tr("配置先の指定を外しました。Pro Tools で選ばれているオーディオトラックに置きます。",
                      "Destination track cleared. Samples go to the audio "
                      "track selected in Pro Tools.")
        return tr(f"配置先を「{self._target_track}」にしました。Pro Tools 側で選び直さなくても、"
                  "ここに置きます。",
                  f"Destination set to “{self._target_track}”. Samples go "
                  "there whatever is selected in Pro Tools.")

    def describe_target_track(self) -> str:
        """いまの配置先の説明（1 行）。"""
        if not self._target_track:
            return tr("配置先: Pro Tools で選ばれているオーディオトラック",
                      "Destination: the audio track selected in Pro Tools")
        return tr(f"配置先: 「{self._target_track}」に固定",
                  f"Destination: fixed to “{self._target_track}”")

    def list_audio_tracks(self) -> tuple[list[str], str]:
        """Pro Tools のオーディオトラック名を取り出す。

        戻り値は (トラック名の一覧, エラー文言)。**接続できないことは
        珍しくない**（Pro Tools を起動していない・セッション未オープン）
        ので、例外ではなく文言で返す。
        """
        if sys.platform != "darwin":
            return [], tr("[警告] Pro Tools との連携は macOS でのみ使えます。",
                          "[Warning] Working with Pro Tools is available "
                          "only on macOS.")
        try:
            from core.placement_planner import audio_track_names
            from integration.ptsl_spot_client import PTSLError, SpotPTSLClient
        except ImportError as exc:
            return [], _cannot_load_ptsl(exc)

        try:
            with SpotPTSLClient(application_name="sample_manager") as client:
                if not client.session_is_open():
                    return [], (_NO_SESSION)
                names = audio_track_names(client.list_tracks())
        except PTSLError as exc:
            return [], (_cannot_connect(exc))
        if not names:
            return [], tr("[警告] このセッションにはオーディオトラックがありません。",
                          "[Warning] This session has no audio tracks.")
        return names, ""

    # ------------------------------------------------------------------
    # ビルド情報
    # ------------------------------------------------------------------
    def describe_build_ja(self) -> str:
        """ビルドの日時とコミットの読み上げ文言。

        `.app` の Info.plist から読む。手元の版はプロジェクトの
        `PTサンプルマネージャ.app`（このファイルの位置から辿る。どこから起動しても
        同じ結果になる）。配布版は、起動時に `app._frozen_setup` が
        環境変数 `SM_APP_BUNDLE` で教える .app 自身。
        """
        from core.build_info import describe_ja, read_build_info

        root = Path(__file__).resolve().parent.parent
        bundle = os.environ.get("SM_APP_BUNDLE") or str(
            root / f"{BUNDLE_NAME}.app")
        return describe_ja(read_build_info(bundle), root)

    def preview_toggle_with_settings(self, sample: Sample | None,
                                     settings: SessionSettings) -> str:
        """加工した音の試聴を切り替える（鳴っていれば止める）。

        **止める手段がないと鳴りっぱなしになる。** 設定を変えながら
        何度も聞くので、同じキー・同じボタンで止められるようにする。
        """
        if self.stop_preview_if_active():
            return tr("[情報] 試聴を止めました", "[Info] Preview stopped")
        return self.preview_with_settings(sample, settings)

    def stop_preview_if_active(self) -> bool:
        """試聴が鳴っていれば止める。止めたら True。

        呼び出し側が「止めた」のか「これから鳴らす」のかを区別できる
        ように bool で返す（止めるだけなら「加工しています」と
        アナウンスする必要はない）。
        """
        if self._player is None or not self._player.is_active:
            return False
        self._player.stop()
        return True

    @property
    def preview_is_playing(self) -> bool:
        """試聴が鳴っているか。"""
        return self._player is not None and self._player.is_active

    def prepare_preview_with_settings(self, sample: Sample | None,
                                      settings: SessionSettings
                                      ) -> "PreparedPreview":
        """試聴の準備（存在確認と加工）。**ワーカースレッドから呼べる。**

        加工（ffmpeg）は数秒かかることがあり、UI スレッドで待つと画面と
        VoiceOver が固まる。ここでは再生を始めない（再生バックエンドが
        スレッド安全とは限らない）ので、重い部分だけを別スレッドへ
        逃がせる。結果は `play_prepared_preview` に渡す。
        """
        if self._player is None:
            return PreparedPreview(
                error=tr("[警告] ", "[Warning] ") + _NO_PREVIEW)
        if sample is None:
            return PreparedPreview(error=tr("[警告] ", "[Warning] ") + NO_SAMPLE)
        if not Path(sample.path).exists():
            return PreparedPreview(error=_file_missing(sample.filename))

        path, note = sample.path, ""
        if settings.can_render:
            try:
                from integration.render_service import render_for_session
            except ImportError as exc:
                return PreparedPreview(
                    error=_cannot_load_render(exc))
            meta = sample.metadata
            outcome = render_for_session(
                sample.path, sample.filename, settings,
                channels=meta.channels, sample_rate=meta.sample_rate,
                duration=meta.duration_sec)
            if not outcome.ok:
                return PreparedPreview(
                    error=tr(f"[エラー] 加工に失敗しました（{outcome.error}）。",
                             f"[Error] Processing failed ({outcome.error})."))
            path, note = outcome.path, outcome.describe_ja()
        elif not settings.auto_render and (settings.has_tempo
                                           or settings.has_key):
            # **加工しない理由を取り違えない。** テンポやキーは入って
            # いるのに「未設定」と読むと、設定が消えたと誤解される
            note = tr("自動加工がオフなので、加工せずそのまま鳴らします。",
                      "Automatic processing is off, so the sample plays as "
                      "it is.")
        else:
            note = tr("テンポもキーも未設定なので、加工せずそのまま鳴らします。",
                      "Neither tempo nor key is set, so the sample plays as "
                      "it is.")
        return PreparedPreview(path=path, filename=sample.filename, note=note)

    def play_prepared_preview(self, prepared: "PreparedPreview") -> str:
        """準備済みの試聴を鳴らす。**UI スレッドから呼ぶこと。**"""
        if not prepared.ok:
            return prepared.error
        if self._player is None:
            return tr("[警告] ", "[Warning] ") + _NO_PREVIEW
        try:
            self._player.load(prepared.path)
            self.apply_preview_volume()
            self._player.play()
        except Exception:
            return tr(f"[エラー] {prepared.filename} を再生できません。",
                      f"[Error] Cannot play {prepared.filename}.")
        return tr(f"[情報] 試聴しています。{prepared.filename}。{prepared.note}",
                  f"[Info] Previewing {prepared.filename}. "
                  f"{prepared.note}").strip()

    def preview_with_settings(self, sample: Sample | None,
                              settings: SessionSettings) -> str:
        """その条件で加工した音を試聴する。**配置する前に耳で確かめる。**

        テンポやキーを変えても、置いてみるまで結果が分からないのは怖い。
        ここで同じ加工を通してから鳴らす。準備（重い）と再生（UI スレッド
        限定）を続けて行う一体版。ダイアログは 2 つを分けて使う
        （ui/session_dialog.py の `_on_preview`）。
        """
        return self.play_prepared_preview(
            self.prepare_preview_with_settings(sample, settings))

    def clear_session_tempo(self) -> str:
        """テンポとキーを空に戻して保存する。**終了時に呼ぶ。**

        **セッション固有の値を次回へ持ち越さない。** 別のセッションを
        開いたまま前回のテンポで配置すると、素材が違う倍率で伸縮される
        （実機で 160 BPM のセッションに 130 BPM が残っていた）。
        気づかないまま置いてしまうのが一番困る。

        配置のしかた（位置・個数・間隔・拍に合わせるか）は好みなので残す。
        サンプルレートも選択肢なので空にはしない。
        """
        self._settings.bpm = None
        self._settings.key = ""
        if self._settings_path is not None:
            self._settings.save(self._settings_path)
        return tr("テンポとキーの設定を空に戻しました。", "Tempo and key were cleared.")

    def detect_session_tempo(self) -> tuple[float | None, str]:
        """Pro Tools からセッションのテンポを読み取る。

        戻り値は (テンポ, エラー文言)。**手で入れた値が実際と違うと、
        素材が間違った倍率で伸縮されてメトロノームと合わなくなる。**
        （実機で 160 BPM のセッションに 130 BPM と設定されていた例があった）

        PTSL はテンポを返さないので、小節・拍とサンプルの対応から
        逆算する（`ptsl_spot_client.detect_tempo`）。
        """
        if sys.platform != "darwin":
            return None, tr("[警告] テンポの取得は macOS でのみ使えます。",
                            "[Warning] Reading the tempo is available only "
                            "on macOS.")
        try:
            from integration.ptsl_spot_client import PTSLError, SpotPTSLClient
        except ImportError as exc:
            return None, _cannot_load_ptsl(exc)

        try:
            with SpotPTSLClient(application_name="sample_manager") as client:
                if not client.session_is_open():
                    return None, (_NO_SESSION)
                tempo = client.detect_tempo()
        except PTSLError as exc:
            return None, (_cannot_connect(exc))
        if tempo is None:
            return None, tr("[警告] テンポを読み取れませんでした。",
                            "[Warning] Could not read the tempo.")
        return tempo, ""

    def tempo_mismatch_warning(self, actual: float | None) -> str:
        """設定のテンポが実際と違うなら、その旨の文言を返す。

        **ここがずれていると配置が実用にならない。** 素材を間違った
        倍率で伸縮してしまい、メトロノームと合わなくなる。
        """
        if actual is None or not self._settings.has_tempo:
            return ""
        if abs(self._settings.bpm - actual) < 0.05:
            return ""
        # キーの名前は実際の割り当てと合わせる（以前は修飾なしの S だった。
        # いまの S は結果リストで頭文字への移動になる）
        return tr(f"[警告] 設定のテンポ {self._settings.bpm:g} は、セッションの {actual:g} "
                  "と違います。このまま配置すると素材の長さが合いません。コマンドとSのセッションの条件で、テンポを合わせてください。",
                  "[Warning] The tempo in the settings, "
                  f"{self._settings.bpm:g}, differs from the session, "
                  f"{actual:g}. If you place the sample now, its length will "
                  "not match. Correct the tempo in Session Settings "
                  "(Command-S).")

    def describe_render_backend(self) -> str:
        """加工に使える道具の説明。設定ダイアログでの案内に使う。"""
        try:
            from integration.render_service import describe_backend
        except ImportError as exc:
            return tr(f"加工の準備を確認できません（{exc}）。",
                      f"Cannot check the processing tools ({exc}).")
        return describe_backend()

    # ------------------------------------------------------------------
    # 検索
    # ------------------------------------------------------------------
    def search(self, form: SearchForm) -> list[str]:
        """検索を実行し、ListBox 用の行文字列リストを返す。

        本体 Sample は内部に保持し、index で参照できるようにする。
        """
        self._results = self._library.search(**form.to_kwargs())
        for sample in self._results:
            self._fill_music(sample)
        return [sample.describe_ja() for sample in self._results]

    def _fill_music(self, sample: Sample) -> None:
        """ファイル名から読んだ BPM とキーを Sample に載せる。

        **DB には保存しない。** 解析はファイル名を見るだけで速く、
        命名規則の改善がそのまま反映されるほうが望ましいため。
        """
        music = self.sample_music(sample)
        sample.bpm = getattr(music, "bpm", None)
        sample.key_label = getattr(music, "key_label", "") or ""

    def describe_row(self, sample: Sample) -> str:
        """一覧に出す 1 行。音楽情報を載せてから組み立てる。"""
        self._fill_music(sample)
        return sample.describe_ja()

    def sample_music(self, sample: Sample):
        """素材の BPM とキー（ファイル名から読む）。

        core 層に借りている部品への依存を持ち込みたくないので、
        `Sample` には持たせずここで解決する。
        """
        try:
            from integration.render_service import read_sample_music
        except ImportError:
            class _Empty:
                bpm = None
                key = ""
                key_label = ""
                known = False

                def describe_ja(self):
                    return ""
            return _Empty()
        return read_sample_music(sample.filename)

    def search_result_summary_ja(self) -> str:
        """検索結果件数の読み上げ文言。"""
        n = len(self._results)
        if n == 0:
            return tr("該当するサンプルはありませんでした", "No samples found")
        return tr(f"{n}件見つかりました", f"{plural(n, 'sample')} found")

    def get_sample(self, index: int) -> Sample | None:
        if 0 <= index < len(self._results):
            return self._results[index]
        return None

    @property
    def result_count(self) -> int:
        return len(self._results)

    # ------------------------------------------------------------------
    # スキャン（フォルダ取り込み）
    # ------------------------------------------------------------------
    @property
    def db_path(self) -> str:
        """ライブラリ DB の保存先（":memory:" またはファイルパス）。"""
        return self._library.db_path

    def scan(self, folder: str, source: Source | None = None) -> ScanResult:
        return scan_folder(self._library, folder, source)

    def scan_isolated(self, folder: str,
                      source: Source | None = None) -> ScanResult:
        """自前の DB 接続でスキャンする（ワーカースレッド用）。

        sqlite3 の接続はスレッド間で共有できないため、ファイル DB の場合は
        同じパスへ新しい接続を張って走査する。":memory:" は別接続だと別 DB に
        なってしまうので、既存接続をそのまま使う（呼び出し側はこの場合
        スレッドを使わないこと）。
        """
        if self._library.db_path == ":memory:":
            return scan_folder(self._library, folder, source)
        lib = Library(self._library.db_path)
        try:
            return scan_folder(lib, folder, source)
        finally:
            lib.close()

    def scan_sources(self, sources: list[Source]) -> str:
        """登録済みの読み取り先をまとめて走査し、読み上げ文言を返す。

        **フォルダを選び直させないため。** 再スキャンのたびに選択画面を
        出すのは手間なので、登録済みを順に走査して合計を伝える。

        1 件が失敗しても残りは続ける（外付けドライブが外れている等）。
        """
        added = skipped = linked = 0
        errors: list[str] = []
        for source in sources:
            try:
                result = self.scan_isolated(source.root, source)
            except OSError as exc:
                errors.append(tr(f"「{source.name}」: {exc}",
                                 f"“{source.name}”: {exc}"))
                continue
            added += result.added
            skipped += result.skipped
            linked += result.linked
            if result.errors:
                # 最初の理由も添える（「フォルダが存在しません」など。
                # 件数だけでは直しようがない）
                reason = result.errors[0][1]
                errors.append(
                    tr(f"「{source.name}」で {len(result.errors)} 件（{reason}）",
                       f"{plural(len(result.errors), 'file')} in "
                       f"“{source.name}” ({reason})"))

        parts = [tr(f"{len(sources)} 件の読み取り先を調べ、{added}件を登録しました。",
                    f"Scanned {plural(len(sources), 'source')} and added "
                    f"{plural(added, 'sample')}. ")]
        if linked:
            parts.append(tr(f"{linked}件は読み取り先に結びつけました。",
                            f"Linked {plural(linked, 'sample')} to a "
                            "source. "))
        if skipped:
            parts.append(tr(f"{skipped}件は登録済みのため省略しました。",
                            f"Skipped {plural(skipped, 'sample')} already "
                            "added. "))
        if errors:
            # **先頭で失敗が分かるようにする。** 通知音は文言の先頭で決まる
            # （`ui/sounds.py`）ので、接頭辞が無いと外付けが外れていても
            # 成功の音が鳴る
            parts.insert(0, tr("[警告] ", "[Warning] "))
            parts.append(tr("読み込めなかったもの: ",
                            "Could not read: ") + COMMA.join(errors))
        return "".join(parts).strip()

    # ------------------------------------------------------------------
    # 読み取り先フォルダ（ソース）
    # ------------------------------------------------------------------
    def list_sources(self) -> list[Source]:
        return self._library.list_sources()

    def register_source(self, name: str, root: str,
                        kind: SourceKind | None = None) -> Source:
        """読み取り先を登録する。

        種類を省くと、フォルダ構造から Splice かどうかを推定する
        （`packs` 階層があるか）。
        """
        root = normalize_source_root(root)
        if kind is None:
            kind = (SourceKind.SPLICE if looks_like_splice(root)
                    else SourceKind.GENERIC)
        return self._library.add_source(
            Source(name=name.strip() or Path(root).name, root=root, kind=kind))

    def source_conflict(self, name: str, root: str) -> tuple[str, str]:
        """登録前に、同じフォルダや同じ名前が無いかを確かめる。

        戻り値は (理由, 読み上げ文言)。理由は "root"（同じフォルダが
        登録済み）か "name"（別のフォルダが同じ名前）、無ければ両方空。

        **Library は同じ名前があると既存の読み取り先を返す**（名前は一意）。
        それを「追加しました」と読むと、増えていないのに増えたことになる。
        名前は省くとフォルダ名になるので、別の場所にある同名のフォルダ
        （`A/Samples` と `B/Samples`）で起きやすい。
        """
        root = normalize_source_root(root)
        name = name.strip() or Path(root).name
        for source in self._library.list_sources():
            if source.root == root:
                return "root", tr(f"「{source.name}」として登録済みのフォルダです: {root}",
                                  "This folder is already added as "
                                  f"“{source.name}”: {root}")
            if source.name == name:
                return "name", tr(f"「{name}」という名前は別のフォルダに使われています（{source.root}"
                                  "）。別の名前を入力してください",
                                  f"The name “{name}” is used by another "
                                  f"folder ({source.root}). Enter a "
                                  "different name")
        return "", ""

    def remove_source(self, source_id: int,
                      delete_samples: bool = True) -> str:
        """読み取り先を外す。読み上げ文言を返す。"""
        source = next(
            (s for s in self._library.list_sources() if s.id == source_id),
            None)
        if source is None:
            return tr("その読み取り先はありません", "That source does not exist")
        removed = self._library.remove_source(source_id, delete_samples)
        if delete_samples:
            return tr(f"「{source.name}」を外し、{removed}件を一覧から消しました。",
                      f"Removed “{source.name}” and "
                      f"{plural(removed, 'sample')} from the list.")
        return tr(f"「{source.name}」を外しました。サンプルは残してあります。",
                  f"Removed “{source.name}”. Its samples were kept.")

    def list_packs(self, source_id: int | None = None,
                   form: SearchForm | None = None) -> list[str]:
        """パック名の一覧（Splice の読み取り先）。"""
        return [name for name, _
                in self.list_packs_with_counts(source_id, form)]

    def list_packs_with_counts(
        self, source_id: int | None = None,
        form: SearchForm | None = None,
    ) -> list[tuple[str, int]]:
        """パック名と件数の一覧。読み上げで数が分かると選びやすい。

        `form` を渡すと**その検索条件に合うものだけを数える。**
        種別を「ループのみ」にしているのにワンショットしか無いパックが
        選択肢に出ると、選んでも 0 件になって探せない。
        """
        if form is None:
            return self._library.list_packs_with_counts(source_id)
        filters = form.to_kwargs()
        # pack は Library 側が無視する（自分自身で絞ると 1 つしか出ない）
        filters["source_id"] = source_id
        return self._library.list_packs_with_counts(**filters)

    def source_label(self, source_id: int | None) -> str:
        """読み取り先の名前。未所属なら「登録前」。"""
        if source_id is None:
            return tr("登録前", "before adding")
        for s in self._library.list_sources():
            if s.id == source_id:
                return s.name
        return tr("不明", "unknown")

    # ------------------------------------------------------------------
    # 試聴
    # ------------------------------------------------------------------
    def preview_toggle(self, index: int) -> str:
        """選択行を試聴トグルし、状態文言を返す。"""
        if self._player is None:
            return _NO_PREVIEW
        sample = self.get_sample(index)
        if sample is None:
            return NO_SAMPLE
        # ファイルの移動・削除やデコード失敗で UI が例外死しないよう防御する
        try:
            # 別ファイルなら読み込み直してから再生。
            # **音量は再生のたびに入れ直す**（読み込みで既定へ戻る
            # バックエンドがあるため）
            if self._player.current_path != sample.path:
                self._player.load(sample.path)
                self.apply_preview_volume()
                self._player.play()
            else:
                self.apply_preview_volume()
                self._player.toggle_play_stop()
        except Exception:
            return tr(f"[エラー] {sample.filename} を再生できません。"
                      "ファイルが移動または削除されていないか確認してください。",
                      f"[Error] Cannot play {sample.filename}. Check whether "
                      "the file was moved or deleted.")
        return self._player_state()

    def _player_state(self) -> str:
        """試聴の状態の読み上げ。プレイヤーは別のアプリも借りているので言語を持たない。"""
        if is_english():
            return self._player.describe_state_en()
        return self._player.describe_state_ja()

    # -- 試聴の音量 ----------------------------------------------------
    @property
    def preview_volume(self) -> float:
        return self._settings.preview_volume

    def apply_preview_volume(self) -> None:
        """いまの音量をプレイヤーへ反映する。**再生前に必ず呼ぶ。**

        バックエンドによっては読み込みのたびに音量が既定へ戻るため。
        """
        if self._player is None:
            return
        try:
            self._player.set_volume(self._settings.preview_volume)
        except Exception:
            pass

    def set_preview_volume(self, volume: float) -> str:
        """試聴の音量を決めて保存し、読み上げ文言を返す。"""
        self._settings.preview_volume = parse_volume(volume)
        self.apply_preview_volume()
        if self._settings_path is not None:
            self._settings.save(self._settings_path)
        return describe_volume_ja(self._settings.preview_volume)

    def nudge_preview_volume(self, up: bool) -> str:
        """音量を一段だけ上げ下げする（キー操作用）。

        端に着いたらその旨を添える。**上げ続けたつもりで変わっていない、
        という状態に気づけないため。**
        """
        before = self._settings.preview_volume
        step = VOLUME_STEP if up else -VOLUME_STEP
        message = self.set_preview_volume(before + step)
        # 値は parse_volume が丸めて正規化している（0.1 刻みの足し引きで
        # 浮動小数の誤差が積もると、この等値比較が端で成立しなくなる）
        if self._settings.preview_volume == before:
            return message + (tr("。これ以上上げられません", ". It cannot go higher") if up
                              else tr("。これ以上下げられません", ". It cannot go lower"))
        return message

    def preview_stop(self) -> str:
        if self._player is None:
            return ""
        self._player.stop()
        return self._player_state()

    # ------------------------------------------------------------------
    # パスコピー（方式C フォールバック）
    # ------------------------------------------------------------------
    def get_path_for_copy(self, index: int) -> str | None:
        """選択行のファイルパスを返す（クリップボードコピー用）。"""
        sample = self.get_sample(index)
        return sample.path if sample is not None else None

    def copy_path(self, index: int) -> str:
        """方式C: 選択サンプルのパスをクリップボードへコピーし、文言を返す。"""
        sample = self.get_sample(index)
        if sample is None:
            return NO_SAMPLE
        from integration.protools import copy_path_to_clipboard

        return copy_path_to_clipboard(sample.path).message

    def import_to_pro_tools(self, index: int, dry_run: bool = True) -> str:
        """方式B: Import Audio 経由で取り込む。既定は dry_run。

        注意: PTSL 方式（place_in_pro_tools）が主経路になった。
        本メソッドは AppleScript 方式のフォールバックとして残置。
        """
        sample = self.get_sample(index)
        if sample is None:
            return NO_SAMPLE
        from integration.protools import import_via_dialog

        return import_via_dialog(sample.path, dry_run=dry_run).message

    def place_in_pro_tools(self, index: int) -> str:
        """主経路: PTSL で選択トラックのカーソル位置へ配置する。

        選択サンプルを Pro Tools の「選択中オーディオトラックの編集カーソル
        位置」へ配置し、結果の読み上げ文言（[情報]/[警告]/[エラー]）を返す。
        """
        sample = self.get_sample(index)
        if sample is None:
            return NO_SAMPLE
        return self.place_sample_in_pro_tools(sample)

    def place_sample_in_pro_tools(self, sample: Sample) -> str:
        """Sample を直接受け取って配置する実体。

        UI はワーカースレッドから本メソッドを呼ぶ（index 経由だと配置中の
        再検索で別サンプルを掴む恐れがあるため、押下時点の Sample を渡す）。
        接続は内部で開閉する。接続不可・各種失敗も文言として返し、例外は
        UI に伝播させない。

        **セッション条件が設定されていれば、配置の前に加工する**
        （移調とタイムストレッチ）。設定が無ければそのまま置く。

        macOS 専用。Windows では未対応の旨を返す。
        """
        if not Path(sample.path).exists():
            return _file_missing(sample.path)
        if sys.platform != "darwin":
            return tr("[警告] Pro Tools への配置は macOS でのみ利用できます。Windows "
                      "ではパスのコピーをご利用ください。",
                      "[Warning] Placing in Pro Tools is available only on "
                      "macOS. On Windows, copy the path instead.")

        # 遅延 import（PTSL の部品は配置するときにだけ要る）。
        # 主経路は spot。一時トラックも Copy / Paste も使わない
        try:
            from integration.ptsl_spot_client import SpotPTSLClient, PTSLError
            from integration.placement_service import place_sample_spot
        except ImportError as exc:
            return _cannot_load_ptsl(exc)

        channels = sample.metadata.channels
        try:
            with SpotPTSLClient(application_name="sample_manager") as client:
                # **加工する前にテンポを確かめる。** 設定が実際と違うまま
                # 伸縮すると、メトロノームとまるで合わなくなる。
                # 接続を開いてからでないと確かめられないので、加工より先に
                # ここへ置いてある（接続は 1 回で済ませる）
                if self._settings.can_render and self._settings.has_tempo:
                    mismatch = self.tempo_mismatch_warning(
                        client.detect_tempo())
                    if mismatch:
                        return mismatch

                # 層 B。加工に失敗したら配置しない（意図しない音程・尺で
                # 置いてしまうより、何もしないほうがよい）
                path, render_note, render_error = self._render_for_session(
                    sample)
                if render_error:
                    return render_error

                positions = self._settings.positions()
                overlap_note = self._overlap_warning(path, sample, positions)

                result = place_sample_spot(
                    client, path, sample.filename, channels, positions,
                    adapt=self._adapt_channels(sample),
                    prefer_track=self._target_track,
                    snap_to_beat=self._settings.snap_to_beat)
                message = result.message
        except PTSLError as exc:
            return _cannot_connect(exc)

        if result.success:
            for note in (render_note, overlap_note):
                if note:
                    message = f"{message} {note}"
        return message

    def _adapt_channels(self, sample: Sample):
        """配置先トラックに合わせて素材のチャンネル数を変える手続きを返す。

        **Pro Tools はチャンネル構成が合わないクリップを置かせてくれない。**
        以前は「モノラルなので配置を中止しました」で終わっていたが、
        素材の側を合わせれば置ける。

        接続してトラックが決まるまで「いくつに合わせるか」が分からないので、
        placement_service から呼んでもらう形にする。
        """
        def adapt(path: str, target_channels: int):
            try:
                from integration.render_service import adapt_channels
            except ImportError as exc:
                return path, "", tr(f"加工の部品を読み込めません（{exc}）",
                                    "Cannot load the audio processing parts "
                                    f"({exc})")
            outcome = adapt_channels(path, sample.filename, target_channels)
            if not outcome.ok:
                return path, "", outcome.error
            return outcome.path, outcome.describe_ja(), ""

        return adapt

    def _overlap_warning(self, path: str, sample: Sample,
                         positions: list[str]) -> str:
        """並べたクリップどうしが重なるなら伝える。

        **間隔より素材が長いと、後から置いたクリップが前のものを食う。**
        止めはしない（利用者が意図している場合もある）が、黙って
        起こると気づけないので必ず読み上げる。
        """
        if len(positions) < 2 or not self._settings.has_tempo:
            return ""
        try:
            from integration.render_service import find_overlaps
        except ImportError:
            return ""

        duration = self._duration_of(path, sample)
        if duration <= 0:
            return ""
        hits = find_overlaps(positions, self._settings.bpm, duration,
                             self._settings.sample_rate)
        if not hits:
            return ""
        where = COMMA.join(hits)
        return tr(f"ただし素材が {duration:.2f} 秒あるため、{where} "
                  "のクリップは次のクリップに上書きされます。間隔を広げるか、テンポを合わせてください。",
                  f"However, the sample is {duration:.2f} seconds long, so "
                  f"the clips at {where} are overwritten by the next clip. "
                  "Widen the interval or correct the tempo.")

    @staticmethod
    def _duration_of(path: str, sample: Sample) -> float:
        """実際に配置するファイルの長さ（秒）。

        加工したファイルは索引の値と長さが違うので、そちらを読みに行く。
        """
        if path == sample.path:
            return float(sample.metadata.duration_sec or 0.0)
        try:
            import wave

            with wave.open(path) as w:
                return w.getnframes() / float(w.getframerate())
        except Exception:
            return float(sample.metadata.duration_sec or 0.0)

    def _render_for_session(self, sample: Sample):
        """配置前の加工。戻り値は (渡すパス, 読み上げの補足, エラー文言)。

        セッション条件が無ければ何もしない。加工に失敗した場合は
        **元のファイルを返さず**、エラー文言を返して配置を止める。
        """
        if not self._settings.can_render:
            return sample.path, "", ""

        try:
            from integration.render_service import render_for_session
        except ImportError as exc:
            return "", "", _cannot_load_render(exc)

        meta = sample.metadata
        outcome = render_for_session(
            sample.path, sample.filename, self._settings,
            channels=meta.channels,
            sample_rate=meta.sample_rate,
            duration=meta.duration_sec,
        )
        if not outcome.ok:
            return "", "", tr(f"[エラー] 加工に失敗したため配置しませんでした（{outcome.error}）。",
                              "[Error] Processing failed, so nothing was "
                              f"placed ({outcome.error}).")
        return outcome.path, outcome.describe_ja(), ""

    # ------------------------------------------------------------------
    # お気に入り
    # ------------------------------------------------------------------
    def toggle_favorite(self, index: int) -> tuple[str, str | None]:
        """お気に入りを付け外しする。

        戻り値は (読み上げ文言, 更新後の行)。行が None なら一覧を
        書き換えない。**数千件の中から前に使った素材を探し直すのは手間**
        なので、印を付けて絞り込めるようにしてある。
        """
        sample = self.get_sample(index)
        if sample is None or sample.id is None:
            return NO_SAMPLE, None

        value = not sample.favorite
        self._library.set_favorite(sample.id, value)
        sample.favorite = value

        total = self._library.count_favorites()
        head = (tr("お気に入りに追加しました", "Added to favorites") if value
                else tr("お気に入りから外しました", "Removed from favorites"))
        return (tr(f"{head}。{sample.filename}。全部で {total} 件です",
                   f"{head}. {sample.filename}. {plural(total, 'favorite')} "
                   "in all"),
                self.describe_row(sample))

    def favorite_count(self) -> int:
        return self._library.count_favorites()

    def drop_result(self, index: int) -> None:
        """内部の結果リストから 1 行取り除く。

        **絞り込みに合わなくなった行を UI が消すとき、一緒に呼ぶ。**
        呼ばないと ListBox と index がずれ、以後の選択操作が別の
        サンプルを掴む。
        """
        if 0 <= index < len(self._results):
            del self._results[index]

    # ------------------------------------------------------------------
    # 手動タグ付け・補正
    # ------------------------------------------------------------------
    def apply_edits(
        self,
        index: int,
        tags_csv: str,
        category: str,
        kind: SampleKind,
    ) -> str | None:
        """編集ダイアログの内容（タグ・カテゴリ・種別）を一括適用する。

        成功時は更新後の describe_ja() 行文字列を返す（UI はこれで
        結果リストの該当行を置き換える）。失敗時は None。
        内部の結果リスト（_results）も更新後の Sample に差し替える。
        """
        sample = self.get_sample(index)
        if sample is None or sample.id is None:
            return None
        tags = [t.strip() for t in tags_csv.split(",") if t.strip()]
        self._library.set_tags(sample.id, tags)
        self._library.set_category(sample.id, category.strip() or None)
        self._library.set_kind(sample.id, kind)
        updated = self._library.get_sample(sample.id)
        if updated is None:
            return None
        self._results[index] = updated
        # 一覧と同じ書式にする（BPM とキーも添える）
        return self.describe_row(updated)

    def set_tags(self, index: int, tags_csv: str) -> bool:
        sample = self.get_sample(index)
        if sample is None or sample.id is None:
            return False
        tags = [t.strip() for t in tags_csv.split(",") if t.strip()]
        self._library.set_tags(sample.id, tags)
        return True

    def set_category(self, index: int, category: str) -> bool:
        sample = self.get_sample(index)
        if sample is None or sample.id is None:
            return False
        self._library.set_category(sample.id, category.strip() or None)
        return True

    def set_kind(self, index: int, kind: SampleKind) -> bool:
        sample = self.get_sample(index)
        if sample is None or sample.id is None:
            return False
        self._library.set_kind(sample.id, kind)
        return True
