"""Vercel serverless entry point.

Vercel 的 Python runtime 會找 api/ 底下的檔案，並在模組裡找一個叫 `app` 的 ASGI
應用程式來掛載。這支唯一的工作就是把 backend/app/main.py 的 FastAPI 實例暴露出來
—— 所有路由、middleware、lifespan 都還是定義在那邊，本機 `python run.py` 跑的也是
同一個物件，兩條路不會走鐘。

vercel.json 的 rewrite 把每一條路徑都導到這裡，所以 /events、/webhook、/checkin
這些 root-level 路由在雲端跟本機是一樣的網址，不用加 /api 前綴。
"""
import sys
from pathlib import Path

# Root Directory 設成 backend/ 時，函式是以 api/ 為起點載入的；把上一層（也就是
# backend/）加進 sys.path，`from app.main import ...` 才找得到套件。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app  # noqa: E402,F401  —— Vercel 靠這個名字找到 ASGI app
