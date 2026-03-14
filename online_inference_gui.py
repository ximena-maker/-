# realtime_apnea_with_gui.py
# 毫米波 K60168 即時「睡眠呼吸中止」偵測主程式
#
# 功能：
# - 即時從 K60168 讀取 feature_map / raw_data
# - 轉成 1D 呼吸強度序列
# - 用 peak 分析呼吸頻率、間隔
# - 偵測呼吸中止事件（兩次呼吸間隔 >= MIN_APNEA_SEC）
# - 用最近一段時間的事件數估算「AHI 指數 (次/小時)」
# - 根據 AHI 分級：正常 / 輕度 / 中度 / 重度
# - GUI 顯示狀態 + 顏色（綠 / 橘 / 紅 / 深紅）
# - 當從較低等級 → 重度時，跳警示視窗 + 嗶嗶聲 + 緊急通知
#
# ⚠ 這只是「即時監測 / 示警工具」，不能取代醫院正式多導睡眠檢查（PSG）

import sys
from collections import deque
from typing import List, Tuple

import numpy as np
from scipy.signal import find_peaks

from PySide2 import QtWidgets
from PySide2.QtCore import Qt

# 嗶嗶聲（Windows）
try:
    import winsound
except ImportError:
    winsound = None

# ======== 你的毫米波設定檔路徑 ========
SETTING_FILE = (
    r"C:\mm\mmWave\mmWave"
    r"\radar-gesture-recognition-chore-update-20250815"
    r"\TempParam\K60168-Test-00256-008-v0.0.8-20230717_60cm"
)

# 使用哪種 KKT 輸出："feature_map" 或 "raw_data"
STREAM_TYPE = "feature_map"

# ======== KKT / Kaiku SDK 匯入（你目前已可正常使用） ========
from KKT_Module import kgl
from KKT_Module.DataReceive.Core import Results
from KKT_Module.DataReceive.DataReceiver import MultiResult4168BReceiver
from KKT_Module.FiniteReceiverMachine import FRM
from KKT_Module.SettingProcess.SettingConfig import SettingConfigs
from KKT_Module.SettingProcess.SettingProccess import SettingProc
from KKT_Module.GuiUpdater.GuiUpdater import Updater


# =========================
# 1. 參數設定
# =========================

class Config:
    # 雷達幀率（Hz）；依 SavedRecords 設定 20Hz
    FRAME_RATE = 20.0

    # 分析視窗長度（秒）——最近多少秒的呼吸用來判斷 & 估 AHI
    WINDOW_SEC = 120.0  # 建議 60~300 秒；這裡先給 2 分鐘

    # 呼吸中止判定：連續 >= MIN_APNEA_SEC 秒沒有呼吸峰 → 視為一次「呼吸中止事件」
    MIN_APNEA_SEC = 10.0

    # peak 偵測參數
    PEAK_PROMINENCE_FACTOR = 0.8      # 越大 → 需要更明顯的呼吸波動才算一個 peak
    PEAK_MIN_INTERVAL_SEC = 1.5       # 兩次呼吸至少間隔 1.5 秒（避免抖動）

    # AHI 估算視窗（分鐘）
    # 這裡直接用 WINDOW_SEC，等同「用最近 WINDOW_SEC 秒事件數」推估每小時發生次數
    @property
    def AHI_WINDOW_MIN(self) -> float:
        return self.WINDOW_SEC / 60.0

    # 是否使用 LINE Notify（預設關閉，只用 GUI 警示 + 嗶嗶聲）
    USE_LINE_NOTIFY = False
    LINE_NOTIFY_TOKEN = ""


# =========================
# 2. 基礎工具：連線、設定
# =========================

def connect_device():
    """連線到 K60168 裝置（失敗時彈框）。"""
    try:
        device = kgl.ksoclib.connectDevice()
        if device == 'Unknow':
            ret = QtWidgets.QMessageBox.warning(
                None, 'Unknown Device', 'Please reconnect device and try again',
                QtWidgets.QMessageBox.Ok | QtWidgets.QMessageBox.Cancel
            )
            if ret == QtWidgets.QMessageBox.Ok:
                connect_device()
    except Exception:
        ret = QtWidgets.QMessageBox.warning(
            None, 'Connection Failed', 'Please reconnect device and try again',
            QtWidgets.QMessageBox.Ok | QtWidgets.QMessageBox.Cancel
        )
        if ret == QtWidgets.QMessageBox.Ok:
            connect_device()


def run_setting_script(setting_name: str):
    """跑 Kaiku 的 setting 流程，啟動量測。"""
    ksp = SettingProc()
    cfg = SettingConfigs()
    cfg.Chip_ID = kgl.ksoclib.getChipID().split(' ')[0]
    cfg.Processes = [
        'Reset Device',
        'Gen Process Script',
        'Gen Param Dict',
        'Get Gesture Dict',
        'Set Script',
        'Run SIC',
        'Phase Calibration',
        'Modulation On'
    ]
    cfg.setScriptDir(f'{setting_name}')
    ksp.startUp(cfg)


def set_properties(obj: object, **kwargs):
    """把 dict 的 key 當屬性塞進物件（跟你原始程式一樣）。"""
    print(f"==== Set properties in {obj.__class__.__name__} ====")
    for k, v in kwargs.items():
        if not hasattr(obj, k):
            print(f'Attribute "{k}" not in {obj.__class__.__name__}.')
            continue
        setattr(obj, k, v)
        print(f'Attribute "{k}", set "{v}"')


# =========================
# 3. 呼吸波形分析
# =========================

def analyze_breath_peaks(
    breath: np.ndarray,
    frame_rate: float,
    peak_prom_factor: float,
    peak_min_interval_sec: float,
):
    """
    給一段呼吸波形（1D），偵測呼吸峰，計算：
      - peak_times (sec)       峰值發生時間
      - breaths_per_min        呼吸頻率（次/分）
      - intervals (sec)        呼吸間隔
    """
    N = len(breath)
    T = N / frame_rate

    if N < 10:
        return np.array([]), np.array([]), None, np.array([])

    # 去 DC / 正規化
    breath = breath.astype(np.float32)
    breath = breath - np.mean(breath)

    std = float(np.std(breath))
    if std == 0.0:
        return np.array([]), np.array([]), None, np.array([])

    prominence = std * peak_prom_factor
    min_distance = int(frame_rate * peak_min_interval_sec)

    peaks, _ = find_peaks(
        breath,
        prominence=prominence,
        distance=min_distance
    )
    if len(peaks) == 0:
        return np.array([]), np.array([]), None, np.array([])

    peak_times = peaks / frame_rate

    if len(peak_times) >= 2:
        intervals = np.diff(peak_times)
        breaths_per_min = 60.0 * len(peak_times) / T
    else:
        intervals = np.array([])
        breaths_per_min = None

    return peaks, peak_times, breaths_per_min, intervals


def detect_apnea_events_from_peaks(
    peak_times: np.ndarray,
    min_apnea_sec: float,
) -> List[Tuple[float, float]]:
    """
    相鄰兩個呼吸峰 gap >= min_apnea_sec → 視為一個呼吸中止事件。
    回傳 [(start_sec, end_sec), ...] 相對於這一段波形的時間。
    """
    events: List[Tuple[float, float]] = []
    if peak_times.size < 2:
        return events

    for i in range(len(peak_times) - 1):
        t1 = peak_times[i]
        t2 = peak_times[i + 1]
        gap = t2 - t1
        if gap >= min_apnea_sec:
            events.append((t1, t2))
    return events


# =========================
# 4. 線上呼吸偵測 Context（含 AHI 分級）
# =========================

class ApneaOnlineContext:
    """
    把每一幀雷達資料轉成一個 scalar「呼吸強度」，用 deque 保存最近 WINDOW_SEC 秒資料，
    持續做 peak 分析 + 呼吸中止事件偵測，並推估 AHI：
      AHI ≈ (最近視窗中的事件數) * (60 / 視窗分鐘數)

    AHI 分級：
      < 5        → 0: 正常
      5 ~ 15     → 1: 輕度
      15 ~ 30    → 2: 中度
      >= 30      → 3: 重度
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.frame_rate = cfg.FRAME_RATE
        self.window_sec = cfg.WINDOW_SEC

        self.max_len = int(self.frame_rate * self.window_sec)
        if self.max_len <= 0:
            self.max_len = 1

        self.buffer = deque(maxlen=self.max_len)

        # 狀態記錄
        self.last_severity = 0

    @staticmethod
    def to_frame(arr) -> np.ndarray:
        """
        KKT 的 feature_map / raw_data 可能是 (2,32,32) 或 (32,32,2)。
        統一轉成 (2,32,32) float32。
        """
        x = np.asarray(arr)
        if x.shape == (2, 32, 32):
            pass
        elif x.shape == (32, 32, 2):
            x = np.transpose(x, (2, 0, 1))
        else:
            raise ValueError(f"Unexpected frame shape: {x.shape}")
        return x.astype(np.float32, copy=True)

    @staticmethod
    def classify_severity_from_ahi(ahi: float) -> int:
        """
        根據 AHI 分級：
          < 5        → 0 正常
          5 ~ 15     → 1 輕度
          15 ~ 30    → 2 中度
          >= 30      → 3 重度
        """
        if ahi < 5:
            return 0
        elif ahi < 15:
            return 1
        elif ahi < 30:
            return 2
        else:
            return 3

    def push_frame_and_analyze(self, frame: np.ndarray):
        """
        - frame: (2,32,32) 雷達圖
        - 轉成一個 scalar 放進 deque
        - 對 buffer 做 peak 分析 & 呼吸中止偵測 & AHI 估算
        回傳 dict：
        {
          'bpm': float 或 None,
          'max_gap': float,
          'mean_gap': float,
          'events': List[(start, end)],
          'ahi': float,
          'severity': int (0~3),
          'severity_just_changed': bool,
        }
        """
        frame = np.asarray(frame, dtype=np.float32)

        # 簡單版「呼吸強度」：整張圖的平均振幅
        amp = float(np.mean(np.abs(frame)))
        self.buffer.append(amp)

        # 若資料還太少，先不分析
        if len(self.buffer) < int(self.frame_rate * 5):  # 至少 5 秒資料
            return {
                'bpm': None,
                'max_gap': 0.0,
                'mean_gap': 0.0,
                'events': [],
                'ahi': 0.0,
                'severity': 0,
                'severity_just_changed': False,
            }

        breath = np.array(self.buffer, dtype=np.float32)

        peaks, peak_times, bpm, intervals = analyze_breath_peaks(
            breath=breath,
            frame_rate=self.frame_rate,
            peak_prom_factor=self.cfg.PEAK_PROMINENCE_FACTOR,
            peak_min_interval_sec=self.cfg.PEAK_MIN_INTERVAL_SEC,
        )

        if intervals.size > 0:
            max_gap = float(np.max(intervals))
            mean_gap = float(np.mean(intervals))
        else:
            max_gap = 0.0
            mean_gap = 0.0

        events = detect_apnea_events_from_peaks(
            peak_times=peak_times,
            min_apnea_sec=self.cfg.MIN_APNEA_SEC,
        )

        # 用最近視窗事件數估算 AHI（次 / 小時）
        # AHI ≈ events_count * (60 / 視窗分鐘數)
        window_min = max(self.cfg.AHI_WINDOW_MIN, 0.1)
        ahi_est = len(events) * (60.0 / window_min)

        severity = self.classify_severity_from_ahi(ahi_est)
        severity_just_changed = (severity != self.last_severity)
        self.last_severity = severity

        return {
            'bpm': bpm,
            'max_gap': max_gap,
            'mean_gap': mean_gap,
            'events': events,
            'ahi': ahi_est,
            'severity': severity,
            'severity_just_changed': severity_just_changed,
        }


# =========================
# 5. 通知機制（console / LINE）
# =========================

def send_alert_console(message: str):
    print("\n================= ALERT =================")
    print(message)
    print("=========================================\n")


def send_line_notify(message: str, token: str):
    """
    用 LINE Notify 發訊息。
    若失敗則改用 console。
    """
    if not token:
        send_alert_console(message)
        return

    try:
        import requests
    except ImportError:
        print("[LINE] 未安裝 requests，改用 console 警示。")
        send_alert_console(message)
        return

    url = "https://notify-api.line.me/api/notify"
    headers = {"Authorization": f"Bearer {token}"}
    data = {"message": message}

    try:
        resp = requests.post(url, headers=headers, data=data, timeout=5)
        if resp.status_code == 200:
            print("[LINE] 通知已送出。")
        else:
            print(f"[LINE] 通知失敗, status_code={resp.status_code}")
            send_alert_console(message)
    except Exception as e:
        print(f"[LINE] 通訊錯誤: {e}")
        send_alert_console(message)


def send_alert(message: str, cfg: Config):
    if cfg.USE_LINE_NOTIFY:
        send_line_notify(message, cfg.LINE_NOTIFY_TOKEN)
    else:
        send_alert_console(message)


def play_beep_alert():
    """重度狀態用嗶嗶聲提醒（Windows winsound）。"""
    if winsound is None:
        return
    try:
        # 三段不同頻率 Beep
        for freq in (1000, 1500, 2000):
            winsound.Beep(freq, 300)  # freq Hz, duration ms
    except Exception:
        pass


# =========================
# 6. GUI：即時顯示呼吸 & 中止狀態（PySide2）
# =========================

class ApneaGUI(QtWidgets.QWidget):
    """
    簡單 GUI：
      - 狀態（正常 / 輕度 / 中度 / 重度）
      - 平均呼吸率
      - 呼吸間隔
      - AHI 估計值
      - 事件數量
    """
    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg

        self.setWindowTitle("SleepRadar - mmWave 即時呼吸中止偵測")
        self.resize(800, 420)

        main_layout = QtWidgets.QVBoxLayout()

        # 標題
        self.title_label = QtWidgets.QLabel("SleepRadar - 毫米波即時呼吸監測（非醫療用途）")
        self.title_label.setAlignment(Qt.AlignCenter)
        self.title_label.setStyleSheet(
            "font-size: 20px; font-weight: bold; padding: 8px;"
        )
        main_layout.addWidget(self.title_label)

        # 狀態區
        self.status_label = QtWidgets.QLabel("狀態：等待資料...")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet(
            "font-size: 18px; font-weight: bold; padding: 10px; "
            "background-color: lightgray; border-radius: 8px;"
        )
        main_layout.addWidget(self.status_label)

        # 統計區
        stats_layout = QtWidgets.QHBoxLayout()

        self.bpm_label = QtWidgets.QLabel("平均呼吸率：-")
        self.bpm_label.setAlignment(Qt.AlignCenter)

        self.gap_label = QtWidgets.QLabel("呼吸間隔：-")
        self.gap_label.setAlignment(Qt.AlignCenter)

        self.ahi_label = QtWidgets.QLabel("AHI 估計值：- 次/小時")
        self.ahi_label.setAlignment(Qt.AlignCenter)

        self.event_label = QtWidgets.QLabel("呼吸中止事件（最近視窗）：-")
        self.event_label.setAlignment(Qt.AlignCenter)

        for lab in (self.bpm_label, self.gap_label, self.ahi_label, self.event_label):
            lab.setStyleSheet("font-size: 14px; padding: 5px;")
            stats_layout.addWidget(lab)

        main_layout.addLayout(stats_layout)

        self.setLayout(main_layout)

    def update_status(self, bpm, max_gap, mean_gap, event_count, ahi, severity: int):
        """由 Updater 呼叫，更新 GUI 文字與顏色。"""
        # 呼吸率
        if bpm is None:
            self.bpm_label.setText("平均呼吸率：資料不足")
        else:
            # 正常成人睡眠/靜止約 12~20 次/分鐘
            self.bpm_label.setText(f"平均呼吸率：約 {bpm:.1f} 次/分鐘")

        # 間隔
        if max_gap <= 0:
            self.gap_label.setText("呼吸間隔：資料不足")
        else:
            self.gap_label.setText(
                f"呼吸間隔：平均 {mean_gap:.1f} 秒 / 最長 {max_gap:.1f} 秒"
            )

        # AHI 估計值
        self.ahi_label.setText(f"AHI 估計值：約 {ahi:.1f} 次/小時")

        # 事件數
        self.event_label.setText(
            f"呼吸中止事件（最近約 {self.cfg.WINDOW_SEC:.0f} 秒）：{event_count} 次"
        )

        # 依 severity 顯示狀態文字 & 顏色（**已移除淺粉色**）
        if severity == 0:
            text = "狀態：正常（AHI < 5）"
            color = "lightgreen"
        elif severity == 1:
            text = "狀態：⚠ 輕度疑似呼吸中止（5 ≤ AHI < 15）"
            color = "#FFD27F"   # 橘色
        elif severity == 2:
            text = "狀態：⚠⚠ 中度疑似呼吸中止（15 ≤ AHI < 30）"
            color = "#FF4C4C"   # 紅色（取代原本淺粉）
        else:  # severity >= 3
            text = "狀態：⚠⚠⚠ 重度疑似呼吸中止（AHI ≥ 30，需立即處理）"
            color = "#B00020"   # 更深紅

        self.status_label.setText(text)
        self.status_label.setStyleSheet(
            "font-size: 18px; font-weight: bold; padding: 10px; "
            f"background-color: {color}; border-radius: 8px;"
        )


# =========================
# 7. Updater：接 KKT 結果 → 呼吸分析 → 更新 GUI
# =========================

class ApneaUpdater(Updater):
    """
    結合 FRM + Receiver：
      - 每次有新 Results，就取 feature_map/raw_data
      - 丟給 ApneaOnlineContext 做呼吸分析 + AHI 推估
      - 更新 GUI
      - 一旦從較低等級 → 重度（severity=3），觸發警示 + 嗶嗶聲 + 緊急通知
    """
    def __init__(self, ctx: ApneaOnlineContext, gui: ApneaGUI, cfg: Config):
        super().__init__()
        self.ctx = ctx
        self.gui = gui
        self.cfg = cfg

    def update(self, res: Results):
        try:
            # 1) 取出雷達幀
            if STREAM_TYPE == "raw_data":
                arr = res['raw_data'].data
            else:
                arr = res['feature_map'].data

            frame = self.ctx.to_frame(arr)  # (2,32,32)

            # 2) 推進呼吸分析
            stats = self.ctx.push_frame_and_analyze(frame)

            bpm = stats['bpm']
            max_gap = stats['max_gap']
            mean_gap = stats['mean_gap']
            events = stats['events']
            ahi = stats['ahi']
            severity = stats['severity']
            severity_changed = stats['severity_just_changed']

            # 3) 更新 GUI
            try:
                self.gui.update_status(
                    bpm=bpm,
                    max_gap=max_gap,
                    mean_gap=mean_gap,
                    event_count=len(events),
                    ahi=ahi,
                    severity=severity,
                )
            except Exception:
                # 不讓 UI 更新錯誤卡住串流
                pass

            # 4) 從「較低等級 → 重度（3）」時觸發警示 + 嗶嗶聲 + 緊急通知
            if severity >= 3 and severity_changed:
                msg = (
                    f"[SleepRadar 緊急警示] 推估 AHI ≈ {ahi:.1f} 次/小時，屬於『重度』疑似呼吸中止。\n"
                    f"最近約 {self.cfg.WINDOW_SEC:.0f} 秒資料中偵測到 {len(events)} 次呼吸中止事件。\n"
                    f"請立即確認使用者狀況，必要時喚醒或就醫。"
                )

                # GUI 警示視窗
                QtWidgets.QMessageBox.warning(self.gui, "SleepRadar 緊急警示", msg)

                # 嗶嗶聲
                play_beep_alert()

                # 緊急聯絡（console / LINE）
                send_alert(msg, self.cfg)

        except Exception:
            # 任何異常直接吞掉，避免 FRM 卡住
            pass


# =========================
# 8. main：組起來
# =========================

def main():
    cfg = Config()

    # Qt 事件迴圈
    app = QtWidgets.QApplication(sys.argv)

    # 啟動 GUI
    gui = ApneaGUI(cfg)
    gui.show()

    # 初始化雷達
    print("[INFO] 初始化 K60168 mmWave 裝置...")
    kgl.setLib()
    connect_device()
    run_setting_script(SETTING_FILE)

    # 切換輸出源（跟你原本手勢程式的寫法一致）
    if STREAM_TYPE == "raw_data":
        # raw_data
        kgl.ksoclib.writeReg(0, 0x50000504, 5, 5, 0)
    else:
        # feature_map
        kgl.ksoclib.writeReg(1, 0x50000504, 5, 5, 0)

    # 建立線上呼吸分析 context + updater
    ctx = ApneaOnlineContext(cfg)
    updater = ApneaUpdater(ctx, gui, cfg)

    # Receiver + FRM
    receiver = MultiResult4168BReceiver()
    set_properties(
        receiver,
        actions=1,
        rbank_ch_enable=7,
        read_interrupt=0,
        clear_interrupt=0,
    )
    FRM.setReceiver(receiver)
    FRM.setUpdater(updater)
    FRM.trigger()
    FRM.start()

    print("[INFO] SleepRadar 即時呼吸偵測開始（關閉視窗或 Ctrl+C 結束）")

    try:
        sys.exit(app.exec_())
    except KeyboardInterrupt:
        pass
    finally:
        try:
            FRM.stop()
        except Exception:
            pass
        try:
            kgl.ksoclib.closeDevice()
        except Exception:
            pass
        print("[INFO] 已停止 mmWave 即時偵測。")


if __name__ == "__main__":
    main()
