"""Video preview: QMediaPlayer + QVideoWidget with play/pause, seek bar, time label and (optionally) Close."""

from __future__ import annotations

import weakref
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget

from .common import fmt_time


_previews: "weakref.WeakSet[VideoPreview]" = weakref.WeakSet()


class VideoPreview(QWidget):
    positionChanged = Signal(float)
    closed = Signal()

    @staticmethod
    def release_everywhere(path: Path | str) -> int:
        """Release `path` in every preview in the app (Results, Analyze, …) so Windows lets it be deleted."""
        from ...pipeline.library import same_path

        released = 0
        for preview in list(_previews):
            try:
                current = preview.source_path
            except RuntimeError:       # the C++ widget is already gone
                continue
            if current and same_path(current, path):
                preview.release()
                released += 1
        return released

    def __init__(self, parent: QWidget | None = None, closable: bool = False, video_min_height: int = 220):
        super().__init__(parent)
        _previews.add(self)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.8)
        self.player.setAudioOutput(self.audio)
        self.video = QVideoWidget(self)
        self.video.setMinimumHeight(video_min_height)
        self.player.setVideoOutput(self.video)
        self.play_btn = QPushButton("Play")
        self.play_btn.setEnabled(False)
        self.play_btn.clicked.connect(self.toggle)
        self.close_btn = QPushButton("Close")
        self.close_btn.setToolTip("Stop playback and release the file")
        self.close_btn.setEnabled(False)
        self.close_btn.clicked.connect(self.close_media)
        self.close_btn.setVisible(closable)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setEnabled(False)
        self.slider.sliderMoved.connect(lambda v: self.player.setPosition(v))
        self.time = QLabel("0:00 / 0:00")
        self.time.setObjectName("Muted")
        self.info = QLabel("No clip loaded")
        self.info.setObjectName("Muted")
        self.error = QLabel("")
        self.error.setObjectName("Muted")
        self.error.setWordWrap(True)
        self.error.hide()                 # takes no space until there is an error to show
        self.player.positionChanged.connect(self._pos)
        self.player.durationChanged.connect(self._dur)
        self.player.playbackStateChanged.connect(self._state)
        self.player.mediaStatusChanged.connect(self._status)
        self.player.errorOccurred.connect(lambda e, msg: self._set_error(f"Preview unavailable: {msg}" if msg else ""))
        controls = QHBoxLayout()
        controls.addWidget(self.play_btn)
        controls.addWidget(self.slider, 1)
        controls.addWidget(self.time)
        controls.addWidget(self.close_btn)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.video, 1)
        lay.addLayout(controls)
        lay.addWidget(self.info)
        lay.addWidget(self.error)
        self._duration_ms = 0
        self._name = ""
        self._poster_pending = False
        self._pending_seek_ms: int | None = None

    def _set_error(self, text: str) -> None:
        self.error.setText(text)
        self.error.setVisible(bool(text))

    @property
    def source_path(self) -> str:
        return self.player.source().toLocalFile()

    def load(self, path: Path | str | None) -> None:
        self.player.stop()
        self._set_error("")
        if not path:
            self._clear()
            return
        self._name = Path(path).name
        self.info.setText(f"{self._name} · loading…")
        self._poster_pending = True
        self._pending_seek_ms = None
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.play_btn.setEnabled(True)
        self.slider.setEnabled(True)
        self.close_btn.setEnabled(True)

    def release(self) -> None:
        """Stop and drop the current file so Windows lets it be deleted or overwritten."""
        self.player.stop()
        self.player.setSource(QUrl())
        self._clear()
        QApplication.processEvents()

    def close_media(self) -> None:
        self.release()
        self.closed.emit()

    def _clear(self) -> None:
        self.player.setSource(QUrl())
        self.play_btn.setEnabled(False)
        self.slider.setEnabled(False)
        self.close_btn.setEnabled(False)
        self.slider.setValue(0)
        self._duration_ms = 0
        self._name = ""
        self._poster_pending = False
        self._pending_seek_ms = None
        self.time.setText("0:00 / 0:00")
        self.info.setText("No clip loaded")

    def seek(self, seconds: float) -> None:
        ms = int(max(0.0, seconds) * 1000)
        if self.player.mediaStatus() in (QMediaPlayer.MediaStatus.NoMedia, QMediaPlayer.MediaStatus.LoadingMedia):
            self._pending_seek_ms = ms      # Qt drops seeks issued while the file is still loading
        else:
            self.player.setPosition(ms)

    def toggle(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def stop(self) -> None:
        self.player.stop()

    def _pos(self, ms: int) -> None:
        if not self.slider.isSliderDown():
            self.slider.setValue(ms)
        self.time.setText(f"{fmt_time(ms / 1000)} / {fmt_time(self._duration_ms / 1000)}")
        self.positionChanged.emit(ms / 1000)

    def _dur(self, ms: int) -> None:
        self._duration_ms = ms
        self.slider.setRange(0, ms)
        self.time.setText(f"{fmt_time(self.player.position() / 1000)} / {fmt_time(ms / 1000)}")

    def _status(self, status) -> None:
        if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia) and self._name:
            tracks = [("video", self.player.hasVideo()), ("audio", self.player.hasAudio())]
            self.info.setText(f"{self._name} · " + " · ".join(f"{n} {'✓' if ok else '✗ none'}" for n, ok in tracks))
            if self._pending_seek_ms is not None:
                self.player.setPosition(self._pending_seek_ms)
                self._pending_seek_ms = None
            if self._poster_pending and self.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState:
                # Paused (not playing) shows the first frame, or the frame at a pending seek, with no sound;
                # a stopped player shows nothing until Play.
                self._poster_pending = False
                self.player.pause()

    def _state(self, state) -> None:
        self.play_btn.setText("Pause" if state == QMediaPlayer.PlaybackState.PlayingState else "Play")
