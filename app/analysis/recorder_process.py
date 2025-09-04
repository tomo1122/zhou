import time
import signal
import logging
import multiprocessing

import yaml

from dataclasses import dataclass
from collections import defaultdict
from typing import Dict, List, Any, Optional, Tuple
from logging.handlers import QueueHandler
from multiprocessing.synchronize import Event as SyncEvent

from pynput import mouse, keyboard

from app.core.config import MergedConfig
from app.core.ipc.double_shared_buffer import DoubleSharedBuffer, FrameData
from app.utils.windows_utils import WindowHelper


logger = logging.getLogger(__name__)


@dataclass
class DeployAction:
    trigger_frame: int
    start_pos: Tuple[int, int]
    end_pos: Optional[Tuple[int, int]] = None
    left_start_pos: Optional[Tuple[int, int]] = None
    direction: Optional[str] = None


class ActionRecorder:
    """
    负责监听用户输入，关联帧数，并记录为结构化动作。
    """

    def __init__(self, config: MergedConfig, frame_buffer: DoubleSharedBuffer, output_plan_path: str, event_queue: Optional[multiprocessing.Queue] = None, final_plan_queue: Optional[multiprocessing.Queue] = None):
        self.config = config
        self.frame_buffer = frame_buffer
        self.output_plan_path = output_plan_path
        self.event_queue = event_queue
        self.final_plan_queue = final_plan_queue 
        self.target_w = 1920
        self.target_h = 1080
        # 干员栏的相对区域
        self.op_bar_rect = [
            self.target_w * 0,
            self.target_h * 0.833,
            self.target_w * 1,
            self.target_h * 1
        ]

        # 使用 WindowHelper 来处理窗口交互
        self.win_helper = WindowHelper(
            main_window_title = config.mumu_window_title,
            render_window_class = config.mumu_render_class,
            target_resolution = (self.target_w, self.target_h)
        )

        self.recorded_actions: List[Dict[str, Any]] = []
        self.mouse_listener = None
        self.keyboard_listener = None
        self.mouse_controller = mouse.Controller()
        self.is_running = False
        self._drag_info = None


    def start(self):
        """启动监听器并开始录制。"""
        if self.is_running:
            return
        
        try:
            self.win_helper.connect()
            logger.info("窗口辅助工具已连接，录制器准备就绪。")
        except ConnectionError as e:
            logger.critical(f"无法启动录制器，因为连接窗口失败: {e}")
            return

        self.mouse_listener = mouse.Listener(on_click=self._on_click)
        self.keyboard_listener = keyboard.Listener(on_press=self._on_press)
        
        self.mouse_listener.start()
        self.keyboard_listener.start()
        self.is_running = True
        logger.info(f"已开始监听 '{self.win_helper.main_window_title}' 内的鼠标和键盘操作。")

    def stop(self):
        """停止监听器并保存录制的动作。"""
        if not self.is_running:
            return
            
        if self.mouse_listener:
            self.mouse_listener.stop()
        if self.keyboard_listener:
            self.keyboard_listener.stop()
            
        self.is_running = False
        logger.info("已停止监听。")
        self._save_plan()

    def _is_in_op_bar(self, virtual_pos: tuple) -> bool:
        """检查一个虚拟坐标是否在干员栏区域内。"""
        x, y = virtual_pos
        x1, y1, x2, y2 = self.op_bar_rect
        return x1 <= x <= x2 and y1 <= y <= y2
    
    def _get_current_frame_data(self) -> FrameData:
        """从IPC获取最新的帧数据。"""
        return self.frame_buffer.get()

    def _on_click(self, x: int, y: int, button: mouse.Button, pressed: bool):
        # 只在目标窗口是前景窗口时才记录
        is_fg = self.win_helper.is_foreground_window()
        if not is_fg:
            return

        self.win_helper.update_render_area()
        virtual_pos = self.win_helper.transform_screen_to_virtual((x, y))
        if not virtual_pos:
            return

        frame_data = self._get_current_frame_data()
        if pressed:
            self._handle_press(button, virtual_pos, frame_data)
        else:
            if button in [mouse.Button.left, mouse.Button.right]:
                self._handle_release(button, virtual_pos, frame_data)
            elif button == mouse.Button.x2:
                self._record_action(frame_data.total_frames, "skill", {"pos": list(virtual_pos)})
                logger.info(f"[帧: {frame_data.total_frames}] skill: {virtual_pos}")
                return
            elif button == mouse.Button.x1:
                self._record_action(frame_data.total_frames, "recall", {"pos": list(virtual_pos)})
                logger.info(f"[帧: {frame_data.total_frames}] recall: {virtual_pos}")
                return 

    def _on_press(self, key: keyboard.Key):
        """键盘按键事件的回调处理函数"""
        # 只在目标窗口是前景窗口时才记录
        if not self.win_helper.is_foreground_window():
            return

        try:
            key_char = key.char
        except AttributeError:
            return

        # 获取通用信息
        frame_data = self._get_current_frame_data()
        self.win_helper.update_render_area()
        current_mouse_pos = self.mouse_controller.position
        virtual_pos = self.win_helper.transform_screen_to_virtual(current_mouse_pos)
        if not virtual_pos:
            logger.warning(f"按键 '{key_char}' 时鼠标在窗口外，不记录动作。")
            return
        
        # 1. 手动撤离: 按 'q'
        if key_char == 'q':
            self._record_action(frame_data.total_frames, "recall", {"pos": list(virtual_pos)})
            logger.info(f"[帧: {frame_data.total_frames}] recall (q): {virtual_pos}")
            return
            
        # 2. 手动技能: 按 'e'
        if key_char == 'e':
            self._record_action(frame_data.total_frames, "skill", {"pos": list(virtual_pos)})
            logger.info(f"[帧: {frame_data.total_frames}] skill (e): {virtual_pos}")
            return

    def _record_action(self, frame: int, action_type: str, params: dict):
        action = {
            "trigger_frame": frame,
            "action_type": action_type,
            "params": params
        }
        self.recorded_actions.append(action)
        logger.info(f"动作录制: {action}")
        if self.event_queue:
            self.event_queue.put(action)

    def _handle_press(self, button: mouse.Button, pos: Tuple[int, int], frame_data: FrameData):
        """处理鼠标按下事件"""
        # 部署 step1：左/右键，起点在干员栏
        if button in [mouse.Button.left, mouse.Button.right] and self._is_in_op_bar(pos):
            self._drag_info = DeployAction(
                trigger_frame=frame_data.total_frames,
                start_pos=tuple(pos),
            )
            logger.info(f"[帧: {frame_data.total_frames}] 部署起始点: {pos}")
            return

        # 部署 step2：左键，且上一步已经结束
        if button == mouse.Button.left and isinstance(self._drag_info, DeployAction):
            if self._drag_info.end_pos and \
               abs(pos[0] - self._drag_info.end_pos[0]) < (self.target_w * 0.06) and \
               abs(pos[1] - self._drag_info.end_pos[1]) < (self.target_h * 0.12):
                self._drag_info.left_start_pos = tuple(pos)
                logger.info(f"[帧: {frame_data.total_frames}] 部署-方向选择 起始点: {pos}")
                        
    def _handle_release(self, button: mouse.Button, pos: Tuple[int, int], frame_data: FrameData):
        """处理鼠标释放事件"""
        # 仅仅处理拖拽事件
        if not isinstance(self._drag_info, DeployAction):
            return
        
        # 部署 step2 完成 (最高优先级判断): 左键释放，且方向选择已开始
        if button == mouse.Button.left and self._drag_info.left_start_pos:
            sx, sy = self._drag_info.left_start_pos
            ex, ey = pos
            dx, dy = ex - sx, ey - sy
            if abs(dx) > abs(dy):
                self._drag_info.direction = "right" if dx > 0 else "left"
            else:
                self._drag_info.direction = "down" if dy > 0 else "up"

            # 确保 end_pos 存在
            if not self._drag_info.end_pos:
                self._drag_info.end_pos = tuple(pos)
                logger.warning(f"部署 step1 的 end_pos 丢失，使用当前位置 {pos} 作为备用。")
                
            self._record_action(
                self._drag_info.trigger_frame,
                "deploy",
                {
                    "start_pos": list(self._drag_info.start_pos),
                    "end_pos": list(self._drag_info.end_pos),
                    "direction": self._drag_info.direction
                }
            )
            logger.info(f"[帧: {frame_data.total_frames}] 部署-方向: {self._drag_info.direction}")
            self._drag_info = None  # 完成部署，清空状态
            return
        
        # 部署 step1 结束: 左/右键释放，且不在干员栏
        if button in [mouse.Button.left, mouse.Button.right] and not self._is_in_op_bar(pos):
            self._drag_info.end_pos = tuple(pos)
            logger.info(f"[帧: {frame_data.total_frames}] 部署结束点: {pos}")
            return


    def _save_plan(self):
        """将录制的动作格式化并保存到YAML文件。"""
        actions_to_save = []
        if self.final_plan_queue and not self.final_plan_queue.empty():
            # If there's a final plan from UI, use it
            try:
                final_data = self.final_plan_queue.get_nowait()
                actions_to_save = final_data.get("actions", [])
                logger.info("使用从 UI 接收到的最终计划进行保存。")
            except multiprocessing.queues.Empty:
                logger.warning("final_plan_queue 为空，将使用录制动作。")
                actions_to_save = self.recorded_actions
        else:
            actions_to_save = self.recorded_actions

        if not actions_to_save:
            logger.warning("没有录制到任何动作，不生成作战计划文件。")
            return
        
        # 将相同帧的动作聚合到一个 "FrameActionGroup"
        grouped_actions = defaultdict(list)
        for action in actions_to_save:
            frame = action['trigger_frame']
            action_entry = {
                "action_type": action['action_type'],
                "params": action.get('params', {})
            }
            if 'comment' in action and action['comment']:
                action_entry['comment'] = action['comment']
            grouped_actions[frame].append(action_entry)
            
        # 构建最终的YAML结构
        final_plan = []
        for frame, actions in grouped_actions.items():
            final_plan.append({
                "trigger_frame": frame,
                "actions": actions
            })
        
        try:
            with open(self.output_plan_path, 'w', encoding='utf-8') as f:
                yaml.dump(final_plan, f, allow_unicode=True, sort_keys=False, indent=2)
            logger.info(f"作战计划已成功保存到: {self.output_plan_path}")
        except Exception as e:
            logger.error(f"保存作战计划失败: {e}", exc_info=True)


def _setup_process_logging(log_queue: multiprocessing.Queue):
    """为当前子进程配置日志，将所有日志发送到队列。"""
    if log_queue:
        root_logger = logging.getLogger()
        # 移除所有可能存在的默认 handlers
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
        
        # 添加队列 handler
        queue_handler = QueueHandler(log_queue)
        root_logger.addHandler(queue_handler)
        # 设置子进程的日志级别，确保 DEBUG 日志能被捕获
        root_logger.setLevel(logging.DEBUG)

def run_recorder_process(
    config: MergedConfig,
    frame_ipc_params: Dict,
    output_plan_name: str,
    stop_event: SyncEvent,
    event_queue: Optional[multiprocessing.Queue] = None,
    log_queue: Optional[multiprocessing.Queue] = None,
    final_plan_queue: Optional[multiprocessing.Queue] = None # New argument
):
    """
    Recorder 进程的入口函数。
    它连接到帧数据流，监听用户输入，并将操作序列化为作战计划文件。
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    logger.info("Recorder 进程已启动。")

    _setup_process_logging(log_queue)
    frame_buffer = None
    recorder = None
    try:
        # 1. 准备输出路径
        plans_dir = config.project_root / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        output_path = plans_dir / f"{output_plan_name}.yaml"
        if output_path.exists():
            logger.warning(f"警告: 作战计划 '{output_path.name}' 已存在，将会被覆盖！")

        # 2. 连接到 IPC 缓冲区
        frame_buffer = DoubleSharedBuffer(**frame_ipc_params, create=False)
        
        # 3. 初始化并运行录制器
        recorder = ActionRecorder(config, frame_buffer, str(output_path), event_queue, final_plan_queue)
        recorder.start()

        # 4. 等待停止信号
        while not stop_event.is_set():
            time.sleep(0.1)

    except Exception as e:
        logger.critical(f"Recorder 进程中发生未处理的异常: {e}", exc_info=True)
    finally:
        if recorder:
            recorder.stop()
        if frame_buffer:
            frame_buffer.close()
        logger.info("Recorder 进程已关闭。")