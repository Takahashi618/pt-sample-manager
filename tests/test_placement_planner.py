"""placement_planner（純粋ロジック）のユニットテスト。

PTSL に一切依存しないため、Windows/macOS どちらでも実行可能。
"""

from __future__ import annotations

import unittest

from core.placement_planner import (
    TRACK_TYPE_AUDIO,
    PlacementStatus,
    TrackFormat,
    TrackInfo,
    build_channel_mismatch_message,
    build_missing_track_message,
    build_no_audio_track_message,
    build_result_message,
    check_channel_match,
    count_selected_audio_tracks,
    describe_snap_ja,
    find_audio_track,
    snap_to_grid,
    select_target_track,
)

# TrackType の非オーディオ値（Instrument=11 を例に）
TRACK_TYPE_INSTRUMENT = 11
# TrackAttributeState
TA_NONE = 1
TA_EXPLICIT = 2


def audio_track(name, fmt=TrackFormat.MONO, selected=TA_NONE,
                edit=TA_NONE, clips=False):
    return TrackInfo(
        name=name, track_type=TRACK_TYPE_AUDIO, track_format=int(fmt),
        is_selected_state=selected, has_edit_selection_state=edit,
        contains_clips=clips)


def inst_track(name, selected=TA_NONE, edit=TA_NONE):
    return TrackInfo(
        name=name, track_type=TRACK_TYPE_INSTRUMENT,
        track_format=int(TrackFormat.STEREO),
        is_selected_state=selected, has_edit_selection_state=edit)


class TestSelectTargetTrack(unittest.TestCase):
    def test_prefers_explicitly_selected_audio(self):
        tracks = [
            inst_track("Inst1", selected=TA_EXPLICIT),  # 選択だが非オーディオ
            audio_track("Audio 1", selected=TA_EXPLICIT),
            audio_track("Audio 2"),
        ]
        target = select_target_track(tracks)
        self.assertIsNotNone(target)
        self.assertEqual(target.name, "Audio 1")

    def test_falls_back_to_edit_selection(self):
        # 明示選択のオーディオが無い → has_edit_selection を使う
        tracks = [
            audio_track("Audio 1"),
            audio_track("Audio 2", edit=TA_EXPLICIT),
        ]
        target = select_target_track(tracks)
        self.assertEqual(target.name, "Audio 2")

    def test_returns_none_when_no_audio_selected(self):
        tracks = [
            inst_track("Inst1", selected=TA_EXPLICIT),
            audio_track("Audio 1"),  # 非選択
        ]
        self.assertIsNone(select_target_track(tracks))

    def test_selected_takes_priority_over_edit(self):
        # 選択トラックと編集カーソルトラックが別々の場合、選択を優先
        tracks = [
            audio_track("Selected", selected=TA_EXPLICIT),
            audio_track("EditCursor", edit=TA_EXPLICIT),
        ]
        self.assertEqual(select_target_track(tracks).name, "Selected")


class TestCountSelected(unittest.TestCase):
    def test_counts_only_selected_audio(self):
        tracks = [
            audio_track("A1", selected=TA_EXPLICIT),
            audio_track("A2", selected=TA_EXPLICIT),
            audio_track("A3"),
            inst_track("I1", selected=TA_EXPLICIT),
        ]
        self.assertEqual(count_selected_audio_tracks(tracks), 2)


class TestChannelMatch(unittest.TestCase):
    def test_mono_track_mono_sample_ok(self):
        t = audio_track("A", fmt=TrackFormat.MONO)
        self.assertTrue(check_channel_match(t, 1))

    def test_mono_track_stereo_sample_mismatch(self):
        t = audio_track("A", fmt=TrackFormat.MONO)
        self.assertFalse(check_channel_match(t, 2))

    def test_stereo_track_stereo_sample_ok(self):
        t = audio_track("A", fmt=TrackFormat.STEREO)
        self.assertTrue(check_channel_match(t, 2))

    def test_stereo_track_mono_sample_mismatch(self):
        t = audio_track("A", fmt=TrackFormat.STEREO)
        self.assertFalse(check_channel_match(t, 1))

    def test_unknown_sample_channels_allowed(self):
        # サンプルのチャンネル数不明なら配置を妨げない
        t = audio_track("A", fmt=TrackFormat.MONO)
        self.assertTrue(check_channel_match(t, None))


class TestMessages(unittest.TestCase):
    def test_ok_message_has_info_prefix_and_details(self):
        msg = build_result_message(
            PlacementStatus.OK, track_name="Drums",
            filename="kick.wav", timecode="00:00:01:00.00")
        self.assertTrue(msg.startswith("[情報]"))
        self.assertIn("kick.wav", msg)
        self.assertIn("Drums", msg)
        self.assertIn("00:00:01:00.00", msg)

    def test_no_audio_track_is_warning(self):
        msg = build_result_message(PlacementStatus.NO_AUDIO_TRACK)
        self.assertTrue(msg.startswith("[警告]"))

    def test_not_connected_is_error(self):
        msg = build_result_message(PlacementStatus.NOT_CONNECTED)
        self.assertTrue(msg.startswith("[エラー]"))

    def test_channel_mismatch_message_describes_both(self):
        t = audio_track("Bass", fmt=TrackFormat.MONO)
        msg = build_channel_mismatch_message(t, 2)
        self.assertTrue(msg.startswith("[警告]"))
        self.assertIn("Bass", msg)
        self.assertIn("モノラル", msg)   # トラック側
        self.assertIn("ステレオ", msg)   # サンプル側

    def test_channel_mismatch_does_not_say_selected(self):
        """コマンドとTで指定したトラックは「選択トラック」ではない。"""
        t = audio_track("Bass", fmt=TrackFormat.MONO)
        msg = build_channel_mismatch_message(t, 2)
        self.assertNotIn("選択トラック", msg)
        self.assertIn("配置先のトラック", msg)


if __name__ == "__main__":
    unittest.main()


class TestNoAudioTrackMessage(unittest.TestCase):
    """**配置が失敗する一番よくある原因の文言。**

    実機のセッションは 18 本中オーディオが 5 本だけで、残りは Inst や AUX
    だった。画面を見られないと「どれを選んでいるのか」が分からないので、
    「選択されていません」だけでは直しようがない。
    """

    def setUp(self):
        self.tracks = [
            inst_track("Inst1", selected=TA_EXPLICIT),
            audio_track("Audio 1"),
            audio_track("Audio 2"),
        ]

    def test_names_what_is_selected_now(self):
        text = build_no_audio_track_message(self.tracks)
        self.assertIn("Inst1", text)

    def test_lists_what_can_be_chosen(self):
        text = build_no_audio_track_message(self.tracks)
        self.assertIn("Audio 1", text)
        self.assertIn("Audio 2", text)

    def test_is_a_warning(self):
        self.assertTrue(
            build_no_audio_track_message(self.tracks).startswith("[警告]"))

    def test_no_audio_track_at_all_says_so(self):
        """オーディオトラックが 1 本も無いなら、選びようがない。"""
        text = build_no_audio_track_message([inst_track("Inst1")])
        self.assertIn("オーディオトラックがありません", text)
        self.assertIn("作って", text)

    def test_nothing_selected_still_lists_choices(self):
        text = build_no_audio_track_message(
            [audio_track("Audio 1"), audio_track("Audio 2")])
        self.assertIn("Audio 1", text)


class TestFindAudioTrack(unittest.TestCase):
    """配置先をアプリ側で指定するための検索。"""

    def setUp(self):
        self.tracks = [
            inst_track("Audio 1"),          # 同名だがオーディオではない
            audio_track("Audio 2"),
        ]

    def test_finds_by_name(self):
        got = find_audio_track(self.tracks, "Audio 2")
        self.assertIsNotNone(got)
        self.assertEqual(got.name, "Audio 2")

    def test_ignores_non_audio_with_the_same_name(self):
        """**種別まで見る。** 名前が合っていても Inst には置けない。"""
        self.assertIsNone(find_audio_track(self.tracks, "Audio 1"))

    def test_missing_name_returns_none(self):
        self.assertIsNone(find_audio_track(self.tracks, "Audio 9"))

    def test_empty_name_returns_none(self):
        self.assertIsNone(find_audio_track(self.tracks, ""))


class TestMissingTrackMessage(unittest.TestCase):
    """指定した配置先が無いとき（セッションを開き直した後など）。"""

    def test_names_the_missing_track_and_the_choices(self):
        text = build_missing_track_message(
            "Audio 9", [audio_track("Audio 1")])
        self.assertIn("Audio 9", text)
        self.assertIn("Audio 1", text)
        self.assertIn("選び直して", text)

    def test_without_any_audio_track(self):
        text = build_missing_track_message("Audio 9", [inst_track("Inst1")])
        self.assertIn("オーディオトラックがありません", text)


class TestSnapToGrid(unittest.TestCase):
    """**拍に合わせる。**

    画面を見ずにカーソルを拍の上へ置くのは難しい。実機では拍の半分
    ずれた位置に置くと 187 ミリ秒ずれ、メトロノームと明らかに合わない。
    """

    def test_snaps_forward_to_the_nearest_beat(self):
        # 1 拍 = 16537.5 サンプル（160 BPM / 44.1kHz）
        self.assertEqual(snap_to_grid(16000, 0, 16537.5), 16538)

    def test_snaps_backward_to_the_nearest_beat(self):
        self.assertEqual(snap_to_grid(1000, 0, 16537.5), 0)

    def test_leaves_a_position_already_on_the_beat(self):
        self.assertEqual(snap_to_grid(33075, 0, 16537.5), 33075)

    def test_respects_the_session_start(self):
        """**小節 1 が 0 とは限らない。** セッション開始がずれていても合う。"""
        self.assertEqual(snap_to_grid(5000 + 16000, 5000, 16537.5),
                         5000 + 16538)

    def test_zero_step_is_left_alone(self):
        """テンポが取れないときは動かさない（配置を妨げない）。"""
        self.assertEqual(snap_to_grid(12345, 0, 0), 12345)

    def test_halfway_rounds_to_the_nearer_side(self):
        beat = 1000.0
        self.assertEqual(snap_to_grid(1600, 0, beat), 2000)
        self.assertEqual(snap_to_grid(1400, 0, beat), 1000)


class TestDescribeSnap(unittest.TestCase):
    """**黙って動かさない。** 何が起きたか必ず伝える。"""

    def test_reports_how_far_it_moved(self):
        text = describe_snap_ja(16000, 16538, 44100.0)
        self.assertIn("拍から外れていた", text)
        self.assertIn("12", text)  # 538 サンプル ≒ 12 ミリ秒
        self.assertIn("後ろ", text)

    def test_reports_the_direction(self):
        self.assertIn("前", describe_snap_ja(1000, 0, 44100.0))

    def test_silent_when_it_did_not_move(self):
        self.assertEqual(describe_snap_ja(16538, 16538, 44100.0), "")

    def test_silent_for_a_negligible_move(self):
        """1 サンプル程度は言わなくてよい。"""
        self.assertEqual(describe_snap_ja(16538, 16539, 44100.0), "")
