# -*- coding: utf-8 -*-
"""Feishu QR Code Device Flow — Device Authorization Grant (RFC 8628)"""
from __future__ import annotations
import base64, io, logging
from dataclasses import dataclass
from typing import Any, Dict, Optional
import httpx

logger = logging.getLogger(__name__)

_FEISHU_ACCOUNTS_DOMAIN = "https://accounts.feishu.cn"
_LARK_ACCOUNTS_DOMAIN = "https://accounts.larksuite.com"
_FEISHU_REGISTER_ENDPOINT = "/oauth/v1/app/registration"
_SOURCE = "goofish-v2"
_ARCHETYPE = "PersonalAgent"


@dataclass
class QRCodeResult:
    scan_url: str
    poll_token: str
    expires_in: int = 300


@dataclass
class PollResult:
    status: str
    credentials: Dict[str, Any]
    message: str = ""


class FeishuDeviceFlow:
    def __init__(self, domain: str = "feishu") -> None:
        self.domain = domain if domain in ("feishu", "lark") else "feishu"

    def _get_accounts_domain(self) -> str:
        return (_LARK_ACCOUNTS_DOMAIN if self.domain == "lark"
                else _FEISHU_ACCOUNTS_DOMAIN)

    @property
    def endpoint(self) -> str:
        return self._get_accounts_domain() + _FEISHU_REGISTER_ENDPOINT

    async def fetch_qrcode(self) -> QRCodeResult:
        async with httpx.AsyncClient(timeout=15) as client:
            init_resp = await client.post(self.endpoint, data={"action": "init"})
            init_resp.raise_for_status()
            methods = init_resp.json().get("supported_auth_methods", [])
            if "client_secret" not in methods:
                raise RuntimeError(f"飞书不支持 client_secret 认证: {methods}")

            begin_resp = await client.post(self.endpoint, data={
                "action": "begin",
                "archetype": _ARCHETYPE,
                "auth_method": "client_secret",
                "request_user_info": "open_id",
            })
            begin_resp.raise_for_status()
            begin_data = begin_resp.json()
            device_code = begin_data.get("device_code", "")
            verification_uri = begin_data.get("verification_uri_complete", "")
            expires_in = int(begin_data.get("expires_in", 300) or 300)
            if not device_code or not verification_uri:
                raise RuntimeError(f"飞书返回缺少 device_code 或二维码 URL: {begin_data}")

            scan_url = (f"{verification_uri}&source={_SOURCE}"
                        if "?" in verification_uri
                        else f"{verification_uri}?source={_SOURCE}")
            return QRCodeResult(scan_url=scan_url,
                                poll_token=device_code,
                                expires_in=expires_in)

    async def poll_status(self, token: str) -> PollResult:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                self.endpoint, data={"action": "poll", "device_code": token})
            data = resp.json()

        if data.get("client_id") and data.get("client_secret"):
            user_info = data.get("user_info", {})
            return PollResult(status="success", credentials={
                "app_id": data["client_id"],
                "app_secret": data["client_secret"],
                "open_id": user_info.get("open_id", ""),
                "tenant_brand": user_info.get("tenant_brand", self.domain),
            })

        error = data.get("error", "")
        if error in ("expired_token", "invalid_grant"):
            return PollResult(status="expired", credentials={},
                              message="二维码已过期，请重新获取")
        elif error == "access_denied":
            return PollResult(status="fail", credentials={},
                              message="用户拒绝了授权")
        elif error and error not in ("authorization_pending", "slow_down"):
            return PollResult(status="fail", credentials={}, message=error)

        return PollResult(status="waiting", credentials={})


def generate_qrcode_image(scan_url: str, scale: int = 6) -> str:
    import segno
    qr_code = segno.make(scan_url, error="M")
    buf = io.BytesIO()
    qr_code.save(buf, kind="png", scale=scale, border=2)
    return base64.b64encode(buf.getvalue()).decode()


async def register_by_scan(domain="feishu", on_qrcode=None,
                           poll_interval=2.0, timeout=300.0):
    import asyncio, time
    flow = FeishuDeviceFlow(domain)
    result = await flow.fetch_qrcode()
    png = generate_qrcode_image(result.scan_url)
    if on_qrcode:
        on_qrcode(result.scan_url, png, result.expires_in)
    start = time.time()
    while time.time() - start < min(timeout, result.expires_in):
        poll = await flow.poll_status(result.poll_token)
        if poll.status == "success":
            return poll.credentials
        if poll.status in ("expired", "fail"):
            raise RuntimeError(poll.message or poll.status)
        await asyncio.sleep(poll_interval)
    raise TimeoutError("扫码授权超时")
