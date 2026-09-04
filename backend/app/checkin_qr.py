"""現場報到 QR：產生 token，並畫成一張可以直接列印的卡片。

執秘在議程編輯頁按一次「報到 QR」，這裡產生 token、畫圖，兩者都存進
event_checkin_qr（見 db.ensure_event_checkin_qr_table）。社友到現場掃它，
POST /checkin 由 token 反查是哪一場活動 —— 所以掃的人不必先選活動，也不會
報到到隔壁那一場去。

QR 內容故意加上 RC3523-CHECKIN: 前綴：/checkin 同時收「社友掃活動碼」與
「主委掃社友碼」兩種內容，靠這個前綴分辨，不必猜字串長相。

圖上的中文字沿用 agenda_pdf 找字型的那一套（它已經處理過「假 CJK 字型缺漢字」
的問題）。找不到字型只是少了下面那兩行說明，QR 本身照常能掃 —— 報到不該因為
主機沒裝中文字型就整個停擺。
"""
import io
import logging
import secrets

import qrcode
from PIL import Image, ImageDraw, ImageFont

from . import agenda_pdf

logger = logging.getLogger(__name__)

PREFIX = "RC3523-CHECKIN:"

_QR_PX = 760          # QR 本身的邊長
_MARGIN = 60
_TITLE_PX = 40
_SUB_PX = 30


def new_token() -> str:
    return secrets.token_urlsafe(16)


def payload_for(token: str) -> str:
    return PREFIX + token


def token_from_payload(value: str) -> str | None:
    """掃到的字串若是活動報到碼，取出 token；不是就回 None（那是社友的 userId）。"""
    value = (value or "").strip()
    return value[len(PREFIX):].strip() if value.startswith(PREFIX) else None


def _font(size: int) -> ImageFont.FreeTypeFont | None:
    path = agenda_pdf.font_path()
    if path is None:
        return None
    try:
        return ImageFont.truetype(str(path), size)
    except Exception:
        # .ttc 之類 PIL 載不動的就算了，寧可少兩行字也不要整張圖產不出來。
        logger.warning("checkin_qr: cannot load font %s", path, exc_info=True)
        return None


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    left, _, right, _ = draw.textbbox((0, 0), text, font=font)
    return right - left


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int, max_lines: int) -> list[str]:
    """把一行字折成幾行，超過就截掉加「…」。

    中文沒有空白可以斷，所以一個字一個字量寬度 —— 公文標題動輒四十幾個字，不折
    行的話會直接畫出卡片外面。"""
    lines: list[str] = []
    cur = ""
    for ch in text:
        if _text_width(draw, cur + ch, font) <= max_width:
            cur += ch
            continue
        if not cur:                      # 單一個字就超寬（字級過大），硬放不折
            cur = ch
            continue
        lines.append(cur)
        cur = ch
        if len(lines) == max_lines:
            break
    if len(lines) < max_lines and cur:
        lines.append(cur)
    elif len(lines) == max_lines and cur:
        last = lines[-1]
        while last and _text_width(draw, last + "…", font) > max_width:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines


def render_png(payload: str, title: str = "", subtitle: str = "") -> bytes:
    """一張白底卡片：QR 置中，下面是活動名稱與日期地點。回傳 PNG bytes。"""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    code = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    code = code.resize((_QR_PX, _QR_PX), Image.NEAREST)

    width = _QR_PX + _MARGIN * 2
    text_width = _QR_PX
    # 先在一張暫時的畫布上量字，才知道折成幾行、卡片要多高。
    ruler = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    blocks: list[tuple[list[str], object, int]] = []
    for text, size, max_lines in ((title, _TITLE_PX, 2), (subtitle, _SUB_PX, 1)):
        font = _font(size) if text else None
        if font is None:
            continue
        blocks.append((_wrap(ruler, text, font, text_width, max_lines), font, size))

    text_h = sum((size + 18) * len(lines) for lines, _, size in blocks)
    height = _QR_PX + _MARGIN * 2 + text_h

    card = Image.new("RGB", (width, height), "white")
    card.paste(code, (_MARGIN, _MARGIN))
    draw = ImageDraw.Draw(card)

    y = _MARGIN + _QR_PX + 12
    for lines, font, size in blocks:
        for line in lines:
            draw.text(((width - _text_width(draw, line, font)) / 2, y),
                      line, font=font, fill=(15, 23, 42))
            y += size + 18

    buf = io.BytesIO()
    card.save(buf, format="PNG")
    return buf.getvalue()
