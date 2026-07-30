import json
import requests
from datetime import datetime
import pytz
from langchain.tools import tool
from langchain_openai import OpenAIEmbeddings

from . import db
from .config import OPENAI_API_KEY, OPENWEATHERMAP_API_KEY

_embeddings = OpenAIEmbeddings(api_key=OPENAI_API_KEY)


def build_tools(user_id: str) -> list:
    """Return a list of LangChain tools scoped to the current LINE user."""

    @tool
    def rag_search(query: str) -> str:
        """Use RAG to find information in the knowledge base."""
        embedding = _embeddings.embed_query(query)
        results = db.vector_search(embedding)
        if not results:
            return "No relevant documents found."
        return "\n\n".join(r["content"] for r in results)

    @tool
    def get_document_rows(
        club_name: str = "",
        person_name: str = "",
        award: str = "",
        nickname: str = "",
        district: str = "",
        time_slot: str = "",
        notes: str = "",
    ) -> str:
        """Search award and member data from the document_rows table.

        ALWAYS use this tool (not RAG) when the user asks about:
        - 分區 (district), 社名 (club name), 姓名 (person name), Nickname
        - 獎項 (award), 頒獎時段 (award time slot), 備註 (notes)
        - 得獎名單, 頒獎名單, 得獎者 (award recipients)

        Pass search terms only for mentioned fields; leave others as empty string "".
        - club_name: club name, strip trailing 社 (e.g. "松青社" → "松青")
        - person_name: Chinese name only, strip title/role suffix such as
          會員、社長、PP、PDG、AG、幹事、秘書、理事
          (e.g. "阿奇・柯藍夫會員" → person_name="" nickname="阿奇・柯藍夫")
        - award: full award name, keep every character including 會員/獎/服務
        - nickname: English or mixed nickname, strip role suffixes
        - district: district name or number (e.g. "3490" or "南區")
        - time_slot: 頒獎時段 value (e.g. "第一時段")
        - notes: keyword to search in 備註 field
        Returns up to 50 rows plus total_count so you know if results are truncated.
        """
        import logging
        logging.getLogger(__name__).info(
            "get_document_rows: club=%r person=%r award=%r nick=%r district=%r slot=%r notes=%r",
            club_name, person_name, award, nickname, district, time_slot, notes,
        )
        club = club_name.removesuffix("社") if club_name else ""
        rows = db.search_document_rows(club, person_name, award, nickname, district, time_slot, notes)
        if not rows:
            return "No records found."
        total = rows[0].get("total_count", len(rows))
        clean = [{k: v for k, v in r.items() if k != "total_count"} for r in rows]
        result: dict = {"total": total, "rows": clean}
        if total > 50:
            result["note"] = f"共 {total} 筆，僅顯示前 50 筆，建議加入更多條件縮小範圍"
        return json.dumps(result, ensure_ascii=False, default=str)

    @tool
    def get_award_stats(
        group_by: str,
        club_name: str = "",
        award: str = "",
    ) -> str:
        """Count award records grouped by a field. Use for ranking or aggregation questions.

        Use when the user asks:
        - 哪個社得最多獎 / 各社得獎次數 → group_by="社名"
        - 哪個人得最多獎 / 各人得獎次數 → group_by="Nickname"
        - 有哪些獎項 / 各獎項幾筆      → group_by="獎項"
        - 哪個分區得最多獎             → group_by="分區"

        group_by: one of "社名", "Nickname", "獎項", "分區"
        club_name: optional, filter by club before grouping
        award: optional, filter by award name before grouping
        """
        import logging
        logging.getLogger(__name__).info(
            "get_award_stats: group_by=%r club=%r award=%r", group_by, club_name, award
        )
        rows = db.get_award_stats(group_by, club_name, award)
        if not rows:
            return "No records found."
        return json.dumps(rows, ensure_ascii=False, default=str)

    @tool
    def get_file_content(file_id: str) -> str:
        """Given a file ID, fetch the full text of that document."""
        rows = db.get_file_content(file_id)
        if not rows:
            return "Document not found."
        return rows[0].get("document_text", "")

    @tool
    def list_documents() -> str:
        """Fetch all available documents including their table schema for CSV/Excel files."""
        rows = db.list_document_metadata()
        if not rows:
            return "No documents found."
        return json.dumps(rows, ensure_ascii=False, default=str)

    @tool
    def get_personal_information() -> str:
        """Retrieve the current user's personal information from the personal_information table.

        Use this tool when the user asks about their identity or personal details:
        我是誰、我的資料、我的名字、我的社名、我的暱稱、who am I、my profile
        """
        rows = db.get_personal_info(user_id)
        if not rows:
            return "尚未填寫個人資料，請先填寫報名表。"
        return json.dumps(rows[0], ensure_ascii=False, default=str)

    @tool
    def get_datetime() -> str:
        """Return the current date and time in Asia/Taipei timezone."""
        now = datetime.now(pytz.timezone("Asia/Taipei"))
        return now.strftime("%Y-%m-%d %H:%M:%S %Z %A")

    @tool
    def get_weather(city: str, language: str = "zh_tw") -> str:
        """Get the current weather for a city.

        Ask the user for the city name if not provided.
        """
        resp = requests.get(
            "http://api.openweathermap.org/data/2.5/weather",
            params={
                "q": city,
                "appid": OPENWEATHERMAP_API_KEY,
                "lang": language,
                "units": "metric",
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return f"Weather lookup failed: {resp.text}"
        data = resp.json()
        weather = data["weather"][0]["description"]
        temp = data["main"]["temp"]
        feels = data["main"]["feels_like"]
        humidity = data["main"]["humidity"]
        return f"{city} 天氣：{weather}，氣溫 {temp}°C（體感 {feels}°C），濕度 {humidity}%"

    return [
        rag_search,
        get_document_rows,
        get_award_stats,
        get_file_content,
        list_documents,
        get_personal_information,
        get_datetime,
        get_weather,
    ]
