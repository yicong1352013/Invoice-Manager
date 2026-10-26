from app.services.ocr_service import FieldExtractor


def _line(text: str, x: float, y: float, width: float = 200, height: float = 12) -> dict:
    return {
        "text": text,
        "min_x": x,
        "max_x": x + width,
        "min_y": y,
        "max_y": y + height,
        "center_x": x + width / 2,
        "center_y": y + height / 2,
    }


def test_extract_buyer_seller_spaced_labels_text():
    text = (
        "电子发票（普通发票）\n"
        "发票号码：25952000000233223073\n"
        "开票日期：2025年11月05日\n"
        "购 名称：示例买方科技有限公司 销 名称：示例卖方服务有限公司分公司\n"
        "买 售\n"
        "方 方\n"
        "信 统一社会信用代码/纳税人识别号：123456789012345 "
        "信 统一社会信用代码/纳税人识别号：987654321098765432\n"
    )

    extractor = FieldExtractor()
    fields = extractor.extract_fields(text)

    assert fields["buyer_name"] == "示例买方科技有限公司"
    assert fields["seller_name"] == "示例卖方服务有限公司分公司"
    assert fields["buyer_tax_id"] == "123456789012345"
    assert fields["seller_tax_id"] == "987654321098765432"


def test_extract_buyer_seller_spaced_labels_lines():
    lines = [
        _line("购 名称：示例买方科技有限公司", x=10, y=10),
        _line("统一社会信用代码/纳税人识别号：123456789012345", x=10, y=24),
        _line("销 名称：示例卖方服务有限公司分公司", x=10, y=60),
        _line("统一社会信用代码/纳税人识别号：987654321098765432", x=10, y=74),
    ]
    text = "\n".join(line["text"] for line in lines)

    extractor = FieldExtractor()
    fields = extractor.extract_fields(text, lines)

    assert fields["buyer_name"] == "示例买方科技有限公司"
    assert fields["seller_name"] == "示例卖方服务有限公司分公司"
    assert fields["buyer_tax_id"] == "123456789012345"
    assert fields["seller_tax_id"] == "987654321098765432"


def test_empty_buyer_address_and_bank_do_not_inherit_seller_values():
    lines = [
        _line('购买方', 0, 10),
        _line('地址、电话：', 10, 30),
        _line('开户行及账号：', 10, 50),
        _line('销售方', 0, 130),
        _line('地址、电话：示例路8号 010-12345678', 10, 150),
        _line('开户行及账号：示例银行 1234567890123456', 10, 170),
    ]
    text = '\n'.join(line['text'] for line in lines)
    for metadata in (lines, []):
        fields = FieldExtractor().extract_fields(text, metadata)
        assert {name: fields.get(name) for name in (
            'buyer_address_phone', 'buyer_bank_account', 'seller_address_phone', 'seller_bank_account'
        )} == {
            'buyer_address_phone': None, 'buyer_bank_account': None,
            'seller_address_phone': '示例路8号 010-12345678',
            'seller_bank_account': '示例银行 1234567890123456',
        }


def test_unlabeled_or_interleaved_plain_text_does_not_guess_address_owner():
    for text in ('发票\n地址、电话：示例路8号\n开户行及账号：示例银行1234',
                 '发票\n购买方 销售方\n地址、电话：示例路8号\n开户行及账号：示例银行1234'):
        fields = FieldExtractor().extract_fields(text)
        assert all(fields.get(name) is None for name in (
            'buyer_address_phone', 'buyer_bank_account', 'seller_address_phone', 'seller_bank_account'
        ))


def test_positioned_words_keep_vertical_party_labels_and_split_bank_values():
    lines = [
        *[_line(char, 0, 10 + i * 12, width=10) for i, char in enumerate('购买方')],
        _line('地', 20, 50, width=10), _line('址、电', 35, 50, width=30), _line('话:', 70, 50, width=20),
        _line('开户行及账号:', 20, 70, width=80),
        *[_line(char, 0, 130 + i * 12, width=10) for i, char in enumerate('销售方')],
        _line('地', 20, 170, width=10), _line('址、电', 35, 170, width=30), _line('话:', 70, 170, width=20),
        _line('示例路8号', 95, 170, width=60), _line('010-12345678', 160, 170, width=80),
        _line('开户行及账号:示例银行', 20, 190, width=140), _line('1234567890123456', 165, 190, width=100),
    ]
    fields = FieldExtractor().extract_fields('电子普通发票\n' + '\n'.join(line['text'] for line in lines), lines)
    assert (fields.get('buyer_address_phone'), fields.get('buyer_bank_account')) == (None, None)
    assert fields.get('seller_address_phone') == '示例路8号 010-12345678'
    assert fields.get('seller_bank_account') == '示例银行 1234567890123456'
