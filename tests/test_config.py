
import pytest
from pydantic import ValidationError
from pathlib import Path
import yaml

# Adjust the path to import from the app
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.config import MergedConfig, load_and_merge_configs

@pytest.fixture
def temp_config_dir(tmp_path: Path) -> Path:
    """Create a temporary directory with dummy config files."""
    config_dir = tmp_path / "configs"
    config_dir.mkdir()

    settings_data = {
        'source': 'mock',
        'fps': 30,
        'log_level': 'DEBUG'
    }
    mumu_data = {
        'mumu_base_path': '/path/to/mumu',
        'mumu_dll_path': '/path/to/dll',
        'mumu_instance_index': 1
    }

    with open(config_dir / "settings.yaml", "w") as f:
        yaml.dump(settings_data, f)
    
    with open(config_dir / "mumu.yaml", "w") as f:
        yaml.dump(mumu_data, f)

    return config_dir

def test_successful_config_loading(temp_config_dir: Path):
    """Tests that configs are loaded, merged, and validated successfully."""
    merged_data = load_and_merge_configs(temp_config_dir)
    config = MergedConfig.model_validate(merged_data)

    assert config.source == 'mock'
    assert config.fps == 30
    assert config.log_level == 'DEBUG'
    assert config.mumu_base_path == '/path/to/mumu'
    assert config.mumu_dll_path == '/path/to/dll'
    assert config.mumu_instance_index == 1

def test_validation_error_on_invalid_data(temp_config_dir: Path):
    """Tests that a Pydantic ValidationError is raised for invalid data types."""
    # Overwrite the valid settings with invalid data to create a deterministic test
    invalid_settings = {
        'source': 'mock',
        'fps': 'not-an-integer', # Invalid type
        'log_level': 'DEBUG'
    }
    with open(temp_config_dir / "settings.yaml", "w") as f:
        yaml.dump(invalid_settings, f)

    merged_data = load_and_merge_configs(temp_config_dir)

    with pytest.raises(ValidationError) as exc_info:
        MergedConfig.model_validate(merged_data)
    
    # Check if the error message contains information about the invalid field
    assert 'fps' in str(exc_info.value)

def test_missing_required_field(temp_config_dir: Path):
    """Tests that a validation error occurs if a required field is missing."""
    # Create a config file that is missing a required field ('source')
    incomplete_data = {
        'fps': 60,
    }
    with open(temp_config_dir / "incomplete.yaml", "w") as f:
        yaml.dump(incomplete_data, f)

    merged_data = load_and_merge_configs(temp_config_dir)

    # Remove the valid settings file to ensure 'source' is missing
    (temp_config_dir / "settings.yaml").unlink()

    merged_data_incomplete = load_and_merge_configs(temp_config_dir)

    with pytest.raises(ValidationError):
        MergedConfig.model_validate(merged_data_incomplete)

