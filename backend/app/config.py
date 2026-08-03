import os
from dotenv import load_dotenv

load_dotenv()

LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_CHANNEL_SECRET       = os.environ["LINE_CHANNEL_SECRET"]
DATABASE_URL              = os.environ.get("DATABASE_URL", "")
OPENAI_API_KEY            = os.environ.get("OPENAI_API_KEY", "")
OPENWEATHERMAP_API_KEY    = os.environ.get("OPENWEATHERMAP_API_KEY", "")
GOOGLE_DRIVE_FOLDER_ID    = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
# Separate Drive folder holding per-event PDFs the 執秘 uploads (named with the
# event id as a leading number, e.g. "102.pdf"). MUST differ from
# GOOGLE_DRIVE_FOLDER_ID so ingest.py doesn't pull these into the vector store.
EVENT_PDF_FOLDER_ID       = os.environ.get("EVENT_PDF_FOLDER_ID", "")
# Font used to draw 議程 PDFs (must cover Traditional Chinese). Leave empty to
# auto-detect: backend/assets/fonts/*.ttf first, then the usual system paths.
AGENDA_FONT_PATH          = os.environ.get("AGENDA_FONT_PATH", "")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000")
LIFF_URL     = os.environ.get("LIFF_URL", "https://liff.line.me/2010535285-kh8rJmpS")
# Public URL of the bulletin editor/viewer (GitHub Pages). Used for the LINE "社刊"
# keyword reply so 主委 can open it in a computer browser to edit.
BULLETIN_BASE_URL = os.environ.get(
    "BULLETIN_BASE_URL", "https://bobby77777.github.io/rotary-3523-liff/bulletin.html")
# Public URL of the calendar + agenda editor (GitHub Pages). Used for the LINE
# "行事曆" keyword reply so 執秘/管理員 can open it in a computer browser to edit.
CALENDAR_BASE_URL = os.environ.get(
    "CALENDAR_BASE_URL", "https://bobby77777.github.io/rotary-3523-liff/calendar.html")
