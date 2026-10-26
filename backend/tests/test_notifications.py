"""No external sends: protect delivery ambiguity, credentials and wire protocol."""

import base64
import json
import smtplib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.database import get_db
from app.models.user import UserRole
from app.routers import notification_channels as routes
from app.schemas.notification import ChannelWrite, validate_target
from app.services import notification_service as service


@pytest.fixture
def key(monkeypatch):
    settings = SimpleNamespace(notification_encryption_key=Fernet.generate_key().decode())
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    return settings


def channel(kind="email", secret=None):
    config = {"host": "smtp.example.com", "port": 587, "tls": "starttls", "username": "sender", "from_address": "sender@example.com", "max_file_size_mb": 20}
    if kind == "wecom_aibot":
        config = {"bot_id": "bot-1", "max_file_size_mb": 20}
    return SimpleNamespace(id=1, name="Test", kind=kind, config=config, encrypted_secret=secret, enabled=True, updated_at=None)


def test_credentials_encrypted_and_never_returned(key):
    encrypted = service.encrypt_secret("private-credential")
    assert "private-credential" not in encrypted
    assert service.decrypt_secret(encrypted) == "private-credential"
    obj = channel(secret=encrypted)
    for admin in (False, True):
        view = service.channel_view(obj, admin)
        assert encrypted not in json.dumps(view)
        assert "private-credential" not in json.dumps(view)
    assert service.channel_view(obj, False)["config"] == {}
    key.notification_encryption_key = Fernet.generate_key().decode()
    with pytest.raises(service.DeliveryRejected, match="无法解密"):
        service.decrypt_secret(encrypted)


def test_missing_key_cannot_persist_secret(key):
    key.notification_encryption_key = ""
    with pytest.raises(service.DeliveryRejected, match="NOTIFICATION_ENCRYPTION_KEY"):
        service.encrypt_secret("secret")


@pytest.mark.parametrize("target", ["a@example.com\r\nBcc: b@example.com", "a@example.com,b@example.com", "Name <a@example.com>", "", "a b@example.com"])
def test_recipient_injection_rejected(target):
    with pytest.raises(ValueError):
        validate_target("email", target)


def test_config_does_not_allow_secret_in_plain_json():
    with pytest.raises(ValidationError):
        ChannelWrite(name="x", kind="email", config={**channel().config, "password": "secret"})
    with pytest.raises(ValidationError):
        ChannelWrite(name="x", kind="email", config={**channel().config, "tls": "none"})


def test_template_only_substitutes_whitelisted_variables():
    assert service.render_message("{{run_name}} / {{ invoice_count }}", {"run_name": "{{error}}", "invoice_count": 0}) == "{{error}} / 0"
    for bad in ("{{__class__}}", "{{run_name.upper()}}", "{% for x in y %}", "{{run_name}"):
        with pytest.raises(ValueError):
            service.render_message(bad, {"run_name": "test"})
    with pytest.raises(ValueError, match="缺少变量"):
        service.render_message("{{run_id}}", {})


class FakeSMTP:
    def __init__(self, data_error=None, rcpt_code=250, mail_error=None):
        self.data_error = data_error
        self.rcpt_code = rcpt_code
        self.mail_error = mail_error
        self.sent = []
        self.recipients = []
        self.closed = False

    def mail(self, sender):
        if self.mail_error:
            raise self.mail_error
        return 250, b"ok"

    def rcpt(self, target):
        self.recipients.append(target)
        return self.rcpt_code, b"message must never surface credentials"

    def data(self, raw):
        self.sent.append(raw)
        if self.data_error:
            raise self.data_error
        return 250, b"ok"

    def close(self):
        self.closed = True


def test_smtp_success_and_attachment(monkeypatch):
    smtp = FakeSMTP()
    monkeypatch.setattr(service, "_smtp_connect", lambda *_: smtp)
    receipt = service._smtp_deliver(channel().config, "secret", "to@example.com", "测试", "正文", [{"name": "报表.xlsx", "content_type": "application/octet-stream", "data": b"report"}])
    assert receipt["accepted"] is True
    assert b"Content-Disposition: attachment" in smtp.sent[0]
    assert smtp.closed


@pytest.mark.parametrize("smtp,expected", [
    (FakeSMTP(data_error=ConnectionError("secret server detail")), service.DeliveryUnknown),
    (FakeSMTP(mail_error=ConnectionError("secret")), service.DeliveryRetryable),
    (FakeSMTP(rcpt_code=450), service.DeliveryRetryable),
    (FakeSMTP(rcpt_code=550), service.DeliveryRejected),
    (FakeSMTP(data_error=smtplib.SMTPDataError(554, b"secret")), service.DeliveryRejected),
])
def test_smtp_only_ambiguous_after_data(monkeypatch, smtp, expected):
    monkeypatch.setattr(service, "_smtp_connect", lambda *_: smtp)
    with pytest.raises(expected) as error:
        service._smtp_deliver(channel().config, "secret", "to@example.com", "subject", "body", [])
    assert "secret" not in str(error.value)
    assert smtp.closed


@pytest.mark.asyncio
async def test_headers_and_oversize_block_before_network(monkeypatch):
    def forbidden(*_):
        pytest.fail("network must not be reached")
    monkeypatch.setattr(service, "_smtp_connect", forbidden)
    with pytest.raises(service.DeliveryRejected, match="主题"):
        await service.deliver(channel(), "to@example.com", "hello\r\nBcc: x@example.com", "body", [])
    obj = channel()
    obj.config["max_file_size_mb"] = 1
    with pytest.raises(service.DeliveryRejected, match="超过"):
        await service.deliver(obj, "to@example.com", "subject", "body", [{"name": "file", "data": b"x" * (1024 * 1024 + 1)}])


class FakeWebSocket:
    def __init__(self, lost=None, rejected=None):
        self.frames = []
        self.lost = lost
        self.rejected = rejected
        self.closed = False

    async def send(self, raw):
        self.frames.append(json.loads(raw))

    async def recv(self):
        frame = self.frames[-1]
        if frame["cmd"] == self.lost:
            raise ConnectionError("secret must not leak")
        body = {}
        if frame["cmd"] == "aibot_upload_media_init":
            body = {"upload_id": "upload-1"}
        elif frame["cmd"] == "aibot_upload_media_finish":
            body = {"media_id": "media-1"}
        return json.dumps({"headers": frame["headers"], "errcode": 40013 if frame["cmd"] == self.rejected else 0, "body": body})

    async def close(self):
        self.closed = True


@pytest.fixture
def bot_network(monkeypatch, key):
    @asynccontextmanager
    async def lock(_):
        yield
    monkeypatch.setattr(service, "_bot_lock", lock)
    sockets = []
    async def connect(*args, **kwargs):
        assert args[0] == "wss://openws.work.weixin.qq.com"
        return sockets[-1]
    monkeypatch.setattr(service.websockets, "connect", connect)
    return sockets


@pytest.mark.asyncio
async def test_bot_auth_upload_and_send_follow_official_protocol(bot_network):
    ws = FakeWebSocket()
    bot_network.append(ws)
    data = b"x" * (512 * 1024 + 1)
    receipt = await service.deliver(channel("wecom_aibot", service.encrypt_secret("bot-secret")), "group-1", "", "", [{"name": "report.xlsx", "content_type": "application/octet-stream", "data": data}])
    assert receipt["accepted"]
    assert [frame["cmd"] for frame in ws.frames] == ["aibot_subscribe", "aibot_upload_media_init", "aibot_upload_media_chunk", "aibot_upload_media_chunk", "aibot_upload_media_finish", "aibot_send_msg"]
    assert ws.frames[0]["body"] == {"bot_id": "bot-1", "secret": "bot-secret"}
    assert ws.frames[1]["body"]["total_chunks"] == 2
    assert base64.b64decode(ws.frames[2]["body"]["base64_data"]) + base64.b64decode(ws.frames[3]["body"]["base64_data"]) == data
    assert ws.frames[-1]["body"] == {"chatid": "group-1", "msgtype": "file", "file": {"media_id": "media-1"}}
    assert "bot-secret" not in json.dumps(receipt)
    assert ws.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("lost,expected", [("aibot_subscribe", service.DeliveryRetryable), ("aibot_send_msg", service.DeliveryUnknown)])
async def test_bot_lost_ack_classified_without_resend(bot_network, lost, expected):
    ws = FakeWebSocket(lost=lost)
    bot_network.append(ws)
    with pytest.raises(expected):
        await service.deliver(channel("wecom_aibot", service.encrypt_secret("bot-secret")), "user-1", "subject", "body", [])
    assert sum(frame["cmd"] == "aibot_send_msg" for frame in ws.frames) <= 1
    assert ws.closed


@pytest.mark.asyncio
async def test_bot_definitive_rejection_is_not_ambiguous(bot_network):
    bot_network.append(FakeWebSocket(rejected="aibot_send_msg"))
    with pytest.raises(service.DeliveryRejected, match="40013"):
        await service.deliver(channel("wecom_aibot", service.encrypt_secret("secret")), "user-1", "subject", "body", [])


@pytest.mark.asyncio
async def test_connection_check_does_not_send_message(bot_network, monkeypatch):
    ws = FakeWebSocket()
    bot_network.append(ws)
    result = await service.check_connection(channel("wecom_aibot", service.encrypt_secret("secret")))
    assert result["ok"]
    assert [frame["cmd"] for frame in ws.frames] == ["aibot_subscribe"]
    smtp = FakeSMTP()
    monkeypatch.setattr(service, "_smtp_connect", lambda *_: smtp)
    await service.check_connection(channel())
    assert not smtp.sent and smtp.closed


def test_discovery_retains_only_safe_target_ids():
    session = service._BotSession(None)
    callback = {"cmd": "aibot_msg_callback", "body": {"chattype": "group", "chatid": "group-1", "from": {"userid": "user-1"}, "text": {"content": "private message"}}}
    session._event(callback)
    session._event(callback)
    session._event({"cmd": "unknown", "body": {"chattype": "group", "chatid": "wrong"}})
    assert session.events == [{"target": "group-1", "kind": "group"}]


@pytest.fixture
def api(monkeypatch):
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/notification-channels")
    user = SimpleNamespace(username="user", role=UserRole.USER)
    db = AsyncMock()
    app.dependency_overrides[get_current_user] = lambda: user
    async def session():
        yield db
    app.dependency_overrides[get_db] = session
    monkeypatch.setattr(routes, "_audit", AsyncMock())
    with TestClient(app) as client:
        yield client, user, db


@pytest.mark.parametrize("path,body", [
    ("", {"name": "x", "kind": "email", "config": channel().config}),
    ("/1/test-connection", {}),
    ("/1/test-send", {"target": "to@example.com"}),
    ("/1/discover-targets", {"seconds": 1}),
])
def test_ordinary_user_cannot_configure_or_send(api, path, body):
    client, _, db = api
    response = client.post("/api/notification-channels" + path, json=body)
    assert response.status_code == 403
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_edit_preserves_secret_without_reencrypting(key, monkeypatch):
    encrypted = service.encrypt_secret("old-secret")
    obj = channel(secret=encrypted)
    db = AsyncMock()
    db.add = lambda _: None
    monkeypatch.setattr(routes, "_audit", AsyncMock())
    monkeypatch.setattr(routes, "encrypt_secret", lambda _: pytest.fail("blank secret must preserve ciphertext"))
    body = ChannelWrite(name="Renamed", kind="email", config=obj.config, enabled=True, secret="")
    view = await routes._save(body, obj, db, None, None)
    assert obj.encrypted_secret == encrypted
    assert view["name"] == "Renamed" and view["has_secret"]
    assert "encrypted_secret" not in view
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_smtp_close_failure_cannot_repeat_accepted_message(monkeypatch):
    smtp = FakeSMTP()
    def close():
        raise ConnectionError("connection gone after acceptance")
    smtp.close = close
    monkeypatch.setattr(service, "_smtp_connect", lambda *_: smtp)
    result = await service.deliver(channel(), "to@example.com", "subject", "body", [])
    assert result["accepted"]
    assert len(smtp.sent) == 1


@pytest.mark.asyncio
async def test_email_cc_headers_do_not_expand_single_recipient_envelope(monkeypatch):
    smtp = FakeSMTP()
    monkeypatch.setattr(service, "_smtp_connect", lambda *_: smtp)
    result = await service.deliver(channel(), "copy@example.com", "subject", "body", [], to_targets=["to@example.com"], cc_targets=["copy@example.com", "copy2@example.com"])
    assert result["accepted"]
    assert smtp.recipients == ["copy@example.com"]
    assert b"To: to@example.com" in smtp.sent[0]
    assert b"Cc: copy@example.com, copy2@example.com" in smtp.sent[0]
    with pytest.raises(service.DeliveryRejected):
        await service.deliver(channel(), "copy@example.com", "subject", "body", [], to_targets=["to@example.com"], cc_targets=["copy@example.com\nBcc: injected@example.com"])


@pytest.mark.asyncio
async def test_advisory_lock_busy_never_opens_bot_connection(monkeypatch):
    from app import database
    conn = AsyncMock()
    conn.scalar.return_value = False
    @asynccontextmanager
    async def connect():
        yield conn
    monkeypatch.setattr(database, "engine", SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), connect=connect))
    with pytest.raises(service.DeliveryRetryable, match="正在连接"):
        async with service._bot_lock("bot-id"):
            pytest.fail("busy bot lock must not be entered")


@pytest.mark.asyncio
async def test_unlock_failure_discards_connection_without_overriding_receipt(monkeypatch):
    from app import database
    conn = AsyncMock()
    conn.scalar.return_value = True
    conn.execute.side_effect = ConnectionError("lost after send")
    @asynccontextmanager
    async def connect():
        yield conn
    monkeypatch.setattr(database, "engine", SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), connect=connect))
    async with service._bot_lock("bot-id"):
        pass
    conn.invalidate.assert_awaited_once()
