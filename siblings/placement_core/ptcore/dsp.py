from __future__ import annotations
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional, Sequence
try:
    from .audiofile import read_audio_info
except ImportError:
    from audiofile import read_audio_info
_ATEMPO_MIN = 0.5
_ATEMPO_MAX = 100.0
_EPSILON = 1e-06
DEFAULT_TIMEOUT = 300.0
_PCM_CODECS = {8: 'pcm_u8', 16: 'pcm_s16le', 24: 'pcm_s24le', 32: 'pcm_s32le'}

@dataclass
class ProcessResult:
    ok: bool
    dest: str = ''
    backend: str = ''
    command: List[str] = field(default_factory=list)
    message: str = ''
    skipped: bool = False

    def describe(self) -> dict:
        return {'結果': '加工しました' if self.ok else f'失敗しました。{self.message}', '使った道具': self.backend or 'なし', '出力': self.dest or 'なし'}

class Backend:
    name = ''
    quality = 0

    def path(self) -> Optional[str]:
        return shutil.which(self.name)

    def available(self) -> bool:
        return self.path() is not None

    def build(self, source: str, dest: str, semitones: float, tempo_ratio: float) -> List[str]:
        raise NotImplementedError

class RubberbandBackend(Backend):
    name = 'rubberband'
    quality = 2

    def build(self, source: str, dest: str, semitones: float, tempo_ratio: float) -> List[str]:
        return [self.name, '--time', f'{tempo_ratio:.10f}', '--pitch', f'{semitones:.10f}', source, dest]

class FfmpegBackend(Backend):
    name = 'ffmpeg'
    quality = 1

    def build(self, source: str, dest: str, semitones: float, tempo_ratio: float) -> List[str]:
        info = read_audio_info(source)
        rate = info.sample_rate if info else 44100
        codec = _PCM_CODECS.get(info.bit_depth if info else 24, 'pcm_s24le')
        chain = build_filter_chain(rate, semitones, tempo_ratio)
        return [self.name, '-hide_banner', '-loglevel', 'error', '-y', '-i', source, '-filter:a', chain, '-c:a', codec, dest]

class NoBackend(Backend):
    name = ''
    quality = -1

    def available(self) -> bool:
        return False

    def build(self, *args, **kwargs) -> List[str]:
        raise RuntimeError('使えるバックエンドがありません')
BACKENDS: Sequence[Backend] = (RubberbandBackend(), FfmpegBackend())

def choose_backend(prefer: str='') -> Backend:
    usable = [b for b in BACKENDS if b.available()]
    if not usable:
        return NoBackend()
    if prefer:
        for b in usable:
            if b.name == prefer:
                return b
    return max(usable, key=lambda b: b.quality)

def describe_backends() -> dict:
    lines = []
    for b in BACKENDS:
        where = b.path()
        lines.append(f"{b.name}: {(where if where else '入っていない')}")
    chosen = choose_backend()
    return {'使える道具': '、'.join(lines), '選ばれる道具': chosen.name or 'なし（加工できません）'}

def pitch_factor(semitones: float) -> float:
    return 2.0 ** (semitones / 12.0)

def atempo_chain(value: float) -> List[float]:
    if value <= 0:
        raise ValueError('atempo に 0 以下は指定できません')
    parts: List[float] = []
    remaining = value
    while remaining < _ATEMPO_MIN:
        parts.append(_ATEMPO_MIN)
        remaining /= _ATEMPO_MIN
    while remaining > _ATEMPO_MAX:
        parts.append(_ATEMPO_MAX)
        remaining /= _ATEMPO_MAX
    parts.append(remaining)
    return parts

def build_filter_chain(sample_rate: int, semitones: float, tempo_ratio: float) -> str:
    if tempo_ratio <= 0:
        raise ValueError('長さの比は正の値である必要があります')
    p = pitch_factor(semitones)
    filters = []
    if abs(semitones) > _EPSILON:
        filters.append(f'asetrate={int(round(sample_rate * p))}')
        filters.append(f'aresample={sample_rate}')
    speed = 1.0 / (p * tempo_ratio)
    if abs(speed - 1.0) > _EPSILON:
        filters.extend((f'atempo={value:.10f}' for value in atempo_chain(speed)))
    return ','.join(filters) if filters else 'anull'

def needs_processing(semitones: float, tempo_ratio: float) -> bool:
    return abs(semitones) > _EPSILON or abs(tempo_ratio - 1.0) > _EPSILON

def convert_channels(source: str, dest: str, channels: int, *, timeout: float=DEFAULT_TIMEOUT) -> ProcessResult:
    if channels < 1:
        return ProcessResult(False, message='チャンネル数は 1 以上です')
    if not os.path.exists(source):
        return ProcessResult(False, message=f'元のファイルがありません: {source}')
    info = read_audio_info(source)
    if info is not None and info.channels == channels:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
            shutil.copyfile(source, dest)
        except OSError as e:
            return ProcessResult(False, message=f'コピーできません: {e}')
        return ProcessResult(True, dest=dest, backend='コピー', skipped=True)
    backend = FfmpegBackend()
    if not backend.available():
        return ProcessResult(False, message='チャンネル数を合わせるには ffmpeg が要ります（brew install ffmpeg）')
    try:
        os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    except OSError as e:
        return ProcessResult(False, message=f'出力先を作れません: {e}')
    codec = _PCM_CODECS.get(info.bit_depth if info else 24, 'pcm_s24le')
    command = [backend.name, '-hide_banner', '-loglevel', 'error', '-y', '-i', source, '-ac', str(channels), '-c:a', codec, dest]
    try:
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, text=True, encoding='utf-8', errors='replace')
    except subprocess.TimeoutExpired:
        return ProcessResult(False, backend=backend.name, command=command, message=f'{timeout:.0f} 秒で終わりませんでした')
    except OSError as e:
        return ProcessResult(False, backend=backend.name, command=command, message=f'ffmpeg を起動できません: {e}')
    if completed.returncode != 0 or not os.path.exists(dest):
        detail = (completed.stderr or '').strip().splitlines()
        return ProcessResult(False, backend=backend.name, command=command, message='チャンネル数を変えられませんでした: ' + (detail[-1] if detail else f'終了コード {completed.returncode}'))
    return ProcessResult(True, dest=dest, backend=backend.name, command=command)

def process(source: str, dest: str, semitones: float=0.0, tempo_ratio: float=1.0, *, prefer: str='', timeout: float=DEFAULT_TIMEOUT) -> ProcessResult:
    if not os.path.exists(source):
        return ProcessResult(False, message=f'元のファイルがありません: {source}')
    dest_dir = os.path.dirname(os.path.abspath(dest))
    if dest_dir and (not os.path.isdir(dest_dir)):
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as e:
            return ProcessResult(False, message=f'出力先を作れません: {e}')
    if not needs_processing(semitones, tempo_ratio):
        try:
            shutil.copyfile(source, dest)
        except OSError as e:
            return ProcessResult(False, message=f'コピーできません: {e}')
        return ProcessResult(True, dest=dest, backend='コピー', skipped=True)
    backend = choose_backend(prefer)
    if not backend.available():
        return ProcessResult(False, message='移調やタイムストレッチに使える道具がありません。ffmpeg か rubberband を入れてください（brew install ffmpeg rubberband）')
    try:
        command = backend.build(source, dest, semitones, tempo_ratio)
    except (ValueError, RuntimeError) as e:
        return ProcessResult(False, backend=backend.name, message=str(e))
    try:
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, text=True, encoding='utf-8', errors='replace')
    except subprocess.TimeoutExpired:
        return ProcessResult(False, backend=backend.name, command=command, message=f'{timeout:.0f} 秒で終わりませんでした')
    except OSError as e:
        return ProcessResult(False, backend=backend.name, command=command, message=f'{backend.name} を起動できません: {e}')
    if completed.returncode != 0:
        detail = (completed.stderr or '').strip().splitlines()
        return ProcessResult(False, backend=backend.name, command=command, message=f'{backend.name} が失敗しました: ' + (detail[-1] if detail else f'終了コード {completed.returncode}'))
    if not os.path.exists(dest) or os.path.getsize(dest) == 0:
        return ProcessResult(False, backend=backend.name, command=command, message='出力ファイルができていません')
    return ProcessResult(True, dest=dest, backend=backend.name, command=command)
