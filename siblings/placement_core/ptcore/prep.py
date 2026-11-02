from __future__ import annotations
import math
from dataclasses import dataclass, field
from fractions import Fraction
from typing import List, Optional, Tuple
try:
    from .splicename import normalize_key, tempo_ratio
    from .library import parse_key_query
except ImportError:
    from splicename import normalize_key, tempo_ratio
    from library import parse_key_query
_PC_TO_NAME = {normalize_key(root, acc)[1]: normalize_key(root, acc)[0] for root in 'CDEFGAB' for acc in ('', '#')}
STRETCH_WARN_RATIO = 0.15
PITCH_WARN_SEMITONES = 4
BAR_FIT_TOLERANCE = 0.01
BAR_GRID = 0.5
SUB_BAR_GRID = 0.25
_EPS = 1e-09

def _relative_minor_pc(pitch_class: int, quality: Optional[str]) -> int:
    if quality == 'major':
        return (pitch_class - 3) % 12
    return pitch_class % 12

def key_label(key: str) -> str:
    pitch_class, quality = parse_key_query(key)
    name = _PC_TO_NAME[pitch_class]
    if quality == 'minor':
        return f'{name}マイナー'
    if quality == 'major':
        return f'{name}メジャー'
    return name

def pitch_shift_semitones(from_key: str, to_key: str, from_quality: Optional[str]=None, to_quality: Optional[str]=None) -> int:
    from_pc, parsed_from_q = parse_key_query(from_key)
    to_pc, parsed_to_q = parse_key_query(to_key)
    from_quality = from_quality or parsed_from_q
    to_quality = to_quality or parsed_to_q
    if from_quality and to_quality:
        a = _relative_minor_pc(from_pc, from_quality)
        b = _relative_minor_pc(to_pc, to_quality)
    else:
        a, b = (from_pc, to_pc)
    diff = (b - a) % 12
    return diff - 12 if diff > 6 else diff

def bar_seconds(bpm: float, meter: Tuple[int, int]=(4, 4)) -> float:
    if bpm <= 0:
        raise ValueError('BPM は正の値である必要があります')
    numerator, denominator = meter
    if numerator <= 0 or denominator <= 0:
        raise ValueError('拍子は正の値である必要があります')
    quarters_per_bar = numerator * 4.0 / denominator
    return quarters_per_bar * 60.0 / bpm

def bars_for_duration(duration: float, bpm: float, meter: Tuple[int, int]=(4, 4)) -> float:
    return duration / bar_seconds(bpm, meter)

def nearest_musical_bars(bars: float) -> float:
    grid = SUB_BAR_GRID if bars < 1 else BAR_GRID
    steps = math.floor(bars / grid + 0.5)
    return max(grid, steps * grid)

@dataclass
class BarFit:
    bars: float
    nearest: Optional[float]
    deviation: float
    agrees: bool

    def describe(self) -> str:
        if self.nearest is None:
            return f'{self.bars:.2f} 小節相当（区切りのよい長さではない）'
        note = '' if self.agrees else '（ずれあり）'
        return f'{self.bars:.2f} 小節相当、最も近いのは {self.nearest:g} 小節{note}'

def corroborate_bpm(duration: float, bpm: float, meter: Tuple[int, int]=(4, 4), tolerance: float=BAR_FIT_TOLERANCE) -> BarFit:
    bars = bars_for_duration(duration, bpm, meter)
    nearest = nearest_musical_bars(bars)
    deviation = bars - nearest
    return BarFit(bars=bars, nearest=nearest, deviation=deviation, agrees=abs(deviation) <= tolerance)

@dataclass
class RepeatPlan:
    loop_bars: float
    target_bars: float
    whole_copies: int
    copies: int
    remainder_bars: float
    overshoot_bars: float
    exact: bool

    @property
    def extra_copies(self) -> int:
        return max(0, self.copies - 1)

    def describe(self) -> str:
        if self.exact:
            return f'{self.loop_bars:g} 小節のループを {self.copies} 個でちょうど埋まる'
        return f'{self.loop_bars:g} 小節のループでは割り切れない。{self.whole_copies} 個で {self.remainder_bars:.2f} 小節足りず、{self.copies} 個なら {self.overshoot_bars:.2f} 小節はみ出す'

def loop_repeats(loop_bars: float, target_bars: float) -> RepeatPlan:
    if loop_bars <= 0:
        raise ValueError('ループの小節数は正の値である必要があります')
    if target_bars <= 0:
        raise ValueError('目標の小節数は正の値である必要があります')
    raw = target_bars / loop_bars
    whole = int(math.floor(raw + _EPS))
    exact = abs(raw - round(raw)) < 1e-06
    if exact:
        whole = int(round(raw))
        copies = whole
    else:
        copies = whole + 1
    return RepeatPlan(loop_bars=loop_bars, target_bars=target_bars, whole_copies=whole, copies=copies, remainder_bars=max(0.0, target_bars - whole * loop_bars), overshoot_bars=max(0.0, copies * loop_bars - target_bars), exact=exact)

@dataclass
class Target:
    sample_rate: int = 48000
    channels: int = 2
    bit_depth: int = 24
    bpm: Optional[float] = None
    key: Optional[str] = None
    meter: Tuple[int, int] = (4, 4)

    @property
    def key_label(self) -> str:
        return key_label(self.key) if self.key else '指定なし'

    @classmethod
    def from_clock(cls, clock, channels: int=2, key: Optional[str]=None, bit_depth: int=24) -> 'Target':
        meter_event = clock.meter_map.events[0]
        return cls(sample_rate=clock.sample_rate, channels=channels, bit_depth=bit_depth, bpm=float(clock.tempo_map.events[0].bpm), key=key, meter=(meter_event.numerator, meter_event.denominator))

@dataclass
class PrepPlan:
    filename: str = ''
    semitones: int = 0
    ratio: Fraction = Fraction(1)
    source_rate: int = 0
    target_rate: int = 0
    source_channels: int = 0
    target_channels: int = 0
    source_duration: float = 0.0
    output_duration: float = 0.0
    bar_fit: Optional[BarFit] = None
    repeat: Optional[RepeatPlan] = None
    blockers: List[str] = field(default_factory=list)
    confirmations: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def needs_pitch(self) -> bool:
        return self.semitones != 0

    @property
    def needs_stretch(self) -> bool:
        return self.ratio != 1

    @property
    def needs_resample(self) -> bool:
        return bool(self.source_rate) and self.source_rate != self.target_rate

    @property
    def destructive(self) -> bool:
        return self.needs_pitch or self.needs_stretch

    @property
    def needs_processing(self) -> bool:
        return self.destructive or self.needs_resample

    @property
    def ok(self) -> bool:
        return not self.blockers

    def steps(self) -> List[str]:
        lines: List[str] = []
        if self.needs_resample:
            lines.append(f'サンプルレートを {self.source_rate} Hz から {self.target_rate} Hz へ変換する')
        if self.needs_stretch:
            direction = '伸ばす' if self.ratio > 1 else '縮める'
            percent = (float(self.ratio) - 1) * 100
            lines.append(f'尺を {float(self.ratio):.4f} 倍に{direction}（{percent:+.1f}%、{self.source_duration:.2f} 秒 → {self.output_duration:.2f} 秒）')
        if self.needs_pitch:
            direction = '上げる' if self.semitones > 0 else '下げる'
            lines.append(f'ピッチを {abs(self.semitones)} 半音{direction}')
        if self.needs_pitch and self.needs_stretch:
            lines.append('ピッチと尺は 1 回の処理でまとめて掛けられる')
        if not lines:
            lines.append('加工は要らない。そのまま配置できる')
        return lines

    def describe(self) -> dict:
        d = {'ファイル': self.filename, '加工': '必要' if self.needs_processing else '不要', '手順': '。'.join(self.steps()), '長さ': f'{self.source_duration:.2f} 秒' + (f' → {self.output_duration:.2f} 秒' if abs(self.output_duration - self.source_duration) > 0.001 else '')}
        if self.destructive:
            d['注意'] = '音を書き換える処理（元ファイルは変更しない）'
        if self.bar_fit is not None:
            d['小節数'] = self.bar_fit.describe()
        if self.repeat is not None:
            d['ループ複製'] = self.repeat.describe()
        if self.blockers:
            d['配置できない理由'] = '。'.join(self.blockers)
        if self.confirmations:
            d['確認が要る点'] = '。'.join(self.confirmations)
        if self.warnings:
            d['注意点'] = '。'.join(self.warnings)
        return d

def plan_prep(entry, target: Target, target_bars: Optional[float]=None) -> PrepPlan:
    plan = PrepPlan(filename=getattr(entry, 'filename', ''), source_rate=int(getattr(entry, 'sample_rate', 0) or 0), target_rate=target.sample_rate, source_channels=int(getattr(entry, 'channels', 0) or 0), target_channels=target.channels, source_duration=float(getattr(entry, 'duration', 0.0) or 0.0))
    plan.output_duration = plan.source_duration
    _check_format(plan, entry, target)
    _plan_tempo(plan, entry, target)
    _plan_pitch(plan, entry, target)
    _plan_bars(plan, entry, target, target_bars)
    return plan

def _check_format(plan: PrepPlan, entry, target: Target) -> None:
    if plan.needs_resample:
        plan.warnings.append(f'サンプルレートが違う（{plan.source_rate} Hz と {plan.target_rate} Hz）。Pro Tools に任せるなら取り込み時に「変換してコンソリデート」を選ぶ')
    src_ch, dst_ch = (plan.source_channels, plan.target_channels)
    if src_ch and dst_ch:
        if src_ch > dst_ch:
            plan.blockers.append(f'{src_ch} ch の素材を {dst_ch} ch のトラックへは配置できない。対象トラックを変えるか、事前にミックスダウンする')
        elif src_ch < dst_ch:
            plan.warnings.append(f'モノラル素材をステレオトラックへ配置する（{src_ch} ch → {dst_ch} ch）。片チャンネルだけになる可能性がある')

def _plan_tempo(plan: PrepPlan, entry, target: Target) -> None:
    if target.bpm is None:
        return
    src_bpm = getattr(entry, 'bpm', None)
    if not src_bpm:
        kind = getattr(entry, 'kind', None)
        if kind == 'loop':
            plan.warnings.append('BPM が読み取れていないため、テンポは合わせられない')
        return
    kind = getattr(entry, 'kind', None)
    if kind in ('one_shot', 'fx'):
        plan.warnings.append(f"種別が{('ワンショット' if kind == 'one_shot' else '効果音')}のため、テンポ合わせは通常不要")
    ratio = tempo_ratio(src_bpm, target.bpm)
    if ratio == 1:
        return
    as_float = float(ratio)
    if abs(as_float - 2) < 0.03 or abs(as_float - 0.5) < 0.03:
        relation = '倍' if as_float > 1 else '半分'
        plan.warnings.append(f'元 BPM {src_bpm} はセッション {target.bpm:g} の{relation}に当たる。倍速・半速として扱うなら加工は要らない')
    plan.ratio = ratio
    plan.output_duration = plan.source_duration * as_float
    if abs(as_float - 1) > STRETCH_WARN_RATIO:
        plan.confirmations.append(f'尺の変化が {abs(as_float - 1) * 100:.0f}% と大きく、音質が変わる')
    if getattr(entry, 'bpm_confidence', 'none') != 'high':
        plan.confirmations.append(f'元 BPM {src_bpm} は確信度が高くない。誤っていると尺がずれる')

def _plan_pitch(plan: PrepPlan, entry, target: Target) -> None:
    if not target.key:
        return
    src_key = getattr(entry, 'key', None)
    if not src_key:
        if getattr(entry, 'kind', None) != 'fx':
            plan.warnings.append('キーが読み取れていないため、移調はできない')
        return
    src_quality = getattr(entry, 'key_quality', None)
    _, target_quality = parse_key_query(target.key)
    plan.semitones = pitch_shift_semitones(src_key, target.key, src_quality, target_quality)
    if not (src_quality and target_quality):
        plan.confirmations.append('長短のどちらかが不明なため、主音どうしの距離で移調量を出した。平行調なら移調は不要かもしれない')
    if not plan.needs_pitch:
        return
    if abs(plan.semitones) >= PITCH_WARN_SEMITONES:
        plan.confirmations.append(f'{abs(plan.semitones)} 半音の移調は音色そのものが変わって聞こえる')
    if getattr(entry, 'key_confidence', 'none') != 'high':
        plan.confirmations.append(f'元キー {src_key} は確信度が高くない。誤っていると音がぶつかる')

def _plan_bars(plan: PrepPlan, entry, target: Target, target_bars: Optional[float]) -> None:
    src_bpm = getattr(entry, 'bpm', None)
    if not src_bpm or plan.source_duration <= 0:
        return
    plan.bar_fit = corroborate_bpm(plan.source_duration, src_bpm, target.meter)
    if not plan.bar_fit.agrees and getattr(entry, 'kind', None) == 'loop':
        plan.confirmations.append(f'実長が {plan.bar_fit.bars:.2f} 小節分で、区切りのよい値から外れている。BPM {src_bpm} が誤っている疑いがある')
    if target_bars:
        fit = plan.bar_fit
        plan.repeat = loop_repeats(fit.nearest if fit.agrees and fit.nearest else fit.bars, target_bars)
