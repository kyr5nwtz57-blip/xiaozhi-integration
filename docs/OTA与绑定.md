# OTA 与设备绑定

## 流程

1. 客户端带上 `Device-Id` / `Client-Id` 向 OTA 接口请求
2. 若设备**未绑定** → 返回 `activation` 六位码 + 提示信息
3. 用户在 xiaozhi.me 控制台输入六位码完成绑定
4. 再次请求 → 返回 `websocket.token`
5. 用这个 token 连 WebSocket

## 设备身份

| 字段 | 说明 | 建议值 |
| :--- | :--- | :--- |
| `Device-Id` | 设备唯一标识 | MAC 地址格式 `aa:bb:cc:dd:ee:ff` |
| `Client-Id` | 客户端标识 | 任意 UUID |

这两个值决定了"这是哪台设备"。同一对值 = 同一台设备。

## 返回结构（示例，已脱敏）

```json
{
  "server_time": {"timestamp": 1791301460081, "timezone_offset": -300},
  "firmware": {"version": "1.0.0", "url": ""},
  "mqtt": {
    "endpoint": "api.tenclass.net",
    "client_id": "GID_test@@@<DEVICE>@@@<CLIENT>",
    "username": "<MQTT_USERNAME>",
    "password": "<MQTT_PASSWORD>",
    "publish_topic": "device-server",
    "subscribe_topic": "null"
  },
  "websocket": {
    "url": "wss://api.tenclass.net/xiaozhi/v1/",
    "token": "<YOUR_TOKEN>"
  }
}
```

## ⚠️ 安全须知

- **`websocket.token` 和 MQTT `password` 是账号凭证，绝不可提交到公开仓库。**
- 本项目所有 sample 均为占位符（`<YOUR_TOKEN>` 等）。
- 若 token 泄露，请在小智控制台重新绑定以刷新。
- 本地保存 token 的配置文件建议 `chmod 600`。

## 代码

见 `src/ota.py`。核心是带正确请求头调用 OTA 端点，解析 `websocket.token` 与 `activation`。
