"""回声消除（AEC）封装：WebRTC 音频处理（APM）。

为什么：官方固件靠 ESP32 硬件 AFE（AEC+降噪+VAD），成熟 PC 客户端 py-xiaozhi
靠 WebRTC 音频处理。我们原来用手写的半双工回声门——她说话时把上行置零——
结果是"她一开始说话，用户的麦就被静音"，用户在她说话时插话全丢。

现在改成真全双工：
  - 麦克风（near）**永不静音**，任何时候都上行；
  - 参考信号（far）= 我们下行解码出来的播放 PCM，喂给 AEC 做回声消除；
  - AEC 只减回声，不动用户语音（实测回声抑制 ~41dB，用户语音 -0.4dB）。

库：libs/webrtc_apm 的 ctypes 封装 + 三端预编译动态库。py-xiaozhi 同款。
macOS arm64/x64 的 dylib 已随包 vendor 在 sidecar/webrtc_apm/ 下，无编译依赖。

数据流（10ms 帧，near/far 同采样率）：
  feed_far(下行 int16 @far_rate)  → 重采样到 near_rate → 入队
  process_near(上行 int16 @near_rate) → 排空 far 队列 → APM.ProcessStream → 同长 int16

失败一律旁路（active=False），绝不让上行断掉。
"""
from __future__ import annotations

import collections
import ctypes
import logging
import threading
from fractions import Fraction

import numpy as np

log = logging.getLogger("aec")

FRAME_MS = 10


def _make_resampler(far_rate: int, near_rate: int):
    """far_rate → near_rate 的重采样器。优先 scipy 多相，缺失退回线性插值。"""
    if far_rate == near_rate:
        return lambda x: np.asarray(x, dtype=np.float32)
    frac = Fraction(near_rate, far_rate)
    up, down = frac.numerator, frac.denominator
    try:
        from scipy.signal import resample_poly

        def _rp(x):
            x = np.asarray(x, dtype=np.float32)
            if len(x) < 2:
                return x
            return resample_poly(x, up, down).astype(np.float32)

        return _rp
    except Exception:  # noqa: BLE001 - scipy 缺失时退回线性插值
        def _lin(x):
            x = np.asarray(x, dtype=np.float32)
            n = len(x)
            if n < 2:
                return x
            n_out = max(1, int(round(n * near_rate / far_rate)))
            idx = np.linspace(0.0, n - 1.0, n_out)
            return np.interp(idx, np.arange(n), x).astype(np.float32)

        return _lin


class Aec:
    """WebRTC APM 封装。near 16k 单声道 int16；far 为下行播放 PCM。"""

    def __init__(self, near_rate: int = 16000, far_rate: int = 24000,
                 delay_ms: int = 120, enable_ns: bool = False,
                 enable_hp: bool = True) -> None:
        import webrtc_apm as W  # vendor 在 sidecar/webrtc_apm/

        self._W = W
        self._near_rate = int(near_rate)
        self._far_rate = int(far_rate)
        self._frame = self._near_rate // 100  # 10ms
        self._active = False
        self._lock = threading.Lock()

        self._far_pending: collections.deque = collections.deque()
        self._far_buf = np.zeros(0, dtype=np.float32)
        self._resample = _make_resampler(self._far_rate, self._near_rate)

        self._apm = W.WebRTCAudioProcessing()
        cfg = W.create_default_config()
        cfg.echo.enabled = True
        cfg.echo.mobile_mode = False
        cfg.high_pass.enabled = bool(enable_hp)
        if enable_ns:
            cfg.noise_suppress.enabled = True
            cfg.noise_suppress.noise_level = W.NoiseSuppressionLevel.MODERATE
        rc = self._apm.apply_config(cfg)
        if rc != 0:
            raise RuntimeError("apply_config 返回 %s" % rc)
        self._sc = self._apm.create_stream_config(self._near_rate, 1)
        self._apm.set_stream_delay_ms(int(delay_ms))

        self._near_in = (ctypes.c_short * self._frame)()
        self._near_out = (ctypes.c_short * self._frame)()
        self._far_in = (ctypes.c_short * self._frame)()
        self._far_out = (ctypes.c_short * self._frame)()
        self._active = True
        log.info("AEC 已启用（WebRTC APM）：near %dHz, far %dHz, delay %dms, ns=%s, hp=%s",
                 self._near_rate, self._far_rate, delay_ms, enable_ns, enable_hp)

    @property
    def active(self) -> bool:
        return self._active

    def set_far_rate(self, rate: int) -> None:
        """下行采样率变化（换解码器）时更新参考信号重采样器。"""
        rate = int(rate)
        if rate == self._far_rate:
            return
        with self._lock:
            self._far_rate = rate
            self._resample = _make_resampler(rate, self._near_rate)
            self._far_pending.clear()
            self._far_buf = np.zeros(0, dtype=np.float32)

    def feed_far(self, pcm_i16) -> None:
        """播放参考信号：下行解码出的 int16 PCM（far_rate）。只在播放线程/收包线程调。"""
        if not self._active:
            return
        try:
            arr = np.asarray(pcm_i16, dtype=np.int16)
            if arr.size:
                with self._lock:
                    self._far_pending.append(arr)
                    # 队列上限防堆积（收包线程停转时）
                    while len(self._far_pending) > 200:
                        self._far_pending.popleft()
        except Exception:  # noqa: BLE001 - 参考信号路径绝不抛
            pass

    def process_near(self, x_i16) -> np.ndarray:
        """处理上行采集帧（int16 @near_rate）。返回消回声后的同长 int16。失败旁路。"""
        if not self._active:
            return x_i16
        x = np.asarray(x_i16, dtype=np.int16)
        n = len(x)
        if n == 0:
            return x
        if n % self._frame != 0:
            # 非 10ms 整数倍：对齐前缀走 AEC，尾巴原样保留
            m = (n // self._frame) * self._frame
            out = x.copy()
            if m:
                out[:m] = self._process_aligned(x[:m])
            return out
        return self._process_aligned(x)

    def _drain_far_locked(self) -> None:
        while self._far_pending:
            blk = self._far_pending.popleft()
            rs = self._resample(blk)
            if len(rs):
                self._far_buf = np.concatenate([self._far_buf, rs])

    def _process_aligned(self, x: np.ndarray) -> np.ndarray:
        F = self._frame
        n = len(x)
        out = np.empty(n, dtype=np.int16)
        try:
            with self._lock:
                self._drain_far_locked()
                far = self._far_buf
                pos = 0
                for off in range(0, n, F):
                    if pos + F <= len(far):
                        ff = far[pos:pos + F]
                        pos += F
                    else:
                        ff = np.zeros(F, dtype=np.float32)
                        take = max(0, len(far) - pos)
                        if take:
                            ff[:take] = far[pos:pos + take]
                        pos = len(far)
                    fi = np.clip(ff, -32768.0, 32767.0).astype(np.int16)
                    ctypes.memmove(self._far_in, fi.ctypes.data, F * 2)
                    self._apm.process_reverse_stream(
                        self._far_in, self._sc, self._sc, self._far_out)
                    ctypes.memmove(self._near_in, x[off:off + F].ctypes.data, F * 2)
                    self._apm.process_stream(
                        self._near_in, self._sc, self._sc, self._near_out)
                    out[off:off + F] = np.frombuffer(
                        self._near_out, dtype=np.int16)
                self._far_buf = far[pos:]
            return out
        except Exception as e:  # noqa: BLE001 - 出错旁路，别断上行
            log.warning("AEC 处理失败，已旁路：%s", e)
            self._active = False
            return x

    def close(self) -> None:
        with self._lock:
            self._active = False
            try:
                if self._apm is not None and self._sc is not None:
                    self._apm.destroy_stream_config(self._sc)
            except Exception:  # noqa: BLE001
                pass
            self._sc = None
            self._apm = None


def make_aec(near_rate: int = 16000, far_rate: int = 24000, **kw):
    """尽力创建 AEC。失败返回 None（调用方退回"只去直流+限幅"，绝不让上行断掉）。"""
    try:
        return Aec(near_rate=near_rate, far_rate=far_rate, **kw)
    except Exception as e:  # noqa: BLE001
        log.warning("AEC 初始化失败，退回无 AEC 原样上行：%s", e)
        return None
