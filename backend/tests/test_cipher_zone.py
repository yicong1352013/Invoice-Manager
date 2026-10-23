"""Tests for VAT cipher-zone (密码区) cleanup.

Business invariant: the left cipher column on paper VAT invoices gets
row-merged into seller-block lines by PaddleOCR and must never leak into
extracted fields (seller_name / item_name), while real values — tax IDs,
phone numbers, amounts, dates — must survive the cleanup untouched.

Samples below are the exact polluted lines found on uploaded invoices
#29/#30/#31 during the 2026-10-23 verification.
"""

from app.services.ocr_service import FieldExtractor, strip_cipher_tokens


# --- token-level cleanup ---------------------------------------------------------


def test_cipher_suffix_removed_from_seller_name_line():
    line = "名 称: 广东浩鲸科技有限公司 0368*/6217+41>614<9<8+/9/6>/"
    assert strip_cipher_tokens(line) == "名 称: 广东浩鲸科技有限公司"


def test_cipher_suffix_removed_but_tax_id_kept():
    line = "纳税人识别号: 91440300MA5EKPEL7P *//98*+0/0+>5200-*>-2-+>0+49"
    assert strip_cipher_tokens(line) == "纳税人识别号: 91440300MA5EKPEL7P"


def test_pure_cipher_line_becomes_empty():
    assert strip_cipher_tokens("039---/73+885/-/>92751-+36>-") == ""
    assert strip_cipher_tokens("/</3-71<5<792>5>7-241*+-38>*") == ""


def test_address_value_that_is_all_cipher_leaves_only_label():
    line = "地 址、电 话: 98160--07-<28*79<11+/>>4534<"
    assert strip_cipher_tokens(line) == "地 址、电 话:"


def test_real_values_survive():
    # pure-alnum tax IDs, single-symbol phones, amounts, dates, invoice numbers
    for line in [
        "纳税人识别号: 91440300MA5EKPEL7P",
        "开户行及账号: 中国银行深圳科技园支行 0755-88888888",
        "价税合计（大写） ￥壹佰零陆圆整",
        "合计 ¥100.00 税额 ¥6.00",
        "发票号码: 246813570139",
        "开票日期:2026年10月01日",
        "*信息技术服务*软件开发服务",
    ]:
        assert strip_cipher_tokens(line) == line, line


def test_short_symbol_runs_not_touched():
    # model numbers / year ranges: symbol-bearing but under the 16-char bar
    assert strip_cipher_tokens("规格: GD-2020-21-A") == "规格: GD-2020-21-A"


# --- end-to-end extraction on a polluted seller block -----------------------------


SELLER_BLOCK_LINES = [
    "购 名称:深圳市测试科技有限公司 销 名称:广东浩鲸科技有限公司 0368*/6217+41>614<9<8+/9/6>/",
    "纳税人识别号: 91440300MA5EKPEL7P *//98*+0/0+>5200-*>-2-+>0+49",
    "地 址、电 话: 98160--07-<28*79<11+/>>4534<",
    "开户行及账号: 2-<021+>0<01+76819174/*4-<-<",
    "发票号码: 246813570139",
    "开票日期:2026年10月01日",
    "*信息技术服务*软件开发服务",
    "合计 ¥100.00 税额 ¥6.00",
    "价税合计（大写） ¥106.00",
]


def _lines_meta(lines):
    return [
        {
            "text": strip_cipher_tokens(t),
            "confidence": 99,
            "bbox": [],
            "min_x": 0, "max_x": 400,
            "min_y": i * 20, "max_y": i * 20 + 15,
            "center_x": 200, "center_y": i * 20 + 7,
        }
        for i, t in enumerate(lines)
        if strip_cipher_tokens(t).strip()
    ]


def test_extraction_immune_to_cipher_pollution():
    text = "\n".join(l["text"] for l in _lines_meta(SELLER_BLOCK_LINES))
    fields = FieldExtractor().extract_fields(text, _lines_meta(SELLER_BLOCK_LINES))

    assert fields["seller_name"] == "广东浩鲸科技有限公司", fields["seller_name"]
    assert fields["buyer_name"] == "深圳市测试科技有限公司", fields["buyer_name"]
    # the only tax id on this sample sits inside the seller block, so label
    # proximity attributes it to the seller — the invariant is that it comes
    # out clean, without the cipher suffix that followed it on the OCR line
    assert fields["seller_tax_id"] == "91440300MA5EKPEL7P", fields["seller_tax_id"]
    assert fields["buyer_tax_id"] is None or fields["buyer_tax_id"] == "91440300MA5EKPEL7P"
    # item_name must be the *类别*品名 line, never a cipher fragment
    assert fields["item_name"] == "信息技术服务*软件开发服务", fields["item_name"]
    assert fields["invoice_number"] == "246813570139"
    assert fields["amount"] == "100.00"
    assert fields["tax_amount"] == "6.00"


# --- single-side label regression (invoice 13060593) --------------------------------
#
# The buyer block's vertical 购/买/方 label is interleaved with 密/码/区 and
# never matches, while the footer "销 售 方:（章）" DOES match as a seller
# label. With only one side labeled, proximity classification used to push
# BOTH 名称 values and BOTH tax ids onto the seller side — duplicating the
# buyer's data into the seller fields.

INVOICE_13060593_TEXT = """广东增值税电子普通发票 发票代码:044002400112
发票号码:13060593
﹊﹋﹌ 开票日期:2025年09月13日
校 验 码:12839 43449 64643 96198
机器编号: 499097989374
名 称: 广东浩鲸科技有限公司
购 密
纳税人识别号: 91440300MA5EKPEL7P
买 码
地 址、电 话:
方 区
开户行及账号:
项目名称 车牌号 类型 通行日期起 通行日期止 金 额 税率 税 额
*经营租赁*通行费 湘ALQ163 客车 20250912 20250912 32.92 3% 0.99
合 计 ￥32.92 ￥0.99
价税合计（大写） 叁拾叁元玖角壹分 （小写） ￥33.91
名 称:广深珠高速公路有限公司
销 备
纳税人识别号:914400006174026084
售
地 址、电 话:广东省广州市越秀区竹丝岗大马路10号之一首层自编G号0769-85555585
方 注
开户行及账号:中国银行虎门支行725057753788
收 款 人: 黄绍芬 复 核: 黄绍芬 开 票 人: 谢慧洁 销 售 方:（章）"""


def test_single_side_label_does_not_duplicate_buyer_into_seller():
    fields = FieldExtractor().extract_fields(INVOICE_13060593_TEXT, [])
    assert fields["buyer_name"] == "广东浩鲸科技有限公司", fields["buyer_name"]
    assert fields["buyer_tax_id"] == "91440300MA5EKPEL7P", fields["buyer_tax_id"]
    assert fields["seller_name"] == "广深珠高速公路有限公司", fields["seller_name"]
    assert fields["seller_tax_id"] == "914400006174026084", fields["seller_tax_id"]
    assert fields["invoice_number"] == "13060593"
    assert fields["amount"] == "32.92"
    assert fields["tax_amount"] == "0.99"
    assert fields["total_with_tax"] == "33.91"
