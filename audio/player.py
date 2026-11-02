"""試聴プレイヤー。just_playback をラップし、状態を日本語で読み上げ可能にする。

設計方針:
- 再生バックエンドを Protocol で抽象化（依存性注入）。
  本番は JustPlaybackBackend（miniaudio）、テストは NullBackend。
  これにより player のロジックを音を出さずにユニットテストできる。
- just_playback の seek() は「再生がアクティブな時のみ有効」という仕様。
  停止状態からの頭出しは play()→seek() の順で吸収する（toggle/seek で考慮）。
- 状態取得は describe_state_ja() に集約し、VoiceOver/PC-Talker が
  「再生中、3.2秒目、全長5.0秒」のように読み上げられる文字列を返す。
- アクセシビリティ上、スペースキー1つで「再生開始/停止」をトグルできる
  toggle_play_stop() を主操作として提供する。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class PlayerBackend(Protocol):
    """再生バックエンドが満たすべきインターフェース。

    just_playback.Playback がそのまま満たす形に合わせてある。
    """

    def load_file(self, path: str) -> None: ...
    def play(self) -> None: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def stop(self) -> None: ...
    def seek(self, pos: float) -> None: ...
    def set_volume(self, volume: float) -> None: ...

    @property
    def active(self) -> bool: ...
    @property
    def playing(self) -> bool: ...
    @property
    def paused(self) -> bool: ...
    @property
    def curr_pos(self) -> float: ...
    @property
    def duration(self) -> float: ...


class NullBackend:
    """音を出さないテスト用バックエンド。状態遷移のみ模擬する。

    just_playback の振る舞い（seek は active 時のみ有効、等）を
    最小限再現し、player ロジックの検証に使う。
    """

    def __init__(self) -> None:
        self._loaded: str | None = None
        self._active = False
        self._paused = False
        self._pos = 0.0
        self._duration = 0.0
        self._volume = 1.0

    def load_file(self, path: str) -> None:
        self._loaded = path
        self._active = False
        self._paused = False
        self._pos = 0.0
        # テストでは固定長とする（実ファイルを読まない）
        self._duration = 5.0

    def play(self) -> None:
        if self._loaded is None:
            return
        self._active = True
        self._paused = False
        self._pos = 0.0

    def pause(self) -> None:
        if self._active:
            self._paused = True

    def resume(self) -> None:
        if self._active:
            self._paused = False

    def stop(self) -> None:
        self._active = False
        self._paused = False
        self._pos = 0.0

    def seek(self, pos: float) -> None:
        # just_playback と同様、active でなければ無効
        if self._active:
            self._pos = max(0.0, min(pos, self._duration))

    def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(volume, 1.0))

    @property
    def active(self) -> bool:
        return self._active

    @property
    def playing(self) -> bool:
        return self._active and not self._paused

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def curr_pos(self) -> float:
        return self._pos

    @property
    def duration(self) -> float:
        return self._duration


def create_default_backend() -> PlayerBackend:
    """本番用バックエンド（just_playback）を生成する。

    just_playback は miniaudio のネイティブ依存があるため、
    import を関数内に閉じ込める。未導入環境では明示的な
    RuntimeError を送出し、呼び出し側で NullBackend に切り替えやすくする。
    """
    try:
        from just_playback import Playback
    except ImportError as exc:  # pragma: no cover - 環境依存
        raise RuntimeError(
            "just_playback が見つかりません。'pip install just_playback' "
            "を実行してください。"
        ) from exc
    return Playback()


# 状態の読み上げの語。別のアプリも借りて読むので、言語の設定（core/i18n.py）は読まない
_WORDS_JA = {"no_file": "ファイルが読み込まれていません", "sep": "、",
             "stopped": "停止中", "paused": "一時停止中", "playing": "再生中",
             "position": "{:.1f}秒目", "no_position": "位置不明",
             "length": "全長{:.1f}秒"}
_WORDS_EN = {"no_file": "No file is loaded", "sep": ", ",
             "stopped": "stopped", "paused": "paused", "playing": "playing",
             "position": "at {:.1f} seconds",
             "no_position": "position unknown",
             "length": "length {:.1f} seconds"}


class PreviewPlayer:
    """1ファイルの試聴を管理するプレイヤー。

    使用例:
        player = PreviewPlayer()              # 本番（just_playback）
        player = PreviewPlayer(NullBackend()) # テスト
        player.load("/path/to/kick.wav")
        player.toggle_play_stop()             # スペースキー割当を想定
        print(player.describe_state_ja())
    """

    SEEK_STEP = 1.0  # 矢印キーでのシーク量（秒）

    def __init__(self, backend: PlayerBackend | None = None) -> None:
        self._backend = (backend if backend is not None
                         else create_default_backend())
        self._current_path: str | None = None

    @property
    def current_path(self) -> str | None:
        return self._current_path

    @property
    def is_active(self) -> bool:
        """鳴っている（再生中または一時停止中）か。

        **止める手段を作るために要る。** 呼び出し側が「いま鳴っているか」
        を知らないと、再生と停止を 1 つのキーで切り替えられない。
        """
        try:
            return bool(self._backend.active)
        except Exception:
            return False

    def load(self, path: str | Path) -> None:
        """ファイルを読み込む（再生はしない）。"""
        path_str = str(path)
        self._backend.load_file(path_str)
        self._current_path = path_str

    def play(self) -> None:
        self._backend.play()

    def stop(self) -> None:
        self._backend.stop()

    def toggle_play_stop(self) -> None:
        """主操作。再生中なら停止、それ以外なら先頭から再生。

        スペースキー1つで完結させるためのトグル。試聴用途では
        「一時停止」より「停止して頭から」の方が直感的なため stop を採用。
        """
        if self._backend.active:
            self._backend.stop()
        else:
            self._backend.play()

    def toggle_pause(self) -> None:
        """一時停止/再開のトグル（補助操作）。"""
        if not self._backend.active:
            return
        if self._backend.paused:
            self._backend.resume()
        else:
            self._backend.pause()

    def seek_relative(self, delta: float) -> None:
        """現在位置から相対シーク。停止中は play してから頭出しする。

        just_playback の seek は active 時のみ有効なため、
        非アクティブ時は一旦 play して有効化する。
        """
        if not self._backend.active:
            self._backend.play()
        target = self._backend.curr_pos + delta
        self._backend.seek(max(0.0, target))

    def seek_to(self, pos: float) -> None:
        if not self._backend.active:
            self._backend.play()
        self._backend.seek(max(0.0, pos))

    def set_volume(self, volume: float) -> None:
        self._backend.set_volume(volume)

    def describe_state_ja(self) -> str:
        """現在の再生状態を日本語で返す（スクリーンリーダー読み上げ用）。"""
        return self._describe_state(_WORDS_JA)

    def describe_state_en(self) -> str:
        """現在の再生状態を英語で返す。"""
        return self._describe_state(_WORDS_EN)

    def _describe_state(self, words: dict[str, str]) -> str:
        if self._current_path is None:
            return words["no_file"]

        name = Path(self._current_path).name
        sep = words["sep"]
        if not self._backend.active:
            return f"{name}{sep}{words['stopped']}"

        state = words["paused"] if self._backend.paused else words["playing"]
        # 実機の just_playback は curr_pos / duration が None を返す場合が
        # あるため（バックエンド未初期化直後など）、None セーフに整形する。
        pos = self._backend.curr_pos
        dur = self._backend.duration
        pos_txt = (words["position"].format(pos)
                   if isinstance(pos, (int, float)) else words["no_position"])
        if isinstance(dur, (int, float)) and dur > 0:
            length = words["length"].format(dur)
            return f"{name}{sep}{state}{sep}{pos_txt}{sep}{length}"
        return f"{name}{sep}{state}{sep}{pos_txt}"
