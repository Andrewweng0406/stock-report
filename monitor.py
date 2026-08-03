"""SEC EDGAR 財報監控器：偵測試點清單公司的新 8-K 財報揭露，自動分析並推播。

啟動方式：
    python3 monitor.py

會持續在背景輪詢（預設每 15 分鐘一輪），按 Ctrl+C 或終止行程即可停止。
資料來源為 SEC EDGAR 公開資料，免費、無需金鑰，但依規定必須在請求標頭附上
可辨識身份的 User-Agent（含聯絡信箱）。
"""

from __future__ import annotations

import os
import time
import traceback
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

import report

load_dotenv()

CONTACT_EMAIL = os.getenv("SEC_CONTACT_EMAIL", "andrewweng.weng@sjsu.edu")
USER_AGENT = f"AI-Earnings-Radar/1.0 ({CONTACT_EMAIL})"
SEC_HEADERS = {"User-Agent": USER_AGENT}
POLL_SECONDS = int(os.getenv("MONITOR_POLL_SECONDS", "900"))

# 心跳／預估財報週曆：每天固定時間（預設 UTC 21:00 ≈ 美股收盤後）發一次，
# 讓 Telegram 頻道在沒有新財報的空窗期也知道監控器還活著、接下來要注意誰。
HEARTBEAT_HOUR_UTC = int(os.getenv("HEARTBEAT_HOUR_UTC", "21"))
UPCOMING_WINDOW_DAYS = 7
QUARTER_CADENCE_DAYS = 91  # 粗估：一般公司約每 91 天發一次季報

# 試點清單：S&P 500 大盤股（含 NYSE）+ Nasdaq-100 成分股，先驗證流程穩定
# 再考慮擴大到完整 S&P 500 名單。Ticker 皆已用 SEC company_tickers.json
# 交叉驗證過，同一公司多股權類別（如 GOOG/GOOGL 同一 CIK）只留一個代表。
SP500_CORE = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "AVGO", "TSLA",
    "JPM", "V", "WMT", "JNJ", "PG", "MA", "HD",
]

NASDAQ_100 = [
    "SPCX", "MU", "AMD", "ASML", "CSCO", "INTC", "COST", "AMAT", "LRCX",
    "NFLX", "PLTR", "PANW", "ARM", "TXN", "KLAC", "LIN", "AMGN", "CRWD",
    "STX", "PEP", "WDC", "TMUS", "SNDK", "ADI", "MRVL", "GILD", "QCOM",
    "SHOP", "BKNG", "APP", "PDD", "ISRG", "VRTX", "SBUX", "FTNT", "ADP",
    "ADBE", "MAR", "DDOG", "MELI", "MNST", "CEG", "CDNS", "CSX", "ABNB",
    "INTU", "DASH", "CMCSA", "CTAS", "ROST", "MDLZ", "HON", "REGN",
    "SNPS", "ORLY", "MPWR", "PCAR", "AEP", "WBD", "HONA", "BKR", "NXPI",
    "TER", "FANG", "LITE", "FAST", "ALAB", "EA", "ADSK", "PYPL", "CCEP",
    "XEL", "NBIS", "EXC", "FER", "TTWO", "ODFL", "IDXX", "TRI", "AXON",
    "KDP", "PAYX", "MCHP", "WDAY", "CRWV", "RKLB", "ROP", "MSTR", "DXCM",
    "GEHC", "KHC", "ALNY", "CPRT",
]

PILOT_TICKERS = sorted(set(SP500_CORE) | set(NASDAQ_100))


def _log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{stamp}] {message}", flush=True)


def load_cik_map() -> dict[str, str]:
    """從 SEC 官方對照表取得 ticker -> 數字 CIK（不補零，符合 Archives 路徑格式）。"""

    resp = requests.get(
        "https://www.sec.gov/files/company_tickers.json",
        headers=SEC_HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    mapping: dict[str, str] = {}
    for entry in resp.json().values():
        mapping[str(entry["ticker"]).upper()] = str(entry["cik_str"])
    return mapping


def fetch_recent_earnings_filings(cik: str) -> list[dict]:
    """回傳該公司最近、含 Item 2.02（財報結果揭露）的 8-K 清單。"""

    cik_padded = cik.zfill(10)
    resp = requests.get(
        f"https://data.sec.gov/submissions/CIK{cik_padded}.json",
        headers=SEC_HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    recent = resp.json().get("filings", {}).get("recent", {})

    forms = recent.get("form", [])
    items = recent.get("items", [])
    accession_numbers = recent.get("accessionNumber", [])
    filing_dates = recent.get("filingDate", [])

    results = []
    for i, form in enumerate(forms):
        if form != "8-K":
            continue
        row_items = items[i] if i < len(items) else ""
        if "2.02" not in row_items:
            continue
        results.append(
            {
                "accession_number": accession_numbers[i],
                "filed_at": filing_dates[i],
            }
        )
    return results


def fetch_press_release_text(cik: str, accession_number: str) -> str:
    """下載 8-K 的新聞稿附件（type 以 EX-99 開頭），回傳清理過的純文字。"""

    accession_nodash = accession_number.replace("-", "")
    index_url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/"
        f"{accession_number}-index.htm"
    )
    index_resp = requests.get(index_url, headers=SEC_HEADERS, timeout=30)
    index_resp.raise_for_status()
    soup = BeautifulSoup(index_resp.content, "lxml")

    table = soup.find("table", class_="tableFile")
    if table is None:
        raise ValueError("這筆 8-K 的索引頁面找不到文件列表。")

    exhibit_href = None
    for row in table.find_all("tr"):
        cells = [c.get_text(strip=True) for c in row.find_all("td")]
        if any(cell.upper().startswith("EX-99") for cell in cells):
            link = row.find("a")
            if link and link.get("href"):
                exhibit_href = link["href"]
                break

    if exhibit_href is None:
        raise ValueError("這筆 8-K 找不到 EX-99 新聞稿附件。")

    doc_url = f"https://www.sec.gov{exhibit_href}"
    doc_resp = requests.get(doc_url, headers=SEC_HEADERS, timeout=30)
    doc_resp.raise_for_status()

    doc_soup = BeautifulSoup(doc_resp.content, "lxml")
    text = doc_soup.get_text(separator="\n")
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def process_filing(ticker: str, cik: str, filing: dict) -> None:
    """分析單筆尚未處理過的 8-K 財報揭露，並推播結果。"""

    accession_number = filing["accession_number"]

    with report.db_session() as session:
        if session.get(report.ProcessedFiling, accession_number) is not None:
            return
        session.add(
            report.ProcessedFiling(
                accession_number=accession_number,
                cik=cik,
                ticker=ticker,
                form_type="8-K",
                filed_at=filing["filed_at"],
                status="processing",
            )
        )

    status = "done"
    error_message = None
    try:
        press_release = fetch_press_release_text(cik, accession_number)
        _log(f"{ticker}：抓到新聞稿全文（{len(press_release)} 字），開始分析…")

        result = report.analyze_earnings(press_release)
        _log(f"{ticker} 分析完成：{result['sentiment']} ({result['sentiment_score']:+d})")

        if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
            report.send_telegram_alert(result)
            _log(f"{ticker} 已推播至 Telegram。")
    except Exception as exc:  # noqa: BLE001 - 監控迴圈需要吃下單筆錯誤並繼續跑下一家
        status = "failed"
        error_message = str(exc)
        _log(f"{ticker} 處理失敗：{exc}")
        traceback.print_exc()

    with report.db_session() as session:
        record = session.get(report.ProcessedFiling, accession_number)
        if record is not None:
            record.status = status
            record.error_message = error_message


def poll_once(cik_map: dict[str, str]) -> None:
    """對試點清單裡的每家公司查一次是否有新的財報 8-K。"""

    for ticker in PILOT_TICKERS:
        cik = cik_map.get(ticker)
        if not cik:
            _log(f"{ticker}：在 SEC 對照表中找不到 CIK，略過。")
            continue
        try:
            filings = fetch_recent_earnings_filings(cik)
        except Exception as exc:
            _log(f"{ticker} 查詢 SEC 失敗：{exc}")
            continue
        for filing in filings:
            process_filing(ticker, cik, filing)
        time.sleep(0.3)  # 禮貌性間隔，避免短時間內對 SEC 送出過多請求。


def _get_state(key: str) -> str | None:
    with report.db_session() as session:
        row = session.get(report.MonitorState, key)
        return row.value if row is not None else None


def _set_state(key: str, value: str) -> None:
    with report.db_session() as session:
        row = session.get(report.MonitorState, key)
        if row is None:
            session.add(report.MonitorState(key=key, value=value))
        else:
            row.value = value


def _latest_filed_dates() -> dict[str, str]:
    """每個 ticker 目前資料庫裡已知最近一次財報揭露日期（含 baseline 與真實分析）。"""

    latest: dict[str, str] = {}
    with report.db_session() as session:
        rows = session.query(report.ProcessedFiling.ticker, report.ProcessedFiling.filed_at).all()
    for ticker, filed_at in rows:
        if ticker not in latest or filed_at > latest[ticker]:
            latest[ticker] = filed_at
    return latest


def _estimate_upcoming(latest_dates: dict[str, str]) -> list[tuple[str, str]]:
    """用「上次財報日 + 91 天」粗估下次財報日，抓出可能落在本週的公司。

    這只是根據歷史申報間隔做的推估，不是官方財報行事曆，僅供參考。
    """

    today = datetime.now(timezone.utc).date()
    upcoming: list[tuple[str, str]] = []
    for ticker, filed_at in latest_dates.items():
        try:
            last_date = datetime.strptime(filed_at, "%Y-%m-%d").date()
        except ValueError:
            continue
        estimated_next = last_date + timedelta(days=QUARTER_CADENCE_DAYS)
        delta_days = (estimated_next - today).days
        if -1 <= delta_days <= UPCOMING_WINDOW_DAYS:
            upcoming.append((ticker, estimated_next.isoformat()))
    return sorted(upcoming, key=lambda item: item[1])


def maybe_send_heartbeat() -> None:
    """每天固定時間發一次心跳＋本週推估財報名單，讓頻道知道監控器還活著。"""

    now = datetime.now(timezone.utc)
    if now.hour < HEARTBEAT_HOUR_UTC:
        return
    today_str = now.date().isoformat()
    if _get_state("last_heartbeat_date") == today_str:
        return

    upcoming = _estimate_upcoming(_latest_filed_dates())
    if upcoming:
        lines = "\n".join(f"▸ {ticker}（推估 {date}）" for ticker, date in upcoming)
        upcoming_block = f"\n\n📅 近期可能發財報（依歷史間隔推估，僅供參考）：\n{lines}"
    else:
        upcoming_block = "\n\n📅 近一週推估沒有試點清單公司要發財報。"

    message = (
        f"🫀 <b>監控器心跳</b>｜{today_str}\n"
        f"持續正常運作中，追蹤 {len(PILOT_TICKERS)} 家公司。"
        f"{upcoming_block}"
    )

    try:
        report.send_telegram_text(message)
        _log("已發送每日心跳訊息。")
    except Exception as exc:
        _log(f"心跳訊息發送失敗：{exc}")
        return

    _set_state("last_heartbeat_date", today_str)


def main() -> None:
    _log(f"啟動財報監控，試點清單共 {len(PILOT_TICKERS)} 家，每 {POLL_SECONDS} 秒輪詢一次。")
    cik_map = load_cik_map()
    _log("已載入 SEC ticker→CIK 對照表。")

    while True:
        try:
            poll_once(cik_map)
        except Exception as exc:
            _log(f"輪詢迴圈發生未預期錯誤：{exc}")
            traceback.print_exc()

        try:
            maybe_send_heartbeat()
        except Exception as exc:
            _log(f"心跳檢查發生未預期錯誤：{exc}")
            traceback.print_exc()

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
