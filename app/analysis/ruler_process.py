import time
import signal
import logging

from typing import Dict
from multiprocessing.synchronize import Event as SyncEvent 

import numpy as np

from app.core.config import MergedConfig
from app.core.ipc.triple_shared_buffer import TripleSharedBuffer
from app.core.ipc.double_shared_buffer import DoubleSharedBuffer, FrameData
from app.analysis.cost_bar_analyzer import CostBarAnalyzer


logger = logging.getLogger(__name__)


def run_ruler_process(
    config: MergedConfig, 
    image_ipc_params: Dict, 
    frame_ipc_params: Dict, 
    stop_event: SyncEvent
):
    """
    Ruler 进程的入口函数。
    它连接到图像流(输入)和帧数据流(输出)，并运行分析循环。

    Args:
        config: 应用程序配置。
        image_ipc_params (dict): 用于连接图像流 `TripleSharedBuffer` 的参数。
        frame_ipc_params (dict): 用于连接帧数据流 `DoubleSharedBuffer` 的参数。
        stop_event (Event): 用于停止进程的事件。
    """
    # 忽略 Ctrl + C 信号
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    
    logger.info("Ruler 进程已启动。")
    image_buffer = None
    frame_buffer = None
    
    try:
        # 1. 连接到 IPC 缓冲区
        image_buffer = TripleSharedBuffer(**image_ipc_params, create=False)
        frame_buffer = DoubleSharedBuffer(**frame_ipc_params, create=False)
        
        # 2. 初始化分析器
        analyzer = CostBarAnalyzer(config)
        
        last_processed_idx = -1
        logger.info("分析器初始化完成，开始主循环...")

        while not stop_event.is_set():
            # 3. 检查是否有新的图像帧
            current_idx = image_buffer.np_latest_idx[0]
            if current_idx != last_processed_idx:
                last_processed_idx = current_idx
                
                # 读取最新的完整帧
                frame_data_raw = image_buffer.np_arrays[current_idx]
                # 原始frame是上下颠倒的
                frame_data = np.flipud(frame_data_raw)
                
                # 4. 调用分析器进行分析
                analysis_result = analyzer.analyze_frame(frame_data)
                logger.debug(analysis_result)
                # 5. 如果有有效结果，则通过 IPC 发布
                if analysis_result:
                    total_frames, logical_frame, cycle_index, total_frames_in_cycle, timestamp = analysis_result
                    
                    frame_buffer.set(
                        total_frames=total_frames,
                        logical_frame=logical_frame,
                        cycle_index=cycle_index,
                        total_frames_in_cycle=total_frames_in_cycle,
                        timestamp=timestamp
                    )
            else:
                # 如果没有新帧，短暂休眠以避免CPU空转
                time.sleep(0.001)

    except (ValueError, FileNotFoundError) as e:
        logger.critical(f"Ruler 进程初始化失败，可能是配置或校准文件问题: {e}", exc_info=False)
    except Exception as e:
        logger.critical(f"Ruler 进程中发生未处理的异常: {e}", exc_info=True)
    finally:
        if image_buffer:
            image_buffer.close()
        if frame_buffer:
            frame_buffer.close()
        logger.info("Ruler 进程已关闭。")