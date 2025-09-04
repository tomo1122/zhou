import sys
import os
import json
import gc
import contextlib

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QTabWidget, QTextEdit,
    QComboBox, QPushButton, QLineEdit, QTableWidget, QProgressBar, QListWidget,
    QTableWidgetItem, QAbstractItemView, QHeaderView, QLabel, QMessageBox
)
from PySide6.QtGui import QScreen, QMouseEvent, QCloseEvent, QColor
from PySide6.QtCore import Qt, QPoint, QThread, Signal

from app.core.config import PROJECT_ROOT
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ui.backend_connector import (
    BackendManager, FrameDataWorker, CommanderEventWorker, 
    RecorderEventWorker, LogWorker, CalibrationWorker
)

class FloatingOverlay(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet("""
            background-color: rgba(0, 0, 0, 180);
            color: white;
            border-radius: 10px;
            font-size: 14px;
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        self.total_frames_label = QLabel("总帧数: -")
        self.cycle_label = QLabel("周期: -")
        self.logical_frame_label = QLabel("逻辑帧: -/-")
        layout.addWidget(self.total_frames_label)
        layout.addWidget(self.cycle_label)
        layout.addWidget(self.logical_frame_label)
        self.drag_position = QPoint()

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent):
        if event.buttons() == Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self.drag_position)
            event.accept()

    def update_data(self, frame_data):
        if frame_data:
            self.total_frames_label.setText(f"总帧数: {frame_data.total_frames}")
            self.cycle_label.setText(f"周期: {frame_data.cycle_index}")
            self.logical_frame_label.setText(f"逻辑帧: {frame_data.logical_frame + 1}/{frame_data.total_frames_in_cycle}")

class MainControlPanel(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("控制面板")
        self.resize(500, 900)

        self.backend_manager = BackendManager()
        self.is_running = False
        self.is_recording = False
        self.is_calibrating = False

        self._init_ui()
        self._connect_signals()
        self._setup_logging()
        self._populate_plans()

        self.overlay = FloatingOverlay()
        self.overlay.show()
        self._move_to_right_side()

    def _init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)
        self.run_tab, self.record_tab, self.calibrate_tab, self.settings_tab = QWidget(), QWidget(), QWidget(), QWidget()
        self.tabs.addTab(self.run_tab, "运行")
        self.tabs.addTab(self.record_tab, "录制")
        self.tabs.addTab(self.calibrate_tab, "校准")
        self.tabs.addTab(self.settings_tab, "设置")
        self._create_run_tab()
        self._create_record_tab()
        self._create_calibrate_tab()
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        main_layout.addWidget(self.log_area)

    def _create_run_tab(self):
        layout = QVBoxLayout(self.run_tab)
        self.plan_selector = QComboBox()
        layout.addWidget(self.plan_selector)
        self.start_run_button = QPushButton("▶️ 开始运行")
        layout.addWidget(self.start_run_button)
        self.action_list = QListWidget()
        self.action_list.setToolTip("当前执行的动作序列")
        layout.addWidget(self.action_list)

    def _create_record_tab(self):
        layout = QVBoxLayout(self.record_tab)
        self.plan_filename_input = QLineEdit()
        self.plan_filename_input.setPlaceholderText("输入计划文件名...")
        layout.addWidget(self.plan_filename_input)
        self.start_record_button = QPushButton("⏺️ 开始录制")
        layout.addWidget(self.start_record_button)
        self.record_table = QTableWidget()
        self.record_table.setColumnCount(4)
        self.record_table.setHorizontalHeaderLabels(["触发帧", "动作类型", "参数", "备注"])
        self.record_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.record_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self.record_table)

    def _create_calibrate_tab(self):
        layout = QVBoxLayout(self.calibrate_tab)
        self.start_calibrate_button = QPushButton("⚙️ 开始校准")
        layout.addWidget(self.start_calibrate_button)
        self.calibrate_progress_bar = QProgressBar()
        layout.addWidget(self.calibrate_progress_bar)
        self.calibration_file_list = QListWidget()
        layout.addWidget(self.calibration_file_list)

    def _setup_logging(self):
        log_queue = self.backend_manager.setup_log_queue()
        self.log_thread = QThread()
        self.log_worker = LogWorker(log_queue)
        self.log_worker.moveToThread(self.log_thread)
        self.log_worker.new_log.connect(self.append_log)
        self.log_thread.started.connect(self.log_worker.run)
        self.log_thread.start()

    def _populate_plans(self):
        try:
            plans_dir = PROJECT_ROOT / 'plans'
            if os.path.exists(plans_dir):
                for f in os.listdir(plans_dir):
                    if f.endswith('.yaml'):
                        self.plan_selector.addItem(f.replace('.yaml', ''))
        except Exception as e:
            self.append_log(f"无法加载计划列表: {e}")

    def _connect_signals(self):
        self.start_run_button.clicked.connect(self._handle_run_clicked)
        self.start_record_button.clicked.connect(self._handle_record_clicked)
        self.start_calibrate_button.clicked.connect(self._handle_calibrate_clicked)
        self.tabs.currentChanged.connect(self._on_tab_changed)

    def _start_background_workers(self):
        self.frame_data_thread = QThread()
        self.frame_data_worker = FrameDataWorker(self.backend_manager.frame_ipc_params)
        self.frame_data_worker.moveToThread(self.frame_data_thread)
        self.frame_data_worker.new_frame_data.connect(self.overlay.update_data)
        self.frame_data_thread.started.connect(self.frame_data_worker.run)
        self.frame_data_thread.start()

    def _stop_background_workers(self):
        if hasattr(self, 'frame_data_worker'):
            self.frame_data_worker.stop()
            self.frame_data_thread.quit()
            self.frame_data_thread.wait()
        if hasattr(self, 'commander_worker'):
            self.commander_worker.stop()
            self.commander_thread.quit()
            self.commander_thread.wait()
        if hasattr(self, 'recorder_worker'):
            self.recorder_worker.stop()
            self.recorder_thread.quit()
            self.recorder_thread.wait()

    def _handle_run_clicked(self):
        if self.is_running:
            self._stop_run()
        else:
            plan_name = self.plan_selector.currentText()
            if not plan_name:
                QMessageBox.warning(self, "提示", "请先选择一个作战计划。У")
                return
            self.backend_manager.start_run_mode(plan_name)
            self._start_background_workers()
            self.commander_thread = QThread()
            self.commander_worker = CommanderEventWorker(self.backend_manager.commander_event_queue)
            self.commander_worker.moveToThread(self.commander_thread)
            self.commander_worker.new_event.connect(self._on_commander_event)
            self.commander_thread.started.connect(self.commander_worker.run)
            self.commander_thread.start()
            self.is_running = True
            self.start_run_button.setText("⏹️ 停止运行")
            self._update_ui_states()

    def _stop_run(self):
        self.backend_manager.stop_all_processes()
        self._stop_background_workers()
        self.is_running = False
        self.start_run_button.setText("▶️ 开始运行")
        self._update_ui_states()

    def _handle_record_clicked(self):
        if self.is_recording:
            self._stop_record()
        else:
            plan_name = self.plan_filename_input.text()
            if not plan_name:
                QMessageBox.warning(self, "提示", "请输入计划文件名。")
                return
            self.record_table.setRowCount(0)
            self.backend_manager.start_record_mode(plan_name)
            self._start_background_workers()
            self.recorder_thread = QThread()
            self.recorder_worker = RecorderEventWorker(self.backend_manager.recorder_event_queue)
            self.recorder_worker.moveToThread(self.recorder_thread)
            self.recorder_worker.new_action.connect(self._on_new_action_recorded)
            self.recorder_thread.started.connect(self.recorder_worker.run)
            self.recorder_thread.start()
            self.is_recording = True
            self.start_record_button.setText("⏹️ 结束录制")
            self._update_ui_states()

    def _stop_record(self):
        self.backend_manager.stop_all_processes()
        self._stop_background_workers()
        self.is_recording = False
        self.start_record_button.setText("⏺️ 开始录制")
        self._update_ui_states()

    def _handle_calibrate_clicked(self):
        self.is_calibrating = True
        self.start_calibrate_button.setText("校准中...")
        self._update_ui_states()

        self.calib_thread = QThread()
        self.calib_worker = self.backend_manager.create_calibration_worker()
        self.calib_worker.moveToThread(self.calib_thread)

        self.calib_worker.calibration_progress.connect(self._update_calibration_progress)
        self.calib_worker.calibration_finished.connect(self._on_calibration_finished)
        self.calib_worker.calibration_failed.connect(self._on_calibration_failed)
        self.calib_thread.started.connect(self.calib_worker.run)
        self.calib_thread.finished.connect(self.calib_thread.deleteLater)

        self.calib_thread.start()

    def append_log(self, text):
        self.log_area.append(text)

    def _on_commander_event(self, event):
        event_type = event.get('type')
        data = event.get('data', {})
        if event_type == 'state_change':
            self.action_list.clear()
            self.action_list.addItem(f"状态 -> {data.get('state')}")
        elif event_type == 'executing_action':
            self.action_list.clear()
            plan = self.backend_manager.plan
            if not plan: return
            current_index = data.get('index', 0)
            for i in range(current_index, min(current_index + 5, len(plan))):
                action_group = plan[i]
                trigger_frame = action_group.trigger_frame
                first_action = action_group.actions[0]
                action_type = first_action.action_type
                item_text = f"帧 {trigger_frame}: {action_type}"
                self.action_list.addItem(item_text)
            if self.action_list.count() > 0:
                current_item = self.action_list.item(0)
                current_item.setBackground(QColor('#4a69bd'))
                current_item.setForeground(QColor('white'))

    def _on_new_action_recorded(self, action):
        row_pos = self.record_table.rowCount()
        self.record_table.insertRow(row_pos)
        self.record_table.setItem(row_pos, 0, QTableWidgetItem(str(action.get('trigger_frame', ''))))
        self.record_table.setItem(row_pos, 1, QTableWidgetItem(action.get('action_type', '')))
        self.record_table.setItem(row_pos, 2, QTableWidgetItem(json.dumps(action.get('params', {}))))
        self.record_table.setItem(row_pos, 3, QTableWidgetItem(action.get('comment', '')))
        self.record_table.scrollToBottom()

    def _update_calibration_progress(self, value):
        self.calibrate_progress_bar.setValue(int(value))

    def _on_calibration_finished(self, saved_path):
        self.calibrate_progress_bar.setValue(100)
        QMessageBox.information(self, "成功", f"校准完成")
        self._finish_calibration()

    def _on_calibration_failed(self, error_msg):
        QMessageBox.critical(self, "失败", f"校准失败: {error_msg}")
        self._finish_calibration()

    def _finish_calibration(self):
        self.is_calibrating = False
        self.start_calibrate_button.setText("⚙️ 开始校准")
        self.calibrate_progress_bar.setValue(0)
        self._update_ui_states()

    def _update_ui_states(self):
        is_any_task_running = self.is_running or self.is_recording or self.is_calibrating

        # Update button enabled state
        self.start_run_button.setEnabled(not is_any_task_running or self.is_running)
        self.start_record_button.setEnabled(not is_any_task_running or self.is_recording)
        self.start_calibrate_button.setEnabled(not is_any_task_running or self.is_calibrating)

        # Update tab enabled state
        self.tabs.setTabEnabled(0, not (self.is_recording or self.is_calibrating))
        self.tabs.setTabEnabled(1, not (self.is_running or self.is_calibrating))
        self.tabs.setTabEnabled(2, not (self.is_running or self.is_recording))

    def _on_tab_changed(self, index):
        self._update_ui_states()

    def closeEvent(self, event: QCloseEvent):
        self.append_log("正在关闭应用程序...")
        self.overlay.close()
        self._stop_background_workers()
        self.backend_manager.stop_all_processes()
        if hasattr(self, 'log_worker'):
            self.log_worker.stop()
            self.log_thread.quit()
            self.log_thread.wait()
        if hasattr(self, 'calib_thread') and self.calib_thread.isRunning():
            self.calib_thread.quit()
            self.calib_thread.wait()

        self.append_log("正在进行最后的资源清理...")
        with contextlib.suppress(BufferError):
            gc.collect()
        event.accept()

    def _move_to_right_side(self):
        try:
            primary_screen = self.screen()
            if primary_screen:
                screen_geometry = primary_screen.availableGeometry()
                x = screen_geometry.width() - self.width()
                y = (screen_geometry.height() - self.height()) // 2
                self.move(x, y)
        except Exception as e:
            self.append_log(f"无法移动窗口: {e}")

def main():
    app = QApplication(sys.argv)
    main_panel = MainControlPanel()
    main_panel.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
