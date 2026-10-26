"""Validated workflow configuration and batch manifests."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
ClientId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class WorkflowModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkflowDestination(WorkflowModel):
    channel_id: int = Field(gt=0)
    targets: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]] = Field(min_length=1, max_length=100)
    cc: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]] = Field(default_factory=list, max_length=100)

    @field_validator("targets", "cc")
    @classmethod
    def unique_targets(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("收件目标不能重复")
        return values


class WorkflowConfig(WorkflowModel):
    formats: list[Literal["excel", "word", "pdf"]] = Field(min_length=1, max_length=3)
    template_id: int | None = Field(default=None, gt=0)
    deliveries: list[WorkflowDestination] = Field(default_factory=list, max_length=20)
    include_originals: bool = False
    include_attachments: bool = False
    notify_review: bool = False
    notify_failure: bool = False
    messages: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_config(self):
        if len(self.formats) != len(set(self.formats)):
            raise ValueError("导出格式不能重复")
        ids = [entry.channel_id for entry in self.deliveries]
        if len(ids) != len(set(ids)):
            raise ValueError("同一渠道请合并收件目标")
        if any(set(entry.targets) & set(entry.cc) for entry in self.deliveries):
            raise ValueError("收件人与抄送不能重复")
        allowed = {"success_subject", "success_body", "review_body", "failure_body"}
        if set(self.messages) - allowed:
            raise ValueError("未知消息模板")
        if any(not value.strip() or len(value) > 10000 for value in self.messages.values()):
            raise ValueError("消息模板须为 1 至 10000 个字符")
        if any(char in self.messages.get("success_subject", "") for char in "\r\n"):
            raise ValueError("邮件标题不能换行")
        return self


class WorkflowSave(WorkflowModel):
    name: Name
    enabled: bool = False
    config: WorkflowConfig


class ManifestFile(WorkflowModel):
    id: ClientId
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]


class UploadManifest(WorkflowModel):
    invoices: list[ManifestFile] = Field(min_length=1, max_length=100)
    attachments: list[ManifestFile] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique_files(self):
        ids = [entry.id for entry in self.invoices + self.attachments]
        if len(ids) > 100 or len(ids) != len(set(ids)):
            raise ValueError("每批最多 100 个文件，文件标识不能重复")
        return self


class WorkflowRunCreate(WorkflowModel):
    workflow_id: int = Field(gt=0)
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150)]
    upload_manifest: UploadManifest | None = None
    invoice_ids: list[Annotated[int, Field(gt=0)]] | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def one_source(self):
        if (self.upload_manifest is None) == (self.invoice_ids is None):
            raise ValueError("请选择上传清单或已有发票，不能同时提供两种来源")
        if self.invoice_ids is not None and len(set(self.invoice_ids)) != len(self.invoice_ids):
            raise ValueError("发票不能重复选择")
        return self


class ManualApproval(WorkflowModel):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class DeliveryResolution(WorkflowModel):
    action: Literal["confirm_sent", "retry"]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
