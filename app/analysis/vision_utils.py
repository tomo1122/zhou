import logging

from typing import Optional, Tuple

import numpy as np


logger = logging.getLogger(__name__)


def find_cost_bar_roi(screen_width: int, screen_height: int) -> Tuple[int, int, int]:
    """根据屏幕分辨率计算费用条的位置 (ROI)"""
    REF_WIDTH, REF_HEIGHT = 1920.0, 1080.0
    REF_ASPECT_RATIO = REF_WIDTH / REF_HEIGHT
    
    X1_OFFSET_FROM_RIGHT_REF = REF_WIDTH - 1739
    X2_OFFSET_FROM_RIGHT_REF = REF_WIDTH - 1919
    Y1_OFFSET_FROM_BOTTOM_REF = REF_HEIGHT - 810
    Y2_OFFSET_FROM_BOTTOM_REF = REF_HEIGHT - 817

    current_aspect_ratio = screen_width / screen_height
    scale = screen_height / REF_HEIGHT if current_aspect_ratio >= REF_ASPECT_RATIO else screen_width / REF_WIDTH

    x1 = screen_width - X1_OFFSET_FROM_RIGHT_REF * scale
    x2 = screen_width - X2_OFFSET_FROM_RIGHT_REF * scale
    y1 = screen_height - Y1_OFFSET_FROM_BOTTOM_REF * scale
    y2 = screen_height - Y2_OFFSET_FROM_BOTTOM_REF * scale

    x1_int, x2_int = round(x1), round(x2)
    y_mid_int = round((y1 + y2) / 2)

    return (x1_int, x2_int, y_mid_int)


def get_raw_filled_pixel_width_np(frame: np.ndarray, roi: Tuple[int, int, int]) -> Optional[int]:
    """从费用条ROI中提取填充部分的像素宽度"""
    WHITE_THRESHOLD = 250
    GRAY_TOLERANCE = 20
    ALPHA_OPAQUE = 255
    x1, x2, y = roi
    
    # 确保 ROI 坐标有效
    height, width, _ = frame.shape
    y = np.clip(y, 0, height - 1)
    x1 = np.clip(x1, 0, width)
    x2 = np.clip(x2, 0, width)
    total_width = x2 - x1
    if total_width <= 0: return None
    
    try:
        line_data = frame[y, x1:x2]
        
        # 1. 健全性检查：检查ROI的末端像素
        b_end, g_end, r_end, a_end = line_data[-1]
        is_end_pixel_grayscale = (np.abs(int(r_end) - int(g_end)) <= GRAY_TOLERANCE and \
                                  np.abs(int(g_end) - int(b_end)) <= GRAY_TOLERANCE)
        if a_end != ALPHA_OPAQUE or not is_end_pixel_grayscale:
            return None
        
        # 2. 满费检查
        if all(c > WHITE_THRESHOLD for c in (r_end, g_end, b_end)):
            return total_width
        
        # 3. 从右向左扫描
        b_ch, g_ch, r_ch, a_ch = line_data[:, 0], line_data[:, 1], line_data[:, 2], line_data[:, 3]
        
        is_grayscale_mask = (np.abs(r_ch.astype(np.int16) - g_ch.astype(np.int16)) <= GRAY_TOLERANCE) & \
                            (np.abs(g_ch.astype(np.int16) - b_ch.astype(np.int16)) <= GRAY_TOLERANCE)
        is_valid_mask = (a_ch == ALPHA_OPAQUE) & is_grayscale_mask
        if not np.all(is_valid_mask):
            return None

        is_white_mask = (r_ch > WHITE_THRESHOLD) & (g_ch > WHITE_THRESHOLD) & (b_ch > WHITE_THRESHOLD)
        white_indices = np.where(is_white_mask)[0]
        
        return white_indices[-1] + 1 if white_indices.size > 0 else 0

    except IndexError:
        return None