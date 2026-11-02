from __future__ import annotations
import os
import struct
import sys
from array import array
from dataclasses import dataclass
from typing import List, Optional, Tuple
WAVE_FORMAT_PCM = 1
WAVE_FORMAT_FLOAT = 3
WAVE_FORMAT_EXTENSIBLE = 65534
WAVE_FORMATS = {WAVE_FORMAT_PCM: 'PCM', WAVE_FORMAT_FLOAT: 'IEEE float', 6: 'A-law', 7: 'mu-law', WAVE_FORMAT_EXTENSIBLE: '拡張'}
AUDIO_EXTENSIONS = ('.wav', '.aif', '.aiff')

@dataclass(frozen=True)
class AudioInfo:
    channels: int
    sample_rate: int
    bit_depth: int
    frames: int
    codec: str

    @property
    def duration(self) -> float:
        return self.frames / self.sample_rate if self.sample_rate else 0.0

    @property
    def channel_label(self) -> str:
        return {1: 'モノラル', 2: 'ステレオ'}.get(self.channels, f'{self.channels}ch')

def long_path(path: str) -> str:
    p = os.path.abspath(path)
    if os.name == 'nt' and (not p.startswith('\\\\?\\')):
        return '\\\\?\\' + p
    return p

@dataclass(frozen=True)
class PcmLayout:
    info: AudioInfo
    format_tag: int
    block_align: int
    data_offset: int
    data_size: int

    @property
    def bytes_per_sample(self) -> int:
        ch = self.info.channels
        return self.block_align // ch if ch else 0

def _read_riff_layout(path: str) -> Optional[PcmLayout]:
    with open(long_path(path), 'rb') as f:
        head = f.read(12)
        if len(head) < 12 or head[8:12] != b'WAVE':
            return None
        fmt = None
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            cid, size = struct.unpack('<4sI', hdr)
            if cid == b'fmt ':
                data = f.read(size)
                if len(data) < 16:
                    return None
                tag, ch, rate, _, block, bits = struct.unpack('<HHIIHH', data[:16])
                real = tag
                if tag == WAVE_FORMAT_EXTENSIBLE and len(data) >= 26:
                    real = struct.unpack('<H', data[24:26])[0]
                fmt = (tag, real, ch, rate, bits, block)
                f.seek(size % 2, 1)
            elif cid == b'data':
                if not fmt:
                    return None
                tag, real, ch, rate, bits, block = fmt
                return PcmLayout(AudioInfo(ch, rate, bits, size // block if block else 0, WAVE_FORMATS.get(tag, f'不明({tag})')), real, block, f.tell(), size)
            else:
                f.seek(size + size % 2, 1)
        if not fmt:
            return None
        tag, real, ch, rate, bits, block = fmt
        return PcmLayout(AudioInfo(ch, rate, bits, 0, WAVE_FORMATS.get(tag, f'不明({tag})')), real, block, 0, 0)

def _read_riff(path: str) -> Optional[AudioInfo]:
    layout = _read_riff_layout(path)
    return layout.info if layout else None

def _read_aiff(path: str) -> Optional[AudioInfo]:
    with open(long_path(path), 'rb') as f:
        if f.read(4) != b'FORM':
            return None
        f.read(4)
        if f.read(4) not in (b'AIFF', b'AIFC'):
            return None
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                return None
            cid, size = struct.unpack('>4sI', hdr)
            if cid == b'COMM':
                data = f.read(size)
                if len(data) < 18:
                    return None
                ch, frames, bits = struct.unpack('>hIh', data[:8])
                exp = struct.unpack('>H', data[8:10])[0]
                mant = struct.unpack('>Q', data[10:18])[0]
                rate = int(mant * 2.0 ** (exp - 16383 - 63))
                return AudioInfo(ch, rate, bits, frames, 'PCM')
            f.seek(size + size % 2, 1)

def read_audio_info(path: str) -> Optional[AudioInfo]:
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == '.wav':
            return _read_riff(path)
        if ext in ('.aif', '.aiff'):
            return _read_aiff(path)
    except (OSError, struct.error, ValueError):
        return None
    return None

def read_pcm_layout(path: str) -> Optional[PcmLayout]:
    if os.path.splitext(path)[1].lower() != '.wav':
        return None
    try:
        return _read_riff_layout(path)
    except (OSError, struct.error, ValueError):
        return None

def _decode(raw: bytes, bits: int, tag: int) -> List[float]:
    if tag == WAVE_FORMAT_FLOAT and bits == 32:
        values = array('f')
        values.frombytes(raw[:len(raw) // 4 * 4])
        if sys.byteorder == 'big':
            values.byteswap()
        return list(values)
    if tag != WAVE_FORMAT_PCM:
        return []
    if bits == 8:
        return [(b - 128) / 128.0 for b in raw]
    if bits == 16:
        values = array('h')
        values.frombytes(raw[:len(raw) // 2 * 2])
        if sys.byteorder == 'big':
            values.byteswap()
        return [v / 32768.0 for v in values]
    if bits == 24:
        n = len(raw) // 3
        padded = bytearray(n * 4)
        padded[1::4] = raw[0:n * 3:3]
        padded[2::4] = raw[1:n * 3:3]
        padded[3::4] = raw[2:n * 3:3]
        values = array('i')
        values.frombytes(bytes(padded))
        if sys.byteorder == 'big':
            values.byteswap()
        return [v / 2147483648.0 for v in values]
    if bits == 32:
        values = array('i')
        values.frombytes(raw[:len(raw) // 4 * 4])
        if sys.byteorder == 'big':
            values.byteswap()
        return [v / 2147483648.0 for v in values]
    return []

def read_samples(path: str, start: float=0.0, frames: Optional[int]=None, mono: bool=True, decimate: int=1) -> Tuple[List[float], int]:
    if decimate < 1:
        raise ValueError('間引き数は 1 以上である必要があります')
    layout = read_pcm_layout(path)
    if layout is None or not layout.block_align or (not layout.data_size):
        return ([], 0)
    if layout.format_tag not in (WAVE_FORMAT_PCM, WAVE_FORMAT_FLOAT):
        return ([], 0)
    info = layout.info
    if decimate > 1 and (not mono) and (info.channels > 1):
        raise ValueError('多チャンネルのまま間引くと左右が混ざります。mono=True にするか、decimate を 1 にしてください')
    available = layout.data_size // layout.block_align
    start_frame = max(0, int(start * info.sample_rate))
    if start_frame >= available:
        return ([], 0)
    count = available - start_frame if frames is None else min(frames, available - start_frame)
    if count <= 0:
        return ([], 0)
    try:
        with open(long_path(path), 'rb') as f:
            f.seek(layout.data_offset + start_frame * layout.block_align)
            raw = f.read(count * layout.block_align)
    except OSError:
        return ([], 0)
    samples = _decode(raw, info.bit_depth, layout.format_tag)
    if not samples:
        return ([], 0)
    channels = info.channels
    if mono and channels > 1:
        samples = [sum(samples[i:i + channels]) / channels for i in range(0, len(samples) - channels + 1, channels)]
    rate = info.sample_rate
    if decimate > 1:
        samples = [sum(samples[i:i + decimate]) / decimate for i in range(0, len(samples) - decimate + 1, decimate)]
        rate = info.sample_rate // decimate
    return (samples, rate)

def write_wav(path: str, data: bytes, channels: int, sample_rate: int, bit_depth: int, float_format: bool=False) -> None:
    if channels < 1 or sample_rate < 1 or bit_depth < 8:
        raise ValueError('チャンネル数・サンプルレート・ビット深度が不正です')
    block_align = channels * (bit_depth // 8)
    tag = WAVE_FORMAT_FLOAT if float_format else WAVE_FORMAT_PCM
    fmt_chunk = struct.pack('<4sIHHIIHH', b'fmt ', 16, tag, channels, sample_rate, sample_rate * block_align, block_align, bit_depth)
    pad = b'\x00' if len(data) % 2 else b''
    body = fmt_chunk + struct.pack('<4sI', b'data', len(data)) + data + pad
    os.makedirs(os.path.dirname(os.path.abspath(path)) or '.', exist_ok=True)
    with open(long_path(path), 'wb') as f:
        f.write(struct.pack('<4sI4s', b'RIFF', 4 + len(body), b'WAVE'))
        f.write(body)

def copy_region(src: str, dst: str, start: float=0.0, end: Optional[float]=None) -> Optional[AudioInfo]:
    layout = read_pcm_layout(src)
    if layout is None or not layout.block_align or (not layout.data_size):
        return None
    if layout.format_tag not in (WAVE_FORMAT_PCM, WAVE_FORMAT_FLOAT):
        return None
    info = layout.info
    available = layout.data_size // layout.block_align
    start_frame = max(0, int(start * info.sample_rate))
    end_frame = available if end is None else min(available, int(end * info.sample_rate))
    count = end_frame - start_frame
    if count <= 0:
        return None
    with open(long_path(src), 'rb') as f:
        f.seek(layout.data_offset + start_frame * layout.block_align)
        raw = f.read(count * layout.block_align)
    write_wav(dst, raw, info.channels, info.sample_rate, info.bit_depth, float_format=layout.format_tag == WAVE_FORMAT_FLOAT)
    return AudioInfo(info.channels, info.sample_rate, info.bit_depth, len(raw) // layout.block_align, info.codec)
