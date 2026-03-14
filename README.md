# 以毫米波偵測睡眠品質的智慧偵測系統

> ⚠️ **重要聲明**：本程式為研究性質之「即時監測／示警工具」，**不具醫療器材資格**，不可作為臨床診斷依據，也不能取代醫院正式多導睡眠檢查（PSG）。

---

## 專案簡介

本專案使用 **K60168 毫米波雷達** 搭配 **Kaiku / KKT SDK**，從雷達回傳的 `feature_map` 或 `raw_data` 即時分析呼吸節律，偵測「睡眠呼吸中止」事件並估算 **AHI（Apnea–Hypopnea Index）**，透過 **PySide2 GUI** 以顏色與文字顯示目前風險等級，必要時發出視窗警示與嗶嗶聲提醒。

---

## 功能特色

| 功能 | 說明 |
|------|------|
| 即時雷達串流 | 連線 K60168，讀取 `feature_map` 或 `raw_data` |
| 呼吸波形分析 | 轉為 1D 呼吸強度序列，用 peak detection 計算呼吸頻率與間隔 |
| 呼吸中止偵測 | 兩次呼吸峰間隔 ≥ 10 秒視為一次事件 |
| AHI 估算 | 以最近視窗事件數估算每小時發生次數 |
| 嚴重程度分級 | 正常 / 輕度 / 中度 / 重度（四色 GUI 顯示）|
| 緊急警示 | 升至重度時跳出對話框 + 嗶嗶聲 + LINE Notify（選用）|

---

## 環境需求

### 作業系統
- **Windows 10 / 11**（嗶嗶聲功能需要 `winsound`）

### 硬體
- K60168 毫米波雷達模組

### Python 套件

```bash
pip install numpy scipy PySide2 requests
```

| 套件 | 用途 |
|------|------|
| `numpy` | 數值運算 |
| `scipy` | `find_peaks` 呼吸峰偵測 |
| `PySide2` | GUI 視窗 |
| `requests` | LINE Notify（選用）|

### KKT / Kaiku SDK
依照官方說明安裝 K60168 驅動程式與 SDK，確保可正常執行：
```python
from KKT_Module import kgl
```

---

## 安裝與執行

### 1. 安裝 Python 套件

```bash
pip install numpy scipy PySide2 requests
```

### 2. 設定雷達路徑與輸出類型

開啟 `online_inference_gui.py`，修改以下兩個變數：

```python
# 毫米波設定檔路徑（依你的實際環境修改）
SETTING_FILE = (
    r"C:\mm\mmWave\mmWave"
    r"\radar-gesture-recognition-chore-update-20250815"
    r"\TempParam\K60168-Test-00256-008-v0.0.8-20230717_60cm"
)

# 使用哪種 KKT 輸出："feature_map" 或 "raw_data"
STREAM_TYPE = "feature_map"
```

### 3. （選用）啟用 LINE Notify

在 `Config` 類別中設定：

```python
USE_LINE_NOTIFY = True
LINE_NOTIFY_TOKEN = "your_token_here"
```

### 4. 執行程式

```bash
python online_inference_gui.py
```

> 啟動後需約 5 秒緩衝資料才會開始顯示分析結果。關閉視窗或按 `Ctrl+C` 可結束程式。

---

## 使用流程

1. 正確連接 K60168 雷達（USB 或專用介面）
2. 確認 `SETTING_FILE` 路徑與裝置設定正確
3. 執行 `python online_inference_gui.py`
4. 等待 GUI 啟動，觀察狀態顯示：

| 顏色 | 狀態 | AHI 範圍 |
|------|------|----------|
| 🟢 綠色 | 正常 | AHI < 5 |
| 🟠 橘色 | 輕度疑似呼吸中止 | 5 ≤ AHI < 15 |
| 🔴 紅色 | 中度疑似呼吸中止 | 15 ≤ AHI < 30 |
| 🟥 深紅色 | 重度疑似呼吸中止 | AHI ≥ 30 |

5. 當狀態首次升至**重度**時：
   - 跳出緊急警示對話框（含 AHI 值與建議）
   - 播放三段嗶嗶聲
   - 發送 console 訊息（或 LINE Notify）

---

## 程式碼架構說明

```
online_inference_gui.py
│
├── [設定] SETTING_FILE / STREAM_TYPE
│
├── [Class] Config               # 所有偵測參數集中管理
│
├── [函式] connect_device()      # KKT SDK 裝置連線
├── [函式] run_setting_script()  # 載入設定檔，啟動量測
├── [函式] set_properties()      # 屬性批次設定工具
│
├── [函式] analyze_breath_peaks()           # 呼吸峰偵測與頻率計算
├── [函式] detect_apnea_events_from_peaks() # 呼吸中止事件偵測
│
├── [Class] ApneaOnlineContext   # 滑動視窗緩衝 + AHI 分級邏輯
│   ├── push_frame_and_analyze() # 每幀資料進來 → 輸出分析結果
│   └── classify_severity_from_ahi() # AHI → 等級對應
│
├── [函式] send_alert_console()  # 終端機警示訊息
├── [函式] send_line_notify()    # LINE Notify 通知
├── [函式] play_beep_alert()     # Windows 嗶嗶聲
│
├── [Class] ApneaGUI             # PySide2 主視窗
│   └── update_status()         # 更新 GUI 文字與背景顏色
│
├── [Class] ApneaUpdater         # KKT Updater 子類別
│   └── update()                # 每幀 → 分析 → 更新 GUI → 觸發警示
│
└── [函式] main()               # 程式進入點
```

### 核心模組說明

#### `Config` — 參數設定

```python
FRAME_RATE = 20.0        # 雷達幀率 (Hz)
WINDOW_SEC = 120.0       # 分析視窗長度 (秒)
MIN_APNEA_SEC = 10.0     # 呼吸中止判定門檻 (秒)
PEAK_PROMINENCE_FACTOR = 0.8  # 峰值偵測靈敏度
PEAK_MIN_INTERVAL_SEC = 1.5   # 兩峰最小間隔 (秒)
```

#### `ApneaOnlineContext` — 核心分析邏輯

- 維護一個長度為 `FRAME_RATE × WINDOW_SEC` 的滑動緩衝區（`deque`）
- 每幀資料計算整張圖的平均振幅作為「呼吸強度」
- 呼叫 `analyze_breath_peaks()` 找峰值，計算呼吸率與間隔
- 呼叫 `detect_apnea_events_from_peaks()` 偵測中止事件
- 估算 AHI：`AHI ≈ 事件數 × (60 / 視窗分鐘數)`

#### `ApneaUpdater` — 串流處理

繼承 KKT 的 `Updater`，每次有新 `Results` 時：
1. 從 `feature_map` 或 `raw_data` 取出雷達幀
2. 丟給 `ApneaOnlineContext` 分析
3. 更新 `ApneaGUI` 顯示
4. 若嚴重程度首次升至重度 → 觸發警示

---

## 參數調整建議

| 參數 | 預設值 | 調整建議 |
|------|--------|----------|
| `WINDOW_SEC` | 120 秒 | 增大可提高 AHI 穩定性，縮小可提高即時性 |
| `MIN_APNEA_SEC` | 10 秒 | 依臨床定義通常為 10 秒，可視需求調整 |
| `PEAK_PROMINENCE_FACTOR` | 0.8 | 環境雜訊大時調高，呼吸訊號弱時調低 |
| `PEAK_MIN_INTERVAL_SEC` | 1.5 秒 | 正常成人呼吸約 3~5 秒一次，此值防止誤偵測 |

---

## 注意事項

- 本程式僅在 **Windows** 上支援嗶嗶聲功能（`winsound`），其他平台會自動略過
- 使用 LINE Notify 前請先申請 Token：[LINE Notify 官網](https://notify-bot.line.me/)
- 雷達需保持穩定連線，若連線中斷需重新執行程式
- 分析結果受環境干擾（移動、震動）影響，僅供參考

---

## License

MIT License — 僅供研究與學術使用，禁止商業醫療用途。
