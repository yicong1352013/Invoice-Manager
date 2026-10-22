"""Read-only MCP server for external AI agents.

Mounted at /api/mcp (streamable HTTP). Enabled only when the admin turns it
on AND sets a token in settings; the token is an admin-level service
credential granting read access to all invoices (see ADR/glossary).
"""

import base64
import logging
from datetime import date
from typing import Optional

from fastmcp import FastMCP
from starlette.responses import JSONResponse

from app.database import async_session_maker
from app.models.attachment import Attachment
from app.models.invoice import Invoice, InvoiceStatus
from app.models.provider import SETTING_MCP_ENABLED, SETTING_MCP_TOKEN
from app.services.provider_service import get_setting

logger = logging.getLogger(__name__)

mcp = FastMCP(name="Invoice Manager", instructions=(
    "发票管理系统只读接口。工具返回中国增值税发票的结构化数据："
    "发票号码、开票日期、购/销双方名称与税号、金额、税额、价税合计、"
    "状态（已上传/解析中/待审核/已确认/已报销/未报销），以及 OCR/LLM "
    "识别结果与差异、附件元数据。"
))


def _invoice_summary(inv: Invoice) -> dict:
    return {
        "id": inv.id,
        "invoice_number": inv.invoice_number,
        "issue_date": str(inv.issue_date) if inv.issue_date else None,
        "buyer_name": inv.buyer_name,
        "buyer_tax_id": inv.buyer_tax_id,
        "seller_name": inv.seller_name,
        "seller_tax_id": inv.seller_tax_id,
        "item_name": inv.item_name,
        "amount": str(inv.amount) if inv.amount is not None else None,
        "tax_amount": str(inv.tax_amount) if inv.tax_amount is not None else None,
        "total_with_tax": str(inv.total_with_tax) if inv.total_with_tax is not None else None,
        "tax_rate": inv.tax_rate,
        "status": inv.status.value if inv.status else None,
        "owner": inv.owner,
        "file_name": inv.file_name,
        "file_type": inv.file_type,
        "created_at": inv.created_at.isoformat() if inv.created_at else None,
    }


def _parse_status(status: Optional[str]) -> Optional[InvoiceStatus]:
    if not status:
        return None
    try:
        return InvoiceStatus(status)
    except ValueError:
        return None


@mcp.tool
async def list_invoices(
    status: Optional[str] = None,
    owner: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """查询发票列表（分页，可按状态/归属人/开票日期范围筛选）。

    status 取值：已上传/解析中/待审核/已确认/已报销/未报销；日期格式 YYYY-MM-DD。
    """
    from sqlalchemy import func, select

    page = max(1, page)
    page_size = max(1, min(100, page_size))

    query = select(Invoice)
    status_enum = _parse_status(status)
    if status_enum:
        query = query.where(Invoice.status == status_enum)
    if owner:
        query = query.where(Invoice.owner == owner)
    if start_date:
        query = query.where(Invoice.issue_date >= date.fromisoformat(start_date))
    if end_date:
        query = query.where(Invoice.issue_date <= date.fromisoformat(end_date))

    async with async_session_maker() as db:
        total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
        result = await db.execute(
            query.order_by(Invoice.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = [_invoice_summary(inv) for inv in result.scalars().all()]

    return {"total": total, "page": page, "page_size": page_size, "items": items}


@mcp.tool
async def get_invoice_detail(invoice_id: int) -> dict:
    """获取一张发票的完整详情：基础字段、OCR 识别结果、LLM 识别结果、字段差异。"""
    from sqlalchemy import select

    async with async_session_maker() as db:
        invoice = await db.get(Invoice, invoice_id)
        if not invoice:
            return {"error": f"发票 {invoice_id} 不存在"}

        from app.models.invoice import LlmResult, OcrResult, ParsingDiff

        ocr_row = (await db.execute(select(OcrResult).where(OcrResult.invoice_id == invoice_id))).scalar_one_or_none()
        llm_row = (await db.execute(select(LlmResult).where(LlmResult.invoice_id == invoice_id))).scalar_one_or_none()
        diff_rows = (await db.execute(
            select(ParsingDiff).where(ParsingDiff.invoice_id == invoice_id).order_by(ParsingDiff.id)
        )).scalars().all()

        def _fields(row, exclude_text=True):
            if row is None:
                return None
            data = {c: getattr(row, c) for c in (
                "invoice_number", "issue_date", "buyer_name", "buyer_tax_id",
                "seller_name", "seller_tax_id", "item_name", "total_with_tax",
                "amount", "tax_amount", "tax_rate",
            )}
            return data

        detail = _invoice_summary(invoice)
        detail["ocr_result"] = _fields(ocr_row)
        detail["llm_result"] = _fields(llm_row)
        if ocr_row is not None and getattr(ocr_row, "raw_text", None):
            detail["ocr_raw_text"] = ocr_row.raw_text
        detail["parsing_diffs"] = [
            {
                "field_name": d.field_name,
                "ocr_value": d.ocr_value,
                "llm_value": d.llm_value,
                "final_value": d.final_value,
                "source": d.source,
                "resolved": bool(d.resolved),
            }
            for d in diff_rows
        ]
    return detail


@mcp.tool
async def get_statistics(status: Optional[str] = None, owner: Optional[str] = None) -> dict:
    """统计发票数量与金额汇总（数量、金额合计、税额合计、价税合计）。"""
    from decimal import Decimal

    from sqlalchemy import select

    query = select(Invoice)
    status_enum = _parse_status(status)
    if status_enum:
        query = query.where(Invoice.status == status_enum)
    if owner:
        query = query.where(Invoice.owner == owner)

    async with async_session_maker() as db:
        result = await db.execute(query)
        invoices = result.scalars().all()

    return {
        "count": len(invoices),
        "total_amount": str(sum((inv.amount or Decimal(0) for inv in invoices), Decimal(0))),
        "total_tax": str(sum((inv.tax_amount or Decimal(0) for inv in invoices), Decimal(0))),
        "total_with_tax": str(sum((inv.total_with_tax or Decimal(0) for inv in invoices), Decimal(0))),
    }


@mcp.tool
async def list_attachments(invoice_id: int) -> dict:
    """列出一张发票的附件（合同、收货单等）元数据。"""
    from sqlalchemy import select

    async with async_session_maker() as db:
        invoice = await db.get(Invoice, invoice_id)
        if not invoice:
            return {"error": f"发票 {invoice_id} 不存在"}
        rows = (await db.execute(
            select(Attachment).where(Attachment.invoice_id == invoice_id).order_by(Attachment.id)
        )).scalars().all()
        items = [
            {
                "id": a.id,
                "category": a.category,
                "file_name": a.file_name,
                "file_type": a.file_type,
                "note": a.note,
                "file_size": len(a.file_data) if a.file_data else 0,
            }
            for a in rows
        ]
    return {"invoice_id": invoice_id, "items": items}


@mcp.tool
async def get_invoice_file(invoice_id: int) -> dict:
    """获取发票原始文件（base64），可直接交给视觉模型或落盘查看。"""
    async with async_session_maker() as db:
        invoice = await db.get(Invoice, invoice_id)
        if not invoice:
            return {"error": f"发票 {invoice_id} 不存在"}
        content = base64.b64encode(invoice.file_data).decode("ascii")

    return {
        "invoice_id": invoice_id,
        "file_name": invoice.file_name,
        "file_type": invoice.file_type,
        "size_bytes": len(invoice.file_data),
        "content_base64": content,
    }


def evaluate_mcp_access(enabled: bool, configured_token: Optional[str], authorization: Optional[str]):
    """Pure gate for the MCP endpoint. Returns (status_code, detail) or None when allowed."""
    if not enabled or not configured_token:
        return 404, "Not Found"
    if not authorization or not authorization.startswith("Bearer "):
        return 401, "需要 Bearer Token 鉴权"
    if authorization[7:].strip() != configured_token:
        return 401, "MCP 访问令牌无效"
    return None


class McpAuthMiddleware:
    """ASGI wrapper: gates the mounted MCP app on per-request settings."""

    def __init__(self, mcp_asgi_app):
        self.mcp_asgi_app = mcp_asgi_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.mcp_asgi_app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        async with async_session_maker() as db:
            enabled = (await get_setting(db, SETTING_MCP_ENABLED, "false")) == "true"
            token = await get_setting(db, SETTING_MCP_TOKEN)

        verdict = evaluate_mcp_access(enabled, token, headers.get("authorization"))
        if verdict is not None:
            status, detail = verdict
            response = JSONResponse({"detail": detail}, status_code=status)
            await response(scope, receive, send)
            return

        await self.mcp_asgi_app(scope, receive, send)


def create_mcp_asgi_app():
    """Build the auth-gated MCP streamable-HTTP app.

    Mounted under /api with internal path /mcp, so agents connect to /api/mcp
    (no trailing slash).
    """
    return McpAuthMiddleware(mcp.http_app(path="/mcp"))
