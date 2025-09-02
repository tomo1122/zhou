import time
import logging
from typing import Optional, Dict, Any, Tuple

import numpy as np

from app.core.config import MergedConfig
from app.analysis.calibrator import CalibrationManager
from app.analysis.vision_utils import find_cost_bar_roi, get_raw_filled_pixel_width_np


logger = logging.getLogger(__name__)


# 定义分析结果的数据结构: (总帧数, 逻辑帧, 周期计数, 时间戳)
AnalysisResult = Tuple[int, int, int, float]


class CostBarAnalyzer:
    """
    一个有状态的分析器，负责将图像帧流转换为逻辑帧流。
    它封装了校准文件的加载、ROI计算、周期检测和状态管理。
    """
    def __init__(self, config: MergedConfig):
        self.config = config
        self.calib_manager = CalibrationManager(config)
        
        # 1. 加载校准配置文件
        self.active_profile: Dict[str, Any] = self._load_profile()

        # 2. 初始化分析所需的状态变量
        self.roi: Optional[Tuple[int, int, int]] = None
        self.reset_state()
    

    def _load_profile(self) -> Dict[str, Any]:
        """根据配置加载校准文件。"""
        profile_name = self.config.active_calibration_profile
        if not profile_name:
            raise ValueError("配置中未指定 'active_calibration_profile'。请在 settings.yaml 中配置。")
        
        profile_data = self.calib_manager.load(profile_name)
        if not profile_data:
            raise FileNotFoundError(f"无法加载校准文件 '{profile_name}'。请先运行校准程序。")
        
        logger.info(f"成功加载校准文件: '{profile_name}'")
        return profile_data
    

    def _get_logical_frame(self, pixel_width: Optional[int], profile_model: Dict[str, Any]) -> Optional[int]:
        """将像素宽度转换为逻辑帧"""
        if pixel_width is None:
            return None

        pixel_map = profile_model.get('pixel_map', {})
        
        # 1. 直接匹配
        if str(pixel_width) in pixel_map:
            return pixel_map[str(pixel_width)]
        
        # 2. 近似匹配
        closest_pixel_value, min_diff = min(
            ((int(k), abs(pixel_width - int(k))) for k in pixel_map.keys() if k.isdigit()),
            key=lambda item: item[1],
            default=(None, float('inf'))
        )
        
        # 容忍5个像素的误差
        TOLERANCE = 5  
        if min_diff <= TOLERANCE:
            return pixel_map[str(closest_pixel_value)]
        else:
            return None


    def analyze_frame(self, frame: np.ndarray) -> Optional[AnalysisResult]:
        """
        分析单帧图像，如果检测到有效的逻辑帧变化，则返回分析结果。
        
        Args:
            frame: BGRA格式的Numpy数组图像。

        Returns:
            如果逻辑帧发生变化，返回 AnalysisResult 元组，否则返回 None。
        """
        # 首次调用时计算ROI
        if self.roi is None:
            height, width, _ = frame.shape
            prof_w = self.active_profile['screen_width']
            prof_h = self.active_profile['screen_height']
            if width != prof_w or height != prof_h:
                logger.warning(f"当前帧分辨率({width}x{height})与校准文件({prof_w}x{prof_h})不匹配。")
            self.roi = find_cost_bar_roi(width, height)

        pixel_width = get_raw_filled_pixel_width_np(frame, self.roi)
        # 未检测到费用条
        if pixel_width is None:
            logger.debug(f'未检测到费用条')
            return None
        
        # （不知道有什么用，我的校准文件总是只有一个模型。但是加进去没啥影响，那就不管。）
        num_profiles = len(self.active_profile['profiles'])
        current_profile_index = self.cycle_counter % num_profiles
        active_profile_model = self.active_profile['profiles'][current_profile_index]
        # 获取当前的逻辑帧
        logical_frame = self._get_logical_frame(pixel_width, active_profile_model)
        self.last_detection_time = time.time()
        
        # 仅在逻辑帧发生变化时才继续处理和发布
        if logical_frame != self.previous_logical_frame:
            # 一个循环多少帧
            total_frames_in_cycle = active_profile_model.get('total_frames', 30)

            # 翻圈检测：当逻辑帧从周期末尾跳到周期开头时
            if self.previous_logical_frame > total_frames_in_cycle * 0.8 and logical_frame < total_frames_in_cycle * 0.2:
                self.cycle_base_frames += total_frames_in_cycle
                self.cycle_counter += 1
            
            current_total_frames = self.cycle_base_frames + logical_frame
            self.previous_logical_frame = logical_frame
            
            return (current_total_frames, logical_frame, self.cycle_counter, time.time())
            
        return None

    def reset_state(self):
        """重置分析器的内部状态。"""
        logger.info("分析器状态已重置。")
        self.cycle_counter = 0
        self.cycle_base_frames = 0
        self.previous_logical_frame = -1
        self.last_detection_time = time.time()