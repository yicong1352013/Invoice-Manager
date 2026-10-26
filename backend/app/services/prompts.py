"""Prompts for LLM-based invoice parsing."""

import json

# JSON Schema definition for strict output format
INVOICE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "invoice_number": {
            "type": ["string", "null"],
            "description": "发票号码，数电票为20位数字，老式票8位数字"
        },
        "invoice_type": {
            "type": ["string", "null"],
            "description": "发票名称/类型，票头原文，如\"电子发票（普通发票）\"、\"电子发票（增值税专用发票）\""
        },
        "invoice_code": {
            "type": ["string", "null"],
            "description": "发票代码，通常为10-12位数字（数电票无发票代码，返回null）"
        },
        "issue_date": {
            "type": ["string", "null"],
            "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
            "description": "开票日期，格式必须为YYYY-MM-DD"
        },
        "buyer_name": {
            "type": ["string", "null"],
            "description": "购买方名称（公司全称）"
        },
        "buyer_tax_id": {
            "type": ["string", "null"],
            "pattern": "^[A-Z0-9]{15,20}$",
            "description": "购买方纳税人识别号，15-20位字母数字"
        },
        "buyer_address_phone": {
            "type": ["string", "null"],
            "description": "购买方地址、电话（购买方区域\"地址\"行的完整内容；无则null）"
        },
        "buyer_bank_account": {
            "type": ["string", "null"],
            "description": "购买方开户行及账号（购买方区域对应行；无则null）"
        },
        "seller_name": {
            "type": ["string", "null"],
            "description": "销售方名称（公司全称）"
        },
        "seller_tax_id": {
            "type": ["string", "null"],
            "pattern": "^[A-Z0-9]{15,20}$",
            "description": "销售方纳税人识别号，15-20位字母数字"
        },
        "seller_address_phone": {
            "type": ["string", "null"],
            "description": "销售方地址、电话（销售方区域\"地址\"行的完整内容；无则null）"
        },
        "seller_bank_account": {
            "type": ["string", "null"],
            "description": "销售方开户行及账号（销售方区域对应行；无则null）"
        },
        "item_name": {
            "type": ["string", "null"],
            "description": "项目名称/货物名称，如有类别标记(*类别*商品)则保留"
        },
        "specification": {
            "type": ["string", "null"],
            "description": "规格型号（明细行；多数发票没有，无则null）"
        },
        "unit": {
            "type": ["string", "null"],
            "description": "单位（明细行计量单位，如\"个\"、\"次\"；无则null）"
        },
        "quantity": {
            "type": ["string", "null"],
            "pattern": "^\\d+(\\.\\d+)?$",
            "description": "数量（纯数字；无则null）"
        },
        "unit_price": {
            "type": ["string", "null"],
            "pattern": "^\\d+(\\.\\d+)?$",
            "description": "单价（不含税，纯数字；无则null）"
        },
        "total_with_tax": {
            "type": ["string", "null"],
            "pattern": "^\\d+(\\.\\d{1,2})?$",
            "description": "价税合计金额（小写），纯数字不含货币符号"
        },
        "total_with_tax_cn": {
            "type": ["string", "null"],
            "description": "价税合计（大写），汉字金额如\"叁佰元整\""
        },
        "amount": {
            "type": ["string", "null"],
            "pattern": "^\\d+(\\.\\d{1,2})?$",
            "description": "金额（不含税合计），纯数字"
        },
        "tax_amount": {
            "type": ["string", "null"],
            "pattern": "^\\d+(\\.\\d{1,2})?$",
            "description": "税额（合计税额），纯数字（免税发票返回\"0\"）"
        },
        "tax_rate": {
            "type": ["string", "null"],
            "description": "税率，如\"6%\"、\"13%\"、\"免税\""
        },
        "remark": {
            "type": ["string", "null"],
            "description": "备注栏内容，原样返回；无则null"
        },
        "drawer": {
            "type": ["string", "null"],
            "description": "开票人姓名；无则null"
        }
    },
    "required": [
        "invoice_number", "invoice_type", "invoice_code", "issue_date",
        "buyer_name", "buyer_tax_id", "buyer_address_phone", "buyer_bank_account",
        "seller_name", "seller_tax_id", "seller_address_phone", "seller_bank_account",
        "item_name", "specification", "unit", "quantity", "unit_price",
        "total_with_tax", "total_with_tax_cn", "amount", "tax_amount", "tax_rate",
        "remark", "drawer"
    ],
    "additionalProperties": False
}

# Required fields list for validation
REQUIRED_FIELDS = list(INVOICE_JSON_SCHEMA["required"])

# Total field count, interpolated into prompts so text never drifts from schema
_FIELD_COUNT = len(INVOICE_JSON_SCHEMA["properties"])

# Vision-based system prompt (for direct image analysis)
INVOICE_VISION_SYSTEM_PROMPT = """你是一个专业的中国发票信息提取助手。
你必须严格按照JSON Schema格式返回结果，无法识别的字段返回null。
只返回JSON对象，不要包含任何其他文字、解释或markdown代码块标记。"""

# Build field descriptions for the prompt
_field_descriptions = json.dumps(
    {k: v["description"] for k, v in INVOICE_JSON_SCHEMA["properties"].items()},
    ensure_ascii=False,
    indent=2
)

# Vision-based extraction prompt (for direct image analysis)
INVOICE_VISION_PROMPT = f"""请分析这张中国发票图片，提取发票信息。

## 输出格式要求（必须严格遵守）

返回一个JSON对象，必须包含以下{_FIELD_COUNT}个字段（不能多也不能少）：
{_field_descriptions}

字段类型规则：
- 所有字段值必须是 string 或 null（数值也用字符串表示）
- 日期格式必须是 YYYY-MM-DD
- 金额字段仅包含数字和小数点
- 税率字段格式如 \"6%\"、\"13%\" 或 \"免税\"
- 不能返回空字符串，用 null 表示缺失

## 购买方与销售方识别规则（最重要）

根据标签文字识别，不要依赖位置：
- 购买方 = 标注为"购买方"、"购方"、"购货单位"的区域
- 销售方 = 标注为"销售方"、"销方"、"销货单位"的区域
- 销售方区域通常还有"开票人"、"收款人"、"复核"等字样

中国发票格式说明：
- 标准增值税发票：购买方在左上，销售方在左下
- 电子普通发票：购买方在上部，销售方在底部（价税合计下方）
- 全电发票：购买方和销售方可能左右并排

## 字段提取规则

1. invoice_number: 查找"发票号码"标签后的数字
2. invoice_type: 票头发票名称原文，如"电子发票（普通发票）"、"电子发票（增值税专用发票）"
3. invoice_code: 查找"发票代码"标签后的数字（数电票无此项，返回null）
4. issue_date: 查找"开票日期"，转换为YYYY-MM-DD格式
5. buyer_name/seller_name: 查找"名称"标签后的公司全称
6. buyer_tax_id/seller_tax_id: 查找"纳税人识别号"后的15-20位编码
7. buyer_address_phone/seller_address_phone: 对应方区域的"地址、电话"行完整内容
8. buyer_bank_account/seller_bank_account: 对应方区域的"开户行及账号"行完整内容
9. item_name: 查找商品行中的项目名称（可能有*分类*前缀）
10. specification/unit/quantity/unit_price: 明细行的规格型号、单位、数量、单价（票面没有则为null）
11. total_with_tax: 查找"价税合计"或"小写"金额，只返回数字
12. total_with_tax_cn: "价税合计（大写）"的汉字金额，如"叁佰元整"
13. amount: 查找"金额"或"合计"行的不含税金额
14. tax_amount: 查找"税额"，免税发票返回"0"
15. tax_rate: 查找"税率"列的值，如"6%"、"免税"
16. remark: 备注栏内容原样返回，无备注返回null
17. drawer: 票尾"开票人"后的姓名，无则null

## 数据清洗规则

- 金额字段去除¥、￥、$、逗号，只保留数字和小数点
- 日期统一转换为YYYY-MM-DD
- 地址电话、开户行及账号、备注保留票面原文，不做拆分
- 无法识别的字段返回null，不要猜测

请直接返回JSON对象："""


# Text-based extraction (fallback when the active model has no vision:
# parses the OCR raw text instead of the image itself)
INVOICE_TEXT_SYSTEM_PROMPT = INVOICE_VISION_SYSTEM_PROMPT

INVOICE_TEXT_PROMPT = f"""以下是中国发票的OCR识别文本（行序可能打乱，含噪音），
请从中提取发票信息。

## OCR原文

{{ocr_text}}

## 输出格式要求（必须严格遵守）

返回一个JSON对象，必须包含以下{_FIELD_COUNT}个字段（不能多也不能少）：
{_field_descriptions}

字段类型规则：
- 所有字段值必须是 string 或 null（数值也用字符串表示）
- 日期格式必须是 YYYY-MM-DD
- 金额字段仅包含数字和小数点
- 税率字段格式如 \\"6%\\"、\\"13%\\" 或 \\"免税\\"
- 不能返回空字符串，用 null 表示缺失

## 字段提取规则

1. invoice_number: 查找"发票号码"附近的数字
2. invoice_type: 票头发票名称原文，如"电子发票（普通发票）"
3. invoice_code: 查找"发票代码"附近的数字（数电票无此项，返回null）
4. issue_date: 查找"开票日期"，转换为YYYY-MM-DD格式
5. buyer_name: "购买方"区域的"名称"后公司全称；销售方区域的"名称"是seller_name
6. buyer_tax_id/seller_tax_id: 对应区域的"纳税人识别号"15-20位编码
7. buyer_address_phone/seller_address_phone: 对应方区域的"地址、电话"内容
8. buyer_bank_account/seller_bank_account: 对应方区域的"开户行及账号"内容
9. item_name: 商品行项目名称（可能有*分类*前缀）
10. specification/unit/quantity/unit_price: 明细行的规格型号、单位、数量、单价（没有则null）
11. total_with_tax: "价税合计"（小写）金额，只返回数字
12. total_with_tax_cn: "价税合计（大写）"的汉字金额
13. amount: "合计"行不含税金额
14. tax_amount: "税额"，免税发票返回"0"
15. tax_rate: "税率"列的值
16. remark: "备注"栏内容原样返回，无则null
17. drawer: "开票人"后的姓名，无则null

OCR文本常见噪音：数字被拆分、标签与值分离、混入表格线字符。请基于上下文
推断，不要猜测不存在的值，无法确定的字段返回null。

请直接返回JSON对象："""

# AI verification (detail-page "AI校验"): re-check the CURRENT stored field
# values (which may include manual edits) against the original invoice.
# The verified set is invoice_service.COMPARABLE_FIELDS.
VERIFY_FIELD_LABELS = {
    "invoice_number": "发票号码（数电票20位、老式票8位数字）",
    "invoice_type": "发票名称/类型（票头原文，如\"电子发票（普通发票）\"）",
    "invoice_code": "发票代码（10-12位数字；数电票无此项）",
    "issue_date": "开票日期（YYYY-MM-DD）",
    "buyer_name": "购买方名称（公司全称）",
    "buyer_tax_id": "购买方纳税人识别号（15-20位字母数字）",
    "buyer_address_phone": "购买方地址、电话（票面原文）",
    "buyer_bank_account": "购买方开户行及账号（票面原文）",
    "seller_name": "销售方名称（公司全称）",
    "seller_tax_id": "销售方纳税人识别号（15-20位字母数字）",
    "seller_address_phone": "销售方地址、电话（票面原文）",
    "seller_bank_account": "销售方开户行及账号（票面原文）",
    "item_name": "项目名称/货物名称（可含*分类*前缀）",
    "specification": "规格型号（明细行，选填）",
    "unit": "单位（明细行计量单位，选填）",
    "quantity": "数量（纯数字，选填）",
    "unit_price": "单价（不含税，纯数字，选填）",
    "total_with_tax": "价税合计（小写，纯数字，不含货币符号）",
    "total_with_tax_cn": "价税合计大写（汉字金额，如\"叁佰元整\"）",
    "amount": "金额，不含税合计（纯数字）",
    "tax_amount": "税额（纯数字，免税发票为0）",
    "tax_rate": "税率（如9%、免税）",
    "remark": "备注（票面原文，选填）",
    "drawer": "开票人（姓名，选填）",
}

INVOICE_VERIFY_SYSTEM_PROMPT = """你是一个专业的中国发票审核助手。
你会拿到发票的当前录入字段值，以及发票原件（图片或OCR识别文本）。
你的任务是逐字段核对录入值与原件内容是否一致。
你必须严格按照JSON格式返回结果，不要包含任何其他文字、解释或markdown代码块标记。"""

_verify_field_list = json.dumps(
    VERIFY_FIELD_LABELS, ensure_ascii=False, indent=2
)

INVOICE_VERIFY_PROMPT = f"""请审核以下中国发票的当前录入字段值。

## 当前录入字段值（待审核，null表示未录入）

{{current_values}}

字段含义说明：
{_verify_field_list}
{{ocr_section}}
## 审核要求

逐字段核对"当前录入字段值"与发票原件（图片，或下方OCR识别文本）：
- "correct"：录入值与原件一致
- "incorrect"：录入值与原件不一致或原件有值而录入缺失，必须给出 corrected_value（从原件读出的正确值，格式与录入规范一致：日期YYYY-MM-DD、金额纯数字不含货币符号、税率如"9%"或"免税"）
- "unsure"：原件上无法确认该字段（原件没有该信息或不可辨认），corrected_value 为 null

注意：
- 密码区乱码、印章文字不是任何字段的内容，不要据此判错。
- 选填字段（发票代码、地址电话、开户行及账号、规格型号、单位、数量、单价、备注、开票人等）
  录入为 null 且票面确实没有该栏时，判 correct，不要判 unsure。
- 价税合计大写按汉字逐字核对，不影响小写金额的判定。
- 文本两端空白、全半角差异、相同数值的小数位差异、同一天的日期格式差异不算错误。
- 项目名称仅相差开头的分类星号时视为一致，例如“运输服务*客运服务费”和“*运输服务*客运服务费”；核心文字、数字或分类内容不同仍须判错。
- 地址电话、开户行及账号、规格型号、单位、数量、单价、价税合计大写、备注、开票人属于提示项，仍须如实报告差异，不因其差异阻断审核。
- 发票类型、发票代码缺失或无法确认时仅提示；已有值与原件明确冲突时才阻断。
- 其余字段为关键项，不符或存疑时阻断。发票号码和代码必须保留前导零，不得猜测 O/0 等字符。

## 输出格式要求（必须严格遵守）

返回一个JSON对象：
{{
  "overall": "pass 或 issues（仅关键问题为 issues，提示项可为 pass；系统会重新计算）",
  "fields": [
    {{"field": "字段名", "status": "correct|incorrect|unsure", "corrected_value": "字符串或null", "reason": "简要说明（30字内）"}}
  ],
  "summary": "一句话审核结论（50字内）"
}}

fields 数组必须逐一覆盖上面"当前录入字段值"中列出的每个字段名，不能多也不能少。
corrected_value 仅在 status 为 incorrect 时非null。
只返回JSON对象："""

INVOICE_VERIFY_OCR_SECTION = """
## 发票OCR识别文本（辅助参考，行序可能打乱、含噪声）

{ocr_text}
"""
