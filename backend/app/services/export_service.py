"""Export and print builders: Word/PDF detail exports and original-file merge.

Pure functions operating on invoice-like rows so they are unit-testable
without a database. Chinese-capable font resolution covers Windows dev
machines and the Debian container (fonts-noto-cjk).
"""

import logging
import os
from decimal import Decimal
from io import BytesIO
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

EXPORT_HEADERS = [
    '发票号码', '开票日期', '发票类型', '购买方名称', '购买方纳税人识别号',
    '销售方名称', '销售方纳税人识别号', '项目名称',
    '金额', '税额', '价税合计', '税率', '备注',
    '状态', '归属人', '文件名', '创建时间',
]

# Candidate CJK fonts for PDF rendering, in preference order
_CJK_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",            # Windows 微软雅黑
    r"C:\Windows\Fonts\simhei.ttf",          # Windows 黑体
    r"C:\Windows\Fonts\simsun.ttc",          # Windows 宋体
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",   # Debian fonts-noto-cjk
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",    # macOS
]


def compute_totals(invoices: Iterable[Any]) -> Dict[str, Decimal]:
    """Sum the monetary columns (missing values count as 0)."""
    totals = {
        'amount': Decimal(0),
        'tax_amount': Decimal(0),
        'total_with_tax': Decimal(0),
    }
    for inv in invoices:
        totals['amount'] += inv.amount or Decimal(0)
        totals['tax_amount'] += inv.tax_amount or Decimal(0)
        totals['total_with_tax'] += inv.total_with_tax or Decimal(0)
    return totals


def find_cjk_font() -> Optional[Tuple[str, str]]:
    """Return (path, subfont_index) of the first available CJK font, or None."""
    for path in _CJK_FONT_CANDIDATES:
        if os.path.exists(path):
            # .ttc collections need a subfont index for reportlab
            return path, (0 if path.lower().endswith('.ttc') else -1)
    return None


def build_word_export(invoices: List[Any], totals: Dict[str, Decimal]) -> BytesIO:
    """Build a Word document: title + detail table + totals row."""
    from docx import Document
    from docx.shared import Pt
    from docx.oxml.ns import qn

    document = Document()
    title = document.add_heading('发票明细', level=1)
    for run in title.runs:
        run.font.name = 'Microsoft YaHei'
        run._element.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')

    table = document.add_table(rows=1, cols=len(EXPORT_HEADERS))
    table.style = 'Table Grid'

    def fill_row(cells, values, bold=False):
        for cell, value in zip(cells, values):
            cell.text = str(value)
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    run.font.size = Pt(9)
                    run.font.name = 'Microsoft YaHei'
                    run._element.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')
                    if bold:
                        run.font.bold = True

    fill_row(table.rows[0].cells, EXPORT_HEADERS, bold=True)

    for inv in invoices:
        fill_row(table.add_row().cells, [
            inv.invoice_number or '',
            str(inv.issue_date) if inv.issue_date else '',
            inv.invoice_type or '',
            inv.buyer_name or '',
            inv.buyer_tax_id or '',
            inv.seller_name or '',
            inv.seller_tax_id or '',
            inv.item_name or '',
            inv.amount if inv.amount is not None else '',
            inv.tax_amount if inv.tax_amount is not None else '',
            inv.total_with_tax if inv.total_with_tax is not None else '',
            inv.tax_rate or '',
            inv.remark or '',
            inv.status.value if inv.status else '',
            inv.owner or '',
            inv.file_name or '',
            inv.created_at.strftime('%Y-%m-%d %H:%M:%S') if inv.created_at else '',
        ])

    # Totals row: label in the 项目名称 column, sums under the money columns
    totals_row = ['合计', '', '', '', '', '', '', '', totals['amount'], totals['tax_amount'], totals['total_with_tax'], '', '', '', '', '', '']
    fill_row(table.add_row().cells, totals_row, bold=True)

    output = BytesIO()
    document.save(output)
    output.seek(0)
    return output


def build_pdf_export(invoices: List[Any], totals: Dict[str, Decimal]) -> BytesIO:
    """Build a PDF: title + landscape detail table + totals row."""
    from xml.sax.saxutils import escape
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle

    font_name = 'Helvetica'  # fallback (no CJK)
    font_tuple = find_cjk_font()
    if font_tuple:
        path, subfont_index = font_tuple
        try:
            if subfont_index >= 0:
                pdfmetrics.registerFont(TTFont('CJK', path, subfontIndex=subfont_index))
            else:
                pdfmetrics.registerFont(TTFont('CJK', path))
            font_name = 'CJK'
        except Exception as e:  # fall back to builtin font
            logger.warning(f"CJK font registration failed ({path}): {e}")
    else:
        logger.warning("No CJK font found; PDF export may show garbled Chinese. "
                       "Install fonts-noto-cjk (Debian) or a system CJK font.")

    output = BytesIO()
    doc = SimpleDocTemplate(
        output, pagesize=landscape(A4),
        leftMargin=10 * mm, rightMargin=10 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
        title='发票明细',
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('CNTitle', parent=styles['Title'], fontName=font_name, fontSize=16)
    cell_style = ParagraphStyle(
        'CNCell', parent=styles['BodyText'], fontName=font_name, fontSize=7.5, leading=9.5,
        wordWrap='CJK',
    )

    story = [Paragraph('发票明细', title_style)]

    data = [[Paragraph(h, cell_style) for h in EXPORT_HEADERS]]
    for inv in invoices:
        data.append([
            Paragraph(escape(str(x if x is not None and x != '' else '-')), cell_style)
            for x in [
                inv.invoice_number, inv.issue_date, inv.invoice_type,
                inv.buyer_name, inv.buyer_tax_id,
                inv.seller_name, inv.seller_tax_id, inv.item_name, inv.amount,
                inv.tax_amount, inv.total_with_tax, inv.tax_rate, inv.remark,
                inv.status.value if inv.status else '', inv.owner, inv.file_name,
                inv.created_at.strftime('%Y-%m-%d %H:%M') if inv.created_at else '',
            ]
        ])
    data.append(
        [Paragraph('<b>合计</b>', cell_style), '', '', '', '', '', '', ''] +
        [
            Paragraph(f"<b>{totals['amount']}</b>", cell_style),
            Paragraph(f"<b>{totals['tax_amount']}</b>", cell_style),
            Paragraph(f"<b>{totals['total_with_tax']}</b>", cell_style),
        ] +
        ['', '', '', '', '', '']
    )

    table = Table(
        data, colWidths=[doc.width / len(EXPORT_HEADERS)] * len(EXPORT_HEADERS),
        repeatRows=1, splitInRow=1,
    )
    table.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.4, colors.grey),
        ('BACKGROUND', (0, 0), (-1, 0), colors.Color(0.92, 0.92, 0.92)),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))
    story.append(table)
    doc.build(story)
    output.seek(0)
    return output


def merge_original_files(files: List[Tuple[str, bytes]]) -> bytes:
    """Merge invoice original files (pdf/image) into one PDF, keeping order.

    Args:
        files: ordered list of (file_type, file_bytes)

    Returns:
        Combined PDF bytes.

    Raises:
        ValueError: when no file could be included.
    """
    from pypdf import PdfReader, PdfWriter
    from PIL import Image

    writer = PdfWriter()
    included = 0
    for file_type, file_data in files:
        try:
            if file_type == 'pdf':
                reader = PdfReader(BytesIO(file_data))
                for page in reader.pages:
                    writer.add_page(page)
                included += 1
            elif file_type in ('jpg', 'jpeg', 'png'):
                buf = BytesIO()
                with Image.open(BytesIO(file_data)) as image:
                    rgb = image.convert('RGB')
                    rgb.save(buf, format='PDF', resolution=150.0)
                for page in PdfReader(buf).pages:
                    writer.add_page(page)
                included += 1
            else:
                logger.warning(f"print-merge: unsupported file type {file_type}, skipped")
        except Exception as e:
            logger.error(f"print-merge: failed to include a {file_type} file: {e}")

    if included == 0:
        raise ValueError("没有可打印的原始文件")

    output = BytesIO()
    writer.write(output)
    return output.getvalue()
