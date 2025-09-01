import logging
import time

import numpy as np
from typing import Tuple, Optional, Dict, Any

from app.core.ipc.triple_shared_buffer import TripleSharedBuffer


logger = logging.getLogger(__name__)


# --- Helper functions for multiprocessing tests ---

def check_connection_task(params):
    """Target function for test_ipc_buffer_creation_and_connection."""
    client_buffer = None
    try:
        client_buffer = TripleSharedBuffer(**params, create=False)
        assert client_buffer is not None, "子进程未能创建 TripleSharedBuffer 实例"
        assert len(client_buffer.np_arrays) == 3, "子进程未能正确附加到 numpy 数组"
        exit(0)
    except Exception as e:
        logger.error(f"子进程连接测试失败: {e}", exc_info=True)
        exit(1)
    finally:
        if client_buffer:
            client_buffer.close()


def verifying_consumer_task(params, stop_event, expected_value):
    """Target function for test_consumer_reads_from_buffer."""
    consumer_buffer = None
    try:
        consumer_buffer = TripleSharedBuffer(**params, create=False)
        start_wait = time.time()
        frame_data_is_correct = False
        while not stop_event.is_set() and time.time() - start_wait < 3:
            read_buffer = consumer_buffer.get_read_buffer()
            if np.all(read_buffer == expected_value):
                frame_data_is_correct = True
                break
            time.sleep(0.01)
        
        assert frame_data_is_correct, f"消费者未能读取到期望值 {expected_value}"
        exit(0)
    except Exception as e:
        logger.error(f"验证消费者任务失败: {e}", exc_info=True)
        exit(1)
    finally:
        if consumer_buffer:
            consumer_buffer.close()


def performance_consumer_task(params, stop_event, result_queue):
    """
    Target function for test_end_to_end_workflow_performance.
    
    优化版 V3: 根据实际 ROI 提取方法进行模拟，包含更健壮的像素检查。
    """
    
    # 辅助函数：根据屏幕分辨率计算费用条ROI
    def _find_cost_bar_roi(screen_width: int, screen_height: int) -> Tuple[int, int, int]:
        """
        根据屏幕分辨率计算费用条的位置 (ROI - Region of Interest)，采用更健壮的比例缩放。
        这是从你的实际代码中复制过来的，用于模拟。
        """
        # 基于 1920x1080 分辨率下的参考坐标
        REF_WIDTH, REF_HEIGHT = 1920.0, 1080.0
        REF_ASPECT_RATIO = REF_WIDTH / REF_HEIGHT
        
        # 参考坐标（相对于右下角）
        X1_OFFSET_FROM_RIGHT_REF = REF_WIDTH - 1739
        X2_OFFSET_FROM_RIGHT_REF = REF_WIDTH - 1919
        Y1_OFFSET_FROM_BOTTOM_REF = REF_HEIGHT - 810
        Y2_OFFSET_FROM_BOTTOM_REF = REF_HEIGHT - 817

        current_aspect_ratio = screen_width / screen_height
        if current_aspect_ratio >= REF_ASPECT_RATIO:
            scale = screen_height / REF_HEIGHT 
        else:
            scale = screen_width / REF_WIDTH 

        # 从屏幕右下角反向计算坐标
        x1 = screen_width - X1_OFFSET_FROM_RIGHT_REF * scale
        x2 = screen_width - X2_OFFSET_FROM_RIGHT_REF * scale
        y1 = screen_height - Y1_OFFSET_FROM_BOTTOM_REF * scale
        y2 = screen_height - Y2_OFFSET_FROM_BOTTOM_REF * scale

        x1_int, x2_int = round(x1), round(x2)
        y_mid_int = round((y1 + y2) / 2) # 取两条扫描线的中间位置

        return (x1_int, x2_int, y_mid_int)
    
    # 辅助函数：模拟 _get_raw_filled_pixel_width
    def _simulate_get_raw_filled_pixel_width(frame_line: np.ndarray, x1: int, x2: int) -> Optional[int]:
        """
        使用 NumPy 高效模拟 _get_raw_filled_pixel_width 的逻辑。
        frame_line: NumPy 数组 (width, 4) 代表单行像素 (RGBA)
        x1, x2: ROI的起始和结束x坐标 (相对于该行)
        """
        WHITE_THRESHOLD = 250
        GRAY_TOLERANCE = 20
        ALPHA_OPAQUE = 255 # MuMu通常是BGRA，这里假设是RGBA，与你的描述保持一致

        total_width = x2 - x1
        if total_width <= 0:
            return None

        # 1. 健全性检查：检查ROI的末端像素
        try:
            b_end, g_end, r_end, a_end = map(int, frame_line[total_width - 1, 0:4])
        except IndexError:
            return None

        # 模拟 is_pixel_grayscale
        is_end_pixel_grayscale = (abs(r_end - g_end) <= GRAY_TOLERANCE and \
                                  abs(g_end - b_end) <= GRAY_TOLERANCE)
        
        if a_end != ALPHA_OPAQUE or not is_end_pixel_grayscale:
            # logger.debug("ROI区域无效: 末端像素不是不透明的灰度色。")
            return None

        # 2. 满费检查
        is_end_pixel_white = all(c > WHITE_THRESHOLD for c in (r_end, g_end, b_end))
        if is_end_pixel_white:
            # logger.debug(f"费用条已满 (末端像素为白色)，宽度: {total_width}")
            return total_width

        # 3. 从右向左扫描
        # 提取RGB通道 (假设输入是BGRA，所以R是索引2，G是1，B是0)
        # 确保通道顺序与你的 is_pixel_grayscale 逻辑匹配
        r_channel = frame_line[:, 2].astype(np.int16) # R
        g_channel = frame_line[:, 1].astype(np.int16) # G
        b_channel = frame_line[:, 0].astype(np.int16) # B
        a_channel = frame_line[:, 3] # A

        # 组合所有条件 (不透明 & 灰度)
        # 检查不透明度: (a_channel == ALPHA_OPAQUE)
        # 检查灰度: (abs(r_channel - g_channel) <= GRAY_TOLERANCE) & (abs(g_channel - b_channel) <= GRAY_TOLERANCE)
        # 检查白色: (r_channel > WHITE_THRESHOLD) & (g_channel > WHITE_THRESHOLD) & (b_channel > WHITE_THRESHOLD)
        is_valid_pixel_mask = (a_channel == ALPHA_OPAQUE) & \
                              (np.abs(r_channel - g_channel) <= GRAY_TOLERANCE) & \
                              (np.abs(g_channel - b_channel) <= GRAY_TOLERANCE)

        # 找出所有非有效像素的索引。如果存在，表示检测中断
        invalid_pixel_indices = np.where(~is_valid_pixel_mask)[0]
        if invalid_pixel_indices.size > 0:
            # 如果有无效像素，返回None (模拟你的原始代码中的中断逻辑)
            return None 

        # 找出所有白色像素的索引 (在已经确定为有效像素的前提下)
        is_white_mask = (r_channel > WHITE_THRESHOLD) & \
                        (g_channel > WHITE_THRESHOLD) & \
                        (b_channel > WHITE_THRESHOLD)
        
        white_indices = np.where(is_white_mask)[0]

        filled_width = 0
        if white_indices.size > 0:
            # white_indices[-1] 是最右边白色像素的索引 (相对于 frame_line 的起始)
            # 加 1 得到像素宽度
            filled_width = white_indices[-1] + 1
        
        return filled_width
    
    # 辅助函数：模拟 get_logical_frame_from_calibration
    def _simulate_get_logical_frame(pixel_width: Optional[int], calibration_profile: Dict[str, Any]) -> Optional[int]:
        """模拟 get_logical_frame_from_calibration 的逻辑。"""
        if pixel_width is None:
            return None

        pixel_map = calibration_profile.get('pixel_map', {})
        
        # 1. 直接匹配
        if str(pixel_width) in pixel_map:
            return pixel_map[str(pixel_width)]
        
        # 2. 近似匹配
        closest_pixel_value = -1
        min_diff = float('inf')

        for pixel_str in pixel_map.keys():
            try:
                pixel_val = int(pixel_str)
            except ValueError:
                continue # Skip invalid keys
            diff = abs(pixel_width - pixel_val)
            if diff < min_diff:
                min_diff = diff
                closest_pixel_value = pixel_val
        
        TOLERANCE = 5 
        if min_diff <= TOLERANCE:
            return pixel_map[str(closest_pixel_value)]
        else:
            return None # 未能匹配

    # --- 测试主逻辑 ---
    consumer_buffer = None
    frames_received = 0
    try:
        consumer_buffer = TripleSharedBuffer(**params, create=False)
        last_processed_idx = -1
        
        # 动态获取帧分辨率以计算ROI
        frame_height, frame_width, _ = consumer_buffer.shape
        x1, x2, y = _find_cost_bar_roi(frame_width, frame_height)
        
        # 准备一个模拟的校准配置文件
        mock_calibration_profile = {
            "pixel_map": {str(i): i // 10 for i in range(0, x2 - x1 + 1)}, # 假设每个像素宽度都有一个校准值
            "TOLERANCE": 5 # 模拟误差容忍
        }
        
        logger.info(f"性能测试消费者启动，动态计算ROI ({x1}, {x2}, {y})，模拟真实扫描和校准。")
        start_time = time.perf_counter()

        while not stop_event.is_set():
            current_idx = consumer_buffer.np_latest_idx[0]
            if current_idx != last_processed_idx:
                frames_received += 1
                last_processed_idx = current_idx
                
                full_frame = consumer_buffer.np_arrays[current_idx]
                
                # 裁剪出包含ROI的单行数据
                # np.clip 用于确保y坐标在有效范围内，防止越界
                scan_y = np.clip(y, 0, frame_height - 1)
                scan_x1 = np.clip(x1, 0, frame_width)
                scan_x2 = np.clip(x2, 0, frame_width)

                # 提取单行数据，注意 x1 和 x2 确保顺序正确
                if scan_x1 < scan_x2:
                    frame_line_data = full_frame[scan_y, scan_x1:scan_x2]
                else:
                    # 如果 ROI 无效，模拟返回 None
                    frame_line_data = np.empty((0, 4), dtype=np.uint8) 

                # 模拟提取填充宽度
                pixel_width = _simulate_get_raw_filled_pixel_width(frame_line_data, scan_x1, scan_x2)
                
                # 模拟逻辑帧转换
                _ = _simulate_get_logical_frame(pixel_width, mock_calibration_profile)
                
            else:
                time.sleep(0.0001)
        
        duration = time.perf_counter() - start_time
        fps = frames_received / duration if duration > 0 else 0
        result_queue.put({"frames": frames_received, "duration": duration, "fps": fps})
        exit(0)
    except Exception as e:
        logger.error(f"性能测试消费者任务失败: {e}", exc_info=True)
        result_queue.put(None)
        exit(1)
    finally:
        if consumer_buffer:
            consumer_buffer.close()