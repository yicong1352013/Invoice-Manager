"""SMTP and WeCom delivery with explicit acceptance/ambiguity boundaries."""

import asyncio
import base64
import hashlib
import json
import re
import smtplib
import ssl
import time
from contextlib import asynccontextmanager, suppress
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import make_msgid
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text
import websockets

from app.config import get_settings
from app.schemas.notification import BotConfig, EmailConfig, validate_target


DEFAULT_MESSAGES = {
    "success_subject": "【发票处理完成】{{run_name}}｜{{invoice_count}} 张",
    "success_body": "本批次发票已完成识别、审核和结果整理。\n\n批次编号：{{run_id}}\n发票数量：{{invoice_count}} 张\n价税合计：{{total_with_tax}} 元\n导出文件：{{artifact_names}}\n完成时间：{{completed_at}}\n\n处理记录：{{run_url}}",
    "review_body": "发票批次 {{run_name}}（{{run_id}}）等待人工处理，共 {{review_count}} 项。\n处理记录：{{run_url}}",
    "failure_body": "发票批次 {{run_name}}（{{run_id}}）执行失败。\n失败数量：{{failed_count}}\n原因：{{error}}\n处理记录：{{run_url}}",
}
MESSAGE_VARIABLES = ("run_name", "run_id", "invoice_count", "total_with_tax", "artifact_names", "completed_at", "run_url", "review_count", "failed_count", "error")
_VARIABLE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")


class DeliveryRejected(Exception):
    """Definitively rejected; configuration or input must change."""


class DeliveryRetryable(Exception):
    """No notification was accepted; a later retry is safe."""


class DeliveryUnknown(Exception):
    """Notification may have been accepted; never retry automatically."""


def render_message(template: str, context: dict) -> str:
    if not isinstance(template, str) or len(template) > 10000:
        raise ValueError("消息模板超过 10000 字符")
    names = _VARIABLE.findall(template)
    if any(name not in MESSAGE_VARIABLES for name in names):
        raise ValueError("消息模板包含不支持的变量")
    remaining = _VARIABLE.sub("", template)
    if "{{" in remaining or "}}" in remaining or "{%" in remaining:
        raise ValueError("消息模板仅支持 {{变量名}}")
    missing = [name for name in names if name not in context]
    if missing:
        raise ValueError("消息模板缺少变量：" + ", ".join(sorted(set(missing))))
    return _VARIABLE.sub(lambda match: str(context[match.group(1)]), template)


def _fernet() -> Fernet:
    key = get_settings().notification_encryption_key
    if not key:
        raise DeliveryRejected("尚未配置 NOTIFICATION_ENCRYPTION_KEY，无法保存或使用渠道凭据")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeError):
        raise DeliveryRejected("NOTIFICATION_ENCRYPTION_KEY 格式无效") from None


def encrypt_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode("utf-8")).decode("ascii")


def decrypt_secret(encrypted: str | None) -> str:
    if not encrypted:
        return ""
    try:
        return _fernet().decrypt(encrypted.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError):
        raise DeliveryRejected("渠道凭据无法解密，请检查加密密钥或重新保存凭据") from None


def channel_view(channel, admin: bool) -> dict:
    return {"id": channel.id, "name": channel.name, "kind": channel.kind,
            "enabled": channel.enabled, "config": channel.config if admin else {},
            "has_secret": bool(channel.encrypted_secret), "updated_at": channel.updated_at}


def _validated_config(channel) -> dict:
    try:
        schema = EmailConfig if channel.kind == "email" else BotConfig if channel.kind == "wecom_aibot" else None
        if schema is None:
            raise ValueError()
        return schema.model_validate(channel.config).model_dump()
    except ValueError:
        raise DeliveryRejected("渠道配置无效，请重新保存") from None


def _smtp_connect(config: dict, secret: str):
    smtp = None
    try:
        if config["tls"] == "ssl":
            smtp = smtplib.SMTP_SSL(config["host"], config["port"], timeout=20, context=ssl.create_default_context())
        else:
            smtp = smtplib.SMTP(config["host"], config["port"], timeout=20)
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
        smtp.ehlo()
        if config["username"]:
            smtp.login(config["username"], secret)
        return smtp
    except Exception as exc:
        if smtp:
            with suppress(Exception):
                smtp.close()
        if isinstance(exc, (smtplib.SMTPAuthenticationError, smtplib.SMTPNotSupportedError, ssl.SSLCertVerificationError)):
            raise DeliveryRejected("邮件认证或 TLS 校验失败，请检查渠道配置") from None
        raise DeliveryRetryable("邮件连接失败，尚未发送") from None


def _smtp_deliver(config, secret, target, subject, body, files, to_targets=None, cc_targets=None):
    msg = EmailMessage(policy=SMTP)
    try:
        msg["From"] = config["from_address"]
        msg["To"] = ", ".join(to_targets if to_targets is not None else [target])
        if cc_targets:
            msg["Cc"] = ", ".join(cc_targets)
        msg["Subject"] = subject
        msg["Message-ID"] = make_msgid()
        msg.set_content(body)
        for item in files:
            maintype, subtype = item["content_type"].split("/", 1)
            msg.add_attachment(item["data"], maintype=maintype, subtype=subtype, filename=item["name"])
        raw = msg.as_bytes()
    except (ValueError, TypeError, KeyError):
        raise DeliveryRejected("邮件主题、正文或文件格式无效") from None
    if len(raw) > config["max_file_size_mb"] * 1024 * 1024:
        raise DeliveryRejected("邮件编码后的总大小超过渠道限制，请减少导出文件或调整限制")
    smtp = _smtp_connect(config, secret)
    data_started = False
    try:
        code, _ = smtp.mail(config["from_address"])
        if code != 250:
            _smtp_rejection(code)
        code, _ = smtp.rcpt(target)
        if code not in (250, 251):
            _smtp_rejection(code)
        data_started = True
        code, _ = smtp.data(raw)
        if code != 250:
            _smtp_rejection(code)
        return {"provider": "email", "accepted": True, "message_id": str(msg["Message-ID"]), "target": target}
    except (DeliveryRejected, DeliveryRetryable):
        raise
    except smtplib.SMTPDataError as exc:
        _smtp_rejection(exc.smtp_code)
    except Exception:
        if data_started:
            raise DeliveryUnknown("邮件提交后未收到明确回执，请核对收件箱后再决定是否重发") from None
        raise DeliveryRetryable("邮件提交前连接中断，尚未发送") from None
    finally:
        # A QUIT failure must not turn an accepted message into a retry.
        with suppress(Exception):
            smtp.close()


def _smtp_rejection(code):
    if 400 <= code < 500:
        raise DeliveryRetryable(f"邮件服务器暂时拒绝接收（{code}）")
    raise DeliveryRejected(f"邮件服务器拒绝接收（{code}）")


@asynccontextmanager
async def _bot_lock(bot_id):
    from app.database import engine
    if engine.dialect.name != "postgresql":
        raise DeliveryRejected("机器人连接互斥要求 PostgreSQL")
    lock_id = int.from_bytes(hashlib.sha256(("invoice-bot:" + bot_id).encode()).digest()[:8], "big", signed=True)
    async with engine.connect() as conn:
        acquired = await conn.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_id})
        if not acquired:
            raise DeliveryRetryable("该机器人正在连接或发送，请稍后重试")
        try:
            yield
        finally:
            try:
                await conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_id})
            except Exception:
                # Never return a connection with a session lock to the pool.
                with suppress(Exception):
                    await conn.invalidate()


class _BotSession:
    """Protocol follows WecomTeam/aibot-node-sdk src/client.ts and src/ws.ts."""
    def __init__(self, ws):
        self.ws = ws
        self.last_heartbeat = time.monotonic()
        self.events = []

    async def request(self, command, body=None, notification=False):
        req_id = f"{command}_{uuid4().hex}"
        frame = {"cmd": command, "headers": {"req_id": req_id}}
        if body is not None:
            frame["body"] = body
        try:
            await self.ws.send(json.dumps(frame))
            async with asyncio.timeout(10):
                while True:
                    ack = json.loads(await self.ws.recv())
                    if ack.get("headers", {}).get("req_id") == req_id:
                        if ack.get("errcode") != 0:
                            if "errcode" not in ack:
                                raise ValueError("missing acknowledgment status")
                            raise DeliveryRejected(f"企业微信拒绝请求（{ack['errcode']}）")
                        return {"req_id": req_id, "body": ack.get("body", {}), "errcode": 0}
                    self._event(ack)
        except DeliveryRejected:
            raise
        except Exception:
            if notification:
                raise DeliveryUnknown("机器人消息提交后未收到明确回执，请核对会话后再决定是否重发") from None
            raise DeliveryRetryable("机器人连接或素材上传中断，尚未投递消息") from None

    def _event(self, frame):
        if frame.get("cmd") not in ("aibot_msg_callback", "aibot_event_callback"):
            return
        body = frame.get("body", {})
        if body.get("event", {}).get("eventtype") == "disconnected_event":
            raise ConnectionError("another bot connection replaced this one")
        target = body.get("chatid") if body.get("chattype") == "group" else body.get("from", {}).get("userid")
        if target:
            try:
                validate_target("wecom_aibot", target)
            except ValueError:
                return
            event = {"target": target, "kind": "group" if body.get("chattype") == "group" else "user"}
            if event not in self.events:
                self.events.append(event)

    async def heartbeat(self):
        if time.monotonic() - self.last_heartbeat >= 20:
            await self.request("ping")
            self.last_heartbeat = time.monotonic()

    async def upload(self, item):
        data = item["data"]
        chunk_size = 512 * 1024
        result = await self.request("aibot_upload_media_init", {
            "type": "file", "filename": item["name"], "total_size": len(data),
            "total_chunks": (len(data) + chunk_size - 1) // chunk_size,
            "md5": hashlib.md5(data).hexdigest(),
        })
        upload_id = result["body"].get("upload_id")
        if not upload_id:
            raise DeliveryRetryable("机器人素材初始化未返回 upload_id")
        for index, start in enumerate(range(0, len(data), chunk_size)):
            await self.heartbeat()
            await self.request("aibot_upload_media_chunk", {
                "upload_id": upload_id, "chunk_index": index,
                "base64_data": base64.b64encode(data[start:start + chunk_size]).decode("ascii"),
            })
        result = await self.request("aibot_upload_media_finish", {"upload_id": upload_id})
        media_id = result["body"].get("media_id")
        if not media_id:
            raise DeliveryRetryable("机器人素材完成后未返回 media_id")
        return media_id


@asynccontextmanager
async def _bot_session(config, secret):
    if not secret:
        raise DeliveryRejected("机器人 Secret 未配置")
    async with _bot_lock(config["bot_id"]):
        try:
            ws = await websockets.connect("wss://openws.work.weixin.qq.com", open_timeout=15, close_timeout=3, max_size=2 * 1024 * 1024)
        except Exception:
            raise DeliveryRetryable("无法连接企业微信，尚未发送") from None
        try:
            session = _BotSession(ws)
            await session.request("aibot_subscribe", {"bot_id": config["bot_id"], "secret": secret})
            yield session
        finally:
            with suppress(Exception):
                await ws.close()


async def deliver(channel, target: str, subject: str, body: str, files: list[dict], *, to_targets=None, cc_targets=None) -> dict:
    if not channel.enabled:
        raise DeliveryRejected("通知渠道已停用")
    config = _validated_config(channel)
    try:
        validate_target(channel.kind, target)
        if channel.kind == "email":
            if to_targets is not None:
                to_targets = [validate_target("email", item) for item in to_targets]
            if cc_targets is not None:
                cc_targets = [validate_target("email", item) for item in cc_targets]
            if to_targets is not None and target not in to_targets + (cc_targets or []):
                raise ValueError("投递目标不在收件人或抄送列表中")
    except ValueError as exc:
        raise DeliveryRejected(str(exc)) from None
    if any(c in subject for c in ("\r", "\n", "\x00")):
        raise DeliveryRejected("消息主题不能包含换行或控制字符")
    for item in files:
        if (not isinstance(item.get("data"), bytes) or not item["data"] or
                len(item["data"]) > config["max_file_size_mb"] * 1024 * 1024 or
                not isinstance(item.get("name"), str) or not item["name"] or
                any(c in item["name"] for c in ("\r", "\n", "\x00", "/", "\\"))):
            raise DeliveryRejected("发送文件为空、名称无效或超过渠道限制")
    secret = decrypt_secret(channel.encrypted_secret)
    if channel.kind == "email":
        return await asyncio.to_thread(_smtp_deliver, config, secret, target, subject, body, files, to_targets, cc_targets)
    if len(files) > 1 or (files and body):
        raise DeliveryRejected("机器人消息和文件必须分别记录与投递，每次仅发送一个项目")
    if not files and (not body or len(body.encode("utf-8")) > 20480):
        raise DeliveryRejected("机器人消息为空或超过 20480 字节")
    async with _bot_session(config, secret) as bot:
        if files:
            media_id = await bot.upload(files[0])
            message = {"chatid": target, "msgtype": "file", "file": {"media_id": media_id}}
        else:
            message = {"chatid": target, "msgtype": "markdown", "markdown": {"content": body}}
        receipt = await bot.request("aibot_send_msg", message, notification=True)
        return {"provider": "wecom_aibot", "accepted": True, "target": target, "req_id": receipt["req_id"]}


async def check_connection(channel) -> dict:
    config = _validated_config(channel)
    secret = decrypt_secret(channel.encrypted_secret)
    if channel.kind == "email":
        def check():
            smtp = _smtp_connect(config, secret)
            with suppress(Exception):
                smtp.close()
        await asyncio.to_thread(check)
    else:
        async with _bot_session(config, secret):
            pass
    return {"ok": True, "message": "连接和认证成功，未发送测试消息"}


async def discover_targets(channel, seconds: int) -> dict:
    if channel.kind != "wecom_aibot":
        raise DeliveryRejected("仅企业微信智能机器人支持会话发现")
    config = _validated_config(channel)
    async with _bot_session(config, decrypt_secret(channel.encrypted_secret)) as bot:
        deadline = time.monotonic() + max(1, min(seconds, 30))
        while time.monotonic() < deadline:
            await bot.heartbeat()
            try:
                frame = await asyncio.wait_for(bot.ws.recv(), timeout=min(5, max(0.01, deadline - time.monotonic())))
                bot._event(json.loads(frame))
            except asyncio.TimeoutError:
                continue
            except Exception:
                raise DeliveryRetryable("会话发现连接中断，请重新开始") from None
        return {"targets": bot.events}
