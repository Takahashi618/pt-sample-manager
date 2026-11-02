"""spot 経路の配置サービスのユニットテスト（スタブ注入）。

`ptsl_spot_client.SpotClientProtocol` を満たす記録型スタブを使い、
PTSL なしでシーケンス・分岐・巻き戻しを検証する。

**このテストで最も重要なのは「応答を信じない」ことの検証。**
実機では `SpotClipsByID` が成功を返しても置いていないことがあり、
チャンネル構成が合わないと指定を無視して別のトラックへ置く。
"""

from __future__ import annotations

import unittest
from typing import List, Optional

from core.placement_planner import PlacementStatus, TrackFormat, TrackInfo
from integration.placement_service import place_sample_spot
from integration.ptsl_client import PTSLError

TRACK_TYPE_AUDIO = 2
TRACK_TYPE_AUX = 3
TA_EXPLICIT = 2
TA_NONE = 1


def audio_track(name: str, fmt: TrackFormat = TrackFormat.STEREO,
                selected: bool = False, edit: bool = False,
                clips: bool = False) -> TrackInfo:
    return TrackInfo(
        name=name,
        track_type=TRACK_TYPE_AUDIO,
        track_format=int(fmt),
        is_selected_state=TA_EXPLICIT if selected else TA_NONE,
        has_edit_selection_state=TA_EXPLICIT if edit else TA_NONE,
        contains_clips=clips,
    )


class FakeSpotClient:
    """SpotClientProtocol を満たすスタブ。呼び出しを記録する。

    `lands_on` に名前を渡すと、**指定と違うトラックへ置かれた**ことに
    できる。実機で起きる「応答は成功なのに別トラック」を再現するため。
    """

    def __init__(self, tracks: List[TrackInfo],
                 session_open: bool = True,
                 cursor_tc: str = "00:00:02:00",
                 cursor_samples: str = "88200",
                 cursor_bb: str = "1| 2| 373",
                 grid=(0, 16537.5),
                 clip_ids: Optional[List[str]] = None,
                 lands_on: Optional[str] = None,
                 import_error: str = "",
                 spot_error: str = "",
                 verify_blind: bool = False) -> None:
        self._tracks = list(tracks)
        self._session_open = session_open
        self._cursor = cursor_tc
        self._cursor_samples = cursor_samples
        self._cursor_bb = cursor_bb
        self._grid = grid
        self._clip_ids = clip_ids if clip_ids is not None else ["c-L", "c-R"]
        self._lands_on = lands_on
        self._import_error = import_error
        self._spot_error = spot_error
        self._verify_blind = verify_blind
        self.calls: List[str] = []
        self.placed_on: Optional[str] = None
        self.placed_at: List[str] = []
        self.time_types: List[str] = []
        self.undone = 0

    # --- Protocol 実装 ---
    def session_is_open(self) -> bool:
        return self._session_open

    def list_tracks(self) -> List[TrackInfo]:
        return list(self._tracks)

    def get_cursor_timecode(self) -> str:
        return self._cursor

    def get_cursor_samples(self) -> str:
        return self._cursor_samples

    def get_cursor_bars_beats(self) -> str:
        return self._cursor_bb

    def grid_info(self):
        return self._grid

    def session_sample_rate(self):
        return 44100.0

    def import_to_clip_list(self, posix_path: str) -> List[str]:
        self.calls.append(f"import:{posix_path}")
        if self._import_error:
            raise PTSLError(self._import_error)
        return list(self._clip_ids)

    def spot_clips(self, clip_ids: List[str], track_name: str,
                   location: str, time_type: str = "TLType_TimeCode") -> None:
        self.calls.append(f"spot:{track_name}@{location}:{len(clip_ids)}ids")
        self.time_types.append(time_type)
        if self._spot_error:
            raise PTSLError(self._spot_error)
        # 実機の挙動を模す。指定が通らないと別のトラックへ落ちる
        self.placed_on = self._lands_on or track_name
        self.placed_at.append(location)

    def tracks_holding(self, clip_name: str) -> List[str]:
        self.calls.append(f"verify:{clip_name}")
        if self._verify_blind or self.placed_on is None:
            return []
        return [self.placed_on]

    def undo(self, steps: int) -> None:
        self.calls.append(f"undo:{steps}")
        self.undone += steps
        self.placed_on = None
        self.placed_at.clear()


class TestSpotHappyPath(unittest.TestCase):
    def setUp(self):
        self.client = FakeSpotClient([audio_track("Audio 1", selected=True)])

    def place(self):
        return place_sample_spot(self.client, "/s/loop.wav", "loop.wav", 2)

    def test_succeeds(self):
        result = self.place()
        self.assertIs(result.status, PlacementStatus.OK)
        self.assertEqual(result.track_name, "Audio 1")

    def test_message_names_the_track_and_position(self):
        message = self.place().message
        self.assertIn("Audio 1", message)
        self.assertIn("00:00:02:00", message)
        self.assertIn("[情報]", message)

    def test_no_temp_track_is_created(self):
        """spot 経路の要。一時トラックも Copy / Paste も使わない。"""
        self.place()
        joined = " ".join(self.client.calls)
        for forbidden in ("cut", "paste", "rename", "clear"):
            self.assertNotIn(forbidden, joined)

    def test_sends_every_clip_id(self):
        """ステレオは複数 ID でひとつのクリップを構成する。"""
        self.place()
        # 位置はサンプルで渡す（タイムコードだとフレームに丸められる）
        self.assertIn("spot:Audio 1@88200:2ids", self.client.calls)

    def test_verifies_after_placing(self):
        self.place()
        self.assertIn("verify:loop", self.client.calls)

    def test_nothing_is_undone(self):
        self.place()
        self.assertEqual(self.client.undone, 0)


class TestSpotAtGivenPositions(unittest.TestCase):
    """小節・拍を指定して並べる。ループの複製がこれにあたる。"""

    def setUp(self):
        self.client = FakeSpotClient([audio_track("Audio 1", selected=True)])
        self.positions = ["1|1|000", "5|1|000", "9|1|000"]
        self.result = place_sample_spot(
            self.client, "/s/loop.wav", "loop.wav", 2, self.positions)

    def test_succeeds(self):
        self.assertIs(self.result.status, PlacementStatus.OK)

    def test_places_at_every_position(self):
        self.assertEqual(self.client.placed_at, self.positions)

    def test_imports_only_once(self):
        """取り込みは 1 回。同じクリップを位置の数だけ置く。"""
        imports = [c for c in self.client.calls if c.startswith("import:")]
        self.assertEqual(len(imports), 1)

    def test_uses_bars_beats_not_timecode(self):
        """位置を指定したときは小節・拍として渡すこと。

        タイムコードのまま渡すと、まったく違う場所に置かれる。
        """
        self.assertTrue(self.client.time_types)
        for t in self.client.time_types:
            self.assertEqual(t, "TLType_BarsBeats")

    def test_does_not_read_the_cursor(self):
        """位置を指定したならカーソルは関係ない。"""
        self.assertNotIn("cursor", " ".join(self.client.calls))

    def test_message_says_how_many(self):
        self.assertIn("3 個", self.result.message)

    def test_single_position_names_it(self):
        client = FakeSpotClient([audio_track("Audio 1", selected=True)])
        result = place_sample_spot(
            client, "/s/a.wav", "a.wav", 2, ["17|1|000"])
        self.assertIn("17|1|000", result.message)

    def test_failure_midway_undoes_everything(self):
        """3 個目で失敗したら、取り込みと 2 個分の配置を戻す。"""
        client = FakeSpotClient([audio_track("Audio 1", selected=True)],
                                spot_error="だめ")
        place_sample_spot(client, "/s/a.wav", "a.wav", 2, self.positions)
        self.assertEqual(client.undone, 1)  # 取り込みの 1 段のみ（初回で失敗）


class TestSpotUsesSamplesAtCursor2(unittest.TestCase):
    def test_cursor_placement_uses_samples(self):
        """**タイムコードでは置かない。** フレーム単位に丸められるため。"""
        client = FakeSpotClient([audio_track("Audio 1", selected=True)])
        place_sample_spot(client, "/s/a.wav", "a.wav", 2)
        self.assertEqual(client.time_types, ["TLType_Samples"])


class TestSpotLandsOnWrongTrack(unittest.TestCase):
    """**最も重要な分岐。** 応答は成功でも別のトラックに置かれる。"""

    def setUp(self):
        self.client = FakeSpotClient(
            [audio_track("Audio 1", selected=True)],
            lands_on="どこか別のトラック",
        )
        self.result = place_sample_spot(
            self.client, "/s/loop.wav", "loop.wav", 2)

    def test_is_not_reported_as_success(self):
        self.assertIsNot(self.result.status, PlacementStatus.OK)

    def test_rolls_back(self):
        self.assertEqual(self.client.undone, 2)

    def test_message_names_both_tracks(self):
        self.assertIn("Audio 1", self.result.message)
        self.assertIn("どこか別のトラック", self.result.message)

    def test_message_says_it_was_undone(self):
        self.assertIn("取り消し", self.result.message)


class TestSpotVerificationBlind(unittest.TestCase):
    """確認そのものができなかった場合は断定しない。"""

    def setUp(self):
        self.client = FakeSpotClient(
            [audio_track("Audio 1", selected=True)], verify_blind=True)
        self.result = place_sample_spot(
            self.client, "/s/loop.wav", "loop.wav", 2)

    def test_reports_verify_failed(self):
        self.assertIs(self.result.status, PlacementStatus.VERIFY_FAILED)

    def test_rolls_back(self):
        self.assertEqual(self.client.undone, 2)

    def test_does_not_say_it_was_placed(self):
        """取り消したあとなので、トラックには何も無い。"""
        self.assertIn("取り消しました", self.result.message)
        self.assertNotIn("配置を実行しました", self.result.message)


class TestSpotUndoFails(unittest.TestCase):
    """取り消せなかったら「取り消しました」と言わない。"""

    def _undo_fails(self, client) -> None:
        def undo(_steps):
            raise PTSLError("undo failed")
        client.undo = undo

    def test_cannot_confirm(self):
        client = FakeSpotClient(
            [audio_track("Audio 1", selected=True)], verify_blind=True)
        self._undo_fails(client)
        result = place_sample_spot(client, "/s/loop.wav", "loop.wav", 2)
        self.assertIs(result.status, PlacementStatus.VERIFY_FAILED)
        self.assertIn("取り消しにも失敗", result.message)
        self.assertIn("Audio 1", result.message)


class TestSpotGuards(unittest.TestCase):
    def test_no_session(self):
        client = FakeSpotClient([audio_track("Audio 1", selected=True)],
                                session_open=False)
        result = place_sample_spot(client, "/s/a.wav", "a.wav", 2)
        self.assertIs(result.status, PlacementStatus.NO_SESSION)
        self.assertEqual(client.calls, [])

    def test_no_audio_track_selected(self):
        client = FakeSpotClient([audio_track("Audio 1")])
        result = place_sample_spot(client, "/s/a.wav", "a.wav", 2)
        self.assertIs(result.status, PlacementStatus.NO_AUDIO_TRACK)

    def test_falls_back_to_edit_selection(self):
        client = FakeSpotClient([audio_track("Audio 1", edit=True)])
        result = place_sample_spot(client, "/s/a.wav", "a.wav", 2)
        self.assertIs(result.status, PlacementStatus.OK)

    def test_stereo_sample_on_mono_track_is_stopped(self):
        """実機では PTSL 側もエラーにするが、手前で止めるほうが速い。"""
        client = FakeSpotClient(
            [audio_track("Mono 1", fmt=TrackFormat.MONO, selected=True)])
        result = place_sample_spot(client, "/s/a.wav", "a.wav", 2)
        self.assertIs(result.status, PlacementStatus.CHANNEL_MISMATCH)
        self.assertEqual(client.calls, [])

    def test_mono_sample_on_stereo_track_is_stopped(self):
        client = FakeSpotClient([audio_track("Audio 1", selected=True)])
        result = place_sample_spot(client, "/s/a.wav", "a.wav", 1)
        self.assertIs(result.status, PlacementStatus.CHANNEL_MISMATCH)

    def test_unknown_channel_count_does_not_block(self):
        client = FakeSpotClient([audio_track("Audio 1", selected=True)])
        result = place_sample_spot(client, "/s/a.wav", "a.wav", None)
        self.assertIs(result.status, PlacementStatus.OK)

    def test_multiple_selection_is_mentioned(self):
        client = FakeSpotClient([
            audio_track("Audio 1", selected=True),
            audio_track("Audio 2", selected=True),
        ])
        result = place_sample_spot(client, "/s/a.wav", "a.wav", 2)
        self.assertIs(result.status, PlacementStatus.OK)
        self.assertIn("複数", result.message)
        self.assertEqual(result.track_name, "Audio 1")

    def test_aux_track_is_not_a_target(self):
        aux = TrackInfo(name="AUX1", track_type=TRACK_TYPE_AUX,
                        track_format=int(TrackFormat.STEREO),
                        is_selected_state=TA_EXPLICIT,
                        has_edit_selection_state=TA_NONE)
        client = FakeSpotClient([aux])
        result = place_sample_spot(client, "/s/a.wav", "a.wav", 2)
        self.assertIs(result.status, PlacementStatus.NO_AUDIO_TRACK)


class TestSpotFailures(unittest.TestCase):
    def test_import_failure_is_reported(self):
        client = FakeSpotClient([audio_track("Audio 1", selected=True)],
                                import_error="変換できません")
        result = place_sample_spot(client, "/s/a.wav", "a.wav", 2)
        self.assertIs(result.status, PlacementStatus.IMPORT_FAILED)
        self.assertIn("変換できません", result.message)

    def test_import_failure_does_not_undo(self):
        """取り込みが失敗したならセッションは変わっていない。"""
        client = FakeSpotClient([audio_track("Audio 1", selected=True)],
                                import_error="だめ")
        place_sample_spot(client, "/s/a.wav", "a.wav", 2)
        self.assertEqual(client.undone, 0)

    def test_spot_failure_undoes_the_import(self):
        client = FakeSpotClient([audio_track("Audio 1", selected=True)],
                                spot_error="構成が合いません")
        result = place_sample_spot(client, "/s/a.wav", "a.wav", 2)
        self.assertIs(result.status, PlacementStatus.PASTE_FAILED)
        self.assertEqual(client.undone, 1)

    def test_no_exception_escapes(self):
        """UI を固めないこと。例外は文言に変換する。"""
        client = FakeSpotClient([audio_track("Audio 1", selected=True)],
                                spot_error="なにか")
        result = place_sample_spot(client, "/s/a.wav", "a.wav", 2)
        self.assertTrue(result.message)


class TestSpotClientMapping(unittest.TestCase):
    """PTSL の文字列 enum を placement_planner の数値へ写す部分。

    ここがずれると、選択されていないトラックに置いたり、
    Aux を配置先に選んだりする。
    """

    def setUp(self):
        from integration import ptsl_spot_client as spot
        self.spot = spot

    def test_audio_track_type(self):
        self.assertEqual(self.spot._TRACK_TYPES["TT_Audio"], TRACK_TYPE_AUDIO)
        self.assertEqual(self.spot._TRACK_TYPES["TType_Audio"],
                         TRACK_TYPE_AUDIO)

    def test_aux_is_not_audio(self):
        self.assertNotEqual(self.spot._TRACK_TYPES["TT_Aux"], TRACK_TYPE_AUDIO)

    def test_formats(self):
        self.assertEqual(self.spot._TRACK_FORMATS["TF_Mono"],
                         int(TrackFormat.MONO))
        self.assertEqual(self.spot._TRACK_FORMATS["TF_Stereo"],
                         int(TrackFormat.STEREO))

    def test_selected_state_crosses_the_threshold(self):
        """SetExplicitly 以上が「選択されている」。"""
        self.assertGreaterEqual(self.spot._ATTR_STATES["SetExplicitly"],
                                TA_EXPLICIT)
        self.assertLess(self.spot._ATTR_STATES["None"], TA_EXPLICIT)

    def test_unknown_value_is_zero(self):
        self.assertEqual(self.spot._TRACK_TYPES.get("知らない値", 0), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestSpotPrefersGivenTrack(unittest.TestCase):
    """**配置先をアプリ側で指定する。**

    画面を見ずに Pro Tools でトラックを選ぶのは難しく、選び損ねると
    配置が失敗する。名前で指定できれば選択状態に左右されない。
    """

    def setUp(self):
        self.client = FakeSpotClient([
            audio_track("Audio 1", selected=True),   # 選ばれているのはこちら
            audio_track("Audio 3"),
        ])

    def test_uses_the_named_track_not_the_selected_one(self):
        result = place_sample_spot(
            self.client, "/s/a.wav", "a.wav", 2, prefer_track="Audio 3")
        self.assertIs(result.status, PlacementStatus.OK)
        self.assertEqual(result.track_name, "Audio 3")
        self.assertEqual(self.client.placed_on, "Audio 3")

    def test_falls_back_to_selection_when_not_given(self):
        result = place_sample_spot(self.client, "/s/a.wav", "a.wav", 2)
        self.assertEqual(result.track_name, "Audio 1")

    def test_works_even_with_nothing_selected(self):
        """**選択が外れていても置ける。** これが指定する目的。"""
        client = FakeSpotClient([audio_track("Audio 3")])  # 選択なし
        result = place_sample_spot(
            client, "/s/a.wav", "a.wav", 2, prefer_track="Audio 3")
        self.assertIs(result.status, PlacementStatus.OK)

    def test_missing_track_is_reported_with_the_choices(self):
        result = place_sample_spot(
            self.client, "/s/a.wav", "a.wav", 2, prefer_track="Audio 9")
        self.assertIs(result.status, PlacementStatus.NO_AUDIO_TRACK)
        self.assertIn("Audio 9", result.message)
        self.assertIn("Audio 1", result.message)
        self.assertEqual(self.client.calls, [])

    def test_does_not_warn_about_multiple_selection(self):
        """指定しているなら、何が選ばれていようと関係ない。"""
        client = FakeSpotClient([
            audio_track("Audio 1", selected=True),
            audio_track("Audio 2", selected=True),
            audio_track("Audio 3"),
        ])
        result = place_sample_spot(
            client, "/s/a.wav", "a.wav", 2, prefer_track="Audio 3")
        self.assertNotIn("複数", result.message)


class TestSpotNoAudioTrackMessage(unittest.TestCase):
    """失敗の原因が分かる文言になっていること。"""

    def test_names_the_selected_non_audio_track(self):
        aux = TrackInfo(name="Inst1", track_type=TRACK_TYPE_AUX,
                        track_format=int(TrackFormat.STEREO),
                        is_selected_state=TA_EXPLICIT,
                        has_edit_selection_state=TA_NONE)
        client = FakeSpotClient([aux, audio_track("Audio 1")])
        result = place_sample_spot(client, "/s/a.wav", "a.wav", 2)
        self.assertIs(result.status, PlacementStatus.NO_AUDIO_TRACK)
        self.assertIn("Inst1", result.message)
        self.assertIn("Audio 1", result.message)


class TestSpotUsesSamplesAtCursor(unittest.TestCase):
    """**カーソル位置はサンプルで渡す。**

    タイムコードはフレーム単位なので、拍や 16 分音符の位置が
    フレーム境界に乗らず、置いた位置がずれる（実機で確認）。
    """

    def setUp(self):
        self.client = FakeSpotClient(
            [audio_track("Audio 1", selected=True)],
            cursor_tc="00:00:02:00", cursor_samples="88200")
        self.result = place_sample_spot(
            self.client, "/s/a.wav", "a.wav", 2)

    def test_places_by_samples(self):
        self.assertEqual(self.client.time_types, ["TLType_Samples"])
        self.assertEqual(self.client.placed_at, ["88200"])

    def test_reads_out_the_timecode(self):
        """位置の読み上げは分かりやすいタイムコードのまま。"""
        self.assertIn("00:00:02:00", self.result.message)

    def test_falls_back_to_timecode_without_sample_support(self):
        """サンプルを返せない相手なら、従来どおりタイムコードで置く。"""
        client = FakeSpotClient([audio_track("Audio 1", selected=True)])
        del client.__class__.get_cursor_samples
        try:
            place_sample_spot(client, "/s/a.wav", "a.wav", 2)
            self.assertEqual(client.time_types, ["TLType_TimeCode"])
        finally:
            FakeSpotClient.get_cursor_samples = (
                lambda self: self._cursor_samples)

    def test_given_positions_still_use_bars_beats(self):
        """並べる指定は小節・拍のまま（テンポマップに追従させる）。"""
        client = FakeSpotClient([audio_track("Audio 1", selected=True)])
        place_sample_spot(client, "/s/a.wav", "a.wav", 2, ["5|1|000"])
        self.assertEqual(client.time_types, ["TLType_BarsBeats"])


class TestSpotSnapsToBeat(unittest.TestCase):
    """**拍に合わせて置く。**

    画面を見ずにカーソルを拍の上へ置くのは難しく、外れた位置に置くと
    メトロノームと合わない（実機で確認: 拍の半分で 187 ミリ秒）。
    """

    def _place(self, cursor, snap=True):
        client = FakeSpotClient(
            [audio_track("Audio 1", selected=True)],
            cursor_samples=cursor)
        result = place_sample_spot(
            client, "/s/a.wav", "a.wav", 2, snap_to_beat=snap)
        return client, result

    def test_moves_an_off_beat_cursor_to_the_nearest_beat(self):
        client, _ = self._place("16000")
        self.assertEqual(client.placed_at, ["16538"])

    def test_says_that_it_moved(self):
        _, result = self._place("16000")
        self.assertIn("拍から外れていた", result.message)

    def test_leaves_an_on_beat_cursor_alone(self):
        client, result = self._place("33075")
        self.assertEqual(client.placed_at, ["33075"])
        self.assertNotIn("拍から外れていた", result.message)

    def test_does_nothing_when_switched_off(self):
        """意図して拍の外に置きたいときは外せる。"""
        client, _ = self._place("16000", snap=False)
        self.assertEqual(client.placed_at, ["16000"])

    def test_placing_still_succeeds_without_a_grid(self):
        """テンポが取れなくても配置は続ける。"""
        client = FakeSpotClient(
            [audio_track("Audio 1", selected=True)],
            cursor_samples="16000", grid=None)
        result = place_sample_spot(
            client, "/s/a.wav", "a.wav", 2, snap_to_beat=True)
        self.assertIs(result.status, PlacementStatus.OK)
        self.assertEqual(client.placed_at, ["16000"])

    def test_given_positions_are_not_snapped(self):
        """小節・拍で指定したなら、そのままの位置に置く。"""
        client = FakeSpotClient([audio_track("Audio 1", selected=True)])
        place_sample_spot(client, "/s/a.wav", "a.wav", 2, ["5|1|000"],
                          snap_to_beat=True)
        self.assertEqual(client.placed_at, ["5|1|000"])


class TestSnapTrustsBarsBeats(unittest.TestCase):
    """**拍の上にいるなら、計算がどう言おうと動かさない。**

    等間隔グリッドは先頭のテンポで測った近似なので、テンポが途中で
    変わるセッションでは後半の拍と食い違う。拍上のカーソルを
    「ずれている」と誤判定して動かすのが最悪のため、Pro Tools 自身の
    小節・拍表現（ティック 000 = 拍の上）を先に信じる。
    """

    def test_on_beat_by_bars_beats_is_left_alone(self):
        # 等間隔計算では拍から外れて見える位置（200000 は 16537.5 の
        # 倍数でない）だが、小節・拍表現は拍の上を指している
        client = FakeSpotClient(
            [audio_track("Audio 1", selected=True)],
            cursor_samples="200000", cursor_bb="9| 1| 000")
        result = place_sample_spot(
            client, "/s/a.wav", "a.wav", 2, snap_to_beat=True)
        self.assertIs(result.status, PlacementStatus.OK)
        self.assertEqual(client.placed_at, ["200000"])
        self.assertNotIn("拍から外れていた", result.message)

    def test_off_beat_ticks_still_snap(self):
        """ティックが 000 でなければ従来どおり合わせる。"""
        client = FakeSpotClient(
            [audio_track("Audio 1", selected=True)],
            cursor_samples="16000", cursor_bb="1| 1| 373")
        place_sample_spot(client, "/s/a.wav", "a.wav", 2, snap_to_beat=True)
        self.assertEqual(client.placed_at, ["16538"])

    def test_unparsable_bars_beats_falls_back_to_the_grid(self):
        """小節・拍が読めない形でも、等間隔グリッドの判定で続行する。"""
        client = FakeSpotClient(
            [audio_track("Audio 1", selected=True)],
            cursor_samples="16000", cursor_bb="こわれた値")
        place_sample_spot(client, "/s/a.wav", "a.wav", 2, snap_to_beat=True)
        self.assertEqual(client.placed_at, ["16538"])


class TestSpotVerifyIgnoresNormalization(unittest.TestCase):
    """配置の確認は、トラック名の書き方（NFD と NFC）の違いで外れない。

    クライアントの tracks_holding はトラック名を NFC にそろえて返す。
    配置先の名前が NFD だと、置けているのに「別のトラックに置かれた」と
    判断して取り消してしまっていた。
    """

    def test_decomposed_target_name_is_accepted(self):
        import unicodedata

        name = unicodedata.normalize("NFD", "ボーカル")

        class NfcClient(FakeSpotClient):
            def tracks_holding(self, clip_name):
                held = super().tracks_holding(clip_name)
                return [unicodedata.normalize("NFC", h) for h in held]

        client = NfcClient([audio_track(name, selected=True)])
        result = place_sample_spot(client, "/s/a.wav", "a.wav", 2)
        self.assertIs(result.status, PlacementStatus.OK)
        self.assertEqual(client.undone, 0)
