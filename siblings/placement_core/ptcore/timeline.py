from __future__ import annotations
import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Iterable, List, Optional, Sequence, Tuple, Union
TICKS_PER_QUARTER = 960
Number = Union[int, float, Fraction]

@dataclass(frozen=True)
class FrameRate:
    name: str
    nominal: int
    pulldown: bool = False
    drop_frame: bool = False

    @property
    def frame_duration(self) -> Fraction:
        d = Fraction(1, self.nominal)
        if self.pulldown:
            d *= Fraction(1001, 1000)
        return d

    @property
    def frames_dropped_per_minute(self) -> int:
        return self.nominal // 15 if self.drop_frame else 0
FRAME_RATES = {fr.name: fr for fr in [FrameRate('23.976', 24, pulldown=True), FrameRate('24', 24), FrameRate('25', 25), FrameRate('29.97', 30, pulldown=True), FrameRate('29.97DF', 30, pulldown=True, drop_frame=True), FrameRate('30', 30), FrameRate('30DF', 30, drop_frame=True), FrameRate('47.952', 48, pulldown=True), FrameRate('48', 48), FrameRate('50', 50), FrameRate('59.94', 60, pulldown=True), FrameRate('59.94DF', 60, pulldown=True, drop_frame=True), FrameRate('60', 60), FrameRate('60DF', 60, drop_frame=True)]}

def get_frame_rate(spec: Union[str, FrameRate]) -> FrameRate:
    if isinstance(spec, FrameRate):
        return spec
    key = str(spec).strip().upper().replace(' ', '')
    for name, fr in FRAME_RATES.items():
        if name.upper() == key:
            return fr
    raise ValueError(f'未知のフレームレートです: {spec!r}')

@dataclass(frozen=True)
class TempoEvent:
    position_quarters: Fraction
    bpm: Fraction

@dataclass(frozen=True)
class MeterEvent:
    bar: int
    numerator: int
    denominator: int

    @property
    def bar_length_quarters(self) -> Fraction:
        return Fraction(self.numerator * 4, self.denominator)

class TempoMap:

    def __init__(self, events: Union[Number, Iterable[Tuple[Number, Number]]]=120):
        if isinstance(events, (int, float, Fraction)):
            evs = [TempoEvent(Fraction(0), Fraction(events).limit_denominator(10 ** 6))]
        else:
            evs = [TempoEvent(Fraction(pos).limit_denominator(10 ** 9), Fraction(bpm).limit_denominator(10 ** 6)) for pos, bpm in events]
            evs.sort(key=lambda e: e.position_quarters)
        if not evs:
            raise ValueError('テンポマップが空です')
        if evs[0].position_quarters != 0:
            raise ValueError('テンポマップは位置 0 から始まる必要があります')
        for e in evs:
            if e.bpm <= 0:
                raise ValueError(f'BPM は正の値である必要があります: {e.bpm}')
        self.events: List[TempoEvent] = evs

    def _segments(self):
        for i, ev in enumerate(self.events):
            end = self.events[i + 1].position_quarters if i + 1 < len(self.events) else None
            yield (ev.position_quarters, end, ev.bpm)

    def quarters_to_seconds(self, quarters: Fraction) -> Fraction:
        if quarters < 0:
            raise ValueError('負の位置は扱えません')
        total = Fraction(0)
        for start, end, bpm in self._segments():
            if start >= quarters:
                break
            stop = quarters if end is None or end > quarters else end
            total += (stop - start) * 60 / bpm
        return total

    def seconds_to_quarters(self, seconds: Fraction) -> Fraction:
        if seconds < 0:
            raise ValueError('負の位置は扱えません')
        acc = Fraction(0)
        for start, end, bpm in self._segments():
            if end is None:
                return start + (seconds - acc) * bpm / 60
            seg_seconds = (end - start) * 60 / bpm
            if acc + seg_seconds > seconds:
                return start + (seconds - acc) * bpm / 60
            acc += seg_seconds
        return self.events[-1].position_quarters

    def bpm_at_quarters(self, quarters: Fraction) -> Fraction:
        result = self.events[0].bpm
        for ev in self.events:
            if ev.position_quarters <= quarters:
                result = ev.bpm
            else:
                break
        return result

class MeterMap:

    def __init__(self, events: Union[Tuple[int, int], Iterable[Tuple[int, int, int]]]=(4, 4)):
        if isinstance(events, tuple) and len(events) == 2 and all((isinstance(x, int) for x in events)):
            evs = [MeterEvent(1, events[0], events[1])]
        else:
            evs = [MeterEvent(int(bar), int(num), int(den)) for bar, num, den in events]
            evs.sort(key=lambda e: e.bar)
        if not evs:
            raise ValueError('拍子マップが空です')
        if evs[0].bar != 1:
            raise ValueError('拍子マップは 1 小節目から始まる必要があります')
        for e in evs:
            if e.numerator <= 0 or e.denominator <= 0:
                raise ValueError(f'不正な拍子です: {e.numerator}/{e.denominator}')
        self.events: List[MeterEvent] = evs

    def _segments(self):
        for i, ev in enumerate(self.events):
            end_bar = self.events[i + 1].bar if i + 1 < len(self.events) else None
            yield (ev, end_bar)

    def meter_at_bar(self, bar: int) -> MeterEvent:
        result = self.events[0]
        for ev in self.events:
            if ev.bar <= bar:
                result = ev
            else:
                break
        return result

    def bar_start_quarters(self, bar: int) -> Fraction:
        if bar < 1:
            raise ValueError('小節番号は 1 以上である必要があります')
        q = Fraction(0)
        for ev, end_bar in self._segments():
            if end_bar is None or bar < end_bar:
                return q + (bar - ev.bar) * ev.bar_length_quarters
            q += (end_bar - ev.bar) * ev.bar_length_quarters
        return q

    def quarters_to_bar_beat_tick(self, quarters: Fraction) -> Tuple[int, int, int]:
        if quarters < 0:
            raise ValueError('負の位置は扱えません')
        acc = Fraction(0)
        for ev, end_bar in self._segments():
            bar_len = ev.bar_length_quarters
            if end_bar is not None:
                span = (end_bar - ev.bar) * bar_len
                if acc + span <= quarters:
                    acc += span
                    continue
            offset = quarters - acc
            bars_in = int(offset // bar_len)
            bar = ev.bar + bars_in
            within = offset - bars_in * bar_len
            beat_len = Fraction(4, ev.denominator)
            beat_index = int(within // beat_len)
            within_beat = within - beat_index * beat_len
            ticks = int(within_beat * TICKS_PER_QUARTER)
            return (bar, beat_index + 1, ticks)
        raise AssertionError('到達しないはず')

    def bar_beat_tick_to_quarters(self, bar: int, beat: int, tick: int) -> Fraction:
        ev = self.meter_at_bar(bar)
        if beat < 1:
            raise ValueError('拍は 1 以上である必要があります')
        if not 0 <= tick < TICKS_PER_QUARTER * 4:
            raise ValueError(f'ティックの値が不正です: {tick}')
        beat_len = Fraction(4, ev.denominator)
        return self.bar_start_quarters(bar) + (beat - 1) * beat_len + Fraction(tick, TICKS_PER_QUARTER)
_TC_RE = re.compile('^\\s*(\\d{1,3})[:;.](\\d{1,2})[:;.](\\d{1,2})[:;.](\\d{1,3})\\s*$')

def parse_timecode(text: str, fr: FrameRate) -> int:
    m = _TC_RE.match(text)
    if not m:
        raise ValueError(f'タイムコードとして解釈できません: {text!r}')
    h, mi, s, f = (int(x) for x in m.groups())
    if mi > 59 or s > 59:
        raise ValueError(f'タイムコードの分または秒が範囲外です: {text!r}')
    if f >= fr.nominal:
        raise ValueError(f'フレーム値が {fr.name} の範囲外です: {text!r}')
    total_minutes = h * 60 + mi
    if fr.drop_frame:
        d = fr.frames_dropped_per_minute
        if s == 0 and total_minutes % 10 != 0 and (f < d):
            raise ValueError(f'ドロップフレームで存在しない値です: {text!r}')
        dropped = d * (total_minutes - total_minutes // 10)
    else:
        dropped = 0
    return (h * 3600 + mi * 60 + s) * fr.nominal + f - dropped

def format_timecode(total_frames: int, fr: FrameRate) -> str:
    if total_frames < 0:
        raise ValueError('負のタイムコードは扱えません')
    n = fr.nominal
    if not fr.drop_frame:
        f = total_frames % n
        total_seconds = total_frames // n
        s = total_seconds % 60
        total_minutes = total_seconds // 60
        mi = total_minutes % 60
        h = total_minutes // 60
        sep = ':'
    else:
        d = fr.frames_dropped_per_minute
        frames_per_min = n * 60 - d
        frames_per_10min = n * 60 * 10 - 9 * d
        ten_blocks, rem = divmod(total_frames, frames_per_10min)
        if rem < n * 60:
            minute_in_block = 0
        else:
            rem -= n * 60
            extra, rem = divmod(rem, frames_per_min)
            minute_in_block = extra + 1
        total_minutes = ten_blocks * 10 + minute_in_block
        if minute_in_block != 0:
            rem += d
        s, f = divmod(rem, n)
        mi = total_minutes % 60
        h = total_minutes // 60
        sep = ';'
    width = 2 if n <= 99 else 3
    return f'{h:02d}:{mi:02d}:{s:02d}{sep}{f:0{width}d}'
FORMATS = ('bars_beats', 'timecode', 'min_secs', 'samples')
_BB_RE = re.compile('^\\s*(\\d+)\\s*(?:[|.\\-]\\s*(\\d+)\\s*(?:[|.\\-]\\s*(\\d+)\\s*)?)?$')
_MS_RE = re.compile('^\\s*(?:(\\d+)\\s*:\\s*)?(\\d{1,2})(?:\\.(\\d{1,6}))?\\s*$')
_SAMPLES_RE = re.compile('^\\s*(\\d+)\\s*(?:samples?|smp)?\\s*$', re.IGNORECASE)

class SessionClock:

    def __init__(self, sample_rate: int=48000, frame_rate: Union[str, FrameRate]='30', start_timecode: str='00:00:00:00', tempo: Union[Number, Iterable[Tuple[Number, Number]], TempoMap]=120, meter: Union[Tuple[int, int], Iterable[Tuple[int, int, int]], MeterMap]=(4, 4)):
        if sample_rate <= 0:
            raise ValueError('サンプルレートは正の値である必要があります')
        self.sample_rate = int(sample_rate)
        self.frame_rate = get_frame_rate(frame_rate)
        self.tempo_map = tempo if isinstance(tempo, TempoMap) else TempoMap(tempo)
        self.meter_map = meter if isinstance(meter, MeterMap) else MeterMap(meter)
        self.start_timecode = start_timecode
        self._start_frames = parse_timecode(start_timecode, self.frame_rate)

    def _seconds_to_samples(self, seconds: Fraction) -> int:
        return int(round(seconds * self.sample_rate))

    def _samples_to_seconds(self, samples: int) -> Fraction:
        return Fraction(int(samples), self.sample_rate)

    @staticmethod
    def detect_format(text: str) -> str:
        t = str(text).strip()
        if _TC_RE.match(t):
            return 'timecode'
        if '|' in t:
            return 'bars_beats'
        if ':' in t or '.' in t:
            return 'min_secs'
        if _SAMPLES_RE.match(t):
            return 'samples'
        raise ValueError(f'形式を判定できません: {text!r}')

    def to_samples(self, value: Union[str, int], fmt: Optional[str]=None) -> int:
        if isinstance(value, int) and fmt in (None, 'samples'):
            return value
        text = str(value).strip()
        fmt = fmt or self.detect_format(text)
        if fmt == 'samples':
            m = _SAMPLES_RE.match(text)
            if not m:
                raise ValueError(f'サンプル値として解釈できません: {value!r}')
            return int(m.group(1))
        if fmt == 'timecode':
            frames = parse_timecode(text, self.frame_rate)
            rel = frames - self._start_frames
            if rel < 0:
                raise ValueError(f'セッション開始 {self.start_timecode} より前の位置です: {value!r}')
            return self._seconds_to_samples(rel * self.frame_rate.frame_duration)
        if fmt == 'min_secs':
            m = _MS_RE.match(text)
            if not m:
                raise ValueError(f'分:秒として解釈できません: {value!r}')
            mins = int(m.group(1) or 0)
            secs = int(m.group(2))
            frac_digits = m.group(3) or ''
            frac = Fraction(int(frac_digits), 10 ** len(frac_digits)) if frac_digits else Fraction(0)
            return self._seconds_to_samples(mins * 60 + secs + frac)
        if fmt == 'bars_beats':
            m = _BB_RE.match(text)
            if not m:
                raise ValueError(f'小節|拍|ティックとして解釈できません: {value!r}')
            bar = int(m.group(1))
            beat = int(m.group(2) or 1)
            tick = int(m.group(3) or 0)
            quarters = self.meter_map.bar_beat_tick_to_quarters(bar, beat, tick)
            return self._seconds_to_samples(self.tempo_map.quarters_to_seconds(quarters))
        raise ValueError(f'未知の形式です: {fmt!r}')

    def from_samples(self, samples: int, fmt: str) -> str:
        samples = int(samples)
        if samples < 0:
            raise ValueError('負のサンプル位置は扱えません')
        if fmt == 'samples':
            return str(samples)
        if fmt == 'timecode':
            seconds = self._samples_to_seconds(samples)
            frames = (seconds / self.frame_rate.frame_duration).__floor__()
            return format_timecode(self._start_frames + frames, self.frame_rate)
        if fmt == 'min_secs':
            seconds = self._samples_to_seconds(samples)
            total_ms = int(round(seconds * 1000))
            mins, rem_ms = divmod(total_ms, 60000)
            secs, ms = divmod(rem_ms, 1000)
            return f'{mins}:{secs:02d}.{ms:03d}'
        if fmt == 'bars_beats':
            seconds = self._samples_to_seconds(samples)
            quarters = self.tempo_map.seconds_to_quarters(seconds)
            bar, beat, tick = self.meter_map.quarters_to_bar_beat_tick(quarters)
            return f'{bar}|{beat}|{tick:03d}'
        raise ValueError(f'未知の形式です: {fmt!r}')

    def convert(self, value: Union[str, int], to_fmt: str, from_fmt: Optional[str]=None) -> str:
        return self.from_samples(self.to_samples(value, from_fmt), to_fmt)

    def all_formats(self, value: Union[str, int], from_fmt: Optional[str]=None) -> dict:
        s = self.to_samples(value, from_fmt)
        return {f: self.from_samples(s, f) for f in FORMATS}

    def duration_samples(self, start: Union[str, int]=0, bars: int=0, beats: int=0, ticks: int=0, start_fmt: Optional[str]=None) -> int:
        start_samples = self.to_samples(start, start_fmt)
        end_samples = self.add(start_samples, bars=bars, beats=beats, ticks=ticks)
        return end_samples - start_samples

    def add(self, position: Union[str, int], bars: int=0, beats: int=0, ticks: int=0, fmt: Optional[str]=None) -> int:
        start_samples = self.to_samples(position, fmt)
        quarters = self.tempo_map.seconds_to_quarters(self._samples_to_seconds(start_samples))
        bar, beat, tick = self.meter_map.quarters_to_bar_beat_tick(quarters)
        if bars:
            target_bar = bar + bars
            quarters = quarters + (self.meter_map.bar_start_quarters(target_bar) - self.meter_map.bar_start_quarters(bar))
        if beats:
            ev = self.meter_map.meter_at_bar(bar)
            quarters += beats * Fraction(4, ev.denominator)
        if ticks:
            quarters += Fraction(ticks, TICKS_PER_QUARTER)
        if quarters < 0:
            raise ValueError('結果がセッション先頭より前になります')
        return self._seconds_to_samples(self.tempo_map.quarters_to_seconds(quarters))

    def bar_length_samples(self, bar: int) -> int:
        start = self.to_samples(f'{bar}|1|000', 'bars_beats')
        end = self.to_samples(f'{bar + 1}|1|000', 'bars_beats')
        return end - start

    def describe(self) -> dict:
        m = self.meter_map.events[0]
        return {'サンプルレート': f'{self.sample_rate} Hz', 'フレームレート': self.frame_rate.name, '開始タイムコード': self.start_timecode, 'テンポ': f'{float(self.tempo_map.events[0].bpm):g} BPM' + ('（変化あり）' if len(self.tempo_map.events) > 1 else ''), '拍子': f'{m.numerator}/{m.denominator}' + ('（変化あり）' if len(self.meter_map.events) > 1 else '')}
