import time
import signal
import logging
import multiprocessing

from enum import Enum, auto
from typing import Dict, Optional, Type
from logging.handlers import QueueHandler
from multiprocessing.synchronize import Event as SyncEvent

from app.core.config import MergedConfig
from app.core.ipc.double_shared_buffer import DoubleSharedBuffer, FrameData
from app.analysis.plan_loader import PlanLoader
from app.control.engine.base import BaseController


logger = logging.getLogger(__name__)


class CommanderState(Enum):
    """
    指挥官进程的状态机。
    定义了从初始化到任务完成或关闭的整个生命周期。
    """
    INITIALIZING = auto()      # 0. 正在初始化资源
    RUNNING = auto()           # 1. 游戏正常运行，等待接近目标帧
    PAUSING = auto()           # 2. 准备暂停游戏以进入精确控制模式
    STEPPING = auto()          # 3. 游戏已暂停，正在逐帧或微调时间以到达目标帧
    EXECUTING = auto()         # 4. 已到达目标帧，正在执行计划中的动作
    DECIDING = auto()          # 5. 动作执行完毕，决策下一步（继续运行或继续步进）
    DONE = auto()              # 6. 所有计划已执行，任务完成
    SHUTTING_DOWN = auto()     # 7. 准备清理资源并退出进程


class Commander:
    """
    封装了 Commander 进程的核心逻辑。

    这个类负责管理状态机、加载和执行作战计划、与控制器交互
    通过一个可选的事件队列与UI进行通信
    """
    def __init__(self, config: MergedConfig, frame_ipc_params: Dict, plan_name: str,
                 controller_class: Type[BaseController], controller_kwargs: Dict,
                 stop_event: SyncEvent, event_queue: Optional[multiprocessing.Queue] = None):
        """
        初始化 Commander 实例。

        Args:
            config: 应用程序的统一配置对象。
            frame_ipc_params: 连接 DoubleSharedBuffer 的参数。
            plan_name: 要执行的作战计划名称。
            controller_class: 要使用的控制器类 (例如 MumuMacroController)。
            controller_kwargs: 实例化控制器时所需的参数。
            stop_event: 用于外部停止进程的同步事件。
            event_queue: (可选) 用于向UI等外部进程发送状态和事件的队列。
        """
        self.config = config
        self.frame_ipc_params = frame_ipc_params
        self.plan_name = plan_name
        self.controller_class = controller_class
        self.controller_kwargs = controller_kwargs
        self.stop_event = stop_event
        self.event_queue = event_queue

        self.state = CommanderState.INITIALIZING
        self.frame_buffer: Optional[DoubleSharedBuffer] = None
        self.controller: Optional[BaseController] = None
        self.is_game_paused = False
        self.plan = None


    def _put_event(self, event_type: str, data: Dict):
        if self.event_queue:
            self.event_queue.put({"type": event_type, "data": data})


    def run(self):
        """此方法包含驱动整个过程的核心状态机。"""
        self._set_state(CommanderState.INITIALIZING)
        try:
            logger.info("[状态: INITIALIZING] 正在初始化资源...")
            # 1. 初始化 IPC
            self.frame_buffer = DoubleSharedBuffer(**self.frame_ipc_params, create=False)
            # 2. 初始化控制器
            self.controller = self.controller_class(**self.controller_kwargs) if self.controller_kwargs else self.controller_class(device_serial=self.config.device_serial)
            if hasattr(self.controller, 'connect'):
                self.controller.connect()
            # 3. 加载作战计划
            plan_loader = PlanLoader(self.config)
            self.plan = plan_loader.load(self.plan_name)
            if not self.plan:
                logger.warning(f"作战计划 '{self.plan_name}' 为空或加载失败，进程退出。")
                self._set_state(CommanderState.SHUTTING_DOWN)
            else:
                self._set_state(CommanderState.RUNNING)

            # 4. 初始化循环变量和常量
            next_action_index = 0
            last_processed_frame = -1
            step_attempts = 0
            # 提前量，在到达目标帧多久之前暂停
            precision_lead_frames = int(getattr(self.config, 'precision_lead_frames', 10))
            # 每帧最多尝试推进次数，防止在STEPPING状态死锁
            MAX_STEP_ATTEMPTS = int(getattr(self.config, 'max_step_attempts', 10))
            # 游戏处理next_frame指令需要的时间，用于在发送指令后等待（大概500ms？）
            FRAME_DURATION_MS = int(getattr(self.config, 'frame_duration_ms', 500))

            # 5. 开始循环
            while self.state not in [CommanderState.DONE, CommanderState.SHUTTING_DOWN]:
                if self.stop_event.is_set():
                    self._set_state(CommanderState.SHUTTING_DOWN)
                    continue
                
                # 获取当前帧数
                frame_data = self.frame_buffer.get()
                current_total_frames = frame_data.total_frames
                
                if next_action_index >= len(self.plan):
                    self._set_state(CommanderState.DONE)
                    continue
                
                target_action_group = self.plan[next_action_index]
                target_frame = target_action_group.trigger_frame

                # RUNNING: 游戏正常运行，等待接近目标帧
                if self.state == CommanderState.RUNNING:
                    # 等待新帧，避免CPU空转
                    if current_total_frames <= last_processed_frame:
                        time.sleep(0.001)
                        continue
                    last_processed_frame = current_total_frames
                    
                    # 检查是否接近目标帧，若是则准备暂停
                    if (target_frame - current_total_frames <= precision_lead_frames):
                        logger.info(f"当前帧：{current_total_frames} 即将到达目标帧数: {target_frame} (提前量: {precision_lead_frames})，准备暂停。")
                        self._set_state(CommanderState.PAUSING)
                
                elif self.state == CommanderState.PAUSING:
                    if not self.is_game_paused:
                        allow_num = 2
                        if current_total_frames < allow_num:
                            time.sleep(0.001)
                            continue
                        logger.info(f"[状态: PAUSING] 当前帧：{current_total_frames} 正在暂停游戏...")
                        self.controller.toggle_pause()
                        self.is_game_paused = True
                    time.sleep(0.1) # 等待模拟器响应暂停操作
                    self._set_state(CommanderState.STEPPING)

                elif self.state == CommanderState.STEPPING:
                    logger.info(current_total_frames)
                    if current_total_frames >= target_frame:
                        logger.info('[状态: STEPPING] 不需要推进')
                        self._set_state(CommanderState.EXECUTING)
                        continue

                    # 分支 1: 检查是否已成功推进到新的一帧
                    if current_total_frames > last_processed_frame:
                        logger.info(f"[状态: STEPPING] 成功推进至新帧: {current_total_frames}。")
                        last_processed_frame = current_total_frames
                        step_attempts = 0 # 成功推进，重置尝试计数器

                        # 检查新帧是否就是我们的目标帧
                        if current_total_frames >= target_frame:
                            self._set_state(CommanderState.EXECUTING)
                        continue # 无论是否到达目标，只要成功推进就立即重新循环
                    
                    # 分支 2: 未检测到新帧，但尝试次数过多，判定为死锁
                    elif step_attempts >= MAX_STEP_ATTEMPTS:
                        logger.error(f"[状态: STEPPING] 在帧 {last_processed_frame} 上尝试推进 {MAX_STEP_ATTEMPTS} 次后仍未更新，系统可能卡住。强制关闭。")
                        self._set_state(CommanderState.SHUTTING_DOWN)
                        continue
                    
                    # 分支 3: 未检测到新帧，且未超次数，这是唯一应该发送指令的情况
                    else:
                        delay = 12 
                        if (target_frame - current_total_frames) > 5: delay = 99
                        elif 1 < (target_frame - current_total_frames) <= 5: delay = 33
                        
                        logger.info(f"[状态: STEPPING] 帧未变化，发送第 {step_attempts + 1}/{MAX_STEP_ATTEMPTS} 次 'next_frame' 指令; delay: {delay}")
                        self.controller.next_frame(delay=delay)
                        step_attempts += 1
                        # 在Python端短暂等待，为IPC更新和模拟器响应留出时间
                        time.sleep(FRAME_DURATION_MS / 1000.0)

                elif self.state == CommanderState.EXECUTING:
                    logger.info(f"[状态: EXECUTING] 到达目标帧 {target_frame} (实际: {current_total_frames})，开始执行动作。")
                    self._put_event('executing_action', {'index': next_action_index, 'frame': target_frame, 'actions': [a.model_dump() for a in target_action_group.actions]})
                    
                    for action in target_action_group.actions:
                        # 在精确控制模式下，跳过任何可能冲突的暂停/播放指令
                        if action.action_type in ['toggle_pause', 'next_frame']:
                            logger.warning(f"在 EXECUTING 状态下检测到 '{action.action_type}' 动作，已跳过。")
                            continue
                        
                        method = getattr(self.controller, action.action_type, None)
                        if method:
                            params = action.params or {}
                            method(**params)
                            logger.info(f"  - 已执行: {action.action_type}({params})")
                        else:
                            logger.warning(f"  - 跳过未知动作类型: '{action.action_type}'")
                    
                    next_action_index += 1
                    self._set_state(CommanderState.DECIDING)

                elif self.state == CommanderState.DECIDING:
                    logger.info(f"[状态: DECIDING] 决策下一步...")
                    if next_action_index >= len(self.plan):
                        self._set_state(CommanderState.DONE)
                    else:
                        next_target_frame = self.plan[next_action_index].trigger_frame
                        if next_target_frame - current_total_frames > precision_lead_frames:
                            logger.info(f"下一个目标 {next_target_frame} 较远，恢复游戏运行。")
                            if self.is_game_paused:
                                self.controller.toggle_pause()
                                self.is_game_paused = False
                            time.sleep(0.2) # 等待模拟器响应恢复操作
                            self._set_state(CommanderState.RUNNING)
                        else:
                            logger.info(f"下一个目标 {next_target_frame} 较近，继续逐帧推进。")
                            self._set_state(CommanderState.STEPPING)

        except Exception as e:
            logger.critical(f"Commander 逻辑中发生未处理的异常: {e}", exc_info=True)
            self._set_state(CommanderState.SHUTTING_DOWN)
        finally:
            self.cleanup()

    def _set_state(self, new_state: CommanderState):
        """
        原子地设置 Commander 的新状态，同时记录日志并向UI发送事件。
        避免重复设置相同的状态。
        """
        if self.state == new_state: 
            return
        logger.info(f"[状态变更] {self.state.name} -> {new_state.name}")
        self.state = new_state
        self._put_event('state_change', {'state': new_state.name})

    def cleanup(self):
        """
        处理所有资源的清理工作，确保进程干净地退出。
        """
        logger.info(f"Commander 正在关闭 (当前状态: {self.state.name})...")
        if self.controller:
            if hasattr(self.controller, 'close'):
                self.controller.close()
        
        if self.frame_buffer:
            self.frame_buffer.close()
        logger.info("Commander 进程已关闭。")


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
        root_logger.setLevel(logging.INFO)


def run_commander_process(
    config: MergedConfig,
    frame_ipc_params: Dict,
    plan_name: str,
    controller_class: Type[BaseController],
    controller_kwargs: Dict,
    stop_event: SyncEvent,
    event_queue: Optional[multiprocessing.Queue] = None,
    log_queue: Optional[multiprocessing.Queue] = None
):
    """
    Commander 进程的公共入口函数。

    它负责实例化并运行一个 Commander 对象，处理进程级别的设置，如信号处理。
    """
    # 忽略 Ctrl+C 信号，由主进程的 stop_event 控制
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    logger.info(f"Commander 进程已启动，使用控制器: {controller_class.__name__}")
    _setup_process_logging(log_queue)
    
    commander_instance = Commander(
        config, frame_ipc_params, plan_name, 
        controller_class, controller_kwargs, 
        stop_event, event_queue
    )
    commander_instance.run()