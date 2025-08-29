# relay_and_consumer_final_test.py
import logging
import time
from multiprocessing import Process, Event
import numpy as np
import subprocess
import sys
import os
import mmap
import ctypes
import signal

# 假设 triple_shared_buffer.py 在同一目录
from triple_shared_buffer import TripleSharedBuffer

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] [%(processName)s] %(message)s')
logger = logging.getLogger(__name__)

# 和生产者共享的常量
SHM_BUFFER_FILENAME = "relay_test_buffer.tmp"
SHM_FLAG_FILENAME = "relay_test_flag.tmp"
INT_SIZE = ctypes.sizeof(ctypes.c_int32)

def run_relay(tsb_params: dict, stop_event: Event):
    # 这个函数完全不变
    tsb = None
    f_buffer, m_buffer = None, None
    f_flag, m_flag = None, None
    frames_relayed = 0

    try:
        tsb = TripleSharedBuffer(**tsb_params, create=False)
        logger.info("Relay attached to TripleSharedBuffer.")
        
        logger.info("Relay waiting for simple SHM files...")
        while not (os.path.exists(SHM_BUFFER_FILENAME) and os.path.exists(SHM_FLAG_FILENAME)):
            if stop_event.is_set(): return
            time.sleep(0.1)
        
        f_buffer = open(SHM_BUFFER_FILENAME, "r+b")
        m_buffer = mmap.mmap(f_buffer.fileno(), 0)
        f_flag = open(SHM_FLAG_FILENAME, "r+b")
        m_flag = mmap.mmap(f_flag.fileno(), 0)
        
        flag = ctypes.c_int32.from_buffer(m_flag)
        source_np_buffer = np.ndarray((1080, 1920, 4), dtype=np.uint8, buffer=m_buffer)
        logger.info("Relay attached to simple SHM. Starting loop.")

        flag.value = 0

        while not stop_event.is_set():
            if flag.value == 2: # Shutdown signal from producer
                logger.info("Producer is shutting down. Exiting relay.")
                break

            if flag.value != 1:
                time.sleep(0.001)
                continue
            
            write_buffer = tsb.get_write_buffer()
            np.copyto(write_buffer, source_np_buffer)
            tsb.done_writing()
            frames_relayed += 1
            
            flag.value = 0
    except Exception as e:
        logger.critical(f"Error in relay process: {e}", exc_info=True)
    finally:
        logger.info(f"Relay shutting down. Total frames relayed: {frames_relayed}")
        # ... (cleanup code is the same)
        flag = None
        source_np_buffer = None
        if m_buffer: m_buffer.close()
        if f_buffer: f_buffer.close()
        if m_flag: m_flag.close()
        if f_flag: f_flag.close()


def run_consumer(tsb_params: dict, stop_event: Event):
    # 这个函数也完全不变
    tsb = None
    try:
        tsb = TripleSharedBuffer(**tsb_params, create=False)
        logger.info("Consumer attached to TripleSharedBuffer.")
        
        frames_received = 0
        last_val = -1
        
        while not stop_event.is_set():
            read_buffer = tsb.get_read_buffer()
            # 访问共享内存中的一个值来模拟处理
            _ = read_buffer[0,0,0]
            frames_received += 1 # 这里我们只计总帧数，不去重了
            time.sleep(1/144) # 模拟一个144hz的消费者
        
        duration = 10 # 估算值
        fps = frames_received / duration
        logger.info(f"Consumer finished. Total frames received: {frames_received}, Estimated FPS: {fps:.2f}")

    except Exception as e:
        logger.critical(f"Error in consumer process: {e}", exc_info=True)


if __name__ == "__main__":
    tsb_params = {"name_prefix": "final_test", "height": 1080, "width": 1920, "channels": 4, "dtype": np.uint8}
    tsb_main = None
    producer_proc = None
    relay_proc = None
    consumer_proc = None
    stop_event = Event()

    try:
        # --- 启动 **真实DLL** 生产者 ---
        current_dir = os.path.dirname(os.path.abspath(__file__)) 
        # 构建生产者的绝对路径
        script_path = os.path.join(current_dir, 'real_dll_producer.py')
        logger.info(f"Starting REAL DLL producer: {script_path}")
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        producer_proc = subprocess.Popen([sys.executable, script_path], creationflags=creationflags)
        
        tsb_main = TripleSharedBuffer(**tsb_params, create=True)
        
        # --- 启动中继和（可选的）消费者 ---
        relay_proc = Process(target=run_relay, name="Relay", args=(tsb_params, stop_event))
        # consumer_proc = Process(target=run_consumer, name="Consumer", args=(tsb_params, stop_event))
        
        logger.info("Starting Relay process...")
        relay_proc.start()
        # consumer_proc.start()
        
        run_duration = 10
        logger.info(f"Running test for {run_duration} seconds...")
        
        # 我们可以监控中继进程的状态
        for i in range(run_duration):
            time.sleep(1)
            if not relay_proc.is_alive():
                logger.error("Relay process has died!")
                break
        
    finally:
        logger.info("Stopping all processes.")
        stop_event.set()
        
        if relay_proc: relay_proc.join(timeout=3)
        # if consumer_proc: consumer_proc.join(timeout=3)
        
        if producer_proc:
            if sys.platform == "win32":
                producer_proc.send_signal(signal.CTRL_C_EVENT)
            else:
                producer_proc.terminate()
            producer_proc.wait(timeout=3)
            
        if tsb_main: tsb_main.close_and_unlink()

        if relay_proc and relay_proc.is_alive(): relay_proc.terminate()
        # if consumer_proc and consumer_proc.is_alive(): consumer_proc.terminate()

        logger.info("Test finished.")