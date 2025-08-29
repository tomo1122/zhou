# relay_and_consumer.py
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
BUFFER_SIZE = 1920 * 1080 * 4

def run_relay(tsb_params: dict, stop_event: Event):
    """中继进程：从简单SHM读取，写入TripleSharedBuffer"""
    tsb = None
    f_buffer, m_buffer = None, None
    f_flag, m_flag = None, None

    try:
        # --- 附加到 TripleSharedBuffer ---
        tsb = TripleSharedBuffer(**tsb_params, create=False)
        logger.info("Relay attached to TripleSharedBuffer.")
        
        # --- 等待并附加到简单共享内存 ---
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

        # 循环开始前，发出一次“准备好”信号
        flag.value = 0

        while not stop_event.is_set():
            # --- 作为消费者 ---
            if flag.value != 1: # 等待生产者数据
                time.sleep(0.001)
                continue
            
            # --- 作为生产者 ---
            write_buffer = tsb.get_write_buffer()
            np.copyto(write_buffer, source_np_buffer) # 复制数据
            tsb.done_writing()
            
            # --- 通知生产者可以继续了 ---
            flag.value = 0

    except Exception as e:
        logger.critical(f"Error in relay process: {e}", exc_info=True)
    finally:
        logger.info("Relay shutting down.")
        if m_buffer: m_buffer.close()
        if f_buffer: f_buffer.close()
        if m_flag: m_flag.close()
        if f_flag: f_flag.close()

def run_consumer(tsb_params: dict, stop_event: Event):
    """消费者进程：只从TripleSharedBuffer读取"""
    tsb = None
    try:
        tsb = TripleSharedBuffer(**tsb_params, create=False)
        logger.info("Consumer attached to TripleSharedBuffer.")
        
        frames_received = 0
        last_val = -1
        
        while not stop_event.is_set():
            read_buffer = tsb.get_read_buffer()
            current_val = read_buffer[0, 0, 0] # 读取一个像素值
            
            if current_val != last_val:
                frames_received += 1
                last_val = current_val
            else:
                time.sleep(0.001)
        
        logger.info(f"Consumer finished. Total unique frames received: {frames_received}")

    except Exception as e:
        logger.critical(f"Error in consumer process: {e}", exc_info=True)


if __name__ == "__main__":
    tsb_params = {"name_prefix": "relay_test", "height": 1080, "width": 1920, "channels": 4, "dtype": np.uint8}
    tsb_main = None
    producer_proc = None
    relay_proc = None
    consumer_proc = None
    stop_event = Event()

    try:
        # --- 启动生产者 ---
        producer_script = os.path.join('min_test', 'pure_python_producer.py')
        logger.info("Starting pure python producer...")
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        producer_proc = subprocess.Popen([sys.executable, producer_script], creationflags=creationflags)
        
        # --- 创建TripleSharedBuffer ---
        tsb_main = TripleSharedBuffer(**tsb_params, create=True)
        
        # --- 启动中继和消费者 ---
        relay_proc = Process(target=run_relay, name="Relay", args=(tsb_params, stop_event))
        consumer_proc = Process(target=run_consumer, name="Consumer", args=(tsb_params, stop_event))
        
        logger.info("Starting Relay and Consumer processes...")
        relay_proc.start()
        consumer_proc.start()
        
        run_duration = 10
        logger.info(f"Running test for {run_duration} seconds...")
        time.sleep(run_duration)

    finally:
        logger.info("Stopping all processes.")
        stop_event.set()
        
        if relay_proc: relay_proc.join(timeout=3)
        if consumer_proc: consumer_proc.join(timeout=3)
        
        if producer_proc:
            if sys.platform == "win32":
                producer_proc.send_signal(signal.CTRL_C_EVENT)
            else:
                producer_proc.terminate()
            producer_proc.wait(timeout=3)
            
        if tsb_main: tsb_main.close_and_unlink()

        if relay_proc and relay_proc.is_alive(): relay_proc.terminate()
        if consumer_proc and consumer_proc.is_alive(): consumer_proc.terminate()

        logger.info("Test finished.")