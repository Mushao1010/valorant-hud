"""GUI 主窗口。

「作为发送端」标签页：
- 启动时用窗口枚举填充 comboBox_2（游戏窗口选择）。
- 点「开始读取计分板」→ 后台 QThread 启动识别并长按 Tab；再点一次停止。
- 初始化识别的名称/干员自动回填到对应玩家行，供用户确认/修改。
- 「TAB键刷新」：计分板被手动 Tab 收起后重新按下展开。
- 「计分板预览」：弹出预览窗，用可调的 91:47 黄色框调整计分板读取范围（写回 config）。
- 点「确认无误后开始发送数据」→ 写 output/sent.json 并启动 WebSocket 广播；
  再点一次停止发送。接收端填 ws://本机IP:8765 连接。
识别结果持续写入 output/scoreboard.json。

「作为接收端」标签页：
- 发送端开 WebSocket Server、接收端连接。填写发送端地址点「连接」，数据预览表格刷新。
- 「启动HUD覆盖层」在游戏画面上显示全屏透明 HUD，数据复用同一接收端连接；
  再点一次（或按 Esc）隐藏。
- 「设置HUD布局」的 4 个参数（左右距离/缩放大小/行间隙/底部距离）实时生效并持久化。
"""

from __future__ import annotations

import json
import os
import sys
import time

# 冻结（PyInstaller）时把 PaddleX 模型缓存指到打包目录，实现离线自包含。
# 必须在 paddleocr/paddlex 首次 import 之前设置（PaddleOcrEngine.__init__ 才懒加载）。
if getattr(sys, "frozen", False):
    from config import PROJECT_ROOT

    os.environ.setdefault("PADDLE_PDX_CACHE_HOME", os.path.join(PROJECT_ROOT, "paddlex_cache"))

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QFontDialog,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)
from ui_main_window import Ui_Form

from broadcast_server import BroadcastServer
from config import HudConfig, load_config, save_hud
from hud_overlay import HUDOverlay
from preview_dialog import ScoreboardPreviewDialog
from receiver.game_state import GameState
from receiver.ws_client import WsClient
from recognition.recognition_worker import RecognitionWorker
from recognition.scoreboard import FIELD_KEYS
from recognition.ult_costs import resolve_ult, ult_entries

# slot(1~10) -> (agent 下拉控件, 名称输入框控件) 属性名，与 main_window.ui 布局对应
_SLOT_WIDGETS = {
    1: ("comboBox_15", "lineEdit_14"),
    2: ("comboBox_19", "lineEdit_18"),
    3: ("comboBox_21", "lineEdit_20"),
    4: ("comboBox_23", "lineEdit_23"),
    5: ("comboBox_25", "lineEdit_25"),
    6: ("comboBox_16", "lineEdit_15"),
    7: ("comboBox_20", "lineEdit_19"),
    8: ("comboBox_22", "lineEdit_22"),
    9: ("comboBox_24", "lineEdit_24"),
    10: ("comboBox_26", "lineEdit_26"),
}

# 发送端广播服务监听端口（接收端默认地址 ws://本机IP:8765）
_BROADCAST_PORT = 8765

# 接收端数据预览表列
_TABLE_COLUMNS = ("slot", "名称", "干员", "阵营", "存活", "击杀", "死亡", "助攻", "经济", "武器", "护甲", "大招")
_SIDE_TEXT = {"attack": "进攻方", "defense": "防守方"}
_DEFAULT_URL = "ws://127.0.0.1:8765"


def _agent_key(name: str) -> str:
    """干员英文名规范化：组合框 'K/O KAY/O' 的 KAY/O → 模板名 'KAYO'。"""
    return name.replace("/", "")


class _ComboRefreshFilter(QObject):
    """拦截窗口选择组合框的鼠标按下：点开下拉前刷新进程列表（保留当前选择）。"""

    def __init__(self, combo, refresh):
        super().__init__(combo)
        self._combo = combo
        self._refresh = refresh

    def eventFilter(self, watched, event) -> bool:
        if watched is self._combo and event.type() == QEvent.Type.MouseButtonPress:
            self._refresh()
        return False


class _GuiHud(HUDOverlay):
    """GUI 内联启动的 HUD：Esc 只关闭覆盖层，不退出整个应用。"""

    hud_closed = Signal()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        self.hud_closed.emit()
        super().closeEvent(event)


class MyWindow(QWidget, Ui_Form):
    def __init__(self):
        super().__init__()
        self.setupUi(self)
        # 窗口左上角图标（exe 文件图标由 PyInstaller 嵌入，这里是运行时窗口图标）
        from config import PROJECT_ROOT

        self.setWindowIcon(QIcon(os.path.join(PROJECT_ROOT, "icon.png")))
        self._worker: RecognitionWorker | None = None
        self._windows = []
        self._time_sum = 0.0
        self._time_count = 0
        self._identity_row: dict[int, int] = {i: i for i in range(1, 11)}
        self._last_roster_agents = None
        self._ult_entries: list | None = None  # 名单干员 → cost 反查缓存（懒加载）
        self._ws_client: WsClient | None = None
        self._hud_overlay: _GuiHud | None = None
        self._hud_conn = None
        self._hud_params = {"margin_side": 0, "scale": 1.0, "spacing": 30, "margin_bottom": 50}
        self._hud_font: QFont | None = None
        self._rx_count = 0
        self._rx_sum = 0.0
        self._rx_last_ts = None
        self._rx_iv = 0.0

        # 发送端
        self.pushButton_5.clicked.connect(self._toggle_recognition)
        self.pushButton_11.clicked.connect(self._toggle_send)
        self.pushButton.clicked.connect(self._on_tab_refresh)
        self.pushButton_2.clicked.connect(self._on_preview)
        self._refresh_windows()
        self._install_combo_refresh()
        self._setup_broadcast_server()
        # 接收端
        self._setup_receiver_tab()

    # ---------------- 发送端：窗口选择 ----------------

    def _refresh_windows(self) -> None:
        from capture import list_windows, set_dpi_awareness

        set_dpi_awareness()
        self._windows = list_windows()
        current = self.comboBox_2.currentData()
        self.comboBox_2.clear()
        for w in self._windows:
            self.comboBox_2.addItem(f"{w.title}  [{w.hwnd}]", w.hwnd)
        if current is not None:
            idx = self.comboBox_2.findData(current)
            if idx >= 0:
                self.comboBox_2.setCurrentIndex(idx)
                return
        if self._windows:
            self.comboBox_2.setCurrentIndex(0)

    def _install_combo_refresh(self) -> None:
        """点击窗口选择下拉框时自动刷新进程列表（先开程序再开游戏也能选到）。"""
        self._combo_filter = _ComboRefreshFilter(self.comboBox_2, self._refresh_windows)
        self.comboBox_2.installEventFilter(self._combo_filter)

    # ---------------- 发送端：识别开关 ----------------

    def _toggle_recognition(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.stop()
            self.pushButton_5.setEnabled(False)
            self.pushButton_5.setText("正在停止...")
            return

        index = self.comboBox_2.currentIndex()
        if index < 0 or not self._windows:
            QMessageBox.warning(self, "提示", "请先在列表中选择游戏窗口")
            return
        hwnd = self._windows[index].hwnd

        self._worker = RecognitionWorker(hwnd)
        self._worker.status.connect(self.label_30.setText)
        self._worker.init_done.connect(self._on_init_done)
        self._worker.error.connect(self._on_worker_error)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.frame_time.connect(self._on_frame_time)
        self._worker.frame_state.connect(self._on_frame_state)
        self._time_sum = 0.0
        self._time_count = 0
        self.label_8.setText("帧处理：-- | 平均：--")
        self.pushButton_5.setText("停止读取计分板")
        self._worker.start()

    def _on_tab_refresh(self) -> None:
        """「TAB键刷新」：计分板被手动 Tab 收起后重新按下展开。"""
        if self._worker is not None and self._worker.isRunning():
            ok = self._worker.refresh_tab()
            if ok:
                self.label_30.setText("已重新按下 Tab")
            else:
                self.label_30.setText("切前台失败：切回游戏将自动恢复计分板")
        else:
            QMessageBox.warning(self, "提示", "请先开始读取计分板")

    def _on_preview(self) -> None:
        """「计分板预览」：弹预览窗调整黄色 91:47 框（计分板读取范围）。

        识别未在跑时预览自建采集 + Tab 看门狗（own_capture），实现「先预览、后开始
        读取计分板」；识别在跑时沿用识别循环已积累的缓存，不重复采集。
        """
        index = self.comboBox_2.currentIndex()
        if index < 0 or not self._windows:
            QMessageBox.warning(self, "提示", "请先在列表中选择游戏窗口")
            return
        hwnd = self._windows[index].hwnd
        recognition_running = self._worker is not None and self._worker.isRunning()
        dlg = ScoreboardPreviewDialog(hwnd, self, own_capture=not recognition_running)
        dlg.exec()

    def _on_init_done(self, players) -> None:
        # 新一局开始：身份 i 初始假设在其物理行 i，后续由 (team_group, agent) 实时跟踪
        self._identity_row = {i: i for i in range(1, 11)}
        for p in players:
            slot = p["slot"]
            combo_name, lineedit_name = _SLOT_WIDGETS.get(slot, (None, None))
            if not combo_name:
                continue
            combo = getattr(self, combo_name)
            lineedit = getattr(self, lineedit_name)
            name = p["name"].get("value")
            agent = p["agent"].get("value")
            lineedit.setText(name if name else "")
            self._select_agent(combo, agent)

    @staticmethod
    def _select_agent(combo, agent: str | None) -> None:
        if not agent:
            return
        key = _agent_key(agent)
        for i in range(combo.count()):
            text = combo.itemText(i).strip()
            parts = text.split()
            if parts and _agent_key(parts[-1]) == key:
                combo.setCurrentIndex(i)
                return
            if key in _agent_key(text):
                combo.setCurrentIndex(i)
                return

    def _on_frame_time(self, process_ms: float) -> None:
        self._time_sum += process_ms
        self._time_count += 1
        avg = self._time_sum / self._time_count
        self.label_8.setText(f"帧处理：{process_ms:.1f} ms | 平均：{avg:.1f} ms")

    def _on_worker_finished(self) -> None:
        self.pushButton_5.setEnabled(True)
        self.pushButton_5.setText("开始读取计分板")
        self.label_30.setText("识别已停止")
        self.label_8.setText("帧处理：-- | 平均：--")

    def _on_worker_error(self, message: str) -> None:
        self._on_worker_finished()
        self.label_30.setText("识别出错")
        QMessageBox.critical(self, "识别错误", message)

    # ---------------- 发送端：广播 ----------------

    def _setup_broadcast_server(self) -> None:
        """发送端广播服务：点「确认无误后开始发送数据」开服，每识别一帧推送一帧。"""
        self._broadcast = BroadcastServer(self)
        self._broadcast.started.connect(self._on_server_started)
        self._broadcast.stopped.connect(self._on_server_stopped)
        self._broadcast.failed.connect(self._on_server_failed)
        self._broadcast.client_count.connect(self._on_server_client_count)
        self._server_addr = ""
        cfg = load_config()
        self._scoreboard_path = cfg.resolve(cfg.json_output.scoreboard)
        self._sent_path = cfg.resolve("output/sent.json")

    def _toggle_send(self) -> None:
        """「确认无误后开始发送数据」↔「停止发送数据」开关。"""
        if self._broadcast.is_running:
            self._broadcast.stop()
            return

        if not os.path.exists(self._scoreboard_path):
            QMessageBox.warning(self, "提示", "尚无识别数据（output/scoreboard.json 不存在），请先开始识别")
            return
        if not self._broadcast.start(_BROADCAST_PORT):
            return

    def _on_frame_state(self, scoreboard: dict) -> None:
        """每识别一帧：若正在发送，按名单+实时干员重排身份→写 sent.json→广播一帧。"""
        if not self._broadcast.is_running:
            return
        roster = self._read_roster()
        scoreboard = self._remap_by_identity(scoreboard, roster)
        agents = [agent for _, agent in roster.values() if agent]
        if agents != self._last_roster_agents:
            self._last_roster_agents = agents
            if self._worker is not None:
                self._worker.set_roster(agents)
        self._persist_sent(scoreboard)
        self._broadcast.broadcast_data(scoreboard)

    def _read_roster(self) -> dict:
        """从 GUI 控件读确认名单：slot -> (名称, 干员)。"""
        roster = {}
        for slot, (combo_name, lineedit_name) in _SLOT_WIDGETS.items():
            combo = getattr(self, combo_name)
            lineedit = getattr(self, lineedit_name)
            name = lineedit.text().strip()
            agent_text = combo.currentText().strip()
            agent = agent_text.split()[-1] if agent_text else None
            roster[slot] = (name or None, _agent_key(agent) if agent else None)
        return roster

    def _ult_cost(self, agent: str | None) -> int | None:
        """干员名 → 大招点数上限（cost）：按 images/ult/{Agent}_{cost}.png 反查，懒扫一次缓存。"""
        if not agent:
            return None
        if self._ult_entries is None:
            cfg = load_config()
            self._ult_entries = ult_entries(cfg.resolve(cfg.templates.ult_dir))
        hit = resolve_ult(self._ult_entries, agent)
        return hit[1] if hit else None

    def _remap_by_identity(self, scoreboard: dict, roster: dict) -> dict:
        """按名单+实时干员把物理行数据映射回身份固定 slot（slot 跟随干员移动）。

        行匹配后对每队做「排除法兜底」：若该队恰好 1 行干员识别失败（agent=None）
        且恰好 1 个身份未匹配上，就把该身份派给该行。避免单个干员识别失败
        （如炼狱模板匹配偏低）导致身份滞留旧行、读到已换行的别人的数据。
        """
        rows = {p.get("slot"): p for p in scoreboard.get("players", [])}
        identity_map = {}
        for slot, (_, agent) in roster.items():
            if agent:
                identity_map[(0 if slot <= 5 else 1, agent)] = slot
        matched_rows: set[int] = set()
        matched_slots: set[int] = set()
        for row, p in rows.items():
            agent = (p.get("agent") or {}).get("value")
            slot = identity_map.get((0 if row <= 5 else 1, agent))
            if slot is not None:
                self._identity_row[slot] = row
                matched_rows.add(row)
                matched_slots.add(slot)
        # 排除法兜底：仅在恰好一对「失败行 / 未匹配身份」时派发，多对则保持原状
        for team in (0, 1):
            team_rows = [r for r in rows if r != 0 and (0 if r <= 5 else 1) == team]
            team_slots = [s for s, (_, a) in roster.items() if a and (0 if s <= 5 else 1) == team]
            unmatched_rows = [r for r in team_rows if r not in matched_rows]
            unmatched_slots = [s for s in team_slots if s not in matched_slots]
            if len(unmatched_rows) == 1 and len(unmatched_slots) == 1:
                self._identity_row[unmatched_slots[0]] = unmatched_rows[0]
        players = []
        for slot in range(1, 11):
            name, agent = roster.get(slot, (None, None))
            src = rows.get(self._identity_row[slot])
            new_p = {"slot": slot}
            for key in FIELD_KEYS:
                new_p[key] = (
                    dict(src.get(key, {"value": None, "confidence": 0.0}))
                    if src else {"value": None, "confidence": 0.0}
                )
            new_p["name"] = {"value": name, "confidence": 1.0 if name else 0.0}
            new_p["agent"] = {"value": agent, "confidence": 1.0 if agent else 0.0}
            # 大招上限按当前显示的名单干员重算：本 slot 的 ult 数值来自物理行
            # (rows[identity_row])，engine 在识别时按「该行实时干员 OCR」推上限——死亡/暗
            # 头像在开始读取时会被误配成名单内别的干员且分仍过阈，两人 cost 不同则上限错。
            # 而头像/名字/球内大招图标都来自名单干员（上面已盖章），上限却随行 OCR 走 →
            # 大招球环（成本类）对不上显示的干员。名单干员才是权威，按它 cost 重写 max；
            # 就绪(无数字)没有 max，保持不动。
            if agent:
                cost = self._ult_cost(agent)
                if cost is not None:
                    uv = new_p.get("ult", {}).get("value")
                    if isinstance(uv, dict) and not uv.get("ready"):
                        uv["max"] = cost
            players.append(new_p)
        return {**scoreboard, "players": players}

    def _persist_sent(self, data: dict) -> str:
        """写 sent_at 并原子写 sent.json；返回路径。"""
        data["sent_at"] = time.time()
        os.makedirs(os.path.dirname(self._sent_path), exist_ok=True)
        with open(self._sent_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return self._sent_path

    def _on_server_started(self, addr: str) -> None:
        self._server_addr = addr
        self.pushButton_11.setText("停止发送数据")
        self.label_7.setText(f"发送中: {addr} · 等待接收端连接")
        self.label_7.setStyleSheet("font-weight:bold; color:#0a0;")

    def _on_server_stopped(self) -> None:
        self._server_addr = ""
        self.pushButton_11.setText("确认无误后开始发送数据")
        self.label_7.setText("已停止发送")
        self.label_7.setStyleSheet("font-weight:bold; color:#888;")

    def _on_server_failed(self, message: str) -> None:
        self._server_addr = ""
        self.pushButton_11.setText("确认无误后开始发送数据")
        self.label_7.setText(message)
        self.label_7.setStyleSheet("font-weight:bold; color:#c00;")

    def _on_server_client_count(self, count: int) -> None:
        if self._server_addr:
            self.label_7.setText(f"发送中: {self._server_addr} · {count} 个接收端连接")

    # ---------------- 接收端：连接与数据 ----------------

    def _setup_receiver_tab(self) -> None:
        """「作为接收端」页：接线 main_window.ui 内建控件（连接 / HUD / 布局 / 数据预览表）。"""
        self._ws_client = WsClient()
        self._ws_client.connection_state.connect(self._set_conn_state)
        self._ws_client.game_state_received.connect(self._on_game_state)
        self._ws_client.error_occurred.connect(self._on_ws_error)
        self.pushButton_3.clicked.connect(self._toggle_connect)
        self.pushButton_7.clicked.connect(self._toggle_hud)
        self.pushButton_4.clicked.connect(self._on_hud_font)
        self.lineEdit.setText(_DEFAULT_URL)
        self._setup_table()
        self._load_hud_layout()

    def _toggle_connect(self) -> None:
        if self._ws_client.is_connected:
            self._ws_client.disconnect()
        else:
            url = self.lineEdit.text().strip() or _DEFAULT_URL
            self._ws_client.connect_to(url)

    def _set_conn_state(self, text: str) -> None:
        self.label_6.setText(text)
        green = text == "已连接"
        self.label_6.setStyleSheet(f"font-weight:bold; color:{'#0a0' if green else '#c00'};")
        self.pushButton_3.setText("断开" if green else "连接")

    def _on_ws_error(self, message: str) -> None:
        self.label_6.setText(message)
        self.label_6.setStyleSheet("font-weight:bold; color:#c00;")

    def _on_game_state(self, gs: GameState) -> None:
        """每帧刷新：帧间隔统计 + 数据预览表。"""
        self._rx_count += 1
        ts = gs.timestamp
        if ts is not None and self._rx_last_ts is not None and ts > self._rx_last_ts:
            self._rx_iv = ts - self._rx_last_ts
            self._rx_sum += self._rx_iv
        if ts is not None:
            self._rx_last_ts = ts
        avg = self._rx_sum / max(1, self._rx_count - 1)
        self.label_4.setText(
            f"帧间隔：{self._rx_iv * 1000:.1f}ms | 平均：{avg * 1000:.1f}ms | 已接收{self._rx_count}帧"
        )
        self._clear_table()
        for i, p in enumerate(gs.players[:10]):
            ult = self._ult(p)
            row = [
                str(p.slot),
                self._text(p.name),
                self._text(p.agent),
                _SIDE_TEXT.get(p.side, self._text(p.side)),
                self._alive(p.alive),
                self._text(p.kills),
                self._text(p.deaths),
                self._text(p.assists),
                self._text(p.credits),
                self._text(p.weapon),
                self._text(p.armor),
                ult,
            ]
            for c, val in enumerate(row):
                self.tableWidget.setItem(i, c, QTableWidgetItem(val))

    @staticmethod
    def _ult(p) -> str:
        """大招格：就绪 → "就绪"；有当前/上限 → "当前/上限"；否则 "—"。"""
        if p.ult_ready:
            return "就绪"
        if p.ult_current is not None and p.ult_max is not None:
            return f"{p.ult_current}/{p.ult_max}"
        if p.ult_current is not None:
            return str(p.ult_current)
        return "—"

    def _setup_table(self) -> None:
        self.tableWidget.setColumnCount(len(_TABLE_COLUMNS))
        self.tableWidget.setHorizontalHeaderLabels(_TABLE_COLUMNS)
        self.tableWidget.setRowCount(10)
        self.tableWidget.verticalHeader().setVisible(False)
        self.tableWidget.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self.tableWidget.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._clear_table()

    def _clear_table(self) -> None:
        for r in range(self.tableWidget.rowCount()):
            for c in range(self.tableWidget.columnCount()):
                self.tableWidget.setItem(r, c, QTableWidgetItem("—"))

    @staticmethod
    def _text(v) -> str:
        return "—" if v is None else str(v)

    @staticmethod
    def _alive(v) -> str:
        if v is None:
            return "—"
        return "存活" if v else "死亡"

    # ---------------- 接收端：HUD 覆盖层与布局 ----------------

    def _toggle_hud(self) -> None:
        if self._hud_overlay is not None and self._hud_overlay.isVisible():
            if self._hud_conn is not None:
                self._ws_client.game_state_received.disconnect(self._hud_conn)
                self._hud_conn = None
            self._hud_overlay.hide()
            self.pushButton_7.setText("启动HUD覆盖层")
            return
        if self._hud_overlay is None:
            self._hud_overlay = _GuiHud()
            self._hud_overlay.hud_closed.connect(self._on_hud_closed)
        self._hud_overlay.set_layout(**self._hud_params)
        if self._hud_font is not None:
            self._hud_overlay.set_font(self._hud_font)
        if self._hud_conn is None:
            self._hud_conn = self._ws_client.game_state_received.connect(
                lambda gs: self._hud_overlay.set_players(gs.players, gs.spike_carrier)
            )
        self._hud_overlay.showFullScreen()
        self.pushButton_7.setText("关闭HUD覆盖层")

    def _on_hud_closed(self) -> None:
        """HUD 上按 Esc 关闭后：断开数据流并复位按钮（覆盖层实例保留，可再次启动）。"""
        if self._hud_conn is not None:
            self._ws_client.game_state_received.disconnect(self._hud_conn)
            self._hud_conn = None
        self.pushButton_7.setText("启动HUD覆盖层")

    def _load_hud_layout(self) -> None:
        """「设置HUD布局」：读 config.hud 填入 4 个输入框，编辑完成后生效并持久化。"""
        hud = load_config().hud
        self.lineEdit_2.setText(str(hud.margin_side))
        self.lineEdit_4.setText(str(hud.scale))
        self.lineEdit_3.setText(str(hud.spacing))
        self.lineEdit_5.setText(str(hud.margin_bottom))
        self._hud_params = {
            "margin_side": hud.margin_side,
            "scale": hud.scale,
            "spacing": hud.spacing,
            "margin_bottom": hud.margin_bottom,
        }
        self._hud_font = None
        if hud.font:
            f = QFont()
            if f.fromString(hud.font):
                self._hud_font = f
        for edit in (self.lineEdit_2, self.lineEdit_4, self.lineEdit_3, self.lineEdit_5):
            edit.editingFinished.connect(self._on_hud_param_edited)

    def _on_hud_font(self) -> None:
        """「改变文本样式」：QFontDialog 选择 HUD 文字字体，即时应用并持久化。"""
        current = self._hud_font or QFont("字体圈伟君黑 W2", 12)
        ok, font = QFontDialog.getFont(current, self, "选择 HUD 文字样式")
        if not ok:
            return
        self._hud_font = font
        if self._hud_overlay is not None:
            self._hud_overlay.set_font(font)
        hud = load_config().hud
        save_hud(HudConfig(
            margin_side=hud.margin_side, scale=hud.scale,
            spacing=hud.spacing, margin_bottom=hud.margin_bottom,
            font=font.toString(),
        ))

    def _parse_hud(self) -> dict | None:
        """解析 4 个输入框为布局参数并钳制；非法输入返回 None。"""
        try:
            margin_side = int(self.lineEdit_2.text().strip() or 0)
            scale = float(self.lineEdit_4.text().strip() or 1.0)
            spacing = int(self.lineEdit_3.text().strip() or 30)
            margin_bottom = int(self.lineEdit_5.text().strip() or 50)
        except ValueError:
            return None
        return {
            "margin_side": min(max(margin_side, 0), 500),
            "scale": min(max(scale, 0.2), 3.0),
            "spacing": min(max(spacing, 0), 300),
            "margin_bottom": min(max(margin_bottom, 0), 500),
        }

    def _on_hud_param_edited(self) -> None:
        values = self._parse_hud()
        if values is None:
            return
        self._hud_params = values
        if self._hud_overlay is not None:
            self._hud_overlay.set_layout(**values)
        hud = load_config().hud
        save_hud(HudConfig(font=hud.font, **values))

    # ---------------- 关闭 ----------------

    def closeEvent(self, event) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(5000)
        if self._ws_client is not None:
            self._ws_client.stop()
        self._broadcast.stop()
        if self._hud_overlay is not None:
            self._hud_overlay.close()
        super().closeEvent(event)


if __name__ == "__main__":
    import sys

    app = QApplication(sys.argv)
    window = MyWindow()
    window.show()
    sys.exit(app.exec())
