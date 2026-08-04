"""一次性腳本：把目前所有試點清單公司既有的財報揭露標記為 baseline，
避免監控器把歷史上所有符合條件的 8-K 當成新資料重新分析＋推播一次。

用法（在有正確 DATABASE_URL 環境變數的地方執行，例如 `railway ssh`）：
    python3 seed_baseline.py
"""

import time

import monitor
import report

cik_map = monitor.load_cik_map()
seeded = 0
skipped_existing = 0
no_cik = []

for ticker in monitor.PILOT_TICKERS:
    cik = cik_map.get(ticker)
    if not cik:
        no_cik.append(ticker)
        continue
    try:
        filings = monitor.fetch_recent_earnings_filings(cik)
    except Exception as exc:
        print(f"{ticker}: 查詢失敗 {exc}")
        continue
    with report.db_session() as session:
        for filing in filings:
            existing = session.get(report.ProcessedFiling, filing["accession_number"])
            if existing is not None:
                skipped_existing += 1
                continue
            session.add(
                report.ProcessedFiling(
                    accession_number=filing["accession_number"],
                    cik=cik,
                    ticker=ticker,
                    form_type="8-K",
                    filed_at=filing["filed_at"],
                    status="baseline",
                )
            )
            seeded += 1
    time.sleep(0.25)

print()
print(f"baseline 標記完成：新標記 {seeded} 筆，已存在略過 {skipped_existing} 筆。")
if no_cik:
    print("找不到 CIK 的 ticker:", no_cik)
