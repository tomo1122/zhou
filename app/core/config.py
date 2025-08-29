
import yaml
from pathlib import Path
from pydantic import BaseModel, Field
from typing import Dict, Any

# Pydantic Models for Validation
class SettingsConfig(BaseModel):
    source: str = Field(..., description="Data source, e.g., 'mumu' or 'mock'")
    fps: int = Field(..., gt=0, description="Target frames per second")
    log_level: str = Field('INFO', description="Logging level")

class MumuConfig(BaseModel):
    mumu_base_path: str = Field(..., description="Path to the MuMu Player installation directory")
    mumu_dll_path: str = Field(..., description="Path to the MuMu Player screen capture DLL")
    mumu_instance_index: int = Field(0, ge=0, description="Index of the MuMu Player instance to connect to")

class MergedConfig(SettingsConfig, MumuConfig):
    """A model that includes all fields from other models."""
    pass

def load_and_merge_configs(config_dir: Path) -> Dict[str, Any]:
    """Loads all YAML files from a directory and merges them."""
    merged_data = {}
    if not config_dir.is_dir():
        raise FileNotFoundError(f"Configuration directory not found: {config_dir}")

    for config_file in config_dir.glob('*.yaml'):
        with open(config_file, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
            if data:
                merged_data.update(data)
    return merged_data

def get_config() -> MergedConfig:
    """
    Loads, merges, validates, and returns the application configuration.
    """
    # Assume the script is run from the project root (zhou_v2)
    # or that the path is relative to the app's execution context.
    project_root = Path(__file__).parent.parent.parent
    config_path = project_root / 'configs'
    
    merged_data = load_and_merge_configs(config_path)
    
    # Validate the merged data with Pydantic
    try:
        config = MergedConfig.model_validate(merged_data)
        return config
    except Exception as e:
        print(f"Configuration validation error: {e}")
        raise

# Global config instance
# This will be executed on module import
try:
    config = get_config()
except Exception as e:
    # Handle cases where config loading fails on import,
    # e.g., during testing or if files are missing.
    config = None
    print(f"Failed to load configuration on module import: {e}")

