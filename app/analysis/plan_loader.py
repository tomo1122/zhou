import yaml
import logging

from pathlib import Path
from typing import List, Dict, Any, Optional

from pydantic import BaseModel, field_validator, ValidationError

from app.core.config import MergedConfig


logger = logging.getLogger(__name__)


class ActionModel(BaseModel):
    """定义单个动作的结构"""
    action_type: str  # e.g., 'deploy', 'skill', 'recall'
    params: Optional[Dict[str, Any]] = None


class FrameActionGroupModel(BaseModel):
    """定义单个触发帧及其包含的所有动作"""
    trigger_frame: int
    actions: List[ActionModel]

    @field_validator('trigger_frame')
    @classmethod
    def frame_must_be_non_negative(cls, v: int) -> int:
        """验证器：确保 trigger_frame 是非负数"""
        if v < 0:
            raise ValueError('trigger_frame 必须为非负整数')
        return v

    @field_validator('actions')
    @classmethod
    def actions_must_not_be_empty(cls, v: List[ActionModel]) -> List[ActionModel]:
        """验证器：确保 actions 列表不为空"""
        if not v:
            raise ValueError('actions 列表不能为空')
        return v


class PlanLoader:
    def __init__(self, config: MergedConfig):
        """
        初始化 PlanLoader。

        Args:
            config: 应用程序的统一配置对象。
        """
        self.plans_dir = config.project_root / "plans"
        self.plans_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"作战计划目录: {self.plans_dir}")

    def load(self, plan_name: str) -> List[FrameActionGroupModel]:
        """
        加载并验证指定的作战计划文件。

        Args:
            plan_name: 作战计划的文件名 (不含 .yaml 后缀)。

        Returns:
            一个已排序和验证的作战计划列表，每个元素都是 FrameActionGroupModel 对象。

        Raises:
            FileNotFoundError: 如果计划文件不存在。
            ValueError: 如果计划文件格式或内容无效。
        """
        filepath = self.plans_dir / f"{plan_name}.yaml"
        if not filepath.is_file():
            raise FileNotFoundError(f"作战计划文件未找到: {filepath}")
        logger.info(f"正在加载作战计划: {filepath.name}...")

        try:
            with filepath.open('r', encoding='utf-8') as f:
                raw_data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValueError(f"'{filepath.name}' 文件 YAML 格式错误: {e}")

        validated_plan: List[FrameActionGroupModel] = []
        for i, group_data in enumerate(raw_data):
            try:
                validated_group = FrameActionGroupModel.model_validate(group_data)
                validated_plan.append(validated_group)
            except ValidationError as e:
                raise ValueError(f"'{filepath.name}' 中第 {i+1} 个动作组验证失败:\n{e}")

        # 按 trigger_frame 对整个计划进行排序
        validated_plan.sort(key=lambda x: x.trigger_frame)
        logger.info(f"作战计划 '{plan_name}' 加载并验证成功，共 {len(validated_plan)} 个触发时间点。")
        return validated_plan


    def get_available_plans(self) -> List[str]:
        """扫描 plans 目录，返回所有可用的作战计划名称列表。 """
        if not self.plans_dir.is_dir():
            return []
        
        return sorted([p.stem for p in self.plans_dir.glob("*.yaml")])