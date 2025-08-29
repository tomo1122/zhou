import logging
import time
from multiprocessing import Process, Event
import numpy as np
from numba import jit
import sys
from pathlib import Path

# Setup paths
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.ipc.triple_shared_buffer import TripleSharedBuffer 
from app.perception.capture_process import run_capture
from app.core.config import get_config 

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] [%(processName)s] %(message)s')
logger = logging.getLogger(__name__)

# 消费者函数 mock_consumer 保持不变

@jit(nopython=True)
def process_frame_kernel(read_buffer):
    """JIT-compiled kernel to perform calculations on a single frame."""
    b, g, r, a = read_buffer[0, 0, 0], read_buffer[0, 0, 1], read_buffer[0, 0, 2], read_buffer[0, 0, 3]
    return (int(r) << 16) + (int(g) << 8) + int(b)

def mock_consumer(ipc_params: dict, stop_event: Event):
    ipc_buffer = None
    try:
        ipc_buffer = TripleSharedBuffer(**ipc_params, create=False)
        logger.info("Consumer process started. JIT compiling...")

        dummy_frame = np.zeros(ipc_buffer.shape, dtype=ipc_buffer.dtype)
        process_frame_kernel(dummy_frame)
        logger.info("JIT compilation finished. Starting benchmark.")
        
        logger.info("Starting benchmark.")
        
        frames_received = 0
        last_processed_idx = -1 
        
        start_time = time.perf_counter()

        while not stop_event.is_set():
            # 1. 直接从 TripleSharedBuffer 获取最新帧的索引
            current_idx = ipc_buffer.np_latest_idx[0]
            
            # 2. 如果索引是新的，我们就处理它
            if current_idx != last_processed_idx:
                frames_received += 1
                last_processed_idx = current_idx
                
                # (可选) 仍然可以读取一下数据，以确保测试的负载相似
                read_buffer = ipc_buffer.np_arrays[current_idx]
                _ = read_buffer[0, 0, 0]
            else:
                # 如果索引没变，说明生产者还没写完下一帧，短暂休眠
                time.sleep(0.0001) # 使用更短的休眠时间
        
        end_time = time.perf_counter()

        duration = end_time - start_time
        fps = frames_received / (duration + 1e-9)
        logger.info("Consumer process shutting down.")
        # --- 修改打印结果，明确我们现在计数的是“新索引” ---
        print(f"    -> RESULT: Total unique indices: {frames_received}, Duration: {duration:.2f}s, FPS: {fps:.2f}")

    except Exception as e:
        logger.critical(f"An error occurred in consumer process: {e}", exc_info=True)


if __name__ == "__main__":
    config = get_config()
    config.source = 'mumu'
    logger.info(f"Loaded config. Testing with real source: '{config.source}'")
    ipc_params = {"name_prefix": "test_capture_direct", "height": 1080, "width": 1920, "channels": 4}
    
    ipc_buffer_main = None
    # --- 不再需要 capture_service_proc 和 relay_process ---
    capture_process = None # 只有一个生产者进程
    consumer_process = None
    stop_event = Event()
    
    try:
        # --- 主进程创建 TripleSharedBuffer ---
        ipc_buffer_main = TripleSharedBuffer(**ipc_params, create=True)

        # --- 启动直连的 Capture 进程和 Consumer 进程 ---
        capture_process = Process(target=run_capture, name="CaptureProcess", args=(config, ipc_params, stop_event))
        consumer_process = Process(target=mock_consumer, name="MockConsumer", args=(ipc_params, stop_event))

        logger.info("Starting Capture and Consumer processes...")
        capture_process.start()
        consumer_process.start()

        run_duration = 10
        logger.info(f"Running for {run_duration} seconds.")
        time.sleep(run_duration)

    except Exception as e:
        logger.critical(f"An error occurred in main process: {e}", exc_info=True)
    finally:
        logger.info("Signaling processes to stop.")
        stop_event.set()
        
        # --- 优雅地关闭进程 ---
        if capture_process: capture_process.join(timeout=3)
        if consumer_process: consumer_process.join(timeout=3)
        
        # Unlink TripleSharedBuffer resources
        if ipc_buffer_main: ipc_buffer_main.close_and_unlink()
        
        # 强制终止
        if capture_process and capture_process.is_alive():
            logger.warning("Capture process did not terminate gracefully. Forcing.")
            capture_process.terminate()
        if consumer_process and consumer_process.is_alive():
            logger.warning("Consumer process did not terminate gracefully. Forcing.")
            consumer_process.terminate()

        logger.info("Main process finished.")