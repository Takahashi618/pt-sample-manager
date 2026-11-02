"""PTSL クライアントの薄いラッパ（macOS / Pro Tools 専用）。

py-ptsl を直接使うが、以下の責務をここに閉じ込める:
- 接続のライフサイクル管理（コンテキストマネージャ）
- py-ptsl 601.0.1 の import_audio バグ回避（import_type=2 を直接指定）
- PTSL の Track メッセージ → core.placement_planner.TrackInfo への詰め替え
- 例外を PTSLError に正規化

これにより、上位（strategies / placement_service）は PTSL の生 API や
protobuf 型を意識せずに済み、テストでは PTSLClientProtocol を満たす
スタブを差し込める。

注意（実機調査で確定済み）:
- engine.import_audio() は import_type=1(Session) をハードコードした
  バグがあり使用不可。低レベル op を import_type=2(Audio) で呼ぶ。
- パスは POSIX のままでよい（HFS 変換不要）。
- トラック削除 API は無い。一時トラックは clear + rename で再利用する。
"""

from __future__ import annotations

from typing import List, Optional, Protocol

from core.i18n import tr
from core.placement_planner import TrackInfo

# py-ptsl が無い環境のユニットテストでは、このモジュールを import しない
# （スタブ側を使う）ため、遅延 import にする。


class PTSLError(Exception):
    """PTSL 操作の失敗を表す正規化済み例外。"""


class PTSLClientProtocol(Protocol):
    """配置に必要な PTSL 操作の最小インターフェース。

    placement_service はこの Protocol にのみ依存する。実体は
    PTSLClient（実機）または NullPTSLClient（テスト）。
    """

    def session_is_open(self) -> bool: ...
    def session_sample_rate(self) -> Optional[int]: ...
    def list_tracks(self) -> List[TrackInfo]: ...
    def get_cursor_timecode(self) -> str: ...
    def import_audio_new_track(self, posix_path: str) -> List[str]: ...
    def select_all_clips_on_track(self, track_name: str) -> None: ...
    def cut(self) -> None: ...
    def select_tracks(self, names: List[str]) -> None: ...
    def set_cursor_timecode(self, timecode: str) -> None: ...
    def paste(self) -> None: ...
    def clear(self) -> None: ...
    def rename_track(self, old: str, new: str) -> None: ...
    def get_edit_mode(self) -> Optional[int]: ...
    def set_edit_mode(self, mode: int) -> None: ...


class PTSLClient:
    """py-ptsl の Engine を包む実体クライアント（macOS 実機専用）。

    with PTSLClient() as client: ... の形で使う。__enter__ で接続し、
    __exit__ で確実に閉じる。
    """

    # 編集モード EMO_Slip の enum 値（実機調査で確認）。
    # paste をカーソル位置へ確実に行うため配置時はこのモードにする。
    EDIT_MODE_SLIP = 1  # EditMode.EMO_Slip

    def __init__(self, company_name: str = "sample_manager",
                 application_name: str = "placement") -> None:
        self._company = company_name
        self._app = application_name
        self._engine = None
        self._pt = None
        self._ops = None

    def __enter__(self) -> "PTSLClient":
        try:
            from ptsl import open_engine
            from ptsl import PTSL_pb2 as pt
            from ptsl import ops
        except ImportError as exc:
            raise PTSLError(tr(f"py-ptsl を import できません: {exc}",
                               f"Cannot import py-ptsl: {exc}")) from exc
        self._pt = pt
        self._ops = ops
        try:
            # open_engine 自体がコンテキストマネージャなので __enter__ を呼ぶ
            self._engine_cm = open_engine(company_name=self._company,
                                          application_name=self._app)
            self._engine = self._engine_cm.__enter__()
        except Exception as exc:
            raise PTSLError(tr(f"Pro Tools への接続に失敗しました: {exc}",
                               "Could not connect to Pro Tools: "
                               f"{exc}")) from exc
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if getattr(self, "_engine_cm", None) is not None:
            try:
                self._engine_cm.__exit__(exc_type, exc_val, exc_tb)
            except Exception:
                pass
        self._engine = None

    # ------------------------------------------------------------------
    # セッション情報
    # ------------------------------------------------------------------
    def session_is_open(self) -> bool:
        try:
            name = self._engine.session_name()
            return bool(name)
        except Exception:
            return False

    def session_sample_rate(self) -> Optional[int]:
        try:
            return self._engine.session_sample_rate()
        except Exception:
            return None

    # ------------------------------------------------------------------
    # トラック一覧の取得（PTSL Track → TrackInfo 詰め替え）
    # ------------------------------------------------------------------
    def list_tracks(self) -> List[TrackInfo]:
        try:
            tracks = self._engine.track_list()
        except Exception as exc:
            raise PTSLError(tr(f"トラック一覧の取得に失敗: {exc}",
                               "Could not get the track list: "
                               f"{exc}")) from exc
        result: List[TrackInfo] = []
        for track in tracks:
            attrs = track.track_attributes
            result.append(TrackInfo(
                name=track.name,
                track_type=int(track.type),
                track_format=int(track.format),
                is_selected_state=int(attrs.is_selected),
                has_edit_selection_state=int(attrs.has_edit_selection),
                contains_clips=bool(attrs.contains_clips),
            ))
        return result

    # ------------------------------------------------------------------
    # 編集カーソル位置（タイムコード文字列）
    # ------------------------------------------------------------------
    def get_cursor_timecode(self) -> str:
        """編集カーソル（タイムライン選択の in 点）を TimeCode 文字列で返す。"""
        try:
            fmt = self._pt.TimelineLocationType.TLType_TimeCode
            in_time, _out = self._engine.get_timeline_selection(format=fmt)
            return in_time
        except Exception as exc:
            raise PTSLError(tr(f"カーソル位置の取得に失敗: {exc}",
                               "Could not get the cursor "
                               f"position: {exc}")) from exc

    def set_cursor_timecode(self, timecode: str) -> None:
        """タイムライン選択の in 点を TimeCode で設定する（カーソル移動）。"""
        try:
            fmt = self._pt.TimelineLocationType.TLType_TimeCode
            self._engine.set_timeline_selection(
                in_time=timecode, location_type=fmt)
        except Exception as exc:
            raise PTSLError(tr(f"カーソル位置の設定に失敗: {exc}",
                               "Could not set the cursor "
                               f"position: {exc}")) from exc

    # ------------------------------------------------------------------
    # オーディオ取り込み（py-ptsl バグ回避: import_type=2 を直接指定）
    # ------------------------------------------------------------------
    def import_audio_new_track(self, posix_path: str) -> List[str]:
        """新規トラックにオーディオを取り込み、増えたトラック名を返す。

        engine.import_audio() のバグ（import_type=1 ハードコード）を避け、
        低レベル op を import_type=2(Audio) で組み立てて実行する。
        取り込み前後の track_list 差分で新規トラック名を特定して返す。
        """
        pt, ops = self._pt, self._ops
        before = {t.name for t in self._engine.track_list()}
        location_data = pt.SpotLocationData(
            location_type=pt.SpotLocationType.Start,
            location_options=pt.TrackOffsetOptions.TimeCode,
            location_value=None,
        )
        audio_data = pt.AudioData(
            file_list=[posix_path],
            audio_operations=pt.AudioOperations.AddAudio,
            audio_destination=pt.MediaDestination.MD_NewTrack,
            audio_location=pt.MediaLocation.ML_SessionStart,
            location_data=location_data,
        )
        try:
            op = ops.CId_Import(import_type=2, audio_data=audio_data)
            self._engine.client.run(op)
        except Exception as exc:
            msg = getattr(exc, "message", str(exc))
            raise PTSLError(tr(f"オーディオ取り込みに失敗: {msg}",
                               f"Audio import failed: {msg}")) from exc
        after = {t.name for t in self._engine.track_list()}
        return sorted(after - before)

    # ------------------------------------------------------------------
    # 編集操作
    # ------------------------------------------------------------------
    def select_all_clips_on_track(self, track_name: str) -> None:
        try:
            self._engine.select_all_clips_on_track(track_name)
        except Exception as exc:
            raise PTSLError(tr(f"クリップ選択に失敗: {exc}",
                               f"Could not select the clip: {exc}")) from exc

    def cut(self) -> None:
        try:
            self._engine.cut()
        except Exception as exc:
            raise PTSLError(tr(f"カットに失敗: {exc}",
                               f"Cut failed: {exc}")) from exc

    def paste(self) -> None:
        try:
            self._engine.paste()
        except Exception as exc:
            raise PTSLError(tr(f"ペーストに失敗: {exc}",
                               f"Paste failed: {exc}")) from exc

    def clear(self) -> None:
        try:
            self._engine.clear()
        except Exception as exc:
            raise PTSLError(tr(f"クリア（クリップ削除）に失敗: {exc}",
                               f"Could not clear the clip: {exc}")) from exc

    def select_tracks(self, names: List[str]) -> None:
        try:
            self._engine.select_tracks_by_name(names)
        except Exception as exc:
            raise PTSLError(tr(f"トラック選択に失敗: {exc}",
                               f"Could not select the track: {exc}")) from exc

    def rename_track(self, old: str, new: str) -> None:
        try:
            self._engine.rename_target_track(old, new)
        except Exception as exc:
            raise PTSLError(tr(f"トラック名変更に失敗: {exc}",
                               f"Could not rename the track: {exc}")) from exc

    def get_edit_mode(self) -> Optional[int]:
        """現在の編集モード enum 値を返す（取得失敗時は None）。"""
        try:
            info = self._engine.get_edit_mode()
            # get_edit_mode は current_setting フィールドを持つメッセージを返す
            return int(info.current_setting)
        except Exception:
            return None

    def set_edit_mode(self, mode: int) -> None:
        try:
            self._engine.set_edit_mode(mode)
        except Exception as exc:
            raise PTSLError(tr(f"編集モード変更に失敗: {exc}",
                               "Could not change the edit mode: "
                               f"{exc}")) from exc
