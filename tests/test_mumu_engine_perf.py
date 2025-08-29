# test_mumu_engine_perf.py (高性能优化版)

import logging
import time
import sys
from pathlib import Path
import numpy as np
import ctypes # 需要 ctypes 来创建缓冲区

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.perception.engines.mumu import MumuCaptureEngine
from app.core.config import get_config

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

def run_performance_test():
    """
    Tests the raw performance of MumuCaptureEngine using pre-allocated buffers
    to minimize overhead, reflecting its true capture speed.
    """
    engine = None
    try:
        # 1. Load configuration
        config = get_config()
        config.source = 'mumu'
        
        logger.info(f"Loaded config. Testing with real source: '{config.source}'")
        logger.info(f"DLL Path: {config.mumu_dll_path}")

        # 2. Instantiate and start the engine
        engine = MumuCaptureEngine(config)
        logger.info("Starting capture engine...")
        engine.start()
        logger.info(f"Engine started. Resolution: {engine.width}x{engine.height}. Beginning performance test.")
        
        # --- 关键修改：在循环外预先分配一个缓冲区 ---
        # 这模拟了我们IPC生产者中的 `temp_ctypes_buffer`
        buffer_size = engine.width * engine.height * 4
        capture_buffer = (ctypes.c_ubyte * buffer_size)()
        logger.info("Pre-allocated capture buffer for high-performance testing.")

        # 3. Loop for 10 seconds and count frames
        frame_count = 0
        duration = 10.0 # 增加测试时长以获得更稳定的结果
        start_time = time.perf_counter()

        while time.perf_counter() - start_time < duration:
            # --- 关键修改：调用最高效的底层函数 ---
            # 直接截图到我们预先分配好的缓冲区
            result = engine.capture_frame_into_buffer(capture_buffer)
            
            if result == 0:
                frame_count += 1
            else:
                logger.warning(f"capture_frame_into_buffer() failed with code {result}.")
        
        end_time = time.perf_counter()
        actual_duration = end_time - start_time

        # 4. Calculate and print results
        fps = frame_count / actual_duration
        logger.info("Performance test finished.")
        print("\n--- High-Performance Test Results ---")
        print(f"Total frames captured in {actual_duration:.2f} seconds: {frame_count}")
        print(f"Average FPS: {fps:.2f}")
        print("-------------------------------------\n")

    except Exception as e:
        logger.critical(f"An error occurred during the test: {e}", exc_info=True)
    finally:
        # 5. Stop the engine
        if engine:
            logger.info("Stopping capture engine...")
            engine.stop()
            logger.info("Engine stopped.")

if __name__ == "__main__":
    run_performance_test()