"""Validated Excel template contract shared by preview and export."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


FIELD_CATALOG = [
    {"key": key, "label": label, "type": kind, "summable": kind == "money"}
    for key, label, kind in [
        ("invoice_number", "发票号码", "text"), ("issue_date", "开票日期", "date"),
        ("invoice_type", "发票类型", "text"), ("buyer_name", "购买方名称", "text"),
        ("buyer_tax_id", "购买方纳税人识别号", "text"), ("seller_name", "销售方名称", "text"),
        ("seller_tax_id", "销售方纳税人识别号", "text"), ("item_name", "项目名称", "text"),
        ("amount", "金额", "money"), ("tax_amount", "税额", "money"),
        ("total_with_tax", "价税合计", "money"), ("tax_rate", "税率", "text"),
        ("remark", "备注", "text"), ("status", "状态", "text"),
        ("owner", "归属人", "text"), ("file_name", "文件名", "text"),
        ("created_at", "创建时间", "datetime"), ("invoice_code", "发票代码", "text"),
        ("specification", "规格型号", "text"), ("unit", "单位", "text"),
        ("quantity", "数量", "number"), ("unit_price", "单价", "number"),
        ("buyer_address_phone", "购买方地址电话", "text"),
        ("buyer_bank_account", "购买方开户行及账号", "text"),
        ("seller_address_phone", "销售方地址电话", "text"),
        ("seller_bank_account", "销售方开户行及账号", "text"),
        ("total_with_tax_cn", "价税合计大写", "text"), ("drawer", "开票人", "text"),
    ]
]
FIELDS = {field["key"]: field for field in FIELD_CATALOG}


class TemplateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ExcelColumn(TemplateModel):
    field: str
    title: str = Field(min_length=1, max_length=80)
    format: Literal["text", "date", "datetime", "money", "number"] = "text"
    width: float = Field(default=24, ge=5, le=80)
    align: Literal["left", "center", "right"] = "left"
    sum: bool = False

    @model_validator(mode="after")
    def check_field(self):
        field = FIELDS.get(self.field)
        if not field:
            raise ValueError("未知发票字段")
        allowed = {field["type"], "text"}
        if self.format not in allowed:
            raise ValueError("字段格式与数据类型不兼容")
        if self.sum and (not field["summable"] or self.format != "money"):
            raise ValueError("仅金额格式的金额字段可以汇总")
        return self


class ExcelStyle(TemplateModel):
    header_color: str = Field(default="#1677FF", pattern=r"^#[0-9a-fA-F]{6}$")
    font_size: int = Field(default=11, ge=8, le=24)
    borders: bool = True
    striped: bool = True


class ExcelTemplateConfig(TemplateModel):
    sheet_name: str = Field(default="发票列表", min_length=1, max_length=31)
    title: str = Field(default="", max_length=150)
    filename_pattern: str = Field(default="发票导出_{date}", min_length=1, max_length=100)
    columns: list[ExcelColumn] = Field(min_length=1, max_length=len(FIELD_CATALOG))
    style: ExcelStyle = Field(default_factory=ExcelStyle)
    freeze_header: bool = True
    auto_filter: bool = True
    show_count: bool = True

    @field_validator("sheet_name")
    @classmethod
    def valid_sheet_name(cls, value):
        if re.search(r"[\\/*?:\[\]\x00-\x1f]", value) or value.startswith("'") or value.endswith("'"):
            raise ValueError("工作表名称包含非法字符")
        return value

    @field_validator("filename_pattern")
    @classmethod
    def valid_filename_pattern(cls, value):
        remaining = re.sub(r"\{(?:date|run_id|run_name)\}", "", value)
        if re.search(r'[{}<>:"/\\|?*\x00-\x1f]', remaining):
            raise ValueError("文件名仅支持 {date}、{run_id}、{run_name} 变量，不允许路径字符")
        return value

    @model_validator(mode="after")
    def unique_columns(self):
        if len({c.field for c in self.columns}) != len(self.columns):
            raise ValueError("不能重复选择字段")
        if len({c.title for c in self.columns}) != len(self.columns):
            raise ValueError("列标题不能重复")
        return self


class ExportTemplateCreate(TemplateModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    config: ExcelTemplateConfig
    enabled: bool = True


class ExportTemplateUpdate(TemplateModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    config: ExcelTemplateConfig | None = None
    enabled: bool | None = None


class ExportTemplatePublish(TemplateModel):
    expected_version: int = Field(ge=0)


class ExportTemplatePreview(TemplateModel):
    config: ExcelTemplateConfig
    invoice_ids: list[int] = Field(default_factory=list, max_length=100)

    @field_validator("invoice_ids")
    @classmethod
    def valid_ids(cls, value):
        if any(identifier <= 0 for identifier in value) or len(set(value)) != len(value):
            raise ValueError("发票 ID 必须为不重复的正整数")
        return value
