# AI 財報分析儀表板

使用 OpenAI GPT 將原始財報轉為結構化指標與繁體中文機構報告，儲存至
SQLite，並透過 Streamlit 顯示及選擇性推播 Telegram。

## 安裝與啟動

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 編輯 .env，至少填入 OPENAI_API_KEY
streamlit run report.py
```

瀏覽器開啟 `http://localhost:8501`，在左側貼上財報文本並開始分析。SQLite
資料庫會在第一次啟動時自動建立。

## Telegram 設定

1. 向 BotFather 建立 Bot，將 token 填入 `TELEGRAM_BOT_TOKEN`。
2. 將 Bot 加入目標頻道並授予發文權限。
3. 將頻道帳號（如 `@my_channel`）或 chat ID 填入 `TELEGRAM_CHAT_ID`。
4. 勾選側欄的「分析成功後推播 Telegram」。

## 模型設定

預設使用 `gpt-5.6`。若帳號改用其他版本（例如更省成本的
`gpt-5.6-terra` / `gpt-5.6-luna`），請在 `.env` 的 `OPENAI_MODEL` 填入可用的
模型 ID，不需修改程式碼。

## 即時財報監控（monitor.py）

`monitor.py` 會持續輪詢 SEC EDGAR 公開資料，偵測試點清單公司是否申報新的
8-K（Item 2.02，財報結果揭露），自動抓取新聞稿全文、呼叫 `analyze_earnings()`
分析、並用 `send_telegram_alert()` 推播，全程不需人工貼文本。

```bash
python3 monitor.py
```

- **試點清單**：S&P 500 大盤股（15 家）+ Nasdaq-100 成分股（見 `monitor.py`
  的 `SP500_CORE` / `NASDAQ_100`），合併去重後共 108 檔，流程穩定後再考慮
  擴大到完整 S&P 500 名單。所有 ticker 皆已用 SEC `company_tickers.json`
  交叉驗證過真實對應公司。
- **輪詢頻率**：預設每 900 秒（15 分鐘）一輪，可用 `.env` 的
  `MONITOR_POLL_SECONDS` 調整。
- **重複防呆**：每筆 8-K 用 SEC 的 accession number 存進 `processed_filings`
  資料表，同一筆不會被分析或推播第二次。
- **首次啟動務必先建立基準線**：否則第一次輪詢會把每家公司歷史上所有
  符合條件的 8-K 一次全部分析＋推播（可能是幾十筆真實 API 呼叫與
  Telegram 訊息）。已經在這個環境跑過一次基準線標記；若要在全新環境
  部署，啟動前應先用同樣邏輯把既有揭露標記為 `baseline`，再啟動常駐輪詢。
- **SEC 規範**：請求標頭需帶可辨識身份的 User-Agent（含聯絡信箱），預設從
  `SEC_CONTACT_EMAIL`（未設定則用專案內建預設值）組成，勿移除。
- 監控器是獨立的常駐行程，跟 Streamlit 分開執行；兩者可以同時開著。
