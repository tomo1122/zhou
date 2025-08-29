# test_step3_full_logic.py (修正版)
import logging
import ctypes
import numpy as np
import time
from multiprocessing import Process, Event
import sys
import os
import mmap

# --- Capture Service DLL (保持不变) ---
from ctypes import wintypes, windll
class CaptureService:
    # (这个类不需要改变)
    def __init__(self, base_path: str, dll_path: str, instance_index: int = 0):
        self.emu_path = base_path; self.dll_path = dll_path; self.instance_index = instance_index
        self.handle = None; self.width = ctypes.c_int(); self.height = ctypes.c_int(); self.buffer = None
        self.dll = ctypes.WinDLL(self.dll_path)
        self._setup_functions()
    def _setup_functions(self):
        self.dll.nemu_connect.argtypes = [ctypes.c_wchar_p, ctypes.c_int]; self.dll.nemu_connect.restype = ctypes.c_int
        self.dll.nemu_disconnect.argtypes = [ctypes.c_int]
        # --- 修正点在这里 ---
        self.dll.nemu_capture_display.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_ubyte)]; self.dll.nemu_capture_display.restype = ctypes.c_int
    def connect(self):
        self.handle = self.dll.nemu_connect(self.emu_path, self.instance_index)
        if self.handle == 0: raise ConnectionError("模拟器连接失败。")
        result = self.dll.nemu_capture_display(self.handle, 0, 0, ctypes.byref(self.width), ctypes.byref(self.height), None)
        if result != 0: self.disconnect(); raise RuntimeError(f"获取分辨率失败, 错误代码: {result}")
        buffer_size = self.width.value * self.height.value * 4
        self.buffer = (ctypes.c_ubyte * buffer_size)()
        logging.info(f"连接成功，分辨率: {self.width.value}x{self.height.value}")
    def disconnect(self):
        if self.handle: self.dll.nemu_disconnect(self.handle); self.handle = None
    def capture_frame_raw(self):
        result = self.dll.nemu_capture_display(self.handle, 0, len(self.buffer), ctypes.byref(self.width), ctypes.byref(self.height), self.buffer)
        if result != 0: raise RuntimeError(f"截图失败, 错误码: {result}")

# --- Shared Constants ---
SHM_INDEX_FILENAME = "my_app_shm_index_mmap.tmp"
SHM_BUFFER_FILENAMES = [f"my_app_shm_buffer_mmap_{i}.tmp" for i in range(3)]
INT_SIZE = np.dtype(np.int32).itemsize

def run_capture_service_with_triple_buffer_logic(stop_event: Event):
    """Target function, 实现完整的三缓冲区生产者逻辑。"""
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [CaptureService-TripleLogic] %(message)s')
    BASE_PATH = "C:/software/MuMuPlayer-12.0"
    DLL_PATH = "C:/software/MuMuPlayer-12.0/shell/sdk/external_renderer_ipc.dll"
    service = None
    
    files, mmaps = [], []
    
    try:
        service = CaptureService(base_path=BASE_PATH, dll_path=DLL_PATH)
        service.connect()
        buffer_size = service.width.value * service.height.value * 4
        
        # --- 附加到所有 mmap 文件 ---
        f_idx = open(SHM_INDEX_FILENAME, "r+b")
        m_idx = mmap.mmap(f_idx.fileno(), 0, access=mmap.ACCESS_WRITE)
        files.append(f_idx); mmaps.append(m_idx)
        
        shm_buffers_ctypes = []
        for fname in SHM_BUFFER_FILENAMES:
            f = open(fname, "r+b")
            m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_WRITE)
            files.append(f); mmaps.append(m)
            shm_buffers_ctypes.append((ctypes.c_ubyte * buffer_size).from_buffer(m))

        np_latest_idx = np.ndarray((1,), dtype=np.int32, buffer=mmaps[0])
        
        # --- 生产者状态 (Producer-side State) ---
        producer_write_idx = 0
        producer_free_idx = 1
        
        logging.info("附加到 mmap 文件成功，开始三缓冲截图循环。")

        while not stop_event.is_set():
            # 1. 获取写入缓冲区
            write_buffer_ctypes = shm_buffers_ctypes[producer_write_idx]
            
            # 2. 截图并写入
            service.capture_frame_raw()
            ctypes.memmove(write_buffer_ctypes, service.buffer, buffer_size)
            
            # 3. 执行 done_writing 逻辑
            new_latest = producer_write_idx
            new_write = producer_free_idx
            
            new_free = int(np_latest_idx[0])
            
            np_latest_idx[0] = new_latest

            producer_write_idx = new_write
            producer_free_idx = new_free

    except Exception as e:
        logging.critical(f"截图循环中发生错误: {e}", exc_info=True)
    finally:
        logging.info("截图服务正在清理资源...")
        if service: service.disconnect()
        np_latest_idx = None
        shm_buffers_ctypes = []
        for m in mmaps: m.close()
        for f in files: f.close()


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [MainConsumer-TripleLogic] %(message)s')
    
    files, mmaps = [], []
    capture_proc = None
    stop_event = Event()
    
    try:
        logging.info("正在创建三缓冲 mmap 文件...")
        buffer_size = 1920 * 1080 * 4
        
        f_idx = open(SHM_INDEX_FILENAME, "w+b")
        f_idx.seek(INT_SIZE - 1); f_idx.write(b'\0'); f_idx.flush()
        m_idx = mmap.mmap(f_idx.fileno(), INT_SIZE, access=mmap.ACCESS_WRITE)
        files.append(f_idx); mmaps.append(m_idx)
        
        np_arrays = []
        for fname in SHM_BUFFER_FILENAMES:
            f = open(fname, "w+b")
            f.seek(buffer_size - 1); f.write(b'\0'); f.flush()
            m = mmap.mmap(f.fileno(), buffer_size, access=mmap.ACCESS_WRITE)
            files.append(f); mmaps.append(m)
            np_arrays.append(np.ndarray((1080, 1920, 4), dtype=np.uint8, buffer=m))

        np_latest_idx = np.ndarray((1,), dtype=np.int32, buffer=mmaps[0])
        np_latest_idx[0] = 2
        logging.info("三缓冲 mmap 文件创建成功。")

        capture_proc = Process(target=run_capture_service_with_triple_buffer_logic, args=(stop_event,))
        capture_proc.start()
        logging.info(f"截图服务已启动 (PID: {capture_proc.pid})。")

        frames_received = 0
        last_frame_idx = -1
        
        start_time = time.perf_counter()
        run_duration = 10
        logging.info(f"开始运行 {run_duration} 秒...")

        while time.perf_counter() - start_time < run_duration:
            current_idx = np_latest_idx[0]
            if current_idx != last_frame_idx:
                read_buffer = np_arrays[current_idx]
                _ = read_buffer[0, 0, 0]
                frames_received += 1
                last_frame_idx = current_idx
            else:
                time.sleep(0.001)

        end_time = time.perf_counter()
        duration = end_time - start_time
        fps = frames_received / (duration + 1e-9)
        print(f"\n--- Triple-Buffer Logic 对比实验结果 ---\n    FPS: {fps:.2f}\n    Frames: {frames_received}\n------------------------------\n")
        
    finally:
        logging.info("正在停止截图服务...")
        stop_event.set()
        if capture_proc:
            capture_proc.join(timeout=5)
            if capture_proc.is_alive():
                logging.warning("截图服务未能优雅退出，强制终止。")
                capture_proc.terminate()
        
        logging.info("正在清理 mmap 对象和文件...")
        np_latest_idx = None
        np_arrays = []
        for m in mmaps: m.close()
        for f in files: f.close()
        
        try:
            if os.path.exists(SHM_INDEX_FILENAME): os.remove(SHM_INDEX_FILENAME)
            for fname in SHM_BUFFER_FILENAMES:
                if os.path.exists(fname): os.remove(fname)
        except OSError as e:
            logging.warning(f"删除临时文件失败: {e}")
            
        logging.info("主进程退出。")

if __name__ == '__main__':
    main()