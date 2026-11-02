"""オーディオメタデータ解析。

長さ・サンプルレート・ビット深度・チャンネル数を取得する。

設計方針:
- WAV/AIFF は標準ライブラリ（wave/aifc）で解析。追加依存なし・高速。
- それ以外（FLAC/MP3 等）は soundfile に遅延フォールバック。
  soundfile を未インストールでも core 層全体は import できるよう、
  関数内で import する（ImportError は安全に握りつぶし、メタdata不明扱い）。
- 解析失敗は例外を投げず、取れた範囲だけ埋めた AudioMetadata を返す。
  これは「1ファイルの解析失敗でスキャン全体を止めない」ための方針。
"""

from __future__ import annotations

import contextlib
import wave
from pathlib import Path

from .models import AudioMetadata

# 標準ライブラリで直接解析できる形式
_NATIVE_WAVE = {"wav"}
_NATIVE_AIFF = {"aif", "aiff", "aifc"}


def extract_metadata(path: str | Path) -> AudioMetadata:
    """ファイルパスからメタデータを抽出する。

    どの解析にも失敗した場合は、全フィールド None の AudioMetadata を返す
    （例外は投げない）。呼び出し側はスキャンを継続できる。
    """
    p = Path(path)
    fmt = p.suffix.lstrip(".").lower()

    if fmt in _NATIVE_WAVE:
        meta = _read_wave(p)
        if meta is not None:
            return meta
    elif fmt in _NATIVE_AIFF:
        meta = _read_aiff(p)
        if meta is not None:
            return meta

    # 標準ライブラリで読めない／読めなかった場合は soundfile に委ねる
    meta = _read_soundfile(p)
    if meta is not None:
        return meta

    return AudioMetadata()


def _read_wave(path: Path) -> AudioMetadata | None:
    """標準 wave モジュールで WAV を解析。失敗時 None。"""
    try:
        with contextlib.closing(wave.open(str(path), "rb")) as wf:
            channels = wf.getnchannels()
            sample_rate = wf.getframerate()
            sample_width = wf.getsampwidth()  # バイト単位
            n_frames = wf.getnframes()
            bit_depth = sample_width * 8 if sample_width else None
            duration = n_frames / sample_rate if sample_rate else None
            return AudioMetadata(
                duration_sec=duration,
                sample_rate=sample_rate or None,
                bit_depth=bit_depth,
                channels=channels or None,
            )
    except (wave.Error, EOFError, OSError):
        return None


def _read_aiff(path: Path) -> AudioMetadata | None:
    """標準 aifc モジュールで AIFF/AIFF-C を解析。失敗時 None。

    aifc は Python 3.13 で deprecated（3.13系で削除予定）。
    そのため import 自体を関数内に閉じ込め、未提供環境では
    soundfile フォールバックへ素直に流れるようにする。
    """
    try:
        import aifc  # 3.13+ で存在しない可能性があるため遅延 import
    except ImportError:
        return None
    try:
        with contextlib.closing(aifc.open(str(path), "rb")) as af:
            channels = af.getnchannels()
            sample_rate = int(af.getframerate())
            sample_width = af.getsampwidth()
            n_frames = af.getnframes()
            bit_depth = sample_width * 8 if sample_width else None
            duration = n_frames / sample_rate if sample_rate else None
            return AudioMetadata(
                duration_sec=duration,
                sample_rate=sample_rate or None,
                bit_depth=bit_depth,
                channels=channels or None,
            )
    except (aifc.Error, EOFError, OSError):
        return None


def _read_soundfile(path: Path) -> AudioMetadata | None:
    """soundfile（libsndfile）で解析。未インストール／失敗時 None。

    FLAC・MP3・OGG 等の多形式に対応する。soundfile は macOS の
    pip wheel に libsndfile が同梱されるため追加作業は通常不要。
    """
    try:
        import soundfile as sf  # 遅延 import（未導入でも core は動く）
    except ImportError:
        return None
    try:
        info = sf.info(str(path))
    except (RuntimeError, OSError):
        return None

    # soundfile の subtype から概算ビット深度を推定（PCM系のみ）
    bit_depth = _bit_depth_from_subtype(info.subtype)
    duration = info.frames / info.samplerate if info.samplerate else None
    return AudioMetadata(
        duration_sec=duration,
        sample_rate=info.samplerate or None,
        bit_depth=bit_depth,
        channels=info.channels or None,
    )


def _bit_depth_from_subtype(subtype: str | None) -> int | None:
    """soundfile の subtype 文字列からビット深度を推定。

    FLAC/MP3 等は厳密なビット深度概念が異なるため、PCM系のみ返す。
    不明な場合は None（UI 側で省略される）。
    """
    if not subtype:
        return None
    mapping = {
        "PCM_S8": 8,
        "PCM_U8": 8,
        "PCM_16": 16,
        "PCM_24": 24,
        "PCM_32": 32,
    }
    return mapping.get(subtype)
