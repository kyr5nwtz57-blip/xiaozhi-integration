"""小智官方 OTA / 激活流程。

已实测验证可用的请求格式（2026-10-06，真机请求官方服务器验证通过）：
  POST https://api.tenclass.net/xiaozhi/ota/
  Headers:
    Device-Id: <config.json 里的 device_id>
    Client-Id: <config.json 里的 client_id>
    Content-Type: application/json
    User-Agent: desktop-girlfriend/girlfriend-sidecar-1.0.0
    Accept-Language: zh-CN
    （注意：默认 v1 模式不要带 Activation-Version 头，带了会 400）
  Body:
    {"application": {"version": "1.0.0", "elf_sha256": "unknown"},
     "board": {"type": "desktop-girlfriend", "name": "girlfriend-sidecar",
               "ip": "<本机IP>", "mac": "<device_id>"}}
  响应 200，JSON 含：
    activation.code / activation.message / activation.challenge（未绑定时）
    websocket.url / websocket.token
    server_time

绑定：用户去 https://xiaozhi.me 控制台输入 6 位码后，
  POST https://api.tenclass.net/xiaozhi/ota/activate 轮询：
    V1 模式 body 为空对象 {}，200=已绑定，202=还没绑（继续等）。
"""
from __future__ import annotations

import json
import socket
import time
import urllib.request
import urllib.error

OTA_URL = "https://api.tenclass.net/xiaozhi/ota/"
ACTIVATE_URL = OTA_URL + "activate"
USER_AGENT = "desktop-girlfriend/girlfriend-sidecar-1.0.0"


def local_ip() -> str:
    """本机对外 IP（只建 UDP socket，不实际发包）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def build_ota_request(cfg: dict) -> tuple[dict, dict]:
    """构造已验证的 OTA 请求（headers, body），便于测试断言。"""
    headers = {
        "Device-Id": cfg["device_id"],
        "Client-Id": cfg["client_id"],
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "Accept-Language": "zh-CN",
    }
    body = {
        "application": {"version": "1.0.0", "elf_sha256": "unknown"},
        "board": {
            "type": "desktop-girlfriend",
            "name": "girlfriend-sidecar",
            "ip": local_ip(),
            "mac": cfg["device_id"],
        },
    }
    return headers, body


def _post(url: str, headers: dict, body: dict, timeout: int = 15) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:
            payload = {}
        return e.code, payload


def ota_check(cfg: dict, timeout: int = 15) -> dict:
    """跑一次 OTA，返回解析后的响应 JSON（抛 RuntimeError 说明失败原因）。"""
    headers, body = build_ota_request(cfg)
    status, payload = _post(OTA_URL, headers, body, timeout)
    if status != 200:
        raise RuntimeError(f"OTA 请求失败，HTTP {status}: {payload}")
    return payload


def poll_activate(cfg: dict, timeout_s: int = 600, interval_s: int = 3,
                  verbose: bool = True) -> bool:
    """轮询 /activate，直到用户在 xiaozhi.me 完成绑定（200）或超时。

    返回 True=已绑定，False=超时。202 表示"还没绑"，继续等。
    """
    headers = {
        "Device-Id": cfg["device_id"],
        "Client-Id": cfg["client_id"],
        "Content-Type": "application/json",
    }
    deadline = time.time() + timeout_s
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        # 先按第三方固件文档带 Activation-Version: 1；若 400 则去掉重试
        status, _ = _post(ACTIVATE_URL, {**headers, "Activation-Version": "1"}, {})
        if status == 400:
            status, _ = _post(ACTIVATE_URL, headers, {})
        if status == 200:
            return True
        if verbose and attempt % 10 == 1:
            print(f"  …等待绑定中（{attempt} 次轮询），请去 https://xiaozhi.me 输入 6 位码")
        if status not in (200, 202):
            print(f"  activate 返回异常 HTTP {status}，继续重试…")
        time.sleep(interval_s)
    return False
