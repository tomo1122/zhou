import yaml
import logging

import numpy as np

from pathlib import Path
from typing import Dict, Any, List, Tuple

from pydantic import BaseModel, Field, ValidationError


logger = logging.getLogger(__name__)


class PlanAction(BaseModel):
    frame: int
    action: str
    params: Dict[str, Any] = Field(default_factory=dict)


class PlanMetadata(BaseModel):
    map_name: str


class MapCoordinates(BaseModel):
    src_points: List[Tuple[int, int]]
    dst_points: List[Tuple[int, int]]
    grid_dimensions: Tuple[int, int]


class BattlePlan(BaseModel):
    metadata: PlanMetadata
    map_coordinates: MapCoordinates
    actions: List[PlanAction]


class ArknightsCoordinateTransformer:
    """坐标变换器，从您的 map_transformer.py 移植并简化。"""
    def __init__(self, src_points: list, dst_points: list):
        self.matrix = self._calculate_matrix(np.array(src_points), np.array(dst_points))

    @staticmethod
    def _calculate_matrix(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
        # 此处省略了复杂的SVD求解过程，直接使用OpenCV的findHomography会更简单健壮，
        # 但为保持与您代码一致，此处假设您的求解方法是有效的。
        # 为简化示例，我们用一个占位符。在实际项目中，您应移入完整的求解代码。
        # from cv2 import findHomography, warpPerspective
        # matrix, _ = findHomography(src, dst)
        # return matrix
        
        # 使用您原来的SVD求解方法
        A = []
        for i in range(4):
            x, y = src[i]
            xp, yp = dst[i]
            A.extend([
                [-x, -y, -1, 0, 0, 0, x*xp, y*xp, xp],
                [0, 0, 0, -x, -y, -1, x*yp, y*yp, yp]
            ])
        A = np.asarray(A)
        _, _, Vh = np.linalg.svd(A)
        matrix = Vh[-1, :].reshape((3, 3))
        return matrix / matrix[2, 2]

    def transform_point(self, point: tuple) -> Tuple[int, int]:
        p_src = np.array([point[0], point[1], 1.0])
        p_dst_h = self.matrix @ p_src
        w = p_dst_h[2]
        if abs(w) < 1e-6: return (0, 0)
        return (int(p_dst_h[0] / w), int(p_dst_h[1] / w))


class PlanHelper:
    """
    负责加载作战计划并提供坐标变换和网格计算功能的辅助类。
    """
    def __init__(self, plan_path: Path, screen_resolution: Tuple[int, int]):
        self.plan: BattlePlan = self._load_and_validate_plan(plan_path)
        self.screen_width, self.screen_height = screen_resolution
        
        coords = self.plan.map_coordinates
        self.transformer = ArknightsCoordinateTransformer(coords.src_points, coords.dst_points)
        self.grid_centers = self._calculate_grid_centers(coords.src_points, coords.grid_dimensions)

    def _load_and_validate_plan(self, plan_path: Path) -> BattlePlan:
        if not plan_path.is_file():
            raise FileNotFoundError(f"作战计划文件未找到: {plan_path}")
        try:
            with open(plan_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
            return BattlePlan.model_validate(data)
        except yaml.YAMLError as e:
            raise IOError(f"解析作战计划YAML文件失败: {e}")
        except ValidationError as e:
            raise ValueError(f"作战计划文件内容校验失败: {e}")

    def _calculate_grid_centers(self, src_corners: list, grid_dimensions: tuple) -> dict:
        cols, rows = grid_dimensions
        p_tl, p_tr, p_br, p_bl = np.array(src_corners, dtype=np.float32)

        centers = {}
        for r in range(rows):
            for c in range(cols):
                u, v = (c + 0.5) / cols, (r + 0.5) / rows
                p_top = (1 - u) * p_tl + u * p_tr
                p_bottom = (1 - u) * p_bl + u * p_br
                src_center = tuple( (1 - v) * p_top + v * p_bottom )
                centers[(c + 1, r + 1)] = self.transformer.transform_point(src_center)
        return centers

    def get_actions(self) -> List[Dict[str, Any]]:
        """获取排序后的动作列表，并预处理坐标。"""
        sorted_actions = sorted([a.model_dump() for a in self.plan.actions], key=lambda x: x['frame'])
        
        for action in sorted_actions:
            params = action.get("params", {})
            if "grid" in params and isinstance(params["grid"], list):
                 grid_key = tuple(params["grid"])
                 if grid_key in self.grid_centers:
                     # 将网格坐标转换为屏幕绝对坐标
                     params["pos"] = self.grid_centers[grid_key]
                 else:
                     logger.warning(f"动作 {action} 中的网格坐标 {grid_key} 无效，可能导致执行失败。")
        
        return sorted_actions