# -*- coding: utf-8 -*-
"""Feishu Bot Core — lark-oapi WebSocket long connection for message handling."""
from __future__ import annotations
import asyncio, base64, collections, json, logging, os, sys, types, uuid, time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional
import httpx

logger = logging.getLogger(__name__)

# 凭据落盘目录：必须与 main.py/crypto.py 的 DATA_DIR 保持一致。
# 原先硬编码 "/app/data/credentials.json"（Docker 路径），桌面版会写到
# <当前盘符>:\app\data —— 与 main.py 的 CRED_FILE 分叉，导致「后台已配置飞书
# 凭据但机器人读不到」。统一走 DATA_DIR，兜底落在项目内。
_DATA_DIR = Path(os.environ.get("DATA_DIR")
                 or Path(__file__).resolve().parents[2] / "data" / "feishu-agent")
_CRED_FILE = _DATA_DIR / "credentials.json"

MSG_TEXT = "text"
MSG_POST = "post"
MSG_IMAGE = "image"
MSG_FILE = "file"
MSG_INTERACTIVE = "interactive"


class FeishuBot:
    """飞书机器人：WebSocket 长连接收发消息。"""

    def __init__(self, app_id: str, app_secret: str,
                 domain: str = "feishu",
                 message_handler=None) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.domain = domain
        self.message_handler = message_handler
        self._client = None
        self._ws_client = None
        self._running = False
        self._token = None
        self._token_expire = 0.0
        self.last_messages = []
        self.last_message_at = 0.0  # epoch of last received event (WS liveness proxy)
        self._processed_ids = set()
        self._processed_ids_order = collections.deque(maxlen=2000)
        # 后台任务强引用集——event loop 对 task 只持弱引用，裸 create_task
        # 可能被 GC 中途静默回收（2026-08-03 监控轮次蒸发同根因）
        self._bg_tasks = set()

    def _processed_ids_add(self, message_id: str) -> None:
        """有序定长去重：deque 维持插入顺序，set 提供 O(1) 查询。

        取代旧的 set(list(...)[-1000:]) 随机截断（会误删近期 ID 导致消息重放）。
        """
        if len(self._processed_ids_order) >= 2000:
            oldest = self._processed_ids_order.popleft()
            self._processed_ids.discard(oldest)
        self._processed_ids_order.append(message_id)
        self._processed_ids.add(message_id)

    async def _get_tenant_token(self) -> str:
        if self._token and time.time() < self._token_expire - 60:
            return self._token

        host = ("https://open.larksuite.com" if self.domain == "lark"
                else "https://open.feishu.cn")
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{host}/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret})
            resp.raise_for_status()
            data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取 tenant_access_token 失败: {data.get('msg')}")
        self._token = data["tenant_access_token"]
        self._token_expire = time.time() + int(data.get("expire", 7200))
        return self._token

    async def send_text(self, receive_id, text,
                        receive_id_type="open_id") -> bool:
        return await self._send(receive_id, MSG_TEXT,
                                {"text": text}, receive_id_type)

    async def send_post(self, receive_id, content,
                        receive_id_type="open_id") -> bool:
        return await self._send(receive_id, MSG_POST,
                                content, receive_id_type)

    async def send_card(self, receive_id, card,
                        receive_id_type="open_id") -> bool:
        return await self._send(receive_id, MSG_INTERACTIVE,
                                card, receive_id_type)

    async def _send(self, receive_id, msg_type, content,
                    receive_id_type) -> bool:
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest, CreateMessageRequestBody)
        body = (CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type(msg_type)
                .content(json.dumps(content, ensure_ascii=False))
                .build())
        req = (CreateMessageRequest.builder()
               .receive_id_type(receive_id_type)
               .request_body(body).build())
        resp = self._client.im.v1.message.create(req)
        if not resp.success():
            logger.error("飞书发送失败: code=%s msg=%s", resp.code, resp.msg)
            return False
        return True

    async def upload_image(self, image_bytes: bytes) -> str:
        """上传图片到飞书素材库，返回 image_key（供 image 消息使用）。"""
        token = await self._get_tenant_token()
        host = ("https://open.larksuite.com" if self.domain == "lark"
                else "https://open.feishu.cn")
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{host}/open-apis/im/v1/images",
                headers={"Authorization": f"Bearer {token}"},
                data={"image_type": "message"},
                files={"image": ("image.png", image_bytes, "image/png")})
            resp.raise_for_status()
            data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"上传图片失败: {data.get('msg')}")
        return data["data"]["image_key"]

    async def send_image(self, receive_id, image_key,
                         receive_id_type="open_id") -> bool:
        """发送图片消息（image_key 由 upload_image 获得）。"""
        return await self._send(receive_id, MSG_IMAGE,
                                {"image_key": image_key}, receive_id_type)

    def _on_message(self, event) -> None:
        try:
            task = asyncio.create_task(self._handle_event(event))
            # 强引用持有，防 GC 静默回收导致消息事件处理中途蒸发
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        except RuntimeError:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(self._handle_event(event))
            loop.close()

    async def _handle_event(self, event) -> None:
        if not event or not event.event or not event.event.message:
            return
        msg = event.event.message
        message_id = msg.message_id or ""
        if message_id and message_id in self._processed_ids:
            return
        if message_id:
            self._processed_ids_add(message_id)

        chat_type = event.event.message.chat_type or ""
        sender_id = (event.event.sender.sender_id or {}) if event.event.sender else {}
        open_id = sender_id.open_id or ""
        content = msg.content or "{}"
        try:
            content_data = json.loads(content)
        except Exception:
            content_data = {}

        text = ""
        if msg.message_type == MSG_TEXT:
            text = content_data.get("text", "")
        elif msg.message_type == MSG_POST:
            text = _extract_post_text(content_data)

        payload = {
            "message_id": message_id,
            "message_type": msg.message_type or "",
            "chat_type": chat_type,
            "chat_id": event.event.message.chat_id or "",
            "open_id": open_id,
            "text": text,
            "raw_content": content_data,
            "create_time": msg.create_time or "",
        }
        self.last_message_at = time.time()
        self.last_messages.append(payload)
        if len(self.last_messages) > 50:
            self.last_messages = self.last_messages[-50:]

        if self.message_handler:
            try:
                reply = await self.message_handler(payload)
                if reply:
                    target = (event.event.message.chat_id or open_id
                              if chat_type == "group" else open_id)
                    target_type = "chat_id" if chat_type == "group" else "open_id"
                    await self.send_text(target, reply, target_type)
            except Exception as exc:
                logger.exception("消息处理失败: %s", exc)

    def start(self) -> None:
        if self._running:
            return
        import lark_oapi as lark
        self._client = (lark.Client.builder()
                        .app_id(self.app_id)
                        .app_secret(self.app_secret)
                        .log_level(lark.LogLevel.INFO)
                        .build())
        self._running = True
        logger.info("飞书机器人已初始化（domain=%s）", self.domain)

    def run_forever(self) -> None:
        import asyncio
        import lark_oapi as lark
        self.start()

        from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
        dispatcher = (EventDispatcherHandler.builder("", "")
                      .register_p2_im_message_receive_v1(self._on_message)
                      .build())

        domain_enum = (getattr(lark, "LARK_DOMAIN", None)
                       if self.domain == "lark"
                       else getattr(lark, "FEISHU_DOMAIN", None))
        ws_kwargs = {
            "app_id": self.app_id,
            "app_secret": self.app_secret,
            "event_handler": dispatcher,
            "log_level": lark.LogLevel.INFO,
        }
        if domain_enum is not None:
            ws_kwargs["domain"] = domain_enum
        self._ws_client = lark.ws.Client(**ws_kwargs)

        # Use SDK's built-in start() which handles reconnection automatically
        logger.info("飞书 WebSocket 长连接启动（SDK 内置重连）")
        try:
            self._ws_client.start()
        except KeyboardInterrupt:
            logger.info("收到退出信号")
        except Exception as exc:
            logger.exception("飞书 WebSocket 运行异常: %s", exc)
        finally:
            self._running = False


def _extract_post_text(content: Dict[str, Any]) -> str:
    parts = []
    for title in content.get("title", ""):
        parts.append(title)
    for line in content.get("content", []):
        for node in line:
            if isinstance(node, dict):
                parts.append(node.get("text", ""))
    return "".join(parts)


def save_credentials(app_id: str, app_secret: str,
                     domain: str = "feishu",
                     path: Optional[Path] = None) -> Path:
    import json, datetime
    if path is None:
        path = _CRED_FILE
    data = {"app_id": app_id, "app_secret": app_secret, "domain": domain,
            "configured_at": datetime.datetime.now().isoformat()}
    try:
        from crypto import write_json_file
        write_json_file(path, data)
    except Exception:
        # Fallback: plain JSON (crypto unavailable)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return path


def load_credentials(path: Optional[Path] = None) -> Optional[Dict[str, str]]:
    import json
    if path is None:
        path = _CRED_FILE
    if not path.exists():
        return None
    try:
        from crypto import read_json_file
        data = read_json_file(path)
        if data is not None:
            return data
    except Exception:
        pass
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
