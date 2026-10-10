"""本地 TTS 语音合成引擎：基于 edge-tts (微软 XiaoxiaoNeural 甜美真人音色)。

彻底废弃 macOS 本地 say -v Tingting 机械音，纯内存直接转码为 24kHz int16 PCM 送给 AudioEngine 播放。
"""
from __future__ import annotations

import asyncio
import io
import logging
import time
import av
import edge_tts
import numpy as np

log = logging.getLogger("local_tts")

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
TARGET_RATE = 24000


class LocalTTS:
    """本地真实音色合成器。"""

    def __init__(self, voice: str = DEFAULT_VOICE, sample_rate: int = TARGET_RATE):
        self.voice = voice
        self.sample_rate = sample_rate

    async def synthesize_pcm(self, text: str) -> np.ndarray:
        """合成文本，纯内存通过 PyAV 解码为 24kHz 单声道 int16 PCM ndarray。"""
        text = text.strip()
        if not text:
            return np.zeros(0, dtype=np.int16)

        t0 = time.perf_counter()
        comm = edge_tts.Communicate(text, self.voice)
        mp3_io = io.BytesIO()
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                mp3_io.write(chunk["data"])

        mp3_io.seek(0)
        if mp3_io.getbuffer().nbytes == 0:
            return np.zeros(0, dtype=np.int16)

        container = av.open(mp3_io, format="mp3")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=self.sample_rate)

        chunks = []
        for frame in container.decode(audio=0):
            for rf in resampler.resample(frame):
                chunks.append(rf.to_ndarray().reshape(-1))

        container.close()
        pcm = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16)
        t1 = time.perf_counter()
        log.info("TTS 甜美音色合成完成 (文本 '%s' -> %.2fs 音频，耗时 %.1fms)",
                 text, len(pcm) / self.sample_rate, (t1 - t0) * 1000)
        return pcm
