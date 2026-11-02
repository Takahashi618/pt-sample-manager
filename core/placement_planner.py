"""配置ロジックのプランナー（PTSL非依存・純粋ロジック）。

Pro Tools / PTSL に一切依存しないため、単体テストできる。
「どのトラックへ配置するか」「チャンネル数は整合するか」「結果文言を
どう読み上げるか」といった判断だけをここに集約する。

実際の Pro Tools 操作（gRPC 呼び出し）は integration 層が担い、
このプランナーが返す TrackInfo / 判定結果を使って実行する。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from core.i18n import COMMA, plural, tr
from core.models import channel_label


# Pro Tools の TrackType（PTSL の TrackType enum と一致させた最小定義）。
# integration 層が PTSL の生 enum 値をこの定数に対応づけて渡す。
TRACK_TYPE_AUDIO = 2

# TrackAttributeState（PTSL の TrackAttributeState enum と一致）。
# 「明示的に選択された」以上を選択とみなす境界値。
TA_STATE_SELECTED_MIN = 2  # SetExplicitly=2, Implicit=3, Both=4


class TrackFormat(enum.IntEnum):
    """トラックのチャンネル構成（PTSL TrackFormat の必要分のみ）。"""

    UNKNOWN = 0
    MONO = 1
    STEREO = 2


@dataclass(frozen=True)
class TrackInfo:
    """配置先候補となるトラックの情報（PTSL から取得した値の写し）。

    integration 層が PTSL の Track メッセージから必要な値だけ抜き出して
    このフラットな構造に詰め替える。プランナーは PTSL 型を知らない。
    """

    name: str
    track_type: int          # TRACK_TYPE_AUDIO など
    track_format: int        # TrackFormat 値
    is_selected_state: int   # TrackAttributeState 値
    has_edit_selection_state: int  # TrackAttributeState 値
    contains_clips: bool = False

    @property
    def is_audio(self) -> bool:
        return self.track_type == TRACK_TYPE_AUDIO

    @property
    def is_selected(self) -> bool:
        """明示選択（SetExplicitly 以上）されているか。"""
        return self.is_selected_state >= TA_STATE_SELECTED_MIN

    @property
    def has_edit_selection(self) -> bool:
        """編集カーソル/選択がこのトラック上にあるか。"""
        return self.has_edit_selection_state >= TA_STATE_SELECTED_MIN


class PlacementStatus(enum.Enum):
    """配置の結果ステータス。UI の読み上げ分岐に使う。"""

    OK = "ok"
    NO_AUDIO_TRACK = "no_audio_track"        # 配置先オーディオトラックが無い
    CHANNEL_MISMATCH = "channel_mismatch"    # ch数不一致で中断
    NOT_CONNECTED = "not_connected"          # PTSL 未接続
    NO_SESSION = "no_session"                # セッション未オープン
    IMPORT_FAILED = "import_failed"          # 取り込み失敗
    PASTE_FAILED = "paste_failed"            # 貼り付け失敗
    VERIFY_FAILED = "verify_failed"          # 配置後の検証で確認できず


# ステータス→読み上げの頭の印（状態を先頭に置く）
_PREFIX = {
    PlacementStatus.OK: tr("[情報]", "[Info]"),
    PlacementStatus.NO_AUDIO_TRACK: tr("[警告]", "[Warning]"),
    PlacementStatus.CHANNEL_MISMATCH: tr("[警告]", "[Warning]"),
    PlacementStatus.NOT_CONNECTED: tr("[エラー]", "[Error]"),
    PlacementStatus.NO_SESSION: tr("[警告]", "[Warning]"),
    PlacementStatus.IMPORT_FAILED: tr("[エラー]", "[Error]"),
    PlacementStatus.PASTE_FAILED: tr("[エラー]", "[Error]"),
    PlacementStatus.VERIFY_FAILED: tr("[警告]", "[Warning]"),
}


@dataclass(frozen=True)
class PlacementResult:
    """配置の最終結果。message はそのまま読み上げる前提。"""

    status: PlacementStatus
    message: str
    track_name: str | None = None

    @property
    def success(self) -> bool:
        return self.status is PlacementStatus.OK


def select_target_track(tracks: list[TrackInfo]) -> TrackInfo | None:
    """配置先トラックを決定する（ユーザー方針: is_selected 優先）。

    1. 明示選択されたオーディオトラックを優先。
    2. 無ければ編集カーソルが乗っているオーディオトラック。
    どちらも無ければ None。複数該当時は先頭を返す（呼び出し側で警告）。
    """
    selected = [t for t in tracks if t.is_audio and t.is_selected]
    if selected:
        return selected[0]
    edit = [t for t in tracks if t.is_audio and t.has_edit_selection]
    if edit:
        return edit[0]
    return None


def count_selected_audio_tracks(tracks: list[TrackInfo]) -> int:
    """明示選択されたオーディオトラックの数（複数選択の警告判定用）。"""
    return sum(1 for t in tracks if t.is_audio and t.is_selected)


def audio_track_names(tracks: list[TrackInfo]) -> list[str]:
    """配置できるトラックの名前。"""
    return [t.name for t in tracks if t.is_audio]


def find_audio_track(tracks: list[TrackInfo], name: str) -> TrackInfo | None:
    """名前でオーディオトラックを探す。

    **配置先をアプリ側で指定できるようにするために要る。** Pro Tools で
    トラックを選ぶ操作は画面を見ないと難しく、選び損ねると配置が失敗する。
    名前で指定できれば、Pro Tools 側の選択状態に左右されない。
    """
    if not name:
        return None
    for track in tracks:
        if track.is_audio and track.name == name:
            return track
    return None


def build_missing_track_message(name: str,
                                tracks: list[TrackInfo]) -> str:
    """指定した配置先が見つからないときの文言。

    セッションを開き直すとトラック構成が変わるので、**指定が古くなる**
    ことがある。そのときに何を選び直せばよいかまで伝える。
    """
    prefix = _PREFIX[PlacementStatus.NO_AUDIO_TRACK]
    audio = audio_track_names(tracks)
    if not audio:
        return tr(f"{prefix} 配置先に指定した「{name}」がありません。このセッションにはオーディオトラックがありません。",
                  f"{prefix} The destination track “{name}” does not exist. "
                  "This session has no audio tracks.")
    names = COMMA.join(audio)
    return tr(f"{prefix} 配置先に指定した「{name}」が見つかりません。"
              f"いまあるのは「{names}」です。配置先を選び直してください。",
              f"{prefix} The destination track “{name}” was not found. The "
              f"audio tracks now are: {names}. Choose the destination again.")


def selected_track_names(tracks: list[TrackInfo]) -> list[str]:
    """いま選ばれているトラックの名前（種別を問わない）。"""
    return [t.name for t in tracks
            if t.is_selected or t.has_edit_selection]


def build_no_audio_track_message(tracks: list[TrackInfo]) -> str:
    """配置先が無いときの文言を、状況に合わせて組み立てる。

    **「選択されていません」だけでは原因が分からない。** 画面を見られない
    利用者にとっては特にそうで、実際には別の種類のトラック（Inst や AUX）を
    選んでいることが多い。**何を選んでいて、代わりに何を選べばよいか**まで
    言わないと直しようがない。
    """
    prefix = _PREFIX[PlacementStatus.NO_AUDIO_TRACK]
    audio = audio_track_names(tracks)
    chosen = selected_track_names(tracks)

    if not audio:
        return tr(f"{prefix} このセッションにはオーディオトラックがありません。"
                  "オーディオトラックを作ってから配置してください。",
                  f"{prefix} This session has no audio tracks. Create an "
                  "audio track, then place the sample.")

    head = tr(f"{prefix} 配置できるトラックが選ばれていません。",
              f"{prefix} No track that can take a sample is selected.")
    if chosen:
        picked = COMMA.join(chosen)
        head += tr(f"いま選ばれているのは「{picked}」で、オーディオトラックではありません。",
                   f" The selected track, “{picked}”, is not an audio track."
                   if len(chosen) == 1 else
                   f" The selected tracks, “{picked}”, are not audio tracks.")
    choices = COMMA.join(audio)
    tail = tr(f"Pro Tools で「{choices}」のどれかを選んでから、もう一度実行してください。",
              " Select one of these in Pro Tools, then try again: "
              f"{choices}.")
    return head + tail


def _format_label_ja(fmt: int) -> str:
    """TrackFormat 値を日本語ラベルに（モデル側の規約に合わせる）。"""
    if fmt in (TrackFormat.MONO, TrackFormat.STEREO):
        return channel_label(int(fmt))
    return tr(f"{fmt}チャンネル構成", f"{fmt} channel format")


def _channels_label_ja(channels: int | None) -> str:
    """サンプルのチャンネル数を日本語ラベルに。"""
    if channels is None:
        return tr("チャンネル数不明", "unknown channel count")
    return channel_label(channels)


def snap_to_grid(position: int, origin: int, step: float) -> int:
    """位置を直近のグリッドへ丸める。

    **画面を見ずに編集カーソルを拍の上へ置くのは難しい。** 拍から
    ずれた位置に置くと、メトロノームと合わずに聞こえる（実機で確認:
    拍の半分ずれると 187 ミリ秒）。

    `origin` は小節 1 の位置、`step` は 1 拍のサンプル数。
    どちらも Pro Tools から取る（セッション開始が 0 とは限らない）。
    """
    if step <= 0:
        return position
    steps = round((position - origin) / step)
    return int(round(origin + steps * step))


def describe_snap_ja(before: int, after: int, sample_rate: float) -> str:
    """スナップで動かした量を読み上げる文言。動いていなければ空文字。

    **黙って動かさない。** 意図して拍の外へ置いた場合に、何が起きたか
    分からなくなるため。
    """
    if before == after or sample_rate <= 0:
        return ""
    diff = (after - before) / sample_rate * 1000.0
    if abs(diff) < 0.5:
        return ""
    direction = tr("後ろ", "later") if diff > 0 else tr("前", "earlier")
    ms = round(abs(diff))
    return tr(f"カーソルが拍から外れていたため、{ms} ミリ秒{direction}の拍に合わせました。",
              "The cursor was off the beat, so the sample was moved "
              f"{plural(ms, 'millisecond')} {direction} to the nearest beat.")


def check_channel_match(track: TrackInfo,
                        sample_channels: int | None) -> bool:
    """トラックとサンプルのチャンネル数が整合するか。

    サンプルのチャンネル数が不明（None）の場合は、判定不能だが
    配置を妨げない方が実用的なので True（整合扱い）とする。
    モノ↔モノ、ステレオ↔ステレオのみ一致とみなす。
    """
    if sample_channels is None:
        return True
    if track.track_format == TrackFormat.MONO:
        return sample_channels == 1
    if track.track_format == TrackFormat.STEREO:
        return sample_channels == 2
    # それ以外（サラウンド等）は厳密一致を求めず、ここでは整合扱い。
    return True


def build_channel_mismatch_message(track: TrackInfo,
                                   sample_channels: int | None) -> str:
    """チャンネル不一致時の読み上げ文言を作る。

    「選択トラック」とは言わない。コマンドとTで配置先を指定していると、
    Pro Tools で選んでいるトラックとは別のトラックの話になるため。
    """
    t = _format_label_ja(track.track_format)
    s = _channels_label_ja(sample_channels)
    return tr(f"{_PREFIX[PlacementStatus.CHANNEL_MISMATCH]} "
              f"配置先のトラック「{track.name}」は{t}ですが、サンプルは{s}です。配置を中止しました。",
              f"{_PREFIX[PlacementStatus.CHANNEL_MISMATCH]} The destination "
              f"track “{track.name}” is {t}, but the sample is {s}. "
              "Placement was canceled.")


def build_rolled_back_message(track_name: str) -> str:
    """置けたかどうかを確かめられず、取り消したときの文言。

    **「置いた」と読まない。** 取り消したあとはトラックに何も無いのに、そう聞くと
    置けたと思ってしまう。
    """
    prefix = _PREFIX[PlacementStatus.VERIFY_FAILED]
    return tr(f"{prefix} 置けたかどうかを確認できなかったため、取り消しました。"
              f"トラック「{track_name}」には置いていません。もう一度試してください。",
              f"{prefix} The placement could not be confirmed, so it was "
              f"undone. Nothing was placed on track “{track_name}”. Please "
              "try again.")


def build_undo_failed_message(track_name: str) -> str:
    """配置を取り消せなかったときの文言。トラックに何か残っているかもしれない。"""
    prefix = _PREFIX[PlacementStatus.VERIFY_FAILED]
    return tr(f"{prefix} 正しく置けたことを確認できず、取り消しにも失敗しました。"
              f"トラック「{track_name}」を確かめてください。",
              f"{prefix} The placement could not be confirmed, and undoing "
              f"it failed. Check track “{track_name}”.")


def build_result_message(status: PlacementStatus,
                         track_name: str | None = None,
                         filename: str | None = None,
                         timecode: str | None = None,
                         detail: str | None = None) -> str:
    """ステータスに応じた読み上げ文言を組み立てる（状態先行）。"""
    prefix = _PREFIX[status]
    if status is PlacementStatus.OK:
        loc = tr(f"、位置 {timecode}", f", at {timecode}") if timecode else ""
        return tr(f"{prefix} 配置しました。{filename}、トラック「{track_name}」{loc}",
                  f"{prefix} Placed {filename} on track “{track_name}”{loc}.")
    if status is PlacementStatus.NO_AUDIO_TRACK:
        return tr(f"{prefix} 配置先のオーディオトラックが選択されていません。"
                  "オーディオトラックを選択してから実行してください。",
                  f"{prefix} No destination audio track is selected. Select "
                  "an audio track, then try again.")
    if status is PlacementStatus.NOT_CONNECTED:
        return tr(f"{prefix} Pro Tools に接続できません。Pro Tools が起動しているか確認してください。",
                  f"{prefix} Cannot connect to Pro Tools. Check that Pro "
                  "Tools is running.")
    if status is PlacementStatus.NO_SESSION:
        return tr(f"{prefix} セッションが開かれていません。", f"{prefix} No session is open.")
    if status is PlacementStatus.IMPORT_FAILED:
        d = tr(f"（{detail}）", f" ({detail})") if detail else ""
        return tr(f"{prefix} サンプルの取り込みに失敗しました{d}。",
                  f"{prefix} Importing the sample failed{d}.")
    if status is PlacementStatus.PASTE_FAILED:
        d = tr(f"（{detail}）", f" ({detail})") if detail else ""
        return tr(f"{prefix} トラックへの配置に失敗しました{d}。",
                  f"{prefix} Placing the sample on the track failed{d}.")
    if status is PlacementStatus.VERIFY_FAILED:
        return tr(f"{prefix} 配置を実行しましたが、結果を確認できませんでした。"
                  f"トラック「{track_name}」を確認してください。",
                  f"{prefix} The sample was placed, but the result could not "
                  f"be checked. Check track “{track_name}”.")
    return tr(f"{prefix} {detail or '不明な状態です。'}",
              f"{prefix} {detail or 'The state is unknown.'}")
