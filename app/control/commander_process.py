import time
import signal
import logging

from typing import Dict, Optional, Type
from multiprocessing.synchronize import Event as SyncEvent

from app.core.config import MergedConfig
from app.core.ipc.double_shared_buffer import DoubleSharedBuffer, FrameData
from app.analysis.plan_loader import PlanLoader
from app.control.engine.base import BaseController


logger = logging.getLogger(__name__)


def run_commander_process(
    config: MergedConfig,
    frame_ipc_params: Dict,
    plan_name: str,
    controller_class: Type[BaseController],
    stop_event: SyncEvent
):
    """
    Commander 进程的入口函数 。

    Args:
        config: 应用程序配置。
        frame_ipc_params (dict): 用于连接帧数据流 `DoubleSharedBuffer` 的参数。
        plan_name (str): 要执行的作战计划名称 (不含.yaml后缀)。
        controller_class (Type[BaseController]): 要使用的控制器引擎类。
        stop_event (Event): 用于停止进程的事件。
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    logger.info(f"Commander 进程已启动，使用控制器: {controller_class.__name__}")
    frame_buffer: Optional[DoubleSharedBuffer] = None
    controller: Optional[BaseController] = None

    try:
        # 1. 初始化 IPC 连接
        logger.info("正在连接到帧索引流 IPC...")
        frame_buffer = DoubleSharedBuffer(**frame_ipc_params, create=False)

        # 2. 初始化控制器
        logger.info(f"正在初始化 {controller_class.__name__} (设备: {config.device_serial})...")
        controller = controller_class(device_serial=config.device_serial)
        if hasattr(controller, 'connect'):
            controller.connect()

        # 3. 加载作战计划
        plan_loader = PlanLoader(config)
        plan = plan_loader.load(plan_name)
        if not plan:
            logger.warning(f"作战计划 '{plan_name}' 为空或加载失败，进程退出。")
            return

        # 4. 初始化主循环状态
        next_action_index = 0
        last_processed_frame = -1
        precision_mode_active = False
        precision_lead_frames = getattr(config, 'precision_lead_frames', 1)
        logger.info(f"精确执行模式已启用，提前量: {precision_lead_frames} 帧。")

        logger.info("初始化完成，进入主循环...")

        # 主循环
        while not stop_event.is_set():
            frame_data: FrameData = frame_buffer.get()
            current_total_frames = frame_data.total_frames

            if current_total_frames <= last_processed_frame or current_total_frames < 0:
                time.sleep(0.001)
                continue
            
            if current_total_frames > last_processed_frame:
                logger.info(f"当前总帧数: {current_total_frames}")
                last_processed_frame = current_total_frames

            if next_action_index >= len(plan):
                logger.info("所有计划已执行, 退出控制器, 游戏暂停等待")
                if not precision_mode_active:
                    controller.toggle_pause()
                break

            target_frame_action_group = plan[next_action_index]
            target_frame = target_frame_action_group.trigger_frame

            # 核心状态机
            if precision_mode_active:
                if current_total_frames < target_frame:
                    frame_before_advance = current_total_frames
                    logger.info(f"准备从 {frame_before_advance} 推进，目标 {target_frame}。")

                    MAX_TOTAL_TIMEOUT = 10.0
                    
                    total_timeout_start = time.time()

                    while time.time() - total_timeout_start < MAX_TOTAL_TIMEOUT:
                        logger.info("  - 发送 next_frame() 请求...")
                        controller.next_frame()
                        latest_frame = frame_buffer.get().total_frames
                        if latest_frame >= target_frame:
                            break
                    
                else:
                    logger.info(f"到达目标帧 {target_frame} (实际: {current_total_frames})。开始执行动作...")
                    
                    for action in target_frame_action_group.actions:
                        if action.action_type == 'toggle_pause':
                            logger.warning("检测到 'toggle_pause' 动作，已跳过以避免逻辑冲突。")
                            continue
                        
                        action_type = action.action_type
                        params = action.params or {}
                        if hasattr(controller, action_type):
                            method = getattr(controller, action_type)
                            method(**params)
                            logger.info(f"  - 已执行: {action_type} (参数: {params})")
                        else:
                            logger.warning(f"  - 跳过未知动作类型: '{action_type}'")

                    next_action_index += 1
                    
                    # 退出暂停模式
                    if next_action_index < len(plan):
                        next_target_frame = plan[next_action_index].trigger_frame
                        if next_target_frame - current_total_frames > precision_lead_frames:
                            logger.info(f"下一个目标 {next_target_frame} 较远，恢复游戏运行。")
                            # 避免和上个动作间隔时间过短，导致模拟器不响应。此时是暂停状态，可以暂停。
                            time.sleep(0.2)
                            controller.toggle_pause()
                            precision_mode_active = False

            else:
                if target_frame - current_total_frames <= precision_lead_frames:
                    logger.info(
                        f"当前 {current_total_frames}, 即将到达目标 {target_frame}, 暂停游戏步进"
                    )
                    controller.toggle_pause()
                    precision_mode_active = True

    except Exception as e:
        logger.critical(f"Commander 进程中发生未处理的异常: {e}", exc_info=True)
        if controller and precision_mode_active:
            try:
                logger.warning("发生异常，尝试恢复游戏运行...")
                controller.toggle_pause()
            except Exception as final_e:
                logger.error(f"恢复游戏运行时再次发生错误: {final_e}")
    finally:
        if controller:
            if hasattr(controller, 'close'):
                logger.info(f"正在关闭 {controller.__class__.__name__} 连接...")
                controller.close()
        if frame_buffer:
            logger.info("正在关闭 IPC 连接...")
            frame_buffer.close()
        
        logger.info("Commander 进程已关闭。")