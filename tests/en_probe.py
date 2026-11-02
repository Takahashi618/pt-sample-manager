"""英語の画面で出る文言を集めて JSON で出す（tests/test_i18n.py が別のプロセスで動かす）。

言語は起動したときに決まるので、`SM_LANG=en` を付けて新しいプロセスで動かす。
窓は出さない。Pro Tools にもつながない。ライブラリは一時的なもの（:memory:）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SM_SILENT", "1")
os.environ.setdefault("SM_NO_ANNOUNCE", "1")


def main() -> int:
    import wx

    from audio.player import NullBackend, PreviewPlayer
    from core import build_info, i18n, placement_planner as pp
    from core.library import Library
    from core.models import (AudioMetadata, Sample, SampleKind, Source,
                             SourceKind)
    from core.scanner import ScanResult
    from core.session_settings import (SessionSettings, describe_key_ja,
                                       describe_volume_ja, parse_bpm,
                                       parse_copies, parse_every_bars,
                                       parse_position)
    from integration import render_service as rs
    from ui.controller import AppController, SearchForm
    from ui.edit_dialog import EditDialog
    from ui.keys_dialog import KEY_HELP, KeysDialog, key_rows
    from ui.main_frame import MainFrame
    from ui.session_dialog import SessionDialog
    from ui.source_dialog import SourceDialog
    from integration.placement_service import place_sample_spot
    # テストの偽物を借りる（Pro Tools にはつながない）
    from tests.test_placement_spot import FakeSpotClient, audio_track

    out: dict[str, list[str]] = {"lang": [i18n.LANG]}
    tmp = Path(tempfile.mkdtemp(prefix="sm_en_probe_"))

    app = wx.App(False)
    library = Library(":memory:")
    player = PreviewPlayer(NullBackend())
    c = AppController(library, player, SessionSettings(),
                      tmp / "session_settings.json")

    # --- 窓の部品 ---------------------------------------------------------
    frame = MainFrame(c)
    bar = frame.GetMenuBar()
    menus = [bar.GetMenuLabelText(i) for i in range(bar.GetMenuCount())]
    items = [item for i in range(bar.GetMenuCount())
             for item in bar.GetMenu(i).GetMenuItems()
             if not item.IsSeparator()]
    out["menus"] = menus
    out["menu_items"] = ([item.GetItemLabel() for item in items]
                         + [item.GetHelp() for item in items])
    widgets: list[str] = []

    def walk(window) -> None:
        for child in window.GetChildren():
            if isinstance(child, (wx.StaticText, wx.Button, wx.CheckBox,
                                  wx.StaticBox)):
                widgets.append(child.GetLabel())
            if isinstance(child, wx.Choice):
                widgets.extend(child.GetItems())
            widgets.append(child.GetName())
            walk(child)

    walk(frame)
    meta = AudioMetadata(duration_sec=2.0, sample_rate=48000, bit_depth=24,
                         channels=2)
    sample = Sample(path="/x/loop_120_Am.wav", filename="loop_120_Am.wav",
                    file_format="wav", kind=SampleKind.LOOP, metadata=meta,
                    favorite=True)
    sample.bpm, sample.key_label = 120, "A minor"
    music = SimpleNamespace(known=True, key="Am",
                            describe_ja=lambda: "120 BPM, A minor")
    dialogs = [
        SessionDialog(frame, SessionSettings(), backend_note="note",
                      sample_music=music, detect_tempo=lambda: (None, ""),
                      preview_prepare=lambda s: None,
                      preview_play=lambda p: "",
                      preview_stop_if_active=lambda: False,
                      preview_sample="x.wav"),
        SourceDialog(frame, c), EditDialog(frame, sample), KeysDialog(frame)]
    for dialog in dialogs:
        walk(dialog)
    # 数が 1 のときの読み上げ（窓の中で組み立てる文）
    frame._packs = ["P"]
    frame._focus_packs()
    frame_texts = [frame._status.GetLabel()]
    frame._jump_to_source(5)
    frame_texts.append(frame._status.GetLabel())
    out["frame"] = frame_texts
    out["window"] = [frame.GetTitle()] + [d.GetTitle() for d in dialogs]
    out["widgets"] = [w for w in widgets if w]
    for dialog in dialogs:
        dialog.Destroy()
    frame.Destroy()

    # --- 組み立てる文 -----------------------------------------------------
    settings = SessionSettings(bpm=120.0, key="Am", copies=4, every_bars=4.0)
    out["settings"] = [
        settings.describe_ja(), SessionSettings().describe_ja(),
        SessionSettings(copies=3).describe_ja(),
        SessionSettings(auto_render=False, at_cursor=False).describe_ja(),
        describe_key_ja("Am"), describe_key_ja("C"), describe_key_ja(""),
        describe_volume_ja(0.0), describe_volume_ja(0.7),
        parse_bpm("abc")[1], parse_bpm("9999")[1],
        parse_position("x")[1], parse_position("0|1|000")[1],
        parse_copies("x")[1], parse_copies("0")[1], parse_copies("999")[1],
        parse_every_bars("x")[1], parse_every_bars("0")[1],
        SessionSettings(bpm=120.0, copies=2, every_bars=1.0).describe_ja()]
    out["samples"] = [sample.describe_ja(), meta.describe_ja(),
                      AudioMetadata().describe_ja(),
                      *[k.label_ja for k in SampleKind],
                      *[k.label_ja for k in SourceKind],
                      str(Source(name="S", root="/s",
                                 kind=SourceKind.SPLICE).describe_ja())]
    result = ScanResult(added=1, skipped=2, linked=3,
                        errors=[("/x", "The folder does not exist")])
    out["scan"] = [result.describe_ja(), ScanResult().describe_ja()]
    audio = pp.TrackInfo(name="Audio 1", track_type=pp.TRACK_TYPE_AUDIO,
                         track_format=pp.TrackFormat.STEREO,
                         is_selected_state=2, has_edit_selection_state=2)
    inst = pp.TrackInfo(name="Inst 1", track_type=99,
                        track_format=pp.TrackFormat.STEREO,
                        is_selected_state=2, has_edit_selection_state=2)
    inst2 = pp.TrackInfo(name="Inst 2", track_type=99,
                         track_format=pp.TrackFormat.STEREO,
                         is_selected_state=2, has_edit_selection_state=2)
    out["placement"] = [pp.build_result_message(status, "Audio 1", "a.wav",
                                                "1|1|000", "detail")
                        for status in pp.PlacementStatus] + [
        pp.describe_snap_ja(0, 4800, 48000),
        pp.describe_snap_ja(4800, 0, 48000),
        pp.build_missing_track_message("X", [audio]),
        pp.build_missing_track_message("X", []),
        pp.build_no_audio_track_message([inst, audio]),
        pp.build_no_audio_track_message([inst]),
        pp.build_channel_mismatch_message(audio, 1),
        pp.build_channel_mismatch_message(audio, None),
        pp.build_channel_mismatch_message(audio, 6),
        pp.describe_snap_ja(0, 48, 48000),
        pp.build_no_audio_track_message(
            [inst, inst2, audio_track("Audio 2")]),
        pp.build_rolled_back_message("Audio 1"),
        pp.build_undo_failed_message("Audio 1"),
        place_sample_spot(
            FakeSpotClient([audio_track("Audio 1", selected=True)]),
            "/s/a.wav", "a.wav", 2,
            ["1|1|000", "5|1|000", "9|1|000", "13|1|000"]).message]
    info = build_info.BuildInfo(version="1.0", built_at="2026-01-01 00:00",
                                commit="abc1234")
    packaged = build_info.BuildInfo(version="1.0", commit="abc1234",
                                    built_at="2026-01-01 00:00",
                                    flavor=build_info.FLAVOR_DISTRIBUTION)
    out["build"] = [build_info.describe_ja(build_info.BuildInfo(), tmp),
                    build_info.describe_ja(info, tmp),
                    build_info.describe_ja(packaged, tmp)]
    out["render"] = [
        rs.describe_backend(),
        rs.RenderOutcome(path="x", changed=True, semitones=4, ratio=0.75,
                         source_bpm=120, source_key="A minor").describe_ja(),
        rs.RenderOutcome(path="x", changed=True, semitones=-1).describe_ja(),
        rs.RenderOutcome(path="x", error="e").describe_ja(),
        rs.ChannelOutcome(path="x", changed=True, from_channels=1,
                          to_channels=2).describe_ja(),
        rs.ChannelOutcome(path="x", changed=True, from_channels=2,
                          to_channels=1).describe_ja(),
        rs.ChannelOutcome(path="x", error="e").describe_ja(),
        rs.read_sample_music("loop_120_Am_x.wav").describe_ja(),
        rs.validate_key("zz"),
        rs.part_message("元のファイルがありません: a.wav"),
        rs.part_message("ffmpeg を起動できません: x"),
        rs.part_message("ffmpeg が失敗しました: 終了コード 1"),
        rs.part_message("60 秒で終わりませんでした")]
    out["sentences"] = [i18n.sentences(["The source was not removed",
                                        "2 sources"])]
    state = PreviewPlayer(NullBackend())
    out["player"] = [state.describe_state_en()]
    state.load("/a/kick.wav")
    out["player"].append(state.describe_state_en())
    state.play()
    out["player"].append(state.describe_state_en())
    state.toggle_pause()
    out["player"].append(state.describe_state_en())

    # --- 操作の結果 -------------------------------------------------------
    source = c.register_source("S", str(tmp))
    out["controller"] = [
        c.update_session_settings(settings), c.set_target_track("Audio 1"),
        c.describe_target_track(), c.set_target_track(""),
        c.describe_target_track(), c.describe_build_ja(),
        c.tempo_mismatch_warning(160.0), c.describe_render_backend(),
        c.clear_session_tempo(), c.source_label(None), c.source_label(99),
        c.source_conflict("S", "/other")[1],
        c.source_conflict("T", str(tmp))[1], c.scan_sources([source]),
        c.remove_source(99), c.remove_source(source.id),
        c.preview_toggle(0), c.copy_path(0), c.place_in_pro_tools(0),
        c.import_to_pro_tools(0), c.toggle_favorite(0)[0],
        c.preview_stop(), c.nudge_preview_volume(True),
        c.set_preview_volume(1.0), c.nudge_preview_volume(True),
        c.set_preview_volume(0.0), c.nudge_preview_volume(False),
        c.play_prepared_preview(c.prepare_preview_with_settings(
            None, settings)),
        c.place_sample_in_pro_tools(sample), player.describe_state_en()]
    c.search(SearchForm())
    out["controller"].append(c.search_result_summary_ja())
    out["keys"] = key_rows() + [cell for row in KEY_HELP for cell in row]

    library.close()
    del app
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
