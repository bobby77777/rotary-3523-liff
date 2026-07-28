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
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000")
LIFF_URL     = os.environ.get("LIFF_URL", "https://liff.line.me/2010535285-kh8rJmpS")
