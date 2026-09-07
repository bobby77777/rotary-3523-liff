import os
from dotenv import load_dotenv

load_dotenv()

# --- 執行環境 ---------------------------------------------------------------
# Vercel 一定會設 VERCEL=1。這個旗標決定那些「只有常駐 process 才成立」的行為要不要
# 開：背景排程執行緒、開機跑建表遷移、可寫的檔案系統。
IS_SERVERLESS = bool(os.environ.get("VERCEL"))

# 開機是否跑 ensure_* 建表遷移。常駐機器維持開著（跟以前一樣）；serverless 預設關掉
# —— 每次冷啟動都跑三十幾條 DDL，既慢又是在對 Postgres 做無謂的重複工作。雲端改成
# 部署後手動打一次 POST /internal/migrate。
RUN_MIGRATIONS_ON_STARTUP = os.environ.get(
    "RUN_MIGRATIONS_ON_STARTUP", "0" if IS_SERVERLESS else "1") == "1"

# 保護 /internal/* 的共用密鑰。Vercel Cron 會自動帶 Authorization: Bearer $CRON_SECRET。
# 沒設的話那些端點一律回 503，不會變成沒鎖的後門。
CRON_SECRET = os.environ.get("CRON_SECRET", "")

# 連線池上限。serverless 每個實例各開一個池，實例一多就會把 Postgres 的連線數吃光，
# 所以預設遠小於常駐機器。雲端請把 DATABASE_URL 指到 Supabase 的 transaction
# pooler（port 6543）而不是 5432 直連。
DB_POOL_MAX = int(os.environ.get("DB_POOL_MAX", "2" if IS_SERVERLESS else "10"))

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
# Google Drive 憑證。本機是讀 backend/secrets/*.json；serverless 沒有可以放私鑰的
# 檔案系統，所以改成把「整份 JSON 內容」塞進環境變數。兩者都支援，環境變數優先。
# 雲端請用 service account（GOOGLE_SERVICE_ACCOUNT_JSON）—— OAuth user token 會過期，
# 而 serverless 上重新整理過的 token 寫不回去，過期就等於 Drive 功能停擺。
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_OAUTH_TOKEN_JSON     = os.environ.get("GOOGLE_OAUTH_TOKEN_JSON", "")
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

# 高球分組表（golf.html）。聊天室打「高爾夫」會回這個連結。
GOLF_BASE_URL = os.environ.get(
    "GOLF_BASE_URL", "https://bobby77777.github.io/rotary-3523-liff/golf.html")

# 社費財務看板（finance.html）。聊天室打「財務」會回這個連結；表格寬，建議用電腦開。
FINANCE_BASE_URL = os.environ.get(
    "FINANCE_BASE_URL", "https://bobby77777.github.io/rotary-3523-liff/finance.html")
