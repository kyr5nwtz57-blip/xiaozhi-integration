"""本地 ASR 引擎：基于 faster-whisper int8 (Mac CPU 0.22s 极速转录)。

零网络延迟，零小智公有云断线问题，本地纯离线高准确率中文语音识别。
"""
from __future__ import annotations

import logging
import time
import numpy as np
from faster_whisper import WhisperModel

log = logging.getLogger("local_asr")


class LocalASR:
    """本地语音识别引擎 (faster-whisper int8)。"""

    def __init__(self, model_size: str = "tiny", device: str = "cpu", compute_type: str = "int8"):
        t0 = time.perf_counter()
        self.model = WhisperModel(model_size, device=device, compute_type=compute_type)
        t1 = time.perf_counter()
        log.info("faster-whisper (%s, %s, %s) 加载就绪，耗时 %.1fms",
                 model_size, device, compute_type, (t1 - t0) * 1000)

    def transcribe(self, audio_data: np.ndarray | bytes, sample_rate: int = 16000) -> str:
        """内存直接转录。

        audio_data: int16 PCM (bytes 或 ndarray)，或 float32 ndarray [-1, 1]。
        返回识别出的中文文本字符串。
        """
        if isinstance(audio_data, bytes):
            pcm = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
        elif audio_data.dtype == np.int16:
            pcm = audio_data.astype(np.float32) / 32768.0
        else:
            pcm = audio_data.astype(np.float32)

        if len(pcm) < sample_rate * 0.3:  # 语音过短 (<0.3s) 直接忽略
            return ""

        t0 = time.perf_counter()
        segments, _ = self.model.transcribe(pcm, language="zh", beam_size=1)
        text = "".join(s.text for s in segments).strip()
        t1 = time.perf_counter()
        log.info("ASR 转录完成 (%.2fs 音频耗时 %.1fms): '%s'",
                 len(pcm) / sample_rate, (t1 - t0) * 1000, text)
        return text
