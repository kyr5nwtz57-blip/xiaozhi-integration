"""配置读写（已脱敏：请填入你自己的 device_id / client_id）。

⚠️ websocket_token 由 OTA 自动获取并写入，请勿手动提交真实 token 到版本库。
"""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

DEFAULT = {
    # 设备唯一标识：MAC 地址格式即可（同一对值 = 同一台设备）
    "device_id": "aa:bb:cc:dd:ee:ff",
    # 客户端标识：任意 UUID
    "client_id": "00000000-0000-4000-8000-000000000000",
    # 官方 WebSocket 端点
    "websocket_url": "wss://api.tenclass.net/xiaozhi/v1/",
    # 由 OTA 获取后自动填充。绝不要提交真实值到公开仓库！
    "websocket_token": "",
}


def load() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            merged = dict(DEFAULT)
            merged.update(cfg)
            return merged
        except (OSError, json.JSONDecodeError):
            pass
    return dict(DEFAULT)


def save(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    # 含 token，收紧权限
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass
