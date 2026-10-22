from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field

from app.models.attachment import AttachmentCategory


class AttachmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    invoice_id: int
    category: AttachmentCategory
    file_name: str
    file_type: str
    note: Optional[str] = None
    created_at: datetime
    file_size: Optional[int] = None


class AttachmentUploadResult(BaseModel):
    """Per-file result for a batch attachment upload (mirrors UploadResponse)."""
    id: int
    file_name: str
    status: str  # success | error
    message: str


class AttachmentUpdateRequest(BaseModel):
    invoice_id: Optional[int] = None  # re-associate to another invoice
    category: Optional[AttachmentCategory] = None
    note: Optional[str] = Field(default=None, max_length=2000)
