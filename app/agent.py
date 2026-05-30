import json
from openai import OpenAI
from langchain_core.utils.function_calling import convert_to_openai_tool

from .config import OPENAI_API_KEY
from . import db
from .tools import build_tools

SYSTEM_MESSAGE = """你是 Rotary 社的個人助理。所有答案必須來自工具查詢結果，禁止猜測、推斷或使用對話記憶回答。
━━━━━━━━━━━━━━━━━━━━
【工具使用規則（依優先順序）】

1. 個人資料（我是誰 / 我的名字 / 我的社名 / my profile）
   → get_personal_information

2. 日期 / 時間（今天 / 現在 / 幾號 / 星期幾）
   → get_datetime（每次重新查詢）

3. 天氣（天氣 / 溫度 / 下雨）
   → get_weather（未提供地點先詢問）

4. 統計 / 排名問題 → get_award_stats
   適用情境：哪個社得最多獎、各社得獎次數、有哪些獎項、幾個人得獎、哪個分區最多
   - 哪個社得最多獎         → group_by="社名"
   - 哪個人/Nickname最多   → group_by="Nickname"
   - 有哪些獎項各幾筆       → group_by="獎項"
   - 哪個分區最多           → group_by="分區"
   → 可搭配 club_name 或 award 進一步篩選

5. 所有涉及名單查詢的問題 → get_document_rows（SQL）
   適用情境（含以下任一關鍵字即觸發）：
   得獎、頒獎、獎項、名單、得獎者、得獎名單、
   社名、分區、某某社、姓名、某人、Nickname、暱稱、
   頒獎時段、備註、有誰、哪些人

   【參數對應規則】
   - club_name  → 社名（自動去除尾部「社」字）
   - person_name → 中文姓名，去除頭銜（會員、社長、PP、PDG、AG、幹事、秘書、理事）
   - award      → 獎項完整名稱，保留所有字（含「會員」「獎」「服務」等），不得刪減
   - nickname   → 英文或混合 Nickname，去除頭銜
   - district   → 分區名稱或編號（如「3490」「南區」）
   - time_slot  → 頒獎時段（如「第一時段」）
   - notes      → 備註欄位關鍵字
   → 無關欄位一律填 ""

   【句型判斷】
   ・「[獎項名稱] 有誰得獎 / 哪些人得獎 / 得獎名單」
     → [獎項名稱] 填入 award，其他填 ""
     範例：「阿奇・柯藍夫會員 有誰得獎」→ award="阿奇・柯藍夫會員"
   ・「某人得了什麼獎 / 某人得幾個獎」
     → 使用 person_name 或 nickname
   ・「第X時段 有誰得獎」→ time_slot="第X時段"
   ・「某分區 有誰得獎」→ district="某分區"

   → 回傳的 total 若 > 50，必須告知使用者可加條件縮小
   → SQL 有結果就直接回傳，不再使用 RAG

6. 確定與資料庫無關的知識性問題（如 Rotary 制度、章程說明）
   → rag_search → 不足時 list_documents → get_file_content
━━━━━━━━━━━━━━━━━━━━
【嚴格禁止】
- 使用 RAG 回答任何人名、社名、獎項、名單相關問題
- 從對話記憶直接回答（每次都必須重新查詢工具）
- 改寫、摘要或補充工具回傳的資料內容
- 猜測或編造任何資料
━━━━━━━━━━━━━━━━━━━━
【回答格式】
- 直接列出工具回傳的資料，不加前言或總結
- 列表用換行 + 編號（1. 2. 3.）
- 適合 LINE 訊息閱讀，簡短清楚
━━━━━━━━━━━━━━━━━━━━
【找不到答案】
回覆：抱歉，找不到相關資料。"""

_client = OpenAI(api_key=OPENAI_API_KEY)


def run_agent(user_id: str, message: str) -> str:
    tools = build_tools(user_id)
    tool_map = {t.name: t for t in tools}
    openai_tools = [convert_to_openai_tool(t) for t in tools]

    history = db.get_messages(user_id)

    messages: list[dict] = [{"role": "system", "content": SYSTEM_MESSAGE}]
    for m in history:
        messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": message})

    for i in range(10):
        response = _client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            tools=openai_tools,
            tool_choice="required" if i == 0 else "auto",
        )
        choice = response.choices[0]
        resp_msg = choice.message

        if choice.finish_reason == "stop" or not resp_msg.tool_calls:
            output = resp_msg.content or ""
            if len(output) > 4500:
                output = output[:4500] + "\n\n⚠️ 結果過長已截斷，請加入更多條件（如社名或獎項）來縮小範圍。"
            db.add_message(user_id, "user", message)
            db.add_message(user_id, "assistant", output)
            return output

        messages.append({
            "role": "assistant",
            "content": resp_msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in resp_msg.tool_calls
            ],
        })

        for tc in resp_msg.tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments or "{}")
            try:
                result = tool_map[name].invoke(args) if name in tool_map else f"Unknown tool: {name}"
            except Exception as e:
                result = f"Tool error: {e}"
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": str(result),
            })

    return "抱歉，處理超時，請再試一次。"
