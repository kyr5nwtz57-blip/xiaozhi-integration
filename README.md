# xiaozhi-integration

把**官方小智（xiaozhi.me）云端语音**接进你自己的设备/程序——一套实测跑通的参考实现与踩坑实录。

> 本项目是一次真实项目的副产品：在一台 Mac 上把「桌面语音助手」接通官方小智，
> 全流程实测、逐坑记录。**目的：为小爱音箱等设备接入小智铺路**，少走弯路。

## 这个仓库解决什么

官方小智的接入文档很薄，真正的坑全在实测里。这里沉淀了：

1. **官方 WebSocket 协议全流程**——握手、全双工、状态机、MCP 扩展
2. **音频链路**——Opus 60ms 分帧、16k 上行 / 24k 下行、编解码细节
3. **回声消除**——WebRTC APM 接入，含一个会导致 AEC 完全失效的致命 bug
4. **OTA 与设备绑定**——拿 token、六位码激活
5. **本地替代方案**——脱离云端，纯本机 ASR + TTS

## 文档

| 文档 | 内容 |
| :--- | :--- |
| [docs/官方协议实录.md](docs/官方协议实录.md) | WebSocket 握手、报文语义、状态机、MCP |
| [docs/踩坑实录.md](docs/踩坑实录.md) | **最值钱**：所有实测踩过的坑与根因 |
| [docs/音频链路.md](docs/音频链路.md) | Opus 分帧、采样率、PyAV 限制 |
| [docs/回声消除.md](docs/回声消除.md) | WebRTC APM AEC 接入与致命 bug |
| [docs/OTA与绑定.md](docs/OTA与绑定.md) | 激活流程与设备身份 |
| [docs/小爱接入路线.md](docs/小爱接入路线.md) | 给小爱音箱的具体接入建议 |

## 代码

`src/` 是可直接改造的参考实现：

| 文件 | 职责 |
| :--- | :--- |
| `xiaozhi.py` | 官方 WebSocket 客户端（握手 + 全双工 + MCP） |
| `opus_av.py` | Opus 编解码封装（PyAV） |
| `ota.py` | OTA 检查与激活 |
| `aec.py` | WebRTC APM 回声消除封装 |
| `config.py` | 配置读写（**已脱敏**） |
| `local_voice/` | 本地 ASR(faster-whisper) + TTS(edge-tts) |

## 快速开始

```bash
pip install websockets av numpy sounddevice
# 1. 填 config.json：device_id / client_id（可用任意 UUID 与 MAC）
# 2. 跑 OTA 拿 token：python src/ota.py
# 3. 用 src/xiaozhi.py 建立连接
```

## 免责

- 本项目为**互操作研究**，非官方 SDK；协议可能随官方更新而变。
- 请遵守小智服务条款，勿用于滥用。
- 仓库内**不含任何真实 token / 密钥**，全部为占位符。

## 来源

代码与结论源自本地项目 `girlfriend-2d` 的 sidecar 模块，经脱敏整理。
