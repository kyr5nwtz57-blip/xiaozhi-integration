"""Opus 编解码（PyAV 版）。

为什么不用 opuslib：opuslib 是源码包，pip 装完还要系统里有 libopus
（macOS 上得 brew install opus）。PyAV 的 wheel 自带 FFmpeg（含 libopus），
纯 pip install av 就能用，零系统依赖。
"""
from __future__ import annotations

from fractions import Fraction

import av
import numpy as np


class OpusEncoder:
    """16kHz 单声道 PCM(int16 LE) → Opus 包。60ms 一帧 = 960 采样点。"""

    def __init__(self, sample_rate: int = 16000):
        self.sample_rate = sample_rate
        ctx = av.CodecContext.create("libopus", "w")
        ctx.sample_rate = sample_rate
        ctx.layout = "mono"
        ctx.format = "s16"
        ctx.time_base = Fraction(1, sample_rate)
        self._ctx = ctx
        self._pts = 0

    def encode_frame(self, pcm: bytes) -> list[bytes]:
        """一帧 PCM → 0~n 个 Opus 包（稳定后每帧 1 个，开头有编码器预热）。"""
        n = len(pcm) // 2
        arr = np.frombuffer(pcm, dtype=np.int16).reshape(1, -1).copy()
        frame = av.AudioFrame.from_ndarray(arr, format="s16", layout="mono")
        frame.sample_rate = self.sample_rate
        frame.time_base = Fraction(1, self.sample_rate)
        frame.pts = self._pts
        self._pts += n
        return [bytes(p) for p in self._ctx.encode(frame)]


class OpusDecoder:
    """Opus 包 → 固定采样点数的 PCM(int16 LE)。多余采样缓存，缺了补零。"""

    def __init__(self, sample_rate: int = 24000, frame_samples: int = 1440):
        self.sample_rate = sample_rate
        self.frame_samples = frame_samples
        self._ctx = av.CodecContext.create("libopus", "r")
        self._resampler = av.AudioResampler(
            format="s16", layout="mono", rate=sample_rate
        )
        self._pending = np.zeros(0, dtype=np.int16)

    def decode_packet(self, packet: bytes) -> bytes:
        try:
            frames = self._ctx.decode(av.Packet(packet))
        except Exception:
            frames = []
        chunks = []
        for f in frames:
            try:
                for rf in self._resampler.resample(f):
                    chunks.append(rf.to_ndarray().reshape(-1))
            except Exception:
                continue
        if chunks:
            self._pending = np.concatenate([self._pending] + chunks)
        need = self.frame_samples
        if len(self._pending) < need:
            out = np.zeros(need, dtype=np.int16)
            out[: len(self._pending)] = self._pending
            self._pending = np.zeros(0, dtype=np.int16)
        else:
            out = self._pending[:need].copy()
            self._pending = self._pending[need:].copy()
        return out.tobytes()


def opus_roundtrip_selftest(sample_rate: int = 16000,
                             frame_samples: int = 960) -> bool:
    """不依赖声卡：编码→解码往返，验证 PyAV/Opus 可用。"""
    enc = OpusEncoder(sample_rate)
    dec = OpusDecoder(sample_rate, frame_samples)
    pcm = (np.sin(2 * np.pi * 440 * np.arange(frame_samples) / sample_rate)
           * 10000).astype(np.int16).tobytes()
    packets = enc.encode_frame(pcm)
    if not packets:
        return False
    out = dec.decode_packet(packets[-1])
    return len(out) == frame_samples * 2 and len(b"".join(packets)) > 0
