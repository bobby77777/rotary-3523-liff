"""Render an event's 議程 (agenda) as a real **vector** PDF.

The calendar editor used to rasterize the agenda in the browser (html2pdf →
html2canvas screenshot) and upload the image-PDF; text was blurry and neither
selectable nor searchable. Here the PDF is built from the agenda JSON the
editor already saves, with an embedded CJK font, so the text stays vector.

Needs one font file that covers Traditional Chinese. Resolution order:
AGENDA_FONT_PATH → a font dropped in backend/assets/fonts/ → the usual macOS /
Linux system paths. Fonts are checked for real Han coverage before being used
(several system "CJK" fonts are Simplified-only and silently drop 輪/會/議).
"""
import hashlib
import json
import logging
from pathlib import Path

from fpdf import FPDF
from fpdf.enums import Align, VAlign
from fpdf.fonts import FontFace

from .config import AGENDA_FONT_PATH

logger = logging.getLogger(__name__)
# fontTools narrates every subsetting step at INFO; keep it out of the app log.
logging.getLogger("fontTools").setLevel(logging.WARNING)

_ASSET_FONT_DIR = Path(__file__).parent.parent / "assets" / "fonts"

# Ordered candidates; the first one that exists *and* covers the probe glyphs wins.
_SYSTEM_FONTS = [
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",          # macOS
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",        # Debian/Ubuntu
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "C:/Windows/Fonts/msjh.ttc",                                     # Windows 正黑體
]

# Traditional-Chinese-only characters that Simplified fonts tend to miss.
_PROBE = "輪會議社長講題"

_font_path: Path | None = None
_font_checked = False


def _covers_chinese(path: Path) -> bool:
    try:
        from fontTools.ttLib import TTFont, TTCollection
        if path.suffix.lower() in (".ttc", ".otc"):
            faces = TTCollection(str(path)).fonts
        else:
            faces = [TTFont(str(path), lazy=True)]
        for face in faces:
            cmap = face.getBestCmap()
            if all(ord(c) in cmap for c in _PROBE):
                return True
        return False
    except Exception as e:
        logger.warning("agenda_pdf: cannot inspect font %s: %s", path, e)
        return False


def font_path() -> Path | None:
    """The CJK font used for agenda PDFs, or None when the host has none."""
    global _font_path, _font_checked
    if _font_checked:
        return _font_path
    _font_checked = True
    candidates: list[Path] = []
    if AGENDA_FONT_PATH:
        candidates.append(Path(AGENDA_FONT_PATH))
    if _ASSET_FONT_DIR.is_dir():
        candidates += sorted(_ASSET_FONT_DIR.glob("*.tt[fc]")) + sorted(_ASSET_FONT_DIR.glob("*.otf"))
    candidates += [Path(p) for p in _SYSTEM_FONTS]
    for c in candidates:
        if c.is_file() and _covers_chinese(c):
            _font_path = c
            logger.info("agenda_pdf: using font %s", c)
            return _font_path
    logger.warning("agenda_pdf: no Traditional-Chinese font found; agenda PDFs disabled. "
                   "Set AGENDA_FONT_PATH or drop a .ttf into backend/assets/fonts/.")
    return None


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60 % 24:02d}:{minutes % 60:02d}"


def _start_minutes(start_time: str) -> int:
    try:
        h, m = start_time.split(":")[:2]
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return 0


_NAVY = (30, 58, 138)
_INK = (30, 41, 59)
_MUTED = (71, 85, 105)
_LINE = (203, 213, 225)
_HEAD_BG = (241, 245, 249)
_LINK = (37, 99, 235)


class _AgendaPDF(FPDF):
    def __init__(self, title: str):
        super().__init__(format="A4")
        self.doc_title = title
        self.set_auto_page_break(True, margin=15)

    def footer(self):
        self.set_y(-12)
        self.set_font("cjk", size=8)
        self.set_text_color(*_MUTED)
        self.cell(0, 6, f"{self.doc_title}　·　第 {self.page_no()} / {{nb}} 頁", align=Align.C)


# Rendering embeds+subsets a ~20 MB CJK font, so an unchanged agenda is served
# from here (members reopen the same PDF a lot). Keyed by the content itself, so
# a re-save invalidates it without any explicit purge.
_CACHE: dict[str, bytes] = {}
_CACHE_MAX = 32


def _cache_key(ev: dict) -> str:
    payload = json.dumps(
        [ev.get(k) for k in ("id", "title", "date", "start_time", "location", "mc", "agenda")],
        ensure_ascii=False, sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def build_agenda_pdf(ev: dict) -> bytes | None:
    """Vector A4 PDF of one event's agenda. None when no usable font is installed."""
    path = font_path()
    if path is None:
        return None
    agenda = [a for a in (ev.get("agenda") or []) if isinstance(a, dict)]
    if not agenda:
        return None

    key = _cache_key(ev)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    title = str(ev.get("title") or "活動")
    pdf = _AgendaPDF(title)
    pdf.add_font("cjk", "", str(path))
    pdf.set_font("cjk", size=10)
    pdf.add_page()

    # ── Header ────────────────────────────────────────────────────────────────
    pdf.set_font("cjk", size=18)
    pdf.set_text_color(*_NAVY)
    pdf.multi_cell(0, 10, f"{title} 議程表", align=Align.C, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    cur = _start_minutes(str(ev.get("start_time") or ""))
    total = sum(int(a.get("duration") or 0) for a in agenda)
    pdf.set_font("cjk", size=10.5)
    pdf.set_text_color(*_INK)
    for label, value in (
        ("日期", ev.get("date") or "尚未設定"),
        ("時間", f"{ev.get('start_time') or '--:--'} ~ {_hhmm(cur + total)}"),
        ("地點", ev.get("location") or "尚未設定"),
        ("司儀", ev.get("mc") or "尚未設定"),
    ):
        pdf.cell(16, 6, f"{label}：")
        pdf.multi_cell(0, 6, str(value), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)
    pdf.set_draw_color(*_NAVY)
    pdf.set_line_width(0.6)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(4)

    # ── Agenda table ──────────────────────────────────────────────────────────
    pdf.set_draw_color(*_LINE)
    pdf.set_line_width(0.2)
    with pdf.table(
        col_widths=(20, 9, 40, 20, 18),
        text_align=(Align.C, Align.C, Align.L, Align.L, Align.L),
        v_align=VAlign.M,
        line_height=6,
        padding=(2, 1.5),
        # The CJK font ships a single weight, so the heading row is set off by a
        # fill instead of bold (fpdf2's default emphasis would ask for cjk-Bold).
        headings_style=FontFace(emphasis=None, color=_INK, fill_color=_HEAD_BG),
    ) as table:
        head = table.row()
        for h in ("時間", "分鐘", "內容 / 主題", "講者 / 負責人", "附件資源"):
            head.cell(h)
        for item in agenda:
            start = _hhmm(cur)
            cur += int(item.get("duration") or 0)
            row = table.row()
            row.cell(f"{start} - {_hhmm(cur)}")
            row.cell(str(item.get("duration") or 0))
            row.cell(str(item.get("content") or ""))
            row.cell(str(item.get("speaker") or ""))
            att = item.get("pdf") or {}
            name = str(att.get("name") or "") if isinstance(att, dict) else ""
            url = str(att.get("url") or "") if isinstance(att, dict) else ""
            if name and url:
                row.cell(name, link=url, style=FontFace(color=_LINK))
            else:
                row.cell("")

    out = bytes(pdf.output())
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = out
    return out
