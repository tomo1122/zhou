import signal
import logging

import yaml

from dataclasses import dataclass
from collections import defaultdict
from typing import Dict, List, Any, Optional, Tuple
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

    def __init__(self, config: MergedConfig, frame_buffer: DoubleSharedBuffer, output_plan_path: str):
        self.config = config
        self.frame_buffer = frame_buffer
        self.output_plan_path = output_plan_path
        self.target_w = 1920
        self.target_h = 1080
        # 干员栏的相对区域
        self.op_bar_rect = [
            self.target_w * 0,
            self.target_h * 0.833,
            self.target_w * 1,
            self.target_h * 1
        ]
        # 
        
        # 使用 WindowHelper 来处理窗口交互
        self.win_helper = WindowHelper(
            main_window_title = config.mumu_window_title,
            render_window_class = config.mumu_render_class,
            target_resolution = (self.target_w, self.target_h)
        )

        self.recorded_actions: List[Dict[str, Any]] = []
        self.mouse_listener = None
        self.keyboard_listener = None
        self.is_running = False
        self._drag_info = None

    def start(self, stop_event: SyncEvent):
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
        # 暂时只关心鼠标点击，键盘可后续扩展
        # self.keyboard_listener = keyboard.Listener(on_press=self._on_press)
        
        self.mouse_listener.start()
        # self.keyboard_listener.start()
        self.is_running = True
        logger.info(f"已开始监听 '{self.win_helper.main_window_title}' 内的鼠标操作。")
        logger.info("在主窗口中按 Ctrl+C 或关闭程序来停止录制并保存。")

        # 阻塞直到 stop_event 被设置
        stop_event.wait()
        self.stop()

    def stop(self):
        """停止监听器并保存录制的动作。"""
        if not self.is_running:
            return
            
        if self.mouse_listener:
            self.mouse_listener.stop()
        # if self.keyboard_listener:
        #     self.keyboard_listener.stop()
            
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
        """
        鼠标点击事件的回调处理函数
        
        宏 deploy:
        deploy第一步
            press事件：
                1. 暂停状态下右键按下  (_is_in_op_bar==True)
                2. 运行状态下左键按下  (_is_in_op_bar==True)
            release事件：
                1. 暂停状态下右键释放  (_is_in_op_bar==False)
                2. 暂停状态下左键释放  (_is_in_op_bar==False)

        deploy第二步
            press事件
                1. start_pos在第一步终点附近
            release事件
                1. 用于判断方向

        宏 skill
            - 暂停状态下鼠标侧键
        
        宏 recall
            - 暂停状态下鼠标侧键 
        """
        # 只在目标窗口是前景窗口时才记录
        if not self.win_helper.is_foreground_window():
            return

        self.win_helper.update_render_area()
        virtual_pos = self.win_helper.transform_screen_to_virtual((x, y))
        if not virtual_pos:
            return

        frame_data = self._get_current_frame_data()

        if pressed:
            self._handle_press(button, virtual_pos, frame_data)
        else:
            self._handle_release(button, virtual_pos, frame_data)

    def _record_action(self, frame: int, action_type: str, params: dict):
        action = {
            "trigger_frame": frame,
            "action_type": action_type,
            "params": params
        }
        self.recorded_actions.append(action)
        logger.info(f"动作录制: {action}")

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
        # 部署 step1 结束：左/右键释放，且不在干员栏
        if button in [mouse.Button.left, mouse.Button.right] \
           and isinstance(self._drag_info, DeployAction) \
           and not self._is_in_op_bar(pos):

            self._drag_info.end_pos = tuple(pos)
            logger.info(f"[帧: {frame_data.total_frames}] 部署结束点: {pos}")
            return

        # 部署 step2 完成：左键释放，计算方向
        if button == mouse.Button.left and isinstance(self._drag_info, DeployAction):
            if self._drag_info.left_start_pos:
                sx, sy = self._drag_info.left_start_pos
                ex, ey = pos
                dx, dy = ex - sx, ey - sy
                if abs(dx) > abs(dy):
                    self._drag_info.direction = "right" if dx > 0 else "left"
                else:
                    self._drag_info.direction = "down" if dy > 0 else "up"

                self._drag_info.end_pos = self._drag_info.end_pos or tuple(pos)
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
                self._drag_info = None
            return

        # 宏 skill
        if button == mouse.Button.x2:
            self._record_action(frame_data.total_frames, "skill", {"pos": list(pos)})
            logger.info(f"[帧: {frame_data.total_frames}] skill: {pos}")
            return

        # 宏 recall
        if button == mouse.Button.x1:
            self._record_action(frame_data.total_frames, "recall", {"pos": list(pos)})
            logger.info(f"[帧: {frame_data.total_frames}] recall: {pos}")
            return

    def _save_plan(self):
        """将录制的动作格式化并保存到YAML文件。"""
        if not self.recorded_actions:
            logger.warning("没有录制到任何动作，不生成作战计划文件。")
            return
        
        # 将相同帧的动作聚合到一个 "FrameActionGroup"
        grouped_actions = defaultdict(list)
        for action in self.recorded_actions:
            frame = action['trigger_frame']
            grouped_actions[frame].append({
                "action_type": action['action_type'],
                "params": action.get('params', {})
            })
            
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


def run_recorder_process(
    config: MergedConfig,
    frame_ipc_params: Dict,
    output_plan_name: str,
    stop_event: SyncEvent
):
    """
    Recorder 进程的入口函数。
    它连接到帧数据流，监听用户输入，并将操作序列化为作战计划文件。
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    logger.info("Recorder 进程已启动。")
    
    frame_buffer = None
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
        recorder = ActionRecorder(config, frame_buffer, str(output_path))
        recorder.start(stop_event)

    except Exception as e:
        logger.critical(f"Recorder 进程中发生未处理的异常: {e}", exc_info=True)
    finally:
        if frame_buffer:
            frame_buffer.close()
        logger.info("Recorder 进程已关闭。")