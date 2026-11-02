from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Union
ROUTE_SPOT = 'spot'
ROUTE_TEMP_TRACK = 'temp_track'
ROUTES = (ROUTE_SPOT, ROUTE_TEMP_TRACK)
TIMELINE_TYPES = {'bars_beats': 'TLType_BarsBeats', 'timecode': 'TLType_TimeCode', 'min_secs': 'TLType_MinSecs', 'samples': 'TLType_Samples'}
DEFAULT_AUDIO = 'AOperations_Default'
CONVERT_AUDIO = 'AOperations_ConvertAudio'
COPY_AUDIO = 'AOperations_CopyAudio'
TEMP_TRACK_NAME = '_sample_tmp'

@dataclass(frozen=True)
class FromResponse:
    step: int
    path: str

    def __str__(self) -> str:
        return f'<手順 {self.step + 1} の応答の {self.path}>'

@dataclass(frozen=True)
class PtslCall:
    command: str
    body: Dict[str, object] = field(default_factory=dict)
    note: str = ''
    modifies: bool = False

    def body_text(self) -> str:
        if not self.body:
            return '引数なし'
        return '、'.join((f'{k} = {_value_text(v)}' for k, v in self.body.items()))

def _value_text(value: object) -> str:
    if isinstance(value, FromResponse):
        return str(value)
    if isinstance(value, dict):
        return '{' + '、'.join((f'{k}: {_value_text(v)}' for k, v in value.items())) + '}'
    if isinstance(value, (list, tuple)):
        return '[' + '、'.join((_value_text(v) for v in value)) + ']'
    return str(value)

@dataclass
class PlacementSequence:
    route: str
    calls: List[PtslCall] = field(default_factory=list)
    requires: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def rollback(self) -> List[PtslCall]:
        count = sum((1 for c in self.calls if c.modifies))
        return [PtslCall('CId_Undo', {}, note=f'巻き戻し {i + 1} 段目') for i in range(count)]

    def steps(self) -> List[str]:
        lines = []
        for i, call in enumerate(self.calls, 1):
            line = f'{i}. {call.command}\u3000{call.body_text()}'
            if call.note:
                line += f'\n   {call.note}'
            lines.append(line)
        return lines

    def describe(self) -> dict:
        return {'経路': {ROUTE_SPOT: 'spot（クリップリスト経由の直接配置）', ROUTE_TEMP_TRACK: 'temp_track（一時トラック経由）'}.get(self.route, self.route), '呼び出し数': f'{len(self.calls)} 回（うちセッションを変えるもの {sum((1 for c in self.calls if c.modifies))} 回）', '前提条件': '。'.join(self.requires) if self.requires else 'なし', '実機で確かめる点': '。'.join(self.notes)}

def find_self_overlaps(positions: Sequence[str], clock, duration_seconds: float) -> List[str]:
    if duration_seconds <= 0 or len(positions) < 2:
        return []
    length = int(round(duration_seconds * clock.sample_rate))
    if length <= 0:
        return []
    try:
        placed = sorted(((clock.to_samples(p), p) for p in positions), key=lambda x: x[0])
    except (ValueError, TypeError):
        return []
    hits = []
    for (start, label), (next_start, _) in zip(placed, placed[1:]):
        if start + length > next_start and label not in hits:
            hits.append(label)
    return hits

def loop_positions(clock, start: Union[str, int], bars: float, copies: int, fmt: str='bars_beats') -> List[str]:
    if copies < 1:
        raise ValueError('個数は 1 以上である必要があります')
    if bars <= 0:
        raise ValueError('ループの小節数は正の値である必要があります')
    whole_bars = int(bars)
    fraction = bars - whole_bars
    positions = []
    at = clock.to_samples(start)
    for _ in range(copies):
        positions.append(clock.from_samples(at, fmt))
        beats = 0
        if fraction:
            bar = int(clock.from_samples(at, 'bars_beats').split('|')[0])
            beats = round(fraction * clock.meter_map.meter_at_bar(bar).numerator)
        at = clock.add(at, bars=whole_bars, beats=beats)
    return positions

def build_placement(source_path: str, track_name: str, positions: Sequence[str], plan=None, position_format: str='bars_beats', route: str=ROUTE_SPOT, convert: Optional[bool]=None, use_repeat_selection: bool=False, temp_track_name: str=TEMP_TRACK_NAME) -> PlacementSequence:
    if route not in ROUTES:
        raise ValueError(f'未知の経路です: {route!r}（{ROUTES} のいずれか）')
    if not positions:
        raise ValueError('位置が 1 つも指定されていません')
    if position_format not in TIMELINE_TYPES:
        raise ValueError(f'未知の位置形式です: {position_format!r}（{tuple(TIMELINE_TYPES)} のいずれか）')
    if plan is not None and (not plan.ok):
        raise ValueError('このままでは配置できません。' + '。'.join(plan.blockers))
    time_type = TIMELINE_TYPES[position_format]
    if convert is None:
        operations = DEFAULT_AUDIO
    else:
        operations = CONVERT_AUDIO if convert else COPY_AUDIO
    if route == ROUTE_SPOT:
        seq = _build_spot(source_path, track_name, positions, time_type, operations)
    else:
        seq = _build_temp_track(source_path, track_name, positions, time_type, operations, use_repeat_selection, temp_track_name)
    _add_plan_notes(seq, plan, convert)
    return seq

def _build_spot(source_path: str, track_name: str, positions: Sequence[str], time_type: str, operations: str) -> PlacementSequence:
    calls = [PtslCall('CId_ImportAudioToClipList', {'file_list': [source_path], 'audio_operations': operations}, note='クリップリストへ取り込む。トラックには何も起きない', modifies=True)]
    clip_ids = FromResponse(0, 'file_list[0].destination_file_list[*].clip_id_list')
    for i, position in enumerate(positions, 1):
        calls.append(PtslCall('CId_SpotClipsByID', {'src_clips': clip_ids, 'dst_track_name': track_name, 'dst_location_data': {'location_type': 'SLType_Start', 'location': {'location': position, 'time_type': time_type}}}, note=f'{i} 個目を {position} に置く', modifies=True))
    return PlacementSequence(route=ROUTE_SPOT, calls=calls, requires=[], notes=['この経路は選択状態に依存しないため、Link Timeline and Edit Selection の設定を問わない（実機で確認済み）', 'src_clips には destination_file_list の全エントリから集めた clip_id を渡す（ステレオ素材は複数 ID でひとつのクリップを構成し、変換が起きるとエントリ自体が L と R に分かれる）', 'SpotClipsByID は status: Completed を返しても置いていないことがある。チャンネル構成が合わないと dst_track_name を黙って無視し、適合する別のトラックへ置く。**配置後に実際の配置先を確認すること**', '配置先が空いているかは事前に確かめること。既存クリップに重なると上書きされ、下のクリップが前後に分断される'])

def _build_temp_track(source_path: str, track_name: str, positions: Sequence[str], time_type: str, operations: str, use_repeat_selection: bool, temp_track_name: str) -> PlacementSequence:
    calls = [PtslCall('CId_GetTrackList', {}, note='取り込み前のトラック名を控える'), PtslCall('CId_Import', {'import_type': 'IType_Audio', 'audio_data': {'file_list': [source_path], 'audio_operations': operations, 'audio_destination': 'MDestination_NewTrack'}}, note='新規トラックへ取り込む。行き先に既存トラックは選べない', modifies=True), PtslCall('CId_GetTrackList', {}, note='手順 1 との差分が一時トラック。名前は Pro Tools が決める')]
    temp = FromResponse(2, 'track_list のうち手順 1 に無かったもの')
    calls += [PtslCall('CId_SelectAllClipsOnTrack', {'track_name': temp}), PtslCall('CId_Copy', {}, note='一時トラックのクリップを写す'), PtslCall('CId_SelectTracksByName', {'track_names': [track_name], 'selection_mode': 'SMode_Replace'}, note='対象トラックへ選択を移す')]

    def paste_at(position: str, label: str) -> List[PtslCall]:
        return [PtslCall('CId_SetTimelineSelection', {'in_time': position, 'out_time': position, 'location_type': time_type}, note=f'{label}の位置を決める'), PtslCall('CId_Paste', {}, note=f'{label}を貼る', modifies=True)]
    notes = ['Import の行き先に既存トラックは選べないため一時トラックを経由する', '一時トラックの名前は Pro Tools が決めるので、GetTrackList の差分で特定する', 'DeleteTracks は 2025.10 で追加されたばかり。py-ptsl が対応していなければ proto から生成する']
    if use_repeat_selection and len(positions) > 1:
        calls += paste_at(positions[0], '1 個目')
        calls.append(PtslCall('CId_RepeatSelection', {'num_repeats': len(positions) - 1}, note=f'残り {len(positions) - 1} 個を複製する', modifies=True))
        notes.append('num_repeats が「追加する回数」か「合計の個数」かは proto の説明では判別できない。実機で確認すること（V10）')
    else:
        for i, position in enumerate(positions, 1):
            calls += paste_at(position, f'{i} 個目')
    calls.append(PtslCall('CId_DeleteTracks', {'track_names': [temp]}, note=f'一時トラックを片付ける（名前を {temp_track_name} に統一しておくと手作業でも消しやすい）', modifies=True))
    return PlacementSequence(route=ROUTE_TEMP_TRACK, calls=calls, requires=['Link Timeline and Edit Selection を ON にしておく（PTSL に SetEditSelection が無く、Paste は編集選択に落ちるため）'], notes=notes)

def _add_plan_notes(seq: PlacementSequence, plan, convert: bool) -> None:
    if plan is None:
        return
    if plan.destructive:
        seq.requires.append('層 B で加工した一時ファイルのパスを渡すこと（この素材は移調またはストレッチが要る）')
    if plan.needs_resample and convert is False:
        seq.requires.append(f'サンプルレートが違う（{plan.source_rate} Hz と {plan.target_rate} Hz）のにCopyAudio を指示している。**この条件では必ず失敗する**（実機で確認済み）。convert の指定を外して Pro Tools に任せること')
    if not plan.needs_resample and convert is True:
        seq.requires.append('サンプルレートが一致しているのに ConvertAudio を指示している。**この条件では必ず失敗する**（実機で確認済み）。convert の指定を外して Pro Tools に任せること')
    if plan.confirmations:
        seq.requires.append('実行前に利用者へ確認する: ' + '。'.join(plan.confirmations))
