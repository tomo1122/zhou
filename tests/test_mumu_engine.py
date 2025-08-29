import time
import ctypes 
import logging
from pathlib import Path

import pytest
import numpy as np

from app.core.config import get_config
from app.perception.engines.mumu import MumuCaptureEngine


logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)


@pytest.fixture(scope="module")
def mumu_config():
    """
    提供一个加载好的、用于MuMu测试的配置对象。
    """
    config = get_config()
    logger.info("加载MuMu测试配置文件成功")
    return config


@pytest.fixture(scope="module")
def initialized_engine(mumu_config):
    """
    提供一个已经初始化并启动的 MumuCaptureEngine 实例。
    """
    engine = None
    try:
        engine = MumuCaptureEngine(mumu_config)
        engine.start()
        logger.info("初始化engine成功")
        yield engine
    finally:
        if engine:
            engine.stop()
            logger.info("关闭engine成功")


def test_config_validity(mumu_config):
    """
    测试项 1: 验证配置文件的存在性和路径的真实性。
    """
    # 检查属性是否存在
    assert hasattr(mumu_config, 'mumu_dll_path'), "Config is missing 'mumu_dll_path'"
    assert hasattr(mumu_config, 'mumu_base_path'), "Config is missing 'mumu_base_path'"
    assert hasattr(mumu_config, 'mumu_instance_index'), "Config is missing 'mumu_instance_index'"

    # 检查路径是否真实存在
    dll_path = Path(mumu_config.mumu_dll_path)
    base_path = Path(mumu_config.mumu_base_path)
    assert dll_path.is_file(), f"DLL path does not exist or is not a file: {dll_path}"
    assert base_path.is_dir(), f"Base path does not exist or is not a directory: {base_path}"
    
    logger.info("Test 'test_config_validity' PASSED.")


def test_engine_initialization(initialized_engine):
    """
    测试项 2: 验证引擎能否成功初始化和启动。
    """
    engine = initialized_engine
    
    assert engine is not None
    assert engine.width > 0, f"当前截图宽度: {engine.width} ;应当大于0"
    assert engine.height > 0, f"当前截图高度: ({engine.height}) ;应当大于0"

    logger.info(f"Test 'test_engine_initialization' PASSED. Resolution: {engine.width}x{engine.height}")


def test_frame_capture(initialized_engine):
    """
    测试项 3: 验证能否成功截图。
    """
    # 指定内存
    engine = initialized_engine
    buffer_size = engine.width * engine.height * 4
    capture_buffer = (ctypes.c_ubyte * buffer_size)()
    
    # 内存初始状态
    buffer_view_np = np.frombuffer(capture_buffer, dtype=np.uint8)
    buffer_view_np.fill(0)
    assert np.all(buffer_view_np == 0)

    # 状态码断言
    result = engine.capture_frame_into_buffer(capture_buffer)
    assert result == 0, f"将截图写入内存失败，错误码: {result}"

    # 内存内容断言
    assert np.any(buffer_view_np != 0), "内存缓冲区截图前后无变化"

    logger.info("Test 'test_single_frame_capture' PASSED.")


@pytest.mark.performance
def test_capture_performance_against_config(mumu_config, initialized_engine):
    """
    测试项 4: 验证截图性能是否达到配置文件中指定的 FPS 要求（默认60fps）
    """
    config = mumu_config
    engine = initialized_engine

    expected_fps = getattr(config, 'fps', 60.0)
    test_duration = getattr(config, 'perf_test_duration', 5.0)
    fps_tolerance = (1 - 0.05)
    required_min_fps = expected_fps * fps_tolerance
    
    logger.info(
        f"开始验证截图性能，目标FPS: {expected_fps} (config.json中可以修改)"
        f"测试时长: {test_duration}s, 最低FPS: {required_min_fps:.2f}"
    )

    # 开始截图
    buffer_size = engine.width * engine.height * 4
    capture_buffer = (ctypes.c_ubyte * buffer_size)()
    
    frame_count = 0
    start_time = time.perf_counter()

    while time.perf_counter() - start_time < test_duration:
        if engine.capture_frame_into_buffer(capture_buffer) == 0:
            frame_count += 1
    
    end_time = time.perf_counter()
    actual_duration = end_time - start_time
    actual_fps = frame_count / actual_duration

    # 结果输出与断言
    print(f"\n-------------- 截图性能 --------------")
    print(f"目标FPS: {expected_fps}")
    print(f"测试时长: {actual_duration:.2f} s")
    print(f"生成截图: {frame_count} 张")
    print(f"实际FPS: {actual_fps:.2f}")
    print("--------------------------------------")
    
    assert actual_fps >= required_min_fps, \
        f"Performance FAILED. Achieved FPS ({actual_fps:.2f}) is below the required minimum ({required_min_fps:.2f})."
