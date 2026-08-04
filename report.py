"""AI 財報分析管道與 Streamlit 儀表板。

啟動方式：
    streamlit run report.py

環境變數可放在同目錄的 .env；必要欄位請參考 .env.example。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime
from html import escape
from typing import Any, Generator, Optional

import streamlit as st
from dotenv import load_dotenv
from openai import OpenAI
from sqlalchemy import DateTime, Integer, String, Text, create_engine, inspect, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from telegram import Bot


load_dotenv()

# 本機預設用 SQLite；雲端部署（如 Railway）用 DATABASE_URL 指向 Postgres，
# 讓 web／worker 兩個服務共用同一份資料。部分平台仍會給舊式的 postgres://
# scheme，SQLAlchemy 2.0 不接受，統一正規化成 postgresql://。
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///earnings_reports.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6")
APP_URL = os.getenv("APP_URL", "http://localhost:8501")


class Base(DeclarativeBase):
    """SQLAlchemy 宣告式模型基底。"""


class EarningsReport(Base):
    """已解析的財報與完整機構分析。"""

    __tablename__ = "earnings_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    period: Mapped[str] = mapped_column(String(50), nullable=False)
    sentiment: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    sentiment_score: Mapped[int] = mapped_column(Integer, nullable=False)
    revenue_status: Mapped[str] = mapped_column(String(10), nullable=False)
    eps_status: Mapped[str] = mapped_column(String(10), nullable=False)
    guidance_status: Mapped[str] = mapped_column(String(12), nullable=False)
    one_line_summary: Mapped[str] = mapped_column(Text, nullable=False)
    key_highlights_json: Mapped[str] = mapped_column(Text, nullable=False)
    next_watch: Mapped[str] = mapped_column(Text, nullable=False)
    trend_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    full_markdown_report: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True
    )


class ProcessedFiling(Base):
    """已處理過的 SEC 8-K 財報揭露，供監控器（monitor.py）避免重複分析。"""

    __tablename__ = "processed_filings"

    accession_number: Mapped[str] = mapped_column(String(30), primary_key=True)
    cik: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    ticker: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    form_type: Mapped[str] = mapped_column(String(10), nullable=False)
    filed_at: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="processing")
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )


class MonitorState(Base):
    """monitor.py 用的簡易 key-value 狀態儲存（例如上次發送心跳的日期）。"""

    __tablename__ = "monitor_state"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(String(200), nullable=False)


engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base.metadata.create_all(engine)


def _ensure_column(table: str, column: str, ddl_type: str) -> None:
    """輕量級遷移：既有資料表若缺少新欄位就補上，不刪資料。用 SQLAlchemy inspect
    取代資料庫專屬語法（如 SQLite 的 PRAGMA），同時相容 SQLite 與 Postgres。"""

    inspector = inspect(engine)
    existing = {col["name"] for col in inspector.get_columns(table)}
    if column not in existing:
        with engine.connect() as conn:
            conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
            conn.commit()


_ensure_column("earnings_reports", "trend_note", "TEXT")


@contextmanager
def db_session() -> Generator[Session, None, None]:
    """提供會自動 commit／rollback／close 的資料庫工作階段。"""

    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


SYSTEM_PROMPT = """
# Role & Task
你是一位在華爾街頂尖對沖基金（Hedge Fund）工作的資深 Equity Research 分析師。
你的任務是從機構「聰明資金（Smart Money）」的視角，仔細拆解並分析傳入的美股
財報文本（新聞稿、10-Q 報告或 Earnings Call 逐字稿）。只依據使用者提供的財報
原文，區分已知事實、管理層說法與你的推論；不可捏造數字。若原文未揭露某項
資料，須清楚標示「文本未揭露」，不要自行補值。

你必須在回答中同時輸出以下兩個部分：
1. 用於前端網頁 UI 讀取標籤的結構化 JSON 區塊（必須嚴格放在 ```json ... ``` 代碼
   塊中，且只能出現一次，前面不要加任何文字）。
2. 一份結構完整、機構等級的繁體中文 Markdown 財報分析報告。

---

# Execution Steps (Chain of Thought)
在生成最終報告前，請在心中依序執行以下推理流程：
1.【數據核對】：核對營收與 EPS 的 GAAP 與 Non-GAAP 數據，並與華爾街共識預期
   （Consensus）比對，判定是 Double Beat、Double Miss 還是 Mixed。
2.【質量評估】：檢查淨利潤中有多少由自由現金流（FCF）支撐，有多少是一次性
   收益（One-off Gains）。
3.【領先指標掃描】：尋找未履約訂單（RPO）、遞延收入（Deferred Revenue）以及
   毛利率（Gross Margin）的變化方向。
4.【風險偵測】：掃描文本中關於資本支出（CapEx）效率、客戶集中度、供應鏈
   瓶頸及 Guidance 上下修的細節。
5.【情緒打分】：根據上述綜合指標，給出 -100（極度利空）到 +100（極度利好）
   的情緒分數（Sentiment Score）。

---

# 輸出要求 1：JSON 格式
```json
{
  "ticker": "大寫股票代碼；若無法判斷則填 UNKNOWN",
  "period": "例如 FY2026 Q4；若無法判斷則填 Unknown",
  "sentiment": "Bullish、Bearish 或 Neutral 三選一",
  "sentiment_score": "-100 到 100 的整數",
  "revenue_status": "Beat、Miss 或 Inline 三選一",
  "eps_status": "Beat、Miss 或 Inline 三選一",
  "guidance_status": "Raised、Lowered 或 Maintained 三選一",
  "one_line_summary": "繁體中文的一句話投資結論",
  "key_highlights": [
    "3-4 條，每條不超過 40 個中文字。優先呈現對『未來』股價與基本面走勢最關鍵的意涵（例如 guidance 是否加速、RPO/訂單能見度、成長動能能否延續），不要只是重複歷史數字本身"
  ],
  "next_watch": "一句話，講清楚散戶接下來最該關注的重點或轉折訊號（例如下季要看什麼指標、guidance 能否兌現），這是給散戶看的，不是給機構看的風險清單"
}
```

---

# 輸出要求 2：繁體中文 Markdown 報告
JSON 區塊之後，接著輸出完整報告，須包含以下六個章節：
一、執行摘要
二、數據核對（Beat / Miss 判定，附實際值、共識值、差異表格）
三、獲利質量評估（一次性損益、FCF 對淨利支撐程度）
四、領先指標掃描（RPO、遞延收入、毛利率趨勢）
五、風險偵測（CapEx 效率、客戶集中度、供應鏈、Guidance 修正方向）
六、情緒打分與結論（分數依據與聰明資金視角總結）
""".strip()


def _extract_response(response_text: str) -> tuple[dict[str, Any], str]:
    """從 GPT 回覆中擷取第一個 JSON fenced block 與其後 Markdown。"""

    match = re.search(r"```json\s*(\{.*?\})\s*```", response_text, re.I | re.S)
    if not match:
        raise ValueError("GPT 回覆缺少 ```json ... ``` 結構化資料區塊。")

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError(f"GPT 回覆中的 JSON 無法解析：{exc.msg}") from exc

    markdown = response_text[match.end() :].strip()
    if not markdown:
        raise ValueError("GPT 回覆缺少 JSON 之後的 Markdown 分析報告。")
    return data, markdown


def _normalize_and_validate(data: dict[str, Any]) -> dict[str, Any]:
    """正規化模型輸出並阻擋不合法的分類值，避免髒資料寫入 DB。"""

    required = {
        "ticker",
        "period",
        "sentiment",
        "sentiment_score",
        "revenue_status",
        "eps_status",
        "guidance_status",
        "one_line_summary",
        "key_highlights",
        "next_watch",
    }
    missing = required - data.keys()
    if missing:
        raise ValueError(f"GPT 回覆缺少欄位：{', '.join(sorted(missing))}")

    normalized = {key: data[key] for key in required}
    normalized["ticker"] = str(normalized["ticker"]).strip().upper()[:20]
    normalized["period"] = str(normalized["period"]).strip()[:50]
    normalized["one_line_summary"] = str(normalized["one_line_summary"]).strip()
    normalized["next_watch"] = str(normalized["next_watch"]).strip()

    highlights = normalized["key_highlights"]
    if not isinstance(highlights, list) or not highlights:
        raise ValueError("key_highlights 必須是非空的字串陣列。")
    normalized["key_highlights"] = [str(item).strip() for item in highlights[:5] if str(item).strip()]
    if not normalized["key_highlights"]:
        raise ValueError("key_highlights 必須是非空的字串陣列。")

    allowed = {
        "sentiment": {"Bullish", "Bearish", "Neutral"},
        "revenue_status": {"Beat", "Miss", "Inline"},
        "eps_status": {"Beat", "Miss", "Inline"},
        "guidance_status": {"Raised", "Lowered", "Maintained"},
    }
    for field, choices in allowed.items():
        value = str(normalized[field]).strip()
        # 接受大小寫差異，但儲存成統一格式。
        lookup = {choice.lower(): choice for choice in choices}
        if value.lower() not in lookup:
            raise ValueError(f"{field} 的值不合法：{value}")
        normalized[field] = lookup[value.lower()]

    try:
        score = int(normalized["sentiment_score"])
    except (TypeError, ValueError) as exc:
        raise ValueError("sentiment_score 必須是整數。") from exc
    if not -100 <= score <= 100:
        raise ValueError("sentiment_score 必須介於 -100 與 100。")
    normalized["sentiment_score"] = score

    if not normalized["ticker"] or not normalized["one_line_summary"]:
        raise ValueError("ticker 與 one_line_summary 不可為空。")
    return normalized


def _compute_trend_note(ticker: str, current_score: int, current_guidance: str, current_revenue: str) -> Optional[str]:
    """跟同一檔股票最近幾季的分析結果比較，抓出情緒與 guidance／營收的連續趨勢。"""

    with db_session() as session:
        history = list(
            session.scalars(
                select(EarningsReport)
                .where(EarningsReport.ticker == ticker)
                .order_by(EarningsReport.created_at.desc())
                .limit(3)
            )
        )
    if not history:
        return None

    previous = history[0]
    score_delta = current_score - previous.sentiment_score
    direction = "走強" if score_delta > 0 else "走弱" if score_delta < 0 else "持平"
    parts = [f"跟上季比：情緒 {previous.sentiment_score:+d} → {current_score:+d}（{direction}）"]

    guidance_history = [previous.guidance_status] + [r.guidance_status for r in history[1:]]
    if current_guidance == "Lowered" and guidance_history[:2] == ["Lowered", "Lowered"]:
        parts.append("⚠️ 連續 3 季下修財測")

    revenue_history = [previous.revenue_status] + [r.revenue_status for r in history[1:]]
    if current_revenue == "Miss" and revenue_history[:2] == ["Miss", "Miss"]:
        parts.append("⚠️ 連續 3 季營收未達預期")

    return "；".join(parts)


def analyze_earnings(raw_text: str) -> dict[str, Any]:
    """呼叫 GPT 分析財報，解析結果並寫入 SQLite。

    Args:
        raw_text: 新聞稿、法說逐字稿或其他原始財報文字。

    Returns:
        包含資料庫 id、結構化欄位及 full_markdown_report 的字典。
    """

    if not raw_text or not raw_text.strip():
        raise ValueError("原始財報文本不可為空。")
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("尚未設定 OPENAI_API_KEY。")

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    response = client.responses.create(
        model=OPENAI_MODEL,
        instructions=SYSTEM_PROMPT,
        input=raw_text.strip(),
        max_output_tokens=4096,
    )
    response_text = response.output_text
    data, markdown = _extract_response(response_text)
    parsed = _normalize_and_validate(data)
    parsed["full_markdown_report"] = markdown
    parsed["trend_note"] = _compute_trend_note(
        parsed["ticker"], parsed["sentiment_score"], parsed["guidance_status"], parsed["revenue_status"]
    )

    db_fields = dict(parsed)
    db_fields["key_highlights_json"] = json.dumps(db_fields.pop("key_highlights"), ensure_ascii=False)

    with db_session() as session:
        report = EarningsReport(**db_fields)
        session.add(report)
        session.flush()
        parsed["id"] = report.id
        parsed["created_at"] = report.created_at.isoformat()

    return parsed


async def _send_telegram_message(message: str) -> None:
    """實際執行非同步 Telegram Bot API 呼叫。"""

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("尚未設定 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID。")
    async with Bot(token=token) as bot:
        await bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


def send_telegram_text(message: str) -> None:
    """發送單純的 HTML 文字訊息（供監控器的心跳／週報等非分析類通知使用）。"""

    asyncio.run(_send_telegram_message(message))


def send_telegram_alert(parsed_data: dict[str, Any]) -> None:
    """推送精簡重點卡片至 Telegram 頻道／群組；完整六大章節報告留在儀表板。"""

    score = int(parsed_data["sentiment_score"])
    light = "🟢" if score > 15 else "🔴" if score < -15 else "🟡"
    guidance_zh = {"Raised": "上修", "Lowered": "下修", "Maintained": "維持"}

    highlights = parsed_data.get("key_highlights") or []
    highlights_block = "\n".join(f"▸ {escape(str(item))}" for item in highlights)

    trend_note = parsed_data.get("trend_note")
    trend_block = f"\n📊 {escape(str(trend_note))}\n" if trend_note else ""

    message = (
        f"{light} <b>{escape(str(parsed_data['ticker']))} "
        f"{escape(str(parsed_data['period']))}</b>｜情緒 {score:+d}\n"
        f"營收：{escape(str(parsed_data['revenue_status']))}｜"
        f"EPS：{escape(str(parsed_data['eps_status']))}｜"
        f"指引：{guidance_zh.get(str(parsed_data['guidance_status']), '維持')}\n"
        f"{trend_block}\n"
        f"{highlights_block}\n\n"
        f"📌 {escape(str(parsed_data['next_watch']))}\n\n"
        f'👉 <a href="{escape(APP_URL, quote=True)}">完整分析</a>'
    )
    asyncio.run(_send_telegram_message(message))


def get_reports(ticker_query: str = "", sentiment: str | None = None) -> list[EarningsReport]:
    """依股票代碼與情緒篩選報告，新的報告排在前面。"""

    with db_session() as session:
        statement = select(EarningsReport)
        if ticker_query.strip():
            statement = statement.where(
                EarningsReport.ticker.ilike(f"%{ticker_query.strip()}%")
            )
        if sentiment:
            statement = statement.where(EarningsReport.sentiment == sentiment)
        statement = statement.order_by(EarningsReport.created_at.desc())
        return list(session.scalars(statement).all())


SENTIMENT_META = {
    "Bullish": ("🟢 利好", "#22c55e", "rgba(34,197,94,.13)"),
    "Bearish": ("🔴 利空", "#ef4444", "rgba(239,68,68,.13)"),
    "Neutral": ("🟡 中性", "#eab308", "rgba(234,179,8,.13)"),
}
STATUS_ZH = {
    "Raised": "上修",
    "Lowered": "下修",
    "Maintained": "維持",
}


def _badge(label: str, foreground: str = "#cbd5e1", background: str = "#1e293b") -> str:
    """產生固定來源內容的狀態 Badge HTML。"""

    return (
        f'<span class="badge" style="color:{foreground};background:{background}">'
        f"{escape(label)}</span>"
    )


def render_report_card(report: EarningsReport) -> None:
    """渲染單一財報卡片與可展開的完整 Markdown。"""

    sentiment_label, color, background = SENTIMENT_META[report.sentiment]
    with st.container(border=True):
        left, right = st.columns([4, 1])
        with left:
            st.markdown(f"### {escape(report.ticker)} · {escape(report.period)}")
        with right:
            st.markdown(
                _badge(sentiment_label, color, background), unsafe_allow_html=True
            )

        badges = " ".join(
            [
                _badge(f"營收: {report.revenue_status}"),
                _badge(f"EPS: {report.eps_status}"),
                _badge(f"指引: {STATUS_ZH[report.guidance_status]}"),
                _badge(f"情緒: {report.sentiment_score:+d}"),
            ]
        )
        st.markdown(badges, unsafe_allow_html=True)
        st.markdown(f"**{report.one_line_summary}**")

        if report.trend_note:
            st.markdown(f"📊 {report.trend_note}")

        highlights = json.loads(report.key_highlights_json)
        for item in highlights:
            st.markdown(f"▸ {item}")
        st.markdown(f"**接下來要看：** {report.next_watch}")

        st.caption(f"建立於 {report.created_at:%Y-%m-%d %H:%M} · 報告 #{report.id}")
        with st.expander("查看完整機構分析"):
            st.markdown(report.full_markdown_report)


def render_app() -> None:
    """Streamlit 頁面進入點。"""

    st.set_page_config(page_title="AI 財報雷達", page_icon="📊", layout="wide")
    st.markdown(
        """
        <style>
        .block-container {max-width: 1280px; padding-top: 2rem;}
        .badge {display:inline-block;padding:.25rem .62rem;border-radius:999px;
                font-size:.78rem;font-weight:700;margin:.1rem .2rem .25rem 0;}
        [data-testid="stVerticalBlockBorderWrapper"] {border-color:#33415555;}
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.title("📊 AI 財報雷達")
    st.caption("GPT 結構化分析 · SQLite 歷史資料 · Telegram 即時推播")

    with st.sidebar:
        st.header("新增分析")
        raw_text = st.text_area(
            "貼上原始財報文本",
            height=300,
            placeholder="貼上 earnings release、法說逐字稿或財報摘要……",
        )
        push_alert = st.checkbox("分析成功後推播 Telegram", value=False)
        if st.button("開始 AI 分析", type="primary", use_container_width=True):
            if not raw_text.strip():
                st.warning("請先貼上原始財報文本。")
            else:
                try:
                    with st.spinner("GPT 正在分析財報……"):
                        result = analyze_earnings(raw_text)
                    st.success(f"{result['ticker']} 分析完成並已存檔。")
                    if push_alert:
                        try:
                            send_telegram_alert(result)
                            st.success("Telegram 推播成功。")
                        except Exception as exc:
                            # DB 已成功落盤，推播失敗不應抹除分析成果。
                            st.warning(f"報告已儲存，但 Telegram 推播失敗：{exc}")
                    st.rerun()
                except Exception as exc:
                    st.error(f"分析失敗：{exc}")

    search_col, filter_col = st.columns([2, 1])
    with search_col:
        ticker_query = st.text_input(
            "股票代碼搜尋", placeholder="例如 MSFT", label_visibility="collapsed"
        )
    sentiment_options = {
        "全部": None,
        "🟢 利好 (Bullish)": "Bullish",
        "🔴 利空 (Bearish)": "Bearish",
        "🟡 中性 (Neutral)": "Neutral",
    }
    with filter_col:
        selected_label = st.selectbox(
            "情緒篩選", sentiment_options, label_visibility="collapsed"
        )

    reports = get_reports(ticker_query, sentiment_options[selected_label])
    st.caption(f"共 {len(reports)} 份符合條件的財報")
    if not reports:
        st.info("目前沒有符合條件的報告。請從左側貼上財報文本開始分析。")
        return

    # 寬螢幕雙欄、窄螢幕由 Streamlit 自動堆疊。
    columns = st.columns(2)
    for index, report in enumerate(reports):
        with columns[index % 2]:
            render_report_card(report)


if __name__ == "__main__":
    render_app()
