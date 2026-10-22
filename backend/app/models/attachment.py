from datetime import datetime
from enum import Enum
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, LargeBinary
from sqlalchemy.orm import relationship

from app.database import Base


class AttachmentCategory(str, Enum):
    CONTRACT = "contract"  # 合同
    RECEIPT = "receipt"    # 收货单
    OTHER = "other"        # 其他


# File types accepted for attachments (wider than invoices themselves)
ATTACHMENT_EXTENSIONS = ["pdf", "jpg", "jpeg", "png", "doc", "docx", "xls", "xlsx"]


class Attachment(Base):
    """A supporting document (contract, delivery note, ...) tied to an invoice."""

    __tablename__ = "attachments"

    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=False, index=True)
    category = Column(String(20), default=AttachmentCategory.OTHER, nullable=False)
    file_name = Column(String(255), nullable=False)
    file_type = Column(String(20), nullable=False)
    file_data = Column(LargeBinary, nullable=False)
    note = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    invoice = relationship("Invoice", back_populates="attachments")
