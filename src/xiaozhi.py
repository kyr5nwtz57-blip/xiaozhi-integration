"""小智官方 WebSocket 客户端（asyncio）。

握手（已按官方文档 https://github.com/78/xiaozhi-esp32/blob/main/docs/websocket_zh.md 核对）：
  WSS + headers:
    Authorization: Bearer <token>   (token 来自 OTA websocket.token)
    Protocol-Version: 1
    Device-Id / Client-Id           (config.json 里的身份)
  → 发 {"type":"hello","version":1,"transport":"websocket",
        "audio_params":{"format":"opus","sample_rate":16000,"channels":1,"frame_duration":60}}
  → 等服务器 hello，取 session_id 与下行 audio_params（通常 24000Hz）

语音循环（真全双工，mode=realtime）：
  点她一下 → 发 {"type":"listen","state":"start","mode":"realtime","session_id"}
              → 麦克风**永不静音**持续推流；本地 WebRTC AEC 消掉她自己的回声。
  服务端自动：listening → stt → thinking → tts(speaking) → listening ...
  说话中插话（barge-in）：她 TTS 期间你说的话也会被服务端收到并自动打断她。
    （实测：mode=auto 时她说话期间的上行会被服务端丢弃，故用 realtime。）
  再点她一下 → 发 {"type":"listen","state":"stop",...} → 回到 idle

状态机：idle → listening → thinking → speaking → listening ... → idle
通过 on_event 回调向外广播：
  {"event":"state","state":...} / {"event":"stt","text":...} / {"event":"tts_text","text":...}
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

import websockets

from audio import AudioEngine, AudioUnavailable

# 秘书工具（设备端 MCP）在 ../secretary/
_SEC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "secretary")
if _SEC not in sys.path:
    sys.path.insert(0, _SEC)
from mcp import McpHandler  # noqa: E402

log = logging.getLogger("xiaozhi")

# 注意：PyAV 15.1 的 CodecContext 无法设置 frame_duration，
# 实际每个 60ms 输入帧会被拆成 3 个 20ms Opus 包。与这里声明的 60 语义等价
# （总时长都是 60ms），实测服务端 STT 正常。不要试图"修"它。
HELLO = {
    "type": "hello",
    "version": 1,
    "features": {"mcp": True},  # 声明支持设备端 MCP，服务器才会下发 initialize/tools
    "transport": "websocket",
    "audio_params": {
        "format": "opus",
        "sample_rate": 16000,
        "channels": 1,
        "frame_duration": 60,
    },
}

IDLE, LISTENING, THINKING, SPEAKING = "idle", "listening", "thinking", "speaking"

# 监听模式。实测（2026-10-09，见 /tmp/barge3.py）：
#   "auto"     —— 她 TTS 说话期间送上去的音频会被服务端**丢弃**，无法插话（barge-in）。
#   "realtime" —— 她在说话时也能收到你插的话，并自动打断她（真全双工）。py-xiaozhi 在
#                 启用 AEC 时就用 realtime。我们用 WebRTC AEC，所以默认 realtime。
# 想回退设 GF_LISTEN_MODE=auto。
LISTEN_MODE = os.environ.get("GF_LISTEN_MODE", "realtime")

# 重连退避：小智服务端会周期性主动结束会话——约 60s 没听到用户说话，它就让助手
# 道别（"拜拜/下次再聊"）并关掉 WebSocket（客户端收到 1005）。所以"断线"是常态，
# 不是网络故障。退避只在**连续失败**时增长，一旦握手成功立刻回到下限，
# 否则每次断线都翻倍（实测 1→2→4→8→16→30s），用户要干等半分钟她才"活过来"。
_BACKOFF_MIN = 1.0
_BACKOFF_MAX = 5.0


class NotBoundError(RuntimeError):
    """WS 能连上但需要先绑定（OTA 返回了 activation）。"""


class XiaozhiClient:
    def __init__(self, cfg: dict, audio: AudioEngine | None,
                 on_event=None):
        self.cfg = cfg
        self.audio = audio
        self.on_event = on_event or (lambda e: None)
        self.ws_url = cfg.get("websocket_url", "wss://api.tenclass.net/xiaozhi/v1/")
        self.token = cfg.get("websocket_token", "")
        self.state = IDLE
        self.session_id = ""
        self._ws = None
        self._want_listen = asyncio.Event()
        self._stop = asyncio.Event()
        self._preroll_ms = 0  # 进入对话时要补发的唤醒前音频长度
        self._backoff = _BACKOFF_MIN   # 重连退避（握手成功即重置）
        self._had_session = False      # 是否成功连过一次（区分首连 / 断线自动恢复）
        self._send_lock = asyncio.Lock()

        # 形象驱动用（bridge 会广播给浏览器）
        self.current_emotion = ""
        self.last_tts_text = ""

        # 设备端 MCP：秘书工具
        self.mcp = McpHandler(log=lambda m: log.info("MCP %s", m))

        # 每轮 TTS 结束后补发 listen start 的任务（连续对话关键，见 _rearm_listen）
        self._rearm_task = None

    # ---------- 对外 API ----------
    def set_state(self, s: str) -> None:
        if s != self.state:
            self.state = s
            log.info("state -> %s", s)
            self.on_event({"event": "state", "state": s})

    async def start_listen(self, preroll_ms: int = 0) -> None:
        """进入连续对话模式（全双工）：麦克风持续推流，服务端 VAD 自动断句。

        preroll_ms > 0 时会先补发这段"唤醒前"的音频 —— 唤醒词检测的几百毫秒
        延迟本来会把「景甜」后面的话吃掉，补发就是为了救回来。
        """
        if self.audio is None:
            raise AudioUnavailable("没有音频设备，无法说话（请去有麦克风的机器上跑）")
        if self._want_listen.is_set():
            return  # 已在对话中
        self._preroll_ms = preroll_ms
        self._want_listen.set()

    async def stop_listen(self) -> None:
        """退出对话模式，回到 idle。"""
        self._want_listen.clear()
        # _sender 循环检测到后会发 listen stop 并停麦克风

    async def abort(self, reason: str = "user_interrupted") -> None:
        """打断她当前说话。如在对话模式中，保持对话继续（她会重新听）。

        reason 用 "wake_word_detected" 表示是被唤醒词打断的（官方协议取值）。
        """
        if self.audio:
            self.audio.stop_playback(immediate=True)   # 立刻静音，别拖泥带水
            self.audio.release_gate_in(0.2)            # 回声尾巴很短，赶紧放开麦克风
        await self._send_json({"type": "abort", "reason": reason,
                               "session_id": self.session_id})
        if self._want_listen.is_set():
            # 仍在对话模式：立即回到 listening 等下一句
            self.set_state(LISTENING)
        else:
            self.set_state(IDLE)

    def shutdown(self) -> None:
        self._stop.set()
        self._want_listen.clear()
        if self._rearm_task is not None:
            self._rearm_task.cancel()

    def is_in_conversation(self) -> bool:
        """是否处于连续对话模式中。"""
        return self._want_listen.is_set()

    def get_telemetry(self) -> dict:
        """给形象渲染层用的实时遥测。"""
        audio_energy = 0.0
        if self.audio:
            audio_energy = round(self.audio.playback_energy, 4)
        return {
            "state": self.state,
            "emotion": self.current_emotion,
            "tts_text": self.last_tts_text,
            "energy": audio_energy,
            "online": self._ws is not None,
        }

    # ---------- 主循环 ----------
    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self._session()
                self._backoff = _BACKOFF_MIN
            except NotBoundError:
                raise
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001 - 重连兜底
                log.warning("连接断开（%s），%.1fs 后重连", e, self._backoff)
                # 断线要单独通知：否则对外只表现为一个普通 idle，
                # 表现层不知道"她是被掐断的"还是"正常回到待机"
                self.on_event({"event": "conn", "online": False, "reason": str(e)})
                self.set_state(IDLE)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._backoff)
                    break
                except asyncio.TimeoutError:
                    pass
                # 只在连续失败时增长；一旦握手成功会在 _session 里重置回下限
                self._backoff = min(self._backoff * 2, _BACKOFF_MAX)

    async def _session(self) -> None:
        if not self.token:
            raise NotBoundError("config.json 里没有 websocket_token，先跑 --activate 并绑定")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Protocol-Version": "1",
            "Device-Id": self.cfg["device_id"],
            "Client-Id": self.cfg["client_id"],
        }
        log.info("连接 %s", self.ws_url)
        # 参数名与返回类型都随 websockets 版本变：本机 12.0 的 connect 走 legacy 实现，
        # 参数是 extra_headers，且返回对象**没有 __aenter__**（不能用 async with）；
        # 新实现用 additional_headers。两种都试，连接后统一 try/finally 收尾。
        try:
            ws = await websockets.connect(self.ws_url, extra_headers=headers,
                                          ping_interval=15, ping_timeout=20)
        except TypeError:
            ws = await websockets.connect(self.ws_url, additional_headers=headers,
                                          ping_interval=15, ping_timeout=20)
        try:
            self._ws = ws
            await self._send_json(HELLO)
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
            except asyncio.TimeoutError:
                raise RuntimeError("等服务器 hello 超时")
            hello = json.loads(raw)
            if hello.get("type") != "hello":
                raise RuntimeError(f"握手异常，收到：{hello}")
            self.session_id = hello.get("session_id", "")
            ap = hello.get("audio_params", {}) or {}
            log.info("握手成功 session=%s 下行采样率=%s",
                     self.session_id, ap.get("sample_rate"))
            # 下行采样率若与 AudioEngine 不一致，只换解码器。
            # 绝不能重建整个 AudioEngine：编码器必须全程唯一，否则 pts 会重置。
            if self.audio and ap.get("sample_rate") not in (None, self.audio.down_sample_rate):
                rate = int(ap["sample_rate"])
                self.audio.rebuild_decoder(rate)
            self.on_event({"event": "conn", "online": True, "reason": ""})
            self._backoff = _BACKOFF_MIN   # 握手成功 → 下次断线只等最小退避
            self.set_state(IDLE)
            recv_task = asyncio.create_task(self._receiver())
            send_task = asyncio.create_task(self._sender())
            done, pending = await asyncio.wait(
                [recv_task, send_task], return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
            for t in done:
                t.result()
            if not self._stop.is_set():
                raise ConnectionResetError("WebSocket 连接已断开")
        finally:
            self._ws = None
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    async def _send_json(self, obj: dict) -> None:
        if self._ws is None:
            return
        async with self._send_lock:
            if self._ws is not None:
                try:
                    await self._ws.send(json.dumps(obj, ensure_ascii=False))
                except Exception:
                    pass

    async def _send_binary(self, data: bytes) -> None:
        if self._ws is None:
            return
        async with self._send_lock:
            if self._ws is not None:
                await self._ws.send(data)

    async def _rearm_listen(self) -> None:
        """每轮 TTS 结束后补发一次 listen start（mode=auto）。

        为什么必须补：小智服务端在**每轮问答**结束后就停止接收上行，必须重新
        listen start 才会开始收下一句。实测（/tmp/rearm.py）：不补，第二句起全丢。
        补发前留一点时间让服务端收尾上一轮（消掉 tts stop 后的残余状态）。
        """
        try:
            await asyncio.sleep(0.25)
            if not self._want_listen.is_set() or self._ws is None:
                return
            await self._send_json({"type": "listen", "state": "start",
                                   "mode": LISTEN_MODE, "session_id": self.session_id})
            log.info("TTS 结束，已补发 listen start（准备听下一句）")
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            log.warning("补发 listen start 失败：%s", e)

    async def _receiver(self) -> None:
        async for msg in self._ws:
            if isinstance(msg, bytes):
                # 下行 Opus 音频
                if self.state == SPEAKING and self.audio:
                    self.audio.feed_opus(msg)
                continue
            try:
                m = json.loads(msg)
            except json.JSONDecodeError:
                continue
            t = m.get("type")
            if t == "stt":
                text = m.get("text", "")
                log.info("她听到：%s", text)
                self.on_event({"event": "stt", "text": text})
                self.set_state(THINKING)
            elif t == "llm":
                emotion = m.get("emotion", "")
                text = m.get("text", "")
                log.info("LLM[%s]: %s", emotion, text)
                self.current_emotion = emotion
                self.on_event({"event": "emotion", "emotion": emotion, "text": text})
            elif t == "tts":
                st = m.get("state")
                if st == "start":
                    self.set_state(SPEAKING)
                    if self.audio:
                        self.audio.start_playback()
                elif st == "sentence_start":
                    text = m.get("text", "")
                    if text:
                        self.last_tts_text = text
                        self.on_event({"event": "tts_text", "text": text})
                elif st == "stop":
                    if self.audio:
                        # 等队列播完再停，别截断她的话（原来 sleep 0.3 就硬停）
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(None, self.audio.wait_playback_done, 3.0)
                        self.audio.stop_playback(immediate=False)
                        # 真全双工：不再有半双工回声门要释放（回声由 AEC 消除），
                        # 麦克风全程不静音。release_gate_in 保留为兼容 no-op。
                    self.last_tts_text = ""
                    if self._want_listen.is_set():
                        self.set_state(LISTENING)
                        # ★关键修复：服务端每轮 TTS 结束后需要**重新 listen start**
                        # 才会开始收下一句。实测（2026-10-09）：不给它 re-arm，第二句及以后
                        # 全部被丢弃（只回第一句）——这正是"连续对话第二句丢"的根因。
                        #
                        # 例外：realtime 模式下服务端**原生**支持一轮内多段自动断句
                        # （实测 /tmp/onecycle.py：一次 listen 内 3 段 → 3 句 STT），
                        # 不需要也不能重复 listen start（py-xiaozhi 同款：非 realtime 才补发）。
                        if LISTEN_MODE != "realtime":
                            if self._rearm_task is not None:
                                self._rearm_task.cancel()
                            self._rearm_task = asyncio.ensure_future(self._rearm_listen())
                    else:
                        self.set_state(IDLE)
            elif t == "mcp":
                # 设备端 MCP：秘书工具。工具可能在跑 shell(最慢 20s)，
                # 丢线程池，别堵住音频收发（_receiver 是唯一的读循环）。
                loop = asyncio.get_running_loop()
                reply = await loop.run_in_executor(None, self.mcp.handle, m)
                if reply is not None:
                    await self._send_json(reply)
            # system 等其余类型暂不处理

    async def _sender(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            await self._want_listen.wait()
            if self._stop.is_set():
                break
            # 进入对话：发 listen start（auto 全双工），麦克风持续推流
            # 服务端 VAD 自动断句：listening → thinking → speaking → listening...
            await self._send_json({"type": "listen", "state": "start",
                                   "mode": LISTEN_MODE, "session_id": self.session_id})
            self.audio.begin_uplink(preroll_ms=self._preroll_ms)
            self._preroll_ms = 0   # 预卷只服务"刚喊完唤醒词"那一次；断线自动恢复时别回放旧音频
            # 断线是服务端常态（~60s 静音超时结束会话）。只要用户没明确退出，就在重连后
            # 自动重新 listen start，让她继续听——用户不必为每句话重新喊"景甜"。
            if self._had_session:
                log.info("断线后自动恢复对话（重新 listen start）")
            else:
                log.info("进入对话（listen start, mode=%s）", LISTEN_MODE)
                self._had_session = True
            self.set_state(LISTENING)
            try:
                while self._want_listen.is_set() and not self._stop.is_set():
                    pkt = await loop.run_in_executor(None, self.audio.read_encoded, 0.2)
                    if pkt:
                        try:
                            await self._send_binary(pkt)
                        except Exception:
                            break
            finally:
                self.audio.end_uplink()
                # 退出对话
                await self._send_json({"type": "listen", "state": "stop",
                                       "mode": LISTEN_MODE, "session_id": self.session_id})
                self.set_state(IDLE)
