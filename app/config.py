import os
from dotenv import load_dotenv

load_dotenv()

LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_CHANNEL_SECRET       = os.environ["LINE_CHANNEL_SECRET"]
DATABASE_URL              = os.environ.get("DATABASE_URL", "")
OPENAI_API_KEY            = os.environ.get("OPENAI_API_KEY", "")
OPENWEATHERMAP_API_KEY    = os.environ.get("OPENWEATHERMAP_API_KEY", "")
GOOGLE_DRIVE_FOLDER_ID    = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000")
LIFF_URL     = os.environ.get("LIFF_URL", "https://liff.line.me/2010535285-kh8rJmpS")
# Where GET /bulletin/pdf redirects when no 社刊 has been published yet — the live
# read-only bulletin viewer served from GitHub Pages.
BULLETIN_VIEWER_URL = os.environ.get(
    "BULLETIN_VIEWER_URL",
    "https://bobby77777.github.io/rotary-3523-liff/bulletin.html?view=1",
)
