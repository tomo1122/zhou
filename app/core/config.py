import yaml

from pathlib import Path
from typing import Dict, Any, Optional

from pydantic import BaseModel, Field, ConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

class SettingsConfig(BaseModel):
    fps: int = Field(60, gt=0, description = "期望FPS")
    perf_test_duration: float = Field(5.0, gt=0, description = "FPS测试任务测试时长")
    log_level: str = Field('INFO', description = "Logging level")
    active_calibration_profile: Optional[str] = Field(None, description="Ruler进程要使用的校准文件名")

class MumuConfig(BaseModel):
    mumu_base_path: str = Field(..., description = "MUMU模拟器地址")
    mumu_dll_path: str = Field(..., description = "MUMU模拟器DLL地址")
    mumu_instance_index: int = Field(0, ge=0, description = "MUMU模拟器实例索引")
    device_serial: str = Field("127.0.0.1:16384", description="adb 连接的地址")
    

class MergedConfig(SettingsConfig, MumuConfig):
    """一个包含所有配置字段的统一模型"""
    model_config = ConfigDict(extra='allow')

def load_and_merge_configs(config_dir: Path) -> Dict[str, Any]:
    """
    从指定目录加载所有 .yaml 文件, 并进行合并
    """
    merged_data = {}
    if not config_dir.is_dir():
        raise FileNotFoundError(f"配置文件夹不存在: {config_dir}")

    for config_file in config_dir.glob('*.yaml'):
        with open(config_file, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
            if data:
                merged_data.update(data)
    return merged_data


def get_config() -> MergedConfig:
    """
    加载、合并、验证并返回应用程序的配置对象
    """
    config_path = PROJECT_ROOT / 'configs'
    merged_data = load_and_merge_configs(config_path)
    config = MergedConfig.model_validate(merged_data)
    config.project_root = PROJECT_ROOT
    return config

