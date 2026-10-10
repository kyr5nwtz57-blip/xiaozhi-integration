# 本地语音替代方案

不想用云端？这两个模块提供**纯本机**的语音能力，零成本、零网络。

| 文件 | 能力 | 实测 |
| :--- | :--- | :--- |
| `local_asr.py` | 语音识别（faster-whisper int8） | 4.14s 音频 → **152ms 出文** |
| `local_tts.py` | 语音合成（edge-tts） | 输出 24kHz PCM，可直接喂播放队列 |

## 依赖
```bash
pip install faster-whisper edge-tts
```

## 说明

- `local_asr.py`：faster-whisper tiny/base 模型 int8 量化，Apple Silicon CPU 上实时率约 27x。
  首次运行会下载模型（~75MB for tiny）。
- `local_tts.py`：edge-tts 是微软的免费神经网络语音（需联网，但不需要 API key）。
  输出转成 24kHz int16 PCM 后可直接进播放链路。

## 取舍

| 维度 | 云端小智 | 本地方案 |
| :--- | :--- | :--- |
| 音质 | 好，且情绪自然 | edge-tts 也不错；whisper 识别中文良好 |
| 延迟 | 取决于网络 | ASR 152ms，TTS 首包 ~300ms |
| 隐私 | 音频上传云端 | **完全本地** |
| 成本 | 依赖账号 | **零成本** |
| 大模型能力 | 云端 LLM | **需要你自己接一个 LLM** |
