# 生成校验文件
import json
import time
import ctypes
import logging
import statistics

from pathlib import Path
from collections import Counter
from typing import Dict, Any, List, Optional, Callable

import numpy as np

from app.core.config import MergedConfig
from app.perception.engines.base import BaseCaptureEngine
from app.analysis.vision_utils import find_cost_bar_roi, get_raw_filled_pixel_width_np


logger = logging.getLogger(__name__)


def _calculate_jaccard_similarity(set1: set, set2: set) -> float:
    """计算两个集合的Jaccard相似度"""
    if not set1 and not set2: return 1.0
    if not set1 or not set2: return 0.0
    intersection = set1.intersection(set2)
    union = set1.union(set2)
    return len(intersection) / len(union) if union else 0.0


class CalibrationManager:
    """负责管理校准文件的加载、保存和查询"""
    def __init__(self, config: MergedConfig):
        self.calibration_dir = config.project_root / "calibration"
        self.calibration_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"校准文件目录: {self.calibration_dir}")

    def save(self, data: Dict[str, Any], basename: str) -> Path:
        """将校准数据保存到文件"""
        profiles = data.get('profiles', [])
        frame_counts_str = "-".join(str(p['total_frames']) for p in profiles) + "f" if profiles else "0f"
        res_w, res_h = data['screen_width'], data['screen_height']
        
        filename = f"{basename}_{frame_counts_str}_{res_w}x{res_h}.json"
        filepath = self.calibration_dir / filename
        
        with filepath.open('w', encoding='utf-8') as f:
            json.dump(data, f, indent=4)
        
        logger.info(f"校准数据已保存到: {filepath}")
        return filepath

    def load(self, filename: str) -> Optional[Dict[str, Any]]:
        """根据文件名加载校准数据"""
        filepath = self.calibration_dir / filename
        if not filepath.is_file():
            logger.error(f"校准文件未找到或不是一个文件: {filepath}")
            return None
        
        try:
            with filepath.open('r', encoding='utf-8') as f:
                data = json.load(f)

            required_keys = ["detection_mode", "profiles", "screen_width", "screen_height", "calibration_time"]
            for key in required_keys:
                if key not in data:
                    raise ValueError(f"校准文件 '{filename}' 缺少必需的键: '{key}'")
            
            if not isinstance(data['profiles'], list) or not data['profiles']:
                 raise ValueError(f"校准文件 '{filename}' 中的 'profiles' 必须是一个非空列表。")
            return data
        
        except json.JSONDecodeError:
            logger.error(f"校准文件格式损坏 (无效的JSON): {filepath}")
            return None
        except ValueError as e:
            logger.error(f"校准文件内容校验失败: {e}")
            return None

    def get_profiles_info(self) -> List[Dict[str, str]]:
        """获取所有可用校准文件的信息列表。"""
        profiles_info = []
        for filepath in sorted(self.calibration_dir.glob("*.json")):
            try:
                parts = filepath.stem.split("_")
                if len(parts) >= 3:
                    profiles_info.append({
                        "filename": filepath.name, 
                        "basename": parts[0],
                        "total_frames_str": parts[1], 
                        "resolution": parts[2]
                    })
                else:
                    logger.warning(f"发现格式不正确的校准文件名，已跳过: {filepath.name}")
            except Exception:
                logger.warning(f"解析校准文件名时出错，已跳过: {filepath.name}")
        return profiles_info


def run_calibration(engine: BaseCaptureEngine, num_cycles: int = 6, progress_callback: Optional[Callable[[float], None]] = None) -> Dict[str, Any]:
    """
    执行完整的费用条校准流程。

    Args:
        engine: 一个已经启动的截图引擎实例。
        num_cycles: 需要收集的完整费用条循环次数。
        progress_callback: 一个可选的回调函数，用于报告进度 (0.0 to 100.0)。

    Returns:
        一个包含校准结果的字典，可直接被 CalibrationManager.save() 使用。
    
    Raises:
        RuntimeError: 如果在校准过程中未能收集到有效数据或构建模型失败。
    """
    logger.info(f"开始费用条校准，目标循环次数: {num_cycles}。")
    
    width, height = engine.width, engine.height
    roi = find_cost_bar_roi(width, height)
    total_bar_width = roi[1] - roi[0]
    
    # 为高性能截图准备临时缓冲区
    buffer_size = width * height * 4
    temp_buffer = (ctypes.c_ubyte * buffer_size)()

    cycle_samples: List[List[int]] = []
    current_cycle_data: List[int] = []
    previous_cost_raw: Optional[int] = None
    is_collecting = False
    
    logger.info("数据收集中...")
    logger.info("需要进入战斗，处于子弹时间状态下让时间流逝")
    start_time = time.time()
    while len(cycle_samples) < num_cycles:
        if time.time() - start_time > 60:
             raise RuntimeError("校准超时：60秒内未能收集到足够数据。")

        # 高性能截图
        engine.capture_frame_into_buffer(temp_buffer)
        frame_np_raw = np.frombuffer(temp_buffer, dtype=np.uint8).reshape((height, width, 4))
        frame_np = np.flipud(frame_np_raw)
        current_cost_raw = get_raw_filled_pixel_width_np(frame_np, roi)

        if progress_callback:
            # 当前费用条的填充比例
            fill_pct = current_cost_raw / total_bar_width if current_cost_raw is not None and total_bar_width > 0 else 0.0 
            # 整个校准任务的总进度
            overall_progress = (len(cycle_samples) + fill_pct) / num_cycles
            # ui callback
            progress_callback(min(100.0, overall_progress * 100))

        # 翻圈检测
        if previous_cost_raw is not None and current_cost_raw is not None and total_bar_width > 0:
            if previous_cost_raw > total_bar_width * 0.9 and current_cost_raw < total_bar_width * 0.1:
                is_collecting = True
                if current_cycle_data:
                    cycle_samples.append(current_cycle_data)
                    logger.info(f"已收集 {len(cycle_samples)}/{num_cycles} 个费用条循环样本。")
                    current_cycle_data = []

        if is_collecting and current_cost_raw is not None:
            current_cycle_data.append(current_cost_raw)
        
        previous_cost_raw = current_cost_raw
        time.sleep(0.001)

    logger.info("数据收集完成，开始聚类和建模...")
    if not cycle_samples:
        raise RuntimeError("未能收集到任何有效的费用条循环数据。")

    # 基于Jaccard相似度的内容聚类
    clusters: List[List[List[int]]] = []
    SIMILARITY_THRESHOLD = 0.9
    for sample in cycle_samples:
        sample_set = set(sample)
        if not sample_set: continue
        
        best_match_idx, max_sim = -1, -1
        for i, cluster in enumerate(clusters):
            representative_set = set(cluster[0])
            similarity = _calculate_jaccard_similarity(sample_set, representative_set)
            if similarity > max_sim:
                max_sim, best_match_idx = similarity, i
        
        if max_sim >= SIMILARITY_THRESHOLD:
            clusters[best_match_idx].append(sample)
        else:
            clusters.append([sample])
    logger.info(f"聚类完成，共形成 {len(clusters)} 个不同的费用循环模型。")

    # 为每个簇独立建模
    final_profiles = []
    for i, cluster in enumerate(clusters):
        logger.info(f"--- 正在为第 {i + 1} 个模型 (含 {len(cluster)} 个样本) 进行分析 ---")
        
        all_widths_in_cluster = [width for sample in cluster for width in sample]
        width_counts = Counter(all_widths_in_cluster)
        
        # 统计学方法检测隐藏帧
        count_zero = width_counts.get(0, 0)
        non_zero_counts = [count for width, count in width_counts.items() if width > 0]
        num_hidden_frames = 0
        if non_zero_counts:
            baseline_freq = statistics.median(non_zero_counts)
            if baseline_freq > 0:
                num_frames_at_zero = round(count_zero / baseline_freq)
                num_hidden_frames = max(0, num_frames_at_zero - 1)
                if num_hidden_frames > 0:
                    logger.warning(f"模型 {i+1}: 检测到 {num_hidden_frames} 个隐藏的辉光帧。")
        
        unique_widths = sorted(width_counts.keys())
        pixel_map = {}
        total_frames = len(unique_widths) + num_hidden_frames
        
        frame_offset = 0
        if 0 in unique_widths:
            pixel_map[str(0)] = 0
            frame_offset = 1 + num_hidden_frames
        
        non_zero_widths = [w for w in unique_widths if w > 0]
        for idx, pixel_width in enumerate(non_zero_widths):
            pixel_map[str(pixel_width)] = idx + frame_offset

        final_profiles.append({"total_frames": total_frames, "pixel_map": pixel_map})
        logger.info(f"模型 {i+1} 构建完成，总帧数: {total_frames}。")
    
    if not final_profiles:
        raise RuntimeError("校准失败：未能构建任何有效的模型。")

    return {
        "detection_mode": "alternating" if len(final_profiles) > 1 else "single",
        "profiles": final_profiles,
        "screen_width": width,
        "screen_height": height,
        "calibration_time": time.time()
    }