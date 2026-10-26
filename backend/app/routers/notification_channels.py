"""Administrator-managed notification channels and explicit connection/send tests."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, require_admin
from app.database import get_db
from app.models.user import User, UserRole
from app.models.workflow import NotificationChannel
from app.schemas.notification import ChannelWrite, DiscoverRequest, TestSendRequest
from app.services.audit_service import get_client_info, log_audit_no_commit
from app.services.notification_service import (
    DEFAULT_MESSAGES, MESSAGE_VARIABLES, DeliveryRejected, DeliveryRetryable,
    DeliveryUnknown, channel_view, check_connection, deliver, discover_targets, encrypt_secret,
)

router = APIRouter()


async def _channel(db, channel_id):
    channel = await db.get(NotificationChannel, channel_id)
    if channel is None:
        raise HTTPException(404, "通知渠道不存在")
    return channel


def _error(exc):
    status = 409 if isinstance(exc, DeliveryUnknown) else 503 if isinstance(exc, DeliveryRetryable) else 400
    return HTTPException(status, str(exc))


async def _audit(db, request, admin, channel, action, details):
    client = get_client_info(request)
    await log_audit_no_commit(
        db=db, entity_type="notification_channel", entity_id=channel.id,
        action=action, user_id=admin.username, new_value=details,
        ip_address=client.get("ip_address"), user_agent=client.get("user_agent"),
    )


@router.get("")
async def list_channels(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    query = select(NotificationChannel).order_by(NotificationChannel.id)
    admin = user.role == UserRole.ADMIN
    if not admin:
        query = query.where(NotificationChannel.enabled.is_(True))
    rows = (await db.execute(query)).scalars().all()
    return [channel_view(row, admin) for row in rows]


@router.get("/defaults")
async def default_messages(_user: User = Depends(get_current_user)):
    return {"messages": DEFAULT_MESSAGES, "variables": MESSAGE_VARIABLES}


async def _save(body, channel, db, request, admin):
    secret = body.secret.get_secret_value() if body.secret else ""
    if channel.kind and channel.kind != body.kind and not secret:
        raise HTTPException(400, "切换渠道类型时需要重新填写凭据")
    if body.enabled and (body.kind == "wecom_aibot" or body.config.get("username")) and not (secret or channel.encrypted_secret):
        raise HTTPException(400, "启用前请填写渠道凭据")
    if secret:
        try:
            channel.encrypted_secret = encrypt_secret(secret)
        except DeliveryRejected as exc:
            raise _error(exc) from None
    channel.name = body.name.strip()
    channel.kind = body.kind
    channel.config = body.config
    channel.enabled = body.enabled
    db.add(channel)
    await db.flush()
    await _audit(db, request, admin, channel, "configure", {
        "name": channel.name, "kind": channel.kind, "enabled": channel.enabled,
        "credentials_updated": bool(secret),
    })
    await db.commit()
    await db.refresh(channel)
    return channel_view(channel, True)


@router.post("")
async def create_channel(body: ChannelWrite, request: Request, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    return await _save(body, NotificationChannel(), db, request, admin)


@router.put("/{channel_id}")
async def update_channel(channel_id: int, body: ChannelWrite, request: Request, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    return await _save(body, await _channel(db, channel_id), db, request, admin)


@router.post("/{channel_id}/test-connection")
async def test_connection(channel_id: int, _admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    try:
        return await check_connection(await _channel(db, channel_id))
    except (DeliveryRejected, DeliveryRetryable, DeliveryUnknown) as exc:
        raise _error(exc) from None


@router.post("/{channel_id}/test-send")
async def test_send(channel_id: int, body: TestSendRequest, request: Request, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    channel = await _channel(db, channel_id)
    await _audit(db, request, admin, channel, "test_send_requested", {"target": body.target})
    await db.commit()
    try:
        receipt = await deliver(channel, body.target, "【发票管理】通知渠道测试", "这是一条由管理员主动发送的通知渠道测试消息。", [])
    except (DeliveryRejected, DeliveryRetryable, DeliveryUnknown) as exc:
        await _audit(db, request, admin, channel, "test_send_result", {"state": "unknown" if isinstance(exc, DeliveryUnknown) else "failed", "error": str(exc)})
        await db.commit()
        raise _error(exc) from None
    await _audit(db, request, admin, channel, "test_send_result", receipt)
    await db.commit()
    return receipt


@router.post("/{channel_id}/discover-targets")
async def find_targets(channel_id: int, body: DiscoverRequest, _admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    try:
        return await discover_targets(await _channel(db, channel_id), body.seconds)
    except (DeliveryRejected, DeliveryRetryable, DeliveryUnknown) as exc:
        raise _error(exc) from None
