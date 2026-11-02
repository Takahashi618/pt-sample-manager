"""placement_service のユニットテスト（スタブPTSLクライアント注入）。

PTSLClientProtocol を満たす記録型スタブを使い、配置サービスの
シーケンス・分岐・後始末を PTSL なしで検証する。
"""

from __future__ import annotations

import unittest
from typing import List, Optional

from core.placement_planner import PlacementStatus, TrackFormat, TrackInfo
from integration.placement_service import TEMP_TRACK_NAME, place_sample

TRACK_TYPE_AUDIO = 2
TA_EXPLICIT = 2
TA_NONE = 1


class FakePTSLClient:
    """PTSLClientProtocol を満たすスタブ。呼び出しを記録する。

    内部に簡易なトラック表を持ち、import/cut/paste/clear/rename を
    その表に反映して、配置サービスの検証ロジックが通る程度に振る舞う。
    """

    def __init__(self, tracks: List[TrackInfo],
                 session_open: bool = True,
                 cursor_tc: str = "00:00:00:00.00") -> None:
        self._tracks = {t.name: t for t in tracks}
        self._session_open = session_open
        self._cursor = cursor_tc
        self.calls: List[str] = []
        self._clipboard_has_audio = False
        self._import_counter = 0

    # --- 記録ヘルパ ---
    def _log(self, name: str) -> None:
        self.calls.append(name)

    # --- Protocol 実装 ---
    def session_is_open(self) -> bool:
        return self._session_open

    def session_sample_rate(self) -> Optional[int]:
        return 44100

    def list_tracks(self) -> List[TrackInfo]:
        return list(self._tracks.values())

    def get_cursor_timecode(self) -> str:
        return self._cursor

    def set_cursor_timecode(self, timecode: str) -> None:
        self._log(f"set_cursor:{timecode}")
        self._cursor = timecode

    def import_audio_new_track(self, posix_path: str) -> List[str]:
        self._log(f"import:{posix_path}")
        self._import_counter += 1
        new_name = f"imported_{self._import_counter}"
        # 取り込んだ新規トラック（クリップあり・モノ）を表に追加
        self._tracks[new_name] = TrackInfo(
            name=new_name, track_type=TRACK_TYPE_AUDIO,
            track_format=int(TrackFormat.MONO),
            is_selected_state=TA_NONE, has_edit_selection_state=TA_NONE,
            contains_clips=True)
        return [new_name]

    def select_all_clips_on_track(self, track_name: str) -> None:
        self._log(f"select_clips:{track_name}")
        self._sel_track = track_name

    def cut(self) -> None:
        self._log("cut")
        # 選択中トラックのクリップをクリップボードへ移す
        self._clipboard_has_audio = True
        if getattr(self, "_sel_track", None) in self._tracks:
            t = self._tracks[self._sel_track]
            self._tracks[self._sel_track] = _with_clips(t, False)

    def paste(self) -> None:
        self._log("paste")
        # クリップボードの内容を「選択トラック」に貼る
        if self._clipboard_has_audio:
            for name in getattr(self, "_selected_tracks", []):
                if name in self._tracks:
                    self._tracks[name] = _with_clips(self._tracks[name], True)
            self._clipboard_has_audio = False

    def clear(self) -> None:
        self._log("clear")
        if getattr(self, "_sel_track", None) in self._tracks:
            t = self._tracks[self._sel_track]
            self._tracks[self._sel_track] = _with_clips(t, False)

    def select_tracks(self, names: List[str]) -> None:
        self._log(f"select_tracks:{','.join(names)}")
        self._selected_tracks = names

    def rename_track(self, old: str, new: str) -> None:
        self._log(f"rename:{old}->{new}")
        if old in self._tracks:
            t = self._tracks.pop(old)
            self._tracks[new] = TrackInfo(
                name=new, track_type=t.track_type,
                track_format=t.track_format,
                is_selected_state=t.is_selected_state,
                has_edit_selection_state=t.has_edit_selection_state,
                contains_clips=t.contains_clips)

    def get_edit_mode(self) -> Optional[int]:
        return 1

    def set_edit_mode(self, mode: int) -> None:
        self._log(f"set_edit_mode:{mode}")


def _with_clips(t: TrackInfo, has: bool) -> TrackInfo:
    return TrackInfo(
        name=t.name, track_type=t.track_type, track_format=t.track_format,
        is_selected_state=t.is_selected_state,
        has_edit_selection_state=t.has_edit_selection_state,
        contains_clips=has)


def audio(name, fmt=TrackFormat.MONO, selected=TA_NONE, clips=False):
    return TrackInfo(
        name=name, track_type=TRACK_TYPE_AUDIO, track_format=int(fmt),
        is_selected_state=selected, has_edit_selection_state=TA_NONE,
        contains_clips=clips)


class TestPlaceSampleHappyPath(unittest.TestCase):
    def test_successful_placement_on_empty_mono_track(self):
        client = FakePTSLClient([
            audio("Audio 1", fmt=TrackFormat.MONO, selected=TA_EXPLICIT),
        ])
        result = place_sample(client, "/path/kick.wav", "kick.wav", 1)
        self.assertEqual(result.status, PlacementStatus.OK)
        self.assertEqual(result.track_name, "Audio 1")
        self.assertIn("kick.wav", result.message)
        # シーケンスが正しい順で呼ばれたか
        self.assertIn("import:/path/kick.wav", client.calls)
        self.assertIn("cut", client.calls)
        self.assertIn("paste", client.calls)
        # import が cut より先、cut が paste より先
        self.assertLess(client.calls.index("import:/path/kick.wav"),
                        client.calls.index("cut"))
        self.assertLess(client.calls.index("cut"),
                        client.calls.index("paste"))

    def test_temp_track_recycled_to_sm_temp(self):
        client = FakePTSLClient([
            audio("Audio 1", selected=TA_EXPLICIT),
        ])
        place_sample(client, "/p/snare.wav", "snare.wav", 1)
        # 一時トラックが SM_TEMP にリネームされている
        rename_calls = [c for c in client.calls if c.startswith("rename:")]
        self.assertTrue(any(TEMP_TRACK_NAME in c for c in rename_calls))
        names = {t.name for t in client.list_tracks()}
        self.assertIn(TEMP_TRACK_NAME, names)

    def test_cursor_timecode_preserved(self):
        client = FakePTSLClient(
            [audio("Audio 1", selected=TA_EXPLICIT)],
            cursor_tc="00:01:23:04.00")
        place_sample(client, "/p/x.wav", "x.wav", 1)
        # paste 前にカーソルTCが復元される
        self.assertIn("set_cursor:00:01:23:04.00", client.calls)


class TestPlaceSampleGuards(unittest.TestCase):
    def test_no_session_returns_no_session_status(self):
        client = FakePTSLClient([audio("A", selected=TA_EXPLICIT)],
                                session_open=False)
        result = place_sample(client, "/p/x.wav", "x.wav", 1)
        self.assertEqual(result.status, PlacementStatus.NO_SESSION)

    def test_no_audio_track_selected(self):
        client = FakePTSLClient([audio("Audio 1")])  # 非選択
        result = place_sample(client, "/p/x.wav", "x.wav", 1)
        self.assertEqual(result.status, PlacementStatus.NO_AUDIO_TRACK)
        # 取り込みは行われない
        self.assertFalse(any(c.startswith("import:") for c in client.calls))

    def test_channel_mismatch_aborts_before_import(self):
        client = FakePTSLClient([
            audio("Audio 1", fmt=TrackFormat.MONO, selected=TA_EXPLICIT),
        ])
        # モノトラックにステレオサンプル
        result = place_sample(client, "/p/stereo.wav", "stereo.wav", 2)
        self.assertEqual(result.status, PlacementStatus.CHANNEL_MISMATCH)
        # 取り込み前に中断される（副作用なし）
        self.assertFalse(any(c.startswith("import:") for c in client.calls))

    def test_placement_on_track_with_existing_clips(self):
        # 既にクリップがあるトラックへの配置も成功扱い
        client = FakePTSLClient([
            audio("Audio 1", selected=TA_EXPLICIT, clips=True),
        ])
        result = place_sample(client, "/p/x.wav", "x.wav", 1)
        self.assertEqual(result.status, PlacementStatus.OK)

    def test_multi_selection_warns_but_succeeds(self):
        client = FakePTSLClient([
            audio("Audio 1", selected=TA_EXPLICIT),
            audio("Audio 2", selected=TA_EXPLICIT),
        ])
        result = place_sample(client, "/p/x.wav", "x.wav", 1)
        self.assertEqual(result.status, PlacementStatus.OK)
        # 先頭トラックが使われ、複数選択の警告が付く
        self.assertEqual(result.track_name, "Audio 1")
        self.assertIn("複数", result.message)


if __name__ == "__main__":
    unittest.main()
