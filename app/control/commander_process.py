import time
import signal
import logging

from enum import Enum, auto
from typing import Dict, Optional, Type
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


def run_commander_process(
    config: MergedConfig,
    frame_ipc_params: Dict,
    plan_name: str,
    controller_class: Type[BaseController],
    controller_kwargs: Dict,
    stop_event: SyncEvent
):
    """
    Commander 进程的入口函数，采用状态机实现。

    该进程负责根据作战计划，在精确的帧数执行指定操作。它通过与分析器进程
    共享的内存（DoubleSharedBuffer）获取实时帧数，并指挥控制器（Controller）
    与模拟器交互，实现暂停、逐帧、执行动作等精确控制。
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    logger.info(f"Commander 进程已启动，使用控制器: {controller_class.__name__}")

    state = CommanderState.INITIALIZING
    frame_buffer: Optional[DoubleSharedBuffer] = None
    controller: Optional[BaseController] = None
    is_game_paused = False

    try:
        # --- 状态: INITIALIZING ---
        # 1. 初始化 IPC
        logger.info("[状态: INITIALIZING] 正在连接到帧数据流 IPC...")
        frame_buffer = DoubleSharedBuffer(**frame_ipc_params, create=False)

        # 2. 初始化控制器
        logger.info(f"[状态: INITIALIZING] 正在初始化 {controller_class.__name__}...")
        controller = controller_class(**controller_kwargs) if controller_kwargs else controller_class(device_serial=config.device_serial)
        if hasattr(controller, 'connect'):
            controller.connect()
        
        # 3. 加载作战计划
        plan_loader = PlanLoader(config)
        plan = plan_loader.load(plan_name)
        if not plan:
            logger.warning(f"[状态: INITIALIZING] 作战计划 '{plan_name}' 为空或加载失败，进程退出。")
            state = CommanderState.SHUTTING_DOWN
        
        # 4. 初始化循环变量和常量
        next_action_index = 0
        last_processed_frame = -1
        step_attempts = 0
        
        # --- 可配置常量 ---
        # 提前量，在到达目标帧多久之前暂停
        precision_lead_frames = int(getattr(config, 'precision_lead_frames', 10))
        # 每帧最多尝试推进次数，防止在STEPPING状态死锁
        MAX_STEP_ATTEMPTS = int(getattr(config, 'max_step_attempts', 10))
        # 游戏处理next_frame指令需要的时间，大概500ms？
        FRAME_DURATION_MS = int(getattr(config, 'frame_duration_ms', 500))
        
        if state == CommanderState.INITIALIZING:
            logger.info("[状态: INITIALIZING] 初始化完成，进入主循环...")
            state = CommanderState.RUNNING

        # 主循环
        while state not in [CommanderState.DONE, CommanderState.SHUTTING_DOWN]:
            if stop_event.is_set():
                logger.info("检测到外部停止事件，准备关闭...")
                state = CommanderState.SHUTTING_DOWN
                continue
            
            # 获取当前帧数
            frame_data = frame_buffer.get()
            current_total_frames = frame_data.total_frames
            
            if next_action_index >= len(plan):
                logger.info("所有计划已执行完毕")
                state = CommanderState.DONE
            else:
                target_action_group = plan[next_action_index]
                target_frame = target_action_group.trigger_frame

            # 状态机核心逻辑
            if state == CommanderState.RUNNING:
                # 等待新帧，避免CPU空转
                if current_total_frames <= last_processed_frame:
                    time.sleep(0.001) 
                    continue
                if current_total_frames > last_processed_frame:
                    logger.info(f"[状态: RUNNING] 游戏运行中... 当前帧: {current_total_frames}, 目标帧: {target_frame}")
                    last_processed_frame = current_total_frames
                
                if target_frame - current_total_frames <= precision_lead_frames:
                    logger.info(f"即将到达目标 {target_frame} (提前量: {precision_lead_frames})，准备暂停。")
                    state = CommanderState.PAUSING
            
            elif state == CommanderState.PAUSING:
                logger.info(f"[状态: PAUSING] 正在暂停游戏...")
                controller.toggle_pause()
                is_game_paused = True
                time.sleep(0.1) # 等待模拟器响应暂停操作
                state = CommanderState.STEPPING

            elif state == CommanderState.STEPPING:
                # 这是最核心的健壮性逻辑，用于在暂停状态下精确抵达目标帧
                # 它能处理“一次next_frame调用不足以推进一整帧”的现实情况
                
                # 分支 1: 检查是否已成功推进到新的一帧
                if current_total_frames > last_processed_frame:
                    logger.info(f"[状态: STEPPING] 成功推进至新帧: {current_total_frames}。")
                    last_processed_frame = current_total_frames
                    step_attempts = 0 # 成功推进，重置尝试计数器

                    # 检查新帧是否就是我们的目标帧
                    if current_total_frames >= target_frame:
                        state = CommanderState.EXECUTING
                    continue
                
                # 分支 2: 未检测到新帧，但尝试次数过多，判定为死锁
                elif step_attempts >= MAX_STEP_ATTEMPTS:
                    logger.error(f"[状态: STEPPING] 在帧 {last_processed_frame} 上尝试推进 {MAX_STEP_ATTEMPTS} 次后仍未更新，系统可能卡住。强制关闭。")
                    state = CommanderState.SHUTTING_DOWN
                    continue
                
                # 分支 3: 未检测到新帧，且未超次数，这是唯一应该发送指令的情况
                else:
                    logger.info(f"[状态: STEPPING] 帧未变化，发送第 {step_attempts + 1}/{MAX_STEP_ATTEMPTS} 次 'next_frame' 指令...")
                    # next_frame 是原子的、非阻塞的
                    if (target_frame - current_total_frames) > 5:
                        controller.next_frame(delay = 166) 
                    elif 1 < (target_frame - current_total_frames) <= 5:
                        controller.next_frame(delay = 33)
                    else:
                        controller.next_frame(delay = 12)
                    
                    step_attempts += 1
                    # 在Python端短暂等待，为IPC更新和模拟器响应留出时间
                    time.sleep(FRAME_DURATION_MS / 1000.0)

            elif state == CommanderState.EXECUTING:
                logger.info(f"[状态: EXECUTING] 到达目标帧 {target_frame} (实际: {current_total_frames})，开始执行动作。")
                for action in target_action_group.actions:
                    # 在精确控制模式下，跳过任何可能冲突的暂停/播放指令
                    if action.action_type in ['toggle_pause', 'next_frame']:
                        logger.warning(f"在 EXECUTING 状态下检测到 '{action.action_type}' 动作，已跳过以避免逻辑冲突。")
                        continue
                    
                    method = getattr(controller, action.action_type, None)
                    if method:
                        params = action.params or {}
                        method(**params)
                        logger.info(f"  - 已执行: {action.action_type}({params})")
                    else:
                        logger.warning(f"  - 跳过未知动作类型: '{action.action_type}'")
                
                next_action_index += 1
                state = CommanderState.DECIDING

            elif state == CommanderState.DECIDING:
                logger.info(f"[状态: DECIDING] 决策下一步...")
                if next_action_index >= len(plan):
                    logger.info("所有计划已执行完毕，任务完成。")
                    state = CommanderState.DONE
                else:
                    next_target_frame = plan[next_action_index].trigger_frame
                    if next_target_frame - current_total_frames > precision_lead_frames:
                        logger.info(f"下一个目标 {next_target_frame} 较远，恢复游戏运行。")
                        controller.toggle_pause()
                        is_game_paused = False
                        time.sleep(0.2) # 等待模拟器响应恢复操作
                        state = CommanderState.RUNNING
                    else:
                        logger.info(f"下一个目标 {next_target_frame} 较近，继续逐帧推进。")
                        state = CommanderState.STEPPING

    except Exception as e:
        logger.critical(f"Commander 进程中发生未处理的异常: {e}", exc_info=True)
        state = CommanderState.SHUTTING_DOWN
    
    finally:
        # 清理工作
        logger.info(f"Commander 进程正在关闭 (当前状态: {state.name})...")
        if controller:
            # 如果进程意外退出时游戏处于暂停状态，尝试恢复游戏运行
            if is_game_paused:
                try:
                    logger.warning("进程退出前，尝试恢复游戏运行...")
                    controller.toggle_pause()
                except Exception as final_e:
                    logger.error(f"恢复游戏运行时再次发生错误: {final_e}")
            if hasattr(controller, 'close'):
                logger.info(f"正在关闭 {controller.__class__.__name__} 连接...")
                controller.close()
        
        if frame_buffer:
            logger.info("正在关闭 IPC 连接...")
            frame_buffer.close()
        
        logger.info("Commander 进程已关闭。")