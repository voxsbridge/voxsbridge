"""Branded invoice PDF (A4) for VoxsBridge."""
import html
import os
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT, TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle)

NAVY = colors.HexColor('#0b1530')
NAVY2 = colors.HexColor('#16224a')
BLUE = colors.HexColor('#2563eb')
CYAN = colors.HexColor('#06b6d4')
INK = colors.HexColor('#111827')
MUTED = colors.HexColor('#6b7280')
LINE = colors.HexColor('#e5e7eb')
SOFT = colors.HexColor('#f5f7fb')
GREEN = colors.HexColor('#059669')

# Page = the VoxsBridge letterhead image (aspect kept, so nothing is stretched).
W = 595.27
LH_W, LH_H = 1102, 1427
H = W * LH_H / LH_W
KX = W / LH_W                      # image pixel -> pt
STATUS_COLORS = {'paid': ('#d1fae5', '#065f46'), 'sent': ('#dbeafe', '#1e40af'),
                 'draft': ('#f3f4f6', '#374151'), 'void': ('#fee2e2', '#991b1b')}
PURPLE = colors.HexColor('#2563ff')   # VoxsBridge blue
HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(os.path.dirname(HERE), 'frontend', 'static')
LETTERHEAD = os.path.join(STATIC, 'img', 'letterhead.png')
_wa = os.getenv('SUPPORT_WHATSAPP', '447476629105').strip().lstrip('+')
CONTACT_PHONE = os.getenv('INVOICE_PHONE', '+' + _wa if _wa else '')
METHOD_LABELS = {'bank': 'Bank transfer', 'card': 'Card', 'usdt': 'Crypto (USDT)', 'other': 'Other'}

STAMP_X, STAMP_Y = 150, 372
_fonts = {}


def _font(name, file, fallback):
    if name in _fonts:
        return _fonts[name]
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        pdfmetrics.registerFont(TTFont(name, os.path.join(STATIC, 'fonts', file)))
        _fonts[name] = name
    except Exception:
        _fonts[name] = fallback
    return _fonts[name]


def _fit_lines(text, font, size, width):
    """Wrap text into at most 2 lines that fit `width`; shrink the size if needed."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    words = (text or '').split()
    while size >= 5.2:
        lines, cur = [], ''
        for w in words:
            trial = (cur + ' ' + w).strip()
            if stringWidth(trial, font, size) <= width or not cur:
                cur = trial
            else:
                lines.append(cur); cur = w
        lines.append(cur)
        if len(lines) <= 2 and all(stringWidth(l, font, size) <= width for l in lines):
            return lines, size
        size -= 0.4
    return [(text or '')[:22]], 5.2


def bank_details():
    """Bank account shown under "How to pay" (override in Render if it changes)."""
    return {
        'name': os.getenv('INVOICE_BANK_ACCOUNT_NAME', 'VoxsBridge').strip(),
        'bank': os.getenv('INVOICE_BANK_NAME', '').strip(),
        'number': os.getenv('INVOICE_BANK_ACCOUNT_NUMBER', '').strip(),
    }


def company_lines():
    raw = os.getenv('INVOICE_FROM_LINES', 'WhatsApp: ' + CONTACT_PHONE)
    return [x.strip() for x in raw.split('|') if x.strip()]


def _e(v):
    return html.escape(str(v if v is not None else ''))


def _pkr(v):
    try:
        return 'PKR {:,.0f}'.format(float(v or 0))
    except (TypeError, ValueError):
        return 'PKR 0'


def _date(d):
    try:
        return d.strftime('%d %b %Y')
    except AttributeError:
        return str(d or '')


def build_invoice_pdf(inv, items, logo_path):
    buf = BytesIO()
    status = str(inv.get('status') or 'draft').lower()

    def page(canvas, doc):
        canvas.saveState()
        if os.path.exists(LETTERHEAD):
            canvas.drawImage(LETTERHEAD, 0, 0, width=W, height=H)
        reg = _font('P-R', 'Poppins-Regular.ttf', 'Helvetica')
        med = _font('P-M', 'Poppins-Medium.ttf', 'Helvetica-Bold')
        bold = _font('P-B', 'Poppins-Bold.ttf', 'Helvetica-Bold')
        # Header strip: the proprietor / address blocks were removed from the letterhead image.
        # Customer name goes where the proprietor's name was; the phone is ours.
        ink, gray = colors.HexColor('#0f1a44'), colors.HexColor('#4b5273')
        x_name, x_phone = 82 * KX, 276 * KX
        canvas.setFillColor(gray); canvas.setFont(reg, 6.6)
        canvas.drawString(x_name, H - 254 * KX, 'Billed To')
        canvas.drawString(x_phone, H - 254 * KX, 'Phone / WhatsApp')
        lines, size = _fit_lines(str(inv.get('customer_name') or ''), med, 7.3, 118 * KX)
        canvas.setFillColor(ink); canvas.setFont(med, size)
        y0 = H - 275 * KX if len(lines) == 1 else H - 270 * KX
        for ln in lines:
            canvas.drawString(x_name, y0, ln); y0 -= size + 1.2
        canvas.setFont(med, 7.5)
        canvas.drawString(x_phone, H - 275 * KX, CONTACT_PHONE)
        # PAID stamp
        if status == 'paid':
            canvas.saveState()
            canvas.translate(STAMP_X, STAMP_Y)
            canvas.rotate(14)
            canvas.setStrokeColor(colors.HexColor('#10b981')); canvas.setFillColor(colors.HexColor('#10b981'))
            canvas.setLineWidth(3)
            canvas.setFillAlpha(0.5); canvas.setStrokeAlpha(0.6)
            canvas.roundRect(-74, -25, 148, 50, 9, fill=0, stroke=1)
            canvas.setFont(bold, 30)
            canvas.drawCentredString(0, -11, 'PAID')
            canvas.restoreState()
        canvas.setFillColor(colors.HexColor('#6b7280')); canvas.setFont(reg, 7)
        canvas.drawRightString(W - 40, 172, 'Page %d' % doc.page)
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=(W, H), leftMargin=36, rightMargin=36, topMargin=172, bottomMargin=162,
                            title=str(inv.get('invoice_number') or 'Invoice'), author='VoxsBridge')
    reg = _font('P-R', 'Poppins-Regular.ttf', 'Helvetica')
    med = _font('P-M', 'Poppins-Medium.ttf', 'Helvetica-Bold')
    bold = _font('P-B', 'Poppins-Bold.ttf', 'Helvetica-Bold')
    NAVYD = colors.HexColor('#0f1a44')
    SOFTP = colors.HexColor('#eaf1ff')
    BORDER = colors.HexColor('#cfdcfb')
    CW = W - 72                                   # content width
    st = lambda name, **kw: ParagraphStyle(name, **kw)
    lbl = st('lbl', fontName=reg, fontSize=8.6, textColor=colors.HexColor('#3b4262'), leading=12)
    vl = st('vl', fontName=med, fontSize=8.8, textColor=NAVYD, leading=12)
    vlr = st('vlr', parent=vl, alignment=TA_RIGHT)
    h2 = st('h2', fontName=bold, fontSize=10.5, textColor=NAVYD, leading=14)
    th = st('th', fontName=med, fontSize=8.6, textColor=colors.white, leading=11, alignment=1)
    thl = st('thl', parent=th, alignment=0)
    td = st('td', fontName=reg, fontSize=8.8, textColor=NAVYD, leading=12)
    tdc = st('tdc', parent=td, alignment=1)
    note = st('note', fontName=reg, fontSize=7.4, textColor=colors.HexColor('#2b3355'), leading=9.4)
    inv_lbl = st('il', fontName=med, fontSize=8.6, textColor=colors.white, leading=12)
    inv_val = st('iv', fontName=reg, fontSize=8.6, textColor=colors.white, leading=12, alignment=TA_RIGHT)
    status_ = status
    total = float(inv.get('total_pkr') or 0)
    received = total if status_ == 'paid' else min(total, float(inv.get('paid_pkr') or 0))
    remaining = max(0.0, total - received)
    story = []

    def card(title, rows_, width, header_fill=SOFTP):
        head_p = Paragraph('<font color="#2563ff">&#9679;</font>&nbsp;&nbsp;' + _e(title), h2)
        t = Table([[head_p]] + rows_, colWidths=[width])
        t.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), header_fill), ('BOX', (0, 0), (-1, -1), 0.8, BORDER),
                               ('LEFTPADDING', (0, 0), (-1, -1), 10), ('RIGHTPADDING', (0, 0), (-1, -1), 10),
                               ('TOPPADDING', (0, 0), (-1, 0), 7), ('BOTTOMPADDING', (0, 0), (-1, 0), 7),
                               ('TOPPADDING', (0, 1), (-1, -1), 1), ('BOTTOMPADDING', (0, 1), (-1, -1), 1),
                               ('BACKGROUND', (0, 1), (-1, -1), colors.white)]))
        return t

    # --- Bill To (left) + Invoice box (right)
    lw, rw = CW * 0.585, CW * 0.395
    brow = lambda a, b: Table([[Paragraph(_e(a), lbl), Paragraph(_e(b), vl)]], colWidths=[lw * 0.36, lw * 0.64 - 20])
    items_b = [('Customer Name:', inv.get('customer_name')), ('Email:', inv.get('customer_email')),
               ('Contact:', inv.get('contact_phone')), ('Address:', inv.get('contact_address'))]
    brows = []
    for a, b in items_b:
        if b:
            r = brow(a, b)
            r.setStyle(TableStyle([('LEFTPADDING', (0, 0), (-1, -1), 0), ('TOPPADDING', (0, 0), (-1, -1), 1), ('BOTTOMPADDING', (0, 0), (-1, -1), 1)]))
            brows.append([r])
    billbox = card('Bill To:', brows or [[Paragraph('-', vl)]], lw)
    iv_rows = [('Invoice No:', inv.get('invoice_number')), ('Invoice Date:', _date(inv.get('issue_date'))),
               ('Due Date:', _date(inv.get('due_date')))]
    ivt = Table([[Paragraph(_e(a), inv_lbl), Paragraph(_e(b), inv_val)] for a, b in iv_rows], colWidths=[rw * 0.42, rw * 0.58 - 24])
    ivt.setStyle(TableStyle([('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                             ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3)]))
    right = Table([[Paragraph('INVOICE', st('big', fontName=bold, fontSize=20, textColor=NAVYD, leading=24, alignment=1))], [ivt]], colWidths=[rw])
    right.setStyle(TableStyle([('BACKGROUND', (0, 0), (0, 0), SOFTP), ('BACKGROUND', (0, 1), (0, 1), NAVYD),
                               ('BOX', (0, 0), (-1, -1), 0.8, BORDER),
                               ('TOPPADDING', (0, 0), (0, 0), 6), ('BOTTOMPADDING', (0, 0), (0, 0), 6),
                               ('LEFTPADDING', (0, 1), (0, 1), 12), ('RIGHTPADDING', (0, 1), (0, 1), 12),
                               ('TOPPADDING', (0, 1), (0, 1), 8), ('BOTTOMPADDING', (0, 1), (0, 1), 8)]))
    top = Table([[billbox, right]], colWidths=[lw + 8, rw], style=[('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (0, 0), 8), ('RIGHTPADDING', (1, 0), (1, 0), 0),
                ('TOPPADDING', (0, 0), (-1, -1), 0), ('BOTTOMPADDING', (0, 0), (-1, -1), 0)])
    story += [top, Spacer(1, 8)]

    # --- items
    rows = [[Paragraph('#', th), Paragraph('Description', thl), Paragraph('Quantity', th),
             Paragraph('Unit Price (PKR)', th), Paragraph('Amount (PKR)', th)]]
    for n, it in enumerate(items, 1):
        rows.append([Paragraph(str(n), tdc), Paragraph(_e(it.get('description')), td), Paragraph(_e(it.get('qty')), tdc),
                     Paragraph('{:,.0f}'.format(float(it.get('unit_price_pkr') or 0)), tdc),
                     Paragraph('{:,.0f}'.format(float(it.get('amount_pkr') or 0)), tdc)])
    t = Table(rows, colWidths=[28, CW - 28 - 60 - 100 - 100, 60, 100, 100], repeatRows=1)
    t.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), PURPLE), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                           ('TOPPADDING', (0, 0), (-1, 0), 8), ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
                           ('TOPPADDING', (0, 1), (-1, -1), 6), ('BOTTOMPADDING', (0, 1), (-1, -1), 6),
                           ('LEFTPADDING', (0, 0), (-1, -1), 8), ('RIGHTPADDING', (0, 0), (-1, -1), 8),
                           ('BOX', (0, 0), (-1, -1), 0.8, BORDER), ('INNERGRID', (0, 0), (-1, -1), 0.5, BORDER),
                           ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f6f9ff')])]))
    story += [t, Spacer(1, 6)]

    # --- totals (right)
    tot = []
    if float(inv.get('discount_pkr') or 0):
        tot.append(('Discount', '- ' + _pkr(inv.get('discount_pkr'))))
    if float(inv.get('tax_pkr') or 0) or float(inv.get('tax_percent') or 0):
        tot.append(('Tax (%s%%)' % '{:g}'.format(float(inv.get('tax_percent') or 0)), _pkr(inv.get('tax_pkr'))))
    tot += [('Total Amount', _pkr(total)), ('Payment Received', _pkr(received))]
    trows = [[Paragraph(_e(a), vl), Paragraph(_e(b), st('tv', parent=vl, alignment=TA_RIGHT, fontName=bold, fontSize=10))] for a, b in tot]
    trows.append([Paragraph('Remaining Payment', st('rl', parent=vl, textColor=colors.white)),
                  Paragraph(_e(_pkr(remaining)), st('rv', parent=vl, textColor=colors.white, alignment=TA_RIGHT, fontName=bold, fontSize=10.5))])
    tw = 250
    totals = Table(trows, colWidths=[tw * 0.5, tw * 0.5])
    totals.setStyle(TableStyle([('BOX', (0, 0), (-1, -1), 0.8, BORDER), ('LINEBELOW', (0, 0), (-1, -2), 0.5, BORDER),
                                ('BACKGROUND', (0, 0), (-1, -2), colors.white), ('BACKGROUND', (0, -1), (-1, -1), PURPLE),
                                ('TOPPADDING', (0, 0), (-1, -1), 4), ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                                ('LEFTPADDING', (0, 0), (-1, -1), 10), ('RIGHTPADDING', (0, 0), (-1, -1), 10),
                                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE')]))
    wrap = Table([['', totals]], colWidths=[CW - tw, tw], style=[('LEFTPADDING', (0, 0), (-1, -1), 0),
                 ('RIGHTPADDING', (0, 0), (-1, -1), 0), ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                 ('TOPPADDING', (0, 0), (-1, -1), 0), ('BOTTOMPADDING', (0, 0), (-1, -1), 0)])
    story += [wrap, Spacer(1, 8)]

    # --- payment details (left) + important notes (right). Bank account ONLY on bank-transfer invoices.
    method = str(inv.get('payment_method') or 'bank').lower()
    pw = CW * 0.5 - 6
    kv = lambda a, b, w: Table([[Paragraph(_e(a), lbl), Paragraph('<b>%s</b>' % _e(b), vl)]], colWidths=[w * 0.40, w * 0.60 - 20],
                               style=[('LEFTPADDING', (0, 0), (-1, -1), 0), ('TOPPADDING', (0, 0), (-1, -1), 2), ('BOTTOMPADDING', (0, 0), (-1, -1), 2)])
    if method == 'card':
        card_name = str(inv.get('card_name') or '').strip() or 'Card'
        prow = [[kv('Payment Method:', 'Card', pw)], [kv('Card:', card_name, pw)]]
        if inv.get('renewal_date'):
            prow.append([kv('Renewal Date:', _date(inv.get('renewal_date')), pw)])
        ptitle = 'Payment Details - Card'
    elif method == 'bank' and bank_details()['number']:
        b = bank_details()
        prow = [[Paragraph('You may complete your payment via bank transfer:', note)],
                [kv('Account Name:', b['name'], pw)], [kv('Bank:', b['bank'], pw)], [kv('Account Number:', b['number'], pw)]]
        if remaining > 0:
            prow.append([kv('Amount:', _pkr(remaining), pw)])
        ptitle = 'Payment Details - Bank Transfer'
    else:
        prow = [[kv('Payment Method:', METHOD_LABELS.get(method, method.title()), pw)],
                [Paragraph('Payment details are shared on WhatsApp: %s' % _e(CONTACT_PHONE), note)]]
        ptitle = 'Payment Details'
    paybox = card(ptitle, prow, pw)
    note_lines = [x.strip() for x in str(inv.get('notes') or '').splitlines() if x.strip()]
    terms_txt = str(inv.get('terms') or '').strip()
    if terms_txt and status_ != 'paid':
        note_lines.append(terms_txt.replace('\n', ' '))
    nrows = []
    for i, line in enumerate(note_lines[:6], 1):
        nrows.append([Table([[Paragraph('<font color="white"><b>%d</b></font>' % i, st('nn', fontName=bold, fontSize=7.5, leading=9, alignment=1)),
                              Paragraph(_e(line), note)]], colWidths=[15, pw - 20 - 21],
                            style=[('BACKGROUND', (0, 0), (0, 0), NAVYD), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                                   ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (0, 0), 0),
                                   ('LEFTPADDING', (1, 0), (1, 0), 7), ('TOPPADDING', (0, 0), (-1, -1), 2), ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
                                   ('ROUNDEDCORNERS', [7, 7, 7, 7])]) ])
    if status_ != 'void' or nrows:
        cells = [paybox if status_ != 'void' else '', card('Important Notes', nrows, pw) if nrows else '']
        if not nrows:
            cells = [paybox]; cws = [CW]
        elif status_ == 'void':
            cells = [card('Important Notes', nrows, CW)]; cws = [CW]
        else:
            cws = [pw + 6, pw + 6]
        story.append(Table([cells], colWidths=cws, style=[('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 0),
                     ('RIGHTPADDING', (0, 0), (0, 0), 6), ('RIGHTPADDING', (1, 0), (1, 0), 0),
                     ('TOPPADDING', (0, 0), (-1, -1), 0), ('BOTTOMPADDING', (0, 0), (-1, -1), 0)]))

    doc.build(story, onFirstPage=page, onLaterPages=page)
    return buf.getvalue()
