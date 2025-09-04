import time
import logging
import multiprocessing
from multiprocessing import Queue
from logging.handlers import QueueHandler
from typing import List, Dict, Any

from PySide6.QtCore import QObject, Signal

from app.core.config import get_config
from app.core.ipc.triple_shared_buffer import TripleSharedBuffer
from app.core.ipc.double_shared_buffer import DoubleSharedBuffer, FrameData
from app.perception.capture_process import run_capture_process
from app.perception.engines.mumu import MumuCaptureEngine
from app.analysis.ruler_process import run_ruler_process
from app.control.commander_process import run_commander_process
from app.control.engine.mumu_macro_adapter import MumuMacroController
from app.analysis.plan_loader import PlanLoader
from app.analysis.recorder_process import run_recorder_process
from app.analysis.calibrator import run_calibration, CalibrationManager

logger = logging.getLogger(__name__)


class BackendManager:
    """管理所有后台进程的生命周期。"""

    def __init__(self):
        self.config = get_config()
        self.processes = []
        self.stop_event = None
        self.image_buffer = None
        self.frame_data_buffer = None
        self.commander_event_queue = None
        self.recorder_event_queue = None
        self.final_plan_queue = None # New queue for final plan data
        self.log_queue = None
        self.plan = None

    def _cleanup_ipc(self):
        """一个专门用来清理 IPC 资源的辅助方法。"""
        logger.info("正在清理上一次的 IPC 资源...")
        if self.image_buffer:
            self.image_buffer.close_and_unlink()
            self.image_buffer = None
        if self.frame_data_buffer:
            self.frame_data_buffer.close_and_unlink()
            self.frame_data_buffer = None
        time.sleep(0.1)
    
    def _initialize_ipc(self):
        """初始化图像和帧数据所需的IPC共享内存。"""
        # 在创建新的 IPC 之前，先确保旧的被完全清理
        self._cleanup_ipc()
        logger.info("正在初始化 IPC 资源...")
        try:
            temp_engine = MumuCaptureEngine(self.config)
            temp_engine.start()
            height, width = temp_engine.height, temp_engine.width
        finally:
            if 'temp_engine' in locals() and temp_engine:
                temp_engine.stop()
        logger.info(f"从模拟器获取到分辨率: {width}x{height}")

        self.image_ipc_params = {
            "name_prefix": f"ark_image_{time.time_ns()}",
            "height": height, "width": width, "channels": 4,
        }
        self.image_buffer = TripleSharedBuffer(**self.image_ipc_params, create=True)

        self.frame_ipc_params = {
            "name_prefix": f"ark_frame_{time.time_ns()}",
        }
        self.frame_data_buffer = DoubleSharedBuffer(**self.frame_ipc_params, create=True)
        logger.info("IPC 资源创建成功。")

    def start_run_mode(self, plan_name: str):
        """启动运行模式所需的所有进程。"""
        self.stop_event = multiprocessing.Event()
        self.commander_event_queue = multiprocessing.Queue()
        self._initialize_ipc()

        # 加载计划
        plan_loader = PlanLoader(self.config)
        self.plan = plan_loader.load(plan_name)
        if not self.plan:
            logger.error(f"作战计划 '{plan_name}' 为空或加载失败!")
            self.stop_all_processes()
            return

        # 1. Capture Process
        capture_proc = multiprocessing.Process(
            target=run_capture_process,
            name="CaptureProcess",
            args=(MumuCaptureEngine, self.config, self.image_ipc_params, self.stop_event),
        )
        self.processes.append(capture_proc)

        # 2. Ruler Process
        ruler_proc = multiprocessing.Process(
            target=run_ruler_process,
            name="RulerProcess",
            args=(self.config, self.image_ipc_params, self.frame_ipc_params, self.stop_event),
        )
        self.processes.append(ruler_proc)

        # 3. Commander Process
        commander_proc = multiprocessing.Process(
            target=run_commander_process,
            name="CommanderProcess",
            args=(
                self.config,
                self.frame_ipc_params,
                plan_name,
                MumuMacroController,
                {},  # controller_kwargs
                self.stop_event,
                self.commander_event_queue, 
                self.log_queue,
            ),
        )
        self.processes.append(commander_proc)

        for p in self.processes:
            p.start()
        logger.info(f"运行模式已启动，执行计划: {plan_name}")

    def start_record_mode(self, plan_name: str):
        """启动录制模式所需的所有进程。"""
        self.stop_event = multiprocessing.Event()
        self.recorder_event_queue = multiprocessing.Queue()
        self.final_plan_queue = multiprocessing.Queue() # Initialize the new queue
        self._initialize_ipc()

        # 1. Capture Process
        capture_proc = multiprocessing.Process(
            target=run_capture_process,
            name="CaptureProcess",
            args=(MumuCaptureEngine, self.config, self.image_ipc_params, self.stop_event),
        )
        self.processes.append(capture_proc)

        # 2. Ruler Process
        ruler_proc = multiprocessing.Process(
            target=run_ruler_process,
            name="RulerProcess",
            args=(self.config, self.image_ipc_params, self.frame_ipc_params, self.stop_event),
        )
        self.processes.append(ruler_proc)

        # 3. Recorder Process
        recorder_proc = multiprocessing.Process(
            target=run_recorder_process,
            name="RecorderProcess",
            args=(
                self.config,
                self.frame_ipc_params,
                plan_name,
                self.stop_event,
                self.recorder_event_queue,  
                self.log_queue,
                self.final_plan_queue, # Pass the new queue
            ),
        )
        self.processes.append(recorder_proc)

        for p in self.processes:
            p.start()
        logger.info(f"录制模式已启动，计划名称: {plan_name}")

    def stop_all_processes(self):
        """停止所有正在运行的后台进程并清理资源。"""
        if self.stop_event:
            logger.info("正在停止所有后台进程...")
            self.stop_event.set()

        for p in self.processes:
            try:
                p.join(timeout=5)
                if p.is_alive():
                    logger.warning(f"进程 {p.name} 未能正常退出，将强制终止。")
                    p.terminate()
            except Exception as e:
                logger.error(f"关闭进程 {p.name} 时出错: {e}")
        
        self.processes.clear()
        self.plan = None # 清理计划
        logger.info("所有后台进程已停止。")

        self._cleanup_ipc()

        logger.info("所有资源已清理。")

    def save_final_recorded_plan(self, plan_name: str, actions: List[Dict[str, Any]]):
        """将最终的录制计划发送给 Recorder 进程进行保存。"""
        if self.final_plan_queue:
            logger.info(f"将最终计划发送到 Recorder 进程进行保存: {plan_name}")
            self.final_plan_queue.put({"plan_name": plan_name, "actions": actions})
        else:
            logger.error("final_plan_queue 未初始化，无法保存最终计划。")

    def reload_config(self):
        self.config = get_config()
        return self.config

    def create_calibration_worker(self):
        return CalibrationWorker(self.config)

    def setup_log_queue(self) -> Queue:
        """配置日志系统，将日志重定向到队列。"""
        self.log_queue = Queue()
        # 获取根 logger，并移除所有现有的 handlers
        root_logger = logging.getLogger()
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
        
        # 添加队列 handler
        queue_handler = QueueHandler(self.log_queue)
        root_logger.addHandler(queue_handler)
        root_logger.setLevel(logging.INFO)
        return self.log_queue


class CalibrationWorker(QObject):
    """在独立线程中执行校准任务。"""
    calibration_progress = Signal(float)
    calibration_finished = Signal(str)
    calibration_failed = Signal(str)

    def __init__(self, config):
        super().__init__()
        self.config = config

    def run(self):
        engine = None
        try:
            logger.info("校准线程：正在启动截图引擎...")
            engine = MumuCaptureEngine(self.config)
            engine.start()
            logger.info(f"校准线程：引擎启动成功，分辨率: {engine.width}x{engine.height}")

            def on_progress(p: float):
                self.calibration_progress.emit(p)

            calibration_result = run_calibration(engine, progress_callback=on_progress)
            
            manager = CalibrationManager(self.config)
            basename = f"profile_{int(time.time())}"
            saved_path = manager.save(calibration_result, basename)
            logger.info(f"校准线程：成功保存校准文件: {saved_path}")
            self.calibration_finished.emit(saved_path)

        except Exception as e:
            logger.critical(f"校准过程中发生严重错误: {e}", exc_info=True)
            self.calibration_failed.emit(str(e))
        finally:
            if engine:
                engine.stop()
            logger.info("校准线程结束。")


class FrameDataWorker(QObject):
    """从 DoubleSharedBuffer 获取帧数据并发送信号。"""
    new_frame_data = Signal(object)  # 发送 FrameData 对象

    def __init__(self, frame_ipc_params):
        super().__init__()
        self.frame_ipc_params = frame_ipc_params
        self.frame_data_buffer = None
        self._is_stopped = False

    def run(self):
        self.frame_data_buffer = DoubleSharedBuffer(**self.frame_ipc_params, create=False)
        logger.info("FrameDataWorker 已连接到 DoubleSharedBuffer。")
        while not self._is_stopped:
            try:
                data = self.frame_data_buffer.get()
                if data and data.total_frames > 0:
                    self.new_frame_data.emit(data)
                # 短暂休眠以避免CPU空转
                time.sleep(0.01)
            except Exception as e:
                logger.error(f"FrameDataWorker 发生错误: {e}")
                break
        logger.info("FrameDataWorker 已停止。")

    def stop(self):
        self._is_stopped = True


class CommanderEventWorker(QObject):
    """从 Commander 进程的队列中获取事件并发送信号。"""
    new_event = Signal(dict)

    def __init__(self, queue: multiprocessing.Queue):
        super().__init__()
        self.queue = queue
        self._is_stopped = False

    def run(self):
        logger.info("CommanderEventWorker 已启动。")
        while not self._is_stopped:
            try:
                event = self.queue.get(timeout=0.1)
                self.new_event.emit(event)
            except Exception:
                continue
        logger.info("CommanderEventWorker 已停止。")

    def stop(self):
        self._is_stopped = True


class RecorderEventWorker(QObject):
    """从 Recorder 进程的队列中获取新动作并发送信号。"""
    new_action = Signal(dict)

    def __init__(self, queue: multiprocessing.Queue):
        super().__init__()
        self.queue = queue
        self._is_stopped = False

    def run(self):
        logger.info("RecorderEventWorker 已启动。")
        while not self._is_stopped:
            try:
                action = self.queue.get(timeout=0.1)
                self.new_action.emit(action)
            except Exception:
                continue
        logger.info("RecorderEventWorker 已停止。")

    def stop(self):
        self._is_stopped = True


class LogWorker(QObject):
    """从日志队列中获取日志记录并发送信号。"""
    new_log = Signal(str)

    def __init__(self, queue: Queue):
        super().__init__()
        self.queue = queue
        self._is_stopped = False

    def run(self):
        logger.info("LogWorker 已启动。")
        while not self._is_stopped:
            try:
                record = self.queue.get(timeout=0.1)
                if record:
                    self.new_log.emit(record.getMessage())
            except Exception:
                continue
        logger.info("LogWorker 已停止。")

    def stop(self):
        self._is_stopped = True