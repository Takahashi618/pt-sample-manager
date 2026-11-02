"""配置サービス（オーケストレーション層）。

core.placement_planner（判断ロジック）と PTSL クライアント（PT操作）を
繋ぎ、サンプルを選択トラックの編集カーソル位置へ配置する一連の手順を
実行する。PTSL の生 API には触れず、Protocol 越しに操作する。

**経路が 2 つある。主経路は spot（`place_sample_spot`）。**

  spot    ImportAudioToClipList -> SpotClipsByID
          一時トラックも Copy / Paste も要らない。**主経路**
  legacy  Import(NewTrack) -> cut -> paste -> 一時トラックの再利用
          py-ptsl を使う旧実装。控えとして残してある

legacy は py-ptsl 601.0.1 の制約に合わせた設計になっている。
py-ptsl が新しいコマンドに追随しておらず、`DeleteTracks` が使えず、
`import_audio` にもバグがあったため、一時トラックを clear + rename で
使い回すしかなかった。**PTSL 自体にはどちらの機能もある**ことが
実機検証で分かっている。

spot 経路のシーケンス:
  1. セッション確認
  2. 配置先トラック特定（is_selected 優先 / has_edit_selection）
  3. チャンネル数チェック（不一致なら中断）
  4. カーソル TC 記録
  5. ImportAudioToClipList でクリップリストへ取り込む
  6. SpotClipsByID で対象トラックの TC 位置へ直接置く
  7. **実際の配置先を確認する**（応答を信じない）
  8. 違っていたら Undo で巻き戻す
  9. 結果を返す（呼び出し側が読み上げる）

legacy 経路のシーケンス（`place_sample`）:
  1〜4 は同じ
  5. 編集モードを Slip に（貼り付け位置を確実に）
  6. 新規トラックへ取り込み（import_type=2 回避策）
  7. 取り込んだクリップを cut
  8. 対象トラック選択 + カーソル復元 → paste
  9. 配置後検証（対象トラックに clips が付いたか）
  10. 一時トラックを clear + SM_TEMP へリネーム（再利用基盤化）
  11. 編集モード復元
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import replace

from core.i18n import COMMA, tr
from core.placement_planner import (
    PlacementResult,
    PlacementStatus,
    build_channel_mismatch_message,
    build_missing_track_message,
    build_no_audio_track_message,
    build_result_message,
    build_rolled_back_message,
    build_undo_failed_message,
    check_channel_match,
    count_selected_audio_tracks,
    describe_snap_ja,
    find_audio_track,
    select_target_track,
    snap_to_grid,
)
from integration.ptsl_client import PTSLClientProtocol, PTSLError

# 一時トラックの固定名（legacy 経路でのみ使う）。
TEMP_TRACK_NAME = "SM_TEMP"

# 位置文字列の読み方（PTSL の TimelineLocationType）。
# **カーソル位置はサンプルで渡す。** タイムコードはフレーム単位なので、
# 拍や 16 分音符の位置がフレーム境界に乗らず、置いた位置がずれる
# （実機で確認: 30fps・44.1kHz で最大 3 サンプル）。
# 並べる指定は小節・拍のまま（テンポマップに追従させるため）。
_TIMECODE = "TLType_TimeCode"
_SAMPLES = "TLType_Samples"
_BARS_BEATS = "TLType_BarsBeats"


def _cursor_for_placing(client) -> tuple[str, str]:
    """配置に使うカーソル位置と、その読み方を返す。

    サンプルで取れないクライアント（旧スタブ）ではタイムコードに落とす。
    **位置と読み方は必ず対で返す**（食い違うとまったく別の場所に置く）。
    """
    getter = getattr(client, "get_cursor_samples", None)
    if getter is not None:
        value = getter()
        if value:
            return value, _SAMPLES
    return client.get_cursor_timecode(), _TIMECODE


def _snap_cursor(client, cursor: str) -> tuple[str, str]:
    """カーソル位置を直近の拍へ丸める。戻り値は (位置, 読み上げの補足)。

    **グリッドが取れないときは何もしない。** 拍に合わせられないことより、
    配置できないことのほうが困るため。丸めた場合は必ず読み上げる
    （黙って動かすと、意図して拍の外へ置いた人が混乱する）。
    """
    # **すでに拍の上ならなにもしない。** 下の等間隔グリッドは先頭の
    # テンポで測った近似なので、テンポが途中で変わるセッションでは
    # 後半の拍と食い違う。Pro Tools 自身の小節・拍表現でティックが
    # 000 なら拍の上にいるので、計算がどう言おうと動かさない
    # （拍上のカーソルを「ずれている」と誤判定して動かすのが最悪のため）
    if _on_the_beat(client):
        return cursor, ""

    grid_getter = getattr(client, "grid_info", None)
    if grid_getter is None:
        return cursor, ""
    try:
        grid = grid_getter()
        position = int(cursor)
    except (PTSLError, ValueError, TypeError):
        return cursor, ""
    if not grid:
        return cursor, ""

    origin, beat = grid
    snapped = snap_to_grid(position, origin, beat)
    if snapped == position:
        return cursor, ""

    rate = 0.0
    rate_getter = getattr(client, "session_sample_rate", None)
    if rate_getter is not None:
        try:
            rate = rate_getter() or 0.0
        except PTSLError:
            rate = 0.0
    return str(snapped), describe_snap_ja(position, snapped, rate)


def _on_the_beat(client) -> bool:
    """カーソルが拍のちょうど上にあるか（小節・拍表現のティックで見る）。

    確かめられないときは偽（等間隔グリッドの判定に任せる）。
    """
    getter = getattr(client, "get_cursor_bars_beats", None)
    if getter is None:
        return False
    try:
        parts = [p.strip() for p in getter().split("|")]
    except PTSLError:
        return False
    return (len(parts) >= 3 and parts[2].isdigit()
            and int(parts[2]) == 0)


def _cursor_for_reading(client) -> str:
    """読み上げに使うカーソル位置（タイムコード）。

    位置を伝えるだけなので、失敗しても配置は止めない。
    """
    try:
        return client.get_cursor_timecode()
    except PTSLError:
        return ""


# ---------------------------------------------------------------------------
# spot 経路（主経路）
# ---------------------------------------------------------------------------

def place_sample_spot(client,
                      posix_path: str,
                      filename: str,
                      sample_channels: int | None,
                      positions: list[str] | None = None,
                      adapt=None,
                      prefer_track: str = "",
                      snap_to_beat: bool = False) -> PlacementResult:
    """サンプルを選択トラックへ配置する（spot 経路）。

    client は `ptsl_spot_client.SpotClientProtocol` を満たすもの。
    接続済み（コンテキストに入っている）前提。
    例外は内部で握って PlacementResult に変換する（UI を固めない）。

    `positions` に小節・拍のリストを渡すと**その位置すべてに置く**
    （ループを並べる）。省くと編集カーソルの位置に 1 個だけ置く。

    `adapt` を渡すと、**チャンネル構成が合わないときに素材を合わせる。**
    `adapt(path, target_channels) -> (新しいパス, 補足の文言, エラー文言)`
    の形。渡さない場合は従来どおり中断する（配置先のトラックを
    変えてもらうしかない）。

    `prefer_track` に名前を渡すと**そのトラックへ置く**。Pro Tools 側の
    選択状態を見ない。画面を見ずにトラックを選ぶのは難しく、選び損ねると
    配置が失敗するため、アプリ側から指定できるようにしてある。

    **一時トラックを作らない。** Copy / Paste も使わないので、
    「Link Timeline and Edit Selection」の状態にも依存しない。
    """
    target, tracks, guard = _prepare(client, sample_channels,
                                     adapt is not None, prefer_track)
    if guard is not None:
        return guard

    adapt_note = ""
    snap_note = ""
    if adapt is not None and not check_channel_match(target, sample_channels):
        # トラックのチャンネル数に素材を合わせる。**接続してトラックが
        # 決まってからでないと、いくつに合わせるか分からない**
        posix_path, adapt_note, adapt_error = adapt(
            posix_path, int(target.track_format))
        if adapt_error:
            return PlacementResult(
                PlacementStatus.CHANNEL_MISMATCH,
                build_channel_mismatch_message(target, sample_channels)
                + tr(f"（{adapt_error}）", f" ({adapt_error})"),
                track_name=target.name)

    # 配置先を指定しているなら、選択状態は関係ないので警告もしない
    multi_warn = ("" if prefer_track
                  else _multi_selection_warning(tracks, target))

    # 位置の指定が無ければ編集カーソルの位置。**この 2 つは時間の形式が
    # 違う**ので、どちらを使ったかを spot_clips に伝える必要がある
    if positions:
        targets = list(positions)
        time_type = _BARS_BEATS
        where = targets[0] if len(targets) == 1 else \
            tr(f"{targets[0]} から {len(targets)} 個",
               f"{targets[0]} ({len(targets)} copies)")
    else:
        # **カーソル位置はサンプルで受け取って、サンプルで置く。**
        # タイムコードはフレーム単位なので、拍や 16 分音符の位置が
        # フレーム境界に乗らず、置いた位置がずれる（実機で確認）。
        # 読み上げにはタイムコードを使う（利用者に分かりやすいため）。
        try:
            cursor, time_type = _cursor_for_placing(client)
            where = _cursor_for_reading(client) or cursor
        except PTSLError as exc:
            return PlacementResult(
                PlacementStatus.NOT_CONNECTED,
                build_result_message(PlacementStatus.NOT_CONNECTED,
                                     detail=str(exc)))
        # **拍に合わせる。** 画面を見ずにカーソルを拍の上へ置くのは難しく、
        # 外れた位置に置くとメトロノームと合わない（実機で確認）
        if snap_to_beat and time_type == _SAMPLES:
            cursor, snap_note = _snap_cursor(client, cursor)
        targets = [cursor]

    steps_done = 0
    try:
        clip_ids = client.import_to_clip_list(posix_path)
        steps_done += 1
    except PTSLError as exc:
        return PlacementResult(
            PlacementStatus.IMPORT_FAILED,
            build_result_message(PlacementStatus.IMPORT_FAILED,
                                 detail=str(exc)))

    for location in targets:
        try:
            client.spot_clips(clip_ids, target.name, location, time_type)
            steps_done += 1
        except PTSLError as exc:
            _safe_undo(client, steps_done)
            return PlacementResult(
                PlacementStatus.PASTE_FAILED,
                build_result_message(PlacementStatus.PASTE_FAILED,
                                     detail=str(exc)),
                track_name=target.name)

    # **応答を信じない。** SpotClipsByID は成功を返しても置いていないことが
    # あり、チャンネル構成が合わないと別のトラックへ勝手に置く
    verdict = _verify_spot(client, target.name, filename)
    if verdict is not None:
        if not _safe_undo(client, steps_done):
            return replace(verdict,
                           message=build_undo_failed_message(target.name))
        return verdict

    msg = build_result_message(
        PlacementStatus.OK, track_name=target.name,
        filename=filename, timecode=where)
    if snap_note:
        msg = f"{msg} {snap_note}"
    if adapt_note:
        msg = f"{msg} {adapt_note}"
    if multi_warn:
        msg = msg + multi_warn
    return PlacementResult(PlacementStatus.OK, msg, track_name=target.name)


def _prepare(client, sample_channels: int | None,
             can_adapt: bool = False, prefer_track: str = ""):
    """セッション・トラック・チャンネル数の事前確認。

    問題がなければ `(target, tracks, None)`、あれば
    `(None, [], PlacementResult)` を返す。tracks は取得済みの一覧
    （複数選択の警告判定に使い回し、PTSL への問い合わせを増やさない）。

    `can_adapt` が真なら**チャンネル数の不一致で止めない**
    （呼び出し側が素材を合わせるため）。

    `prefer_track` があれば**その名前のトラックを使う**（Pro Tools 側の
    選択状態を見ない）。見つからなければ、いまあるトラックを挙げて返す。
    """
    try:
        if not client.session_is_open():
            return None, [], PlacementResult(
                PlacementStatus.NO_SESSION,
                build_result_message(PlacementStatus.NO_SESSION))
    except PTSLError:
        return None, [], PlacementResult(
            PlacementStatus.NOT_CONNECTED,
            build_result_message(PlacementStatus.NOT_CONNECTED))

    try:
        tracks = client.list_tracks()
    except PTSLError as exc:
        return None, [], PlacementResult(
            PlacementStatus.NOT_CONNECTED,
            build_result_message(PlacementStatus.NOT_CONNECTED,
                                 detail=str(exc)))

    if prefer_track:
        target = find_audio_track(tracks, prefer_track)
        if target is None:
            return None, [], PlacementResult(
                PlacementStatus.NO_AUDIO_TRACK,
                build_missing_track_message(prefer_track, tracks))
    else:
        target = select_target_track(tracks)
    if target is None:
        # **何を選んでいて、代わりに何を選べばよいかまで言う。**
        # 「選択されていません」だけでは、画面を見られないと直しようがない
        return None, [], PlacementResult(
            PlacementStatus.NO_AUDIO_TRACK,
            build_no_audio_track_message(tracks))

    if not can_adapt and not check_channel_match(target, sample_channels):
        return None, [], PlacementResult(
            PlacementStatus.CHANNEL_MISMATCH,
            build_channel_mismatch_message(target, sample_channels),
            track_name=target.name)

    return target, tracks, None


def _multi_selection_warning(tracks, target) -> str:
    """複数トラックが選択されているときに添える文言。

    tracks は _prepare が取得した一覧をそのまま渡す（同じスナップショットで
    判定することで、配置先の決定と警告の食い違いも防ぐ）。
    """
    if count_selected_audio_tracks(tracks) > 1:
        return tr(f"（複数のトラックが選択されています。先頭の「{target.name}」を使用します）",
                  " (Several tracks are selected. Using the first one, "
                  f"“{target.name}”.)")
    return ""


def _verify_spot(client, target_name: str, filename: str):
    """実際の配置先を確かめる。問題があれば PlacementResult を返す。

    **PTSL の応答では判断できない。** チャンネル構成が合わないと
    Pro Tools は `dst_track_name` を黙って無視し、適合する別のトラックへ
    置いたうえで成功を返す（実機で確認済み）。
    """
    stem = os.path.splitext(os.path.basename(filename))[0]
    try:
        holders = client.tracks_holding(stem)
    except PTSLError:
        holders = []

    if not holders:
        # 確認できなかっただけの可能性もあるので、断定しない文言を使う
        return PlacementResult(
            PlacementStatus.VERIFY_FAILED,
            build_rolled_back_message(target_name),
            track_name=target_name)

    # 名前の形（濁点を分けた NFD か NFC か）をそろえて比べる。
    # tracks_holding はトラック名を NFC にそろえて返す
    if (unicodedata.normalize("NFC", target_name)
            not in [unicodedata.normalize("NFC", h) for h in holders]):
        others = COMMA.join(holders)
        return PlacementResult(
            PlacementStatus.CHANNEL_MISMATCH,
            tr(f"[警告] トラック「{target_name}」ではなく「{others}」に置かれたため、取り消しました。"
               "チャンネル構成が合っているか確認してください。",
               f"[Warning] The sample went to “{others}” instead of track "
               f"“{target_name}”, so it was undone. Check that the channel "
               "formats match."),
            track_name=target_name)

    return None


def _safe_undo(client, steps: int) -> bool:
    """巻き戻す。できたら True。後始末なので失敗しても例外は投げない。"""
    try:
        client.undo(steps)
    except PTSLError:
        return False
    return True


# ---------------------------------------------------------------------------
# legacy 経路（py-ptsl・一時トラック）
# ---------------------------------------------------------------------------

def place_sample(client: PTSLClientProtocol,
                 posix_path: str,
                 filename: str,
                 sample_channels: int | None) -> PlacementResult:
    """サンプル1件を選択トラックのカーソル位置へ配置する（legacy 経路）。

    **主経路は `place_sample_spot`。** こちらは py-ptsl を使う旧実装で、
    spot 経路が使えない場合の控えとして残してある。

    client は接続済み（コンテキストに入っている）前提。
    例外は内部で握って PlacementResult に変換する（UI を固めない）。
    """
    # 1. セッション確認
    try:
        if not client.session_is_open():
            return PlacementResult(
                PlacementStatus.NO_SESSION,
                build_result_message(PlacementStatus.NO_SESSION))
    except PTSLError:
        return PlacementResult(
            PlacementStatus.NOT_CONNECTED,
            build_result_message(PlacementStatus.NOT_CONNECTED))

    # 2. 配置先トラック特定
    try:
        tracks = client.list_tracks()
    except PTSLError as exc:
        return PlacementResult(
            PlacementStatus.NOT_CONNECTED,
            build_result_message(PlacementStatus.NOT_CONNECTED,
                                 detail=str(exc)))
    target = select_target_track(tracks)
    if target is None:
        return PlacementResult(
            PlacementStatus.NO_AUDIO_TRACK,
            build_no_audio_track_message(tracks))

    # 複数選択時は先頭を採用する旨を結果メッセージに添える
    multi_warn = _multi_selection_warning(tracks, target)

    # 3. チャンネル数チェック
    if not check_channel_match(target, sample_channels):
        return PlacementResult(
            PlacementStatus.CHANNEL_MISMATCH,
            build_channel_mismatch_message(target, sample_channels),
            track_name=target.name)

    return _run_placement(client, posix_path, filename,
                          target.name, multi_warn,
                          target_had_clips=target.contains_clips)


def _run_placement(client: PTSLClientProtocol,
                   posix_path: str,
                   filename: str,
                   target_name: str,
                   multi_warn: str,
                   target_had_clips: bool = False) -> PlacementResult:
    """配置の副作用フェーズ（取り込み→cut→paste→後始末→検証）。

    手順の途中で失敗したら、可能な範囲で後始末（一時トラックのクリア）を
    試みてから結果を返す。
    """
    saved_edit_mode = None
    temp_track: str | None = None
    try:
        # 4. カーソル TC 記録
        cursor_tc = client.get_cursor_timecode()

        # 5. 編集モードを Slip に
        saved_edit_mode = client.get_edit_mode()
        try:
            client.set_edit_mode(_SLIP_MODE)
        except PTSLError:
            pass  # モード変更失敗は致命的でないので続行

        # 6. 新規トラックへ取り込み
        try:
            new_tracks = client.import_audio_new_track(posix_path)
        except PTSLError as exc:
            return PlacementResult(
                PlacementStatus.IMPORT_FAILED,
                build_result_message(PlacementStatus.IMPORT_FAILED,
                                     detail=str(exc)))
        if not new_tracks:
            return PlacementResult(
                PlacementStatus.IMPORT_FAILED,
                build_result_message(
                    PlacementStatus.IMPORT_FAILED,
                    detail=tr("新規トラックを特定できませんでした",
                              "Could not find the new track")))
        temp_track = new_tracks[0]

        # 7. 取り込んだクリップを cut
        client.select_all_clips_on_track(temp_track)
        client.cut()

        # 8. 対象トラック選択 + カーソル復元 → paste
        client.select_tracks([target_name])
        client.set_cursor_timecode(cursor_tc)
        client.paste()

        # 9. 配置後検証（対象トラックに clips が付いたか）を recycle より先に行う。
        #    recycle の clear が paste 直後のクリップを巻き込み得るため、
        #    必ず検証を先に実施して結果を確定させる。
        #    配置前から clips があったトラックは contains_clips 比較で
        #    判定できないため、その場合は paste 完了をもって成功とみなす。
        if target_had_clips:
            verified = True
        else:
            verified = _verify_placed(client, target_name)

        # 10. 一時トラックを空にして SM_TEMP へリネーム（再利用基盤化）。
        #     検証後なので、ここでの clear は配置結果に影響しない。
        _recycle_temp_track(client, temp_track, protect_track=target_name)

        if not verified:
            return PlacementResult(
                PlacementStatus.VERIFY_FAILED,
                build_result_message(PlacementStatus.VERIFY_FAILED,
                                     track_name=target_name),
                track_name=target_name)

        # 12. 成功
        msg = build_result_message(
            PlacementStatus.OK, track_name=target_name,
            filename=filename, timecode=cursor_tc)
        if multi_warn:
            msg = msg + multi_warn
        return PlacementResult(PlacementStatus.OK, msg,
                               track_name=target_name)

    except PTSLError as exc:
        # 想定外の PTSL 失敗。一時トラックが残っていれば後始末を試みる。
        if temp_track:
            _recycle_temp_track(client, temp_track, protect_track=target_name)
        return PlacementResult(
            PlacementStatus.PASTE_FAILED,
            build_result_message(PlacementStatus.PASTE_FAILED,
                                 detail=str(exc)),
            track_name=target_name)
    finally:
        # 11. 編集モード復元
        if saved_edit_mode is not None:
            try:
                client.set_edit_mode(saved_edit_mode)
            except PTSLError:
                pass


# 編集モード EMO_Slip の値（PTSLClient と一致）。
_SLIP_MODE = 1


def _recycle_temp_track(client: PTSLClientProtocol, temp_track: str,
                        protect_track: str | None = None) -> None:
    """一時トラックのクリップを消し、SM_TEMP にリネームして再利用基盤化。

    既に SM_TEMP が存在する場合はリネームせず、クリップのクリアのみ行う。
    後始末なので失敗しても例外は投げない（ベストエフォート）。

    protect_track: 誤って消したくないトラック（=配置先）。clear が
    選択中トラックに作用する PT の挙動対策として、clear 前に必ず
    一時トラックを単独選択し、protect_track を選択から外す。
    """
    try:
        # clear が配置先トラックに波及しないよう、一時トラックを単独選択。
        client.select_tracks([temp_track])
        client.select_all_clips_on_track(temp_track)
        client.clear()
    except PTSLError:
        pass
    if temp_track == TEMP_TRACK_NAME:
        return
    try:
        existing = {t.name for t in client.list_tracks()}
        if TEMP_TRACK_NAME not in existing:
            client.rename_track(temp_track, TEMP_TRACK_NAME)
    except PTSLError:
        pass


def _verify_placed(client: PTSLClientProtocol, target_name: str) -> bool:
    """対象トラックにクリップが配置されたかを track_list で確認する。"""
    try:
        for t in client.list_tracks():
            if t.name == target_name:
                return t.contains_clips
    except PTSLError:
        return False
    return False
