# consumer_with_shm.py (使用 flag.value 同步的版本)
import logging
import ctypes
from ctypes import wintypes, windll
import numpy as np
import signal
import time
from multiprocessing import Process, Event
import sys
from pathlib import Path

# --- Windows API Definitions for IPC ---
INVALID_HANDLE_VALUE = -1; PAGE_READWRITE = 0x04; EVENT_ALL_ACCESS = 0x1F0003; INFINITE = 0xFFFFFFFF
FILE_MAP_ALL_ACCESS = 0xF001F
kernel32 = windll.kernel32
CreateFileMappingW = kernel32.CreateFileMappingW; CreateFileMappingW.restype=wintypes.HANDLE; CreateFileMappingW.argtypes=[wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR]
# CreateEventW and related functions are no longer needed
MapViewOfFile = kernel32.MapViewOfFile; MapViewOfFile.restype=wintypes.LPVOID; MapViewOfFile.argtypes=[wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
UnmapViewOfFile = kernel32.UnmapViewOfFile; UnmapViewOfFile.argtypes=[wintypes.LPCVOID]
CloseHandle = kernel32.CloseHandle; CloseHandle.argtypes=[wintypes.HANDLE]
OpenFileMappingW = kernel32.OpenFileMappingW; OpenFileMappingW.restype=wintypes.HANDLE; OpenFileMappingW.argtypes=[wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]

# --- Shared IPC Constants ---
SHM_BUFFER_NAME = "my_app_shm_buffer_flag_test"
SHM_FLAG_NAME = "my_app_shm_flag_test" # New SHM for the flag
INT_SIZE = ctypes.sizeof(ctypes.c_int32)
# Flag values: 0=Consumer's turn, 1=Producer's turn

# --- Capture Service Logic ---
class CaptureService:
    # (This class is identical to the previous version)
    def __init__(self, base_path: str, dll_path: str, instance_index: int = 0):
        self.emu_path = base_path; self.dll_path = dll_path; self.instance_index = instance_index
        self.handle = None; self.width = ctypes.c_int(); self.height = ctypes.c_int(); self.buffer = None
        self.dll = ctypes.WinDLL(self.dll_path)
        self._setup_functions()
    def _setup_functions(self):
        self.dll.nemu_connect.argtypes = [ctypes.c_wchar_p, ctypes.c_int]; self.dll.nemu_connect.restype = ctypes.c_int
        self.dll.nemu_disconnect.argtypes = [ctypes.c_int]
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

def run_capture_service_with_flag(stop_event: Event):
    """Target function for the capture process, using a flag for sync."""
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [CaptureService-Flag] %(message)s')
    BASE_PATH = "C:/software/MuMuPlayer-12.0"
    DLL_PATH = "C:/software/MuMuPlayer-12.0/shell/sdk/external_renderer_ipc.dll"
    service, handles, pointers = None, [], []
    try:
        service = CaptureService(base_path=BASE_PATH, dll_path=DLL_PATH)
        service.connect()
        buffer_size = service.width.value * service.height.value * 4
        
        # Attach to the two shared memory objects
        h_buffer = OpenFileMappingW(FILE_MAP_ALL_ACCESS, False, SHM_BUFFER_NAME)
        p_buffer = MapViewOfFile(h_buffer, FILE_MAP_ALL_ACCESS, 0, 0, buffer_size)
        h_flag = OpenFileMappingW(FILE_MAP_ALL_ACCESS, False, SHM_FLAG_NAME)
        p_flag = MapViewOfFile(h_flag, FILE_MAP_ALL_ACCESS, 0, 0, INT_SIZE)
        handles.extend([h_buffer, h_flag])
        pointers.extend([p_buffer, p_flag])
        if not all(handles) or not all(pointers): raise RuntimeError("附加到内核对象失败。")
        
        shm_buffer = (ctypes.c_ubyte * buffer_size).from_address(p_buffer)
        flag = ctypes.c_int32.from_address(p_flag)
        logging.info("附加成功，开始截图循环。")

        while not stop_event.is_set():
            # Wait for consumer to be ready (flag == 0)
            if flag.value != 0:
                time.sleep(0.001) # Busy-wait with a small sleep
                continue

            service.capture_frame_raw()
            ctypes.memmove(shm_buffer, service.buffer, buffer_size)
            
            # Signal that capture is done (flag = 1)
            flag.value = 1

    except Exception as e:
        logging.critical(f"截图循环中发生错误: {e}", exc_info=True)
    finally:
        logging.info("截图服务正在清理资源...")
        if service: service.disconnect()
        for p in pointers: UnmapViewOfFile(p)
        for h in handles: CloseHandle(h)

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [MainConsumer-Flag] %(message)s')
    
    handles, pointers = [], []
    capture_proc = None
    stop_event = Event()
    
    try:
        logging.info("正在创建共享内存...")
        buffer_size = 1920 * 1080 * 4
        # Create buffer SHM
        h_buffer = CreateFileMappingW(INVALID_HANDLE_VALUE, None, PAGE_READWRITE, 0, buffer_size, SHM_BUFFER_NAME)
        p_buffer = MapViewOfFile(h_buffer, FILE_MAP_ALL_ACCESS, 0, 0, buffer_size)
        # Create flag SHM
        h_flag = CreateFileMappingW(INVALID_HANDLE_VALUE, None, PAGE_READWRITE, 0, INT_SIZE, SHM_FLAG_NAME)
        p_flag = MapViewOfFile(h_flag, FILE_MAP_ALL_ACCESS, 0, 0, INT_SIZE)
        
        handles.extend([h_buffer, h_flag])
        pointers.extend([p_buffer, p_flag])
        if not all(handles) or not all(pointers): raise RuntimeError("创建内核对象失败。")
        
        # Initialize the flag
        flag = ctypes.c_int32.from_address(p_flag)
        flag.value = 0 # 0 means consumer is ready
        logging.info("内核对象创建成功。")

        # --- 使用 multiprocessing.Process 启动截图服务 ---
        capture_proc = Process(target=run_capture_service_with_flag, args=(stop_event,))
        capture_proc.start()
        logging.info(f"截图服务已启动 (PID: {capture_proc.pid})。")

        # 主进程作为消费者进入循环
        numpy_view = np.frombuffer((ctypes.c_ubyte * buffer_size).from_address(p_buffer), dtype=np.uint8)
        
        frames_received = 0
        start_time = time.perf_counter()
        run_duration = 10
        logging.info(f"开始运行 {run_duration} 秒...")

        while time.perf_counter() - start_time < run_duration:
            # Wait for producer to finish (flag == 1)
            if flag.value != 1:
                time.sleep(0.001) # Busy-wait
                continue
            
            _ = numpy_view[0]
            frames_received += 1
            
            # Signal that we are ready for the next frame (flag = 0)
            flag.value = 0
        
        end_time = time.perf_counter()
        duration = end_time - start_time
        fps = frames_received / (duration + 1e-9)
        print(f"\n--- Flag Sync 对比实验结果 ---\n    FPS: {fps:.2f}\n------------------------------\n")
        
    finally:
        logging.info("正在停止截图服务...")
        stop_event.set()
        if capture_proc:
            capture_proc.join(timeout=5)
            if capture_proc.is_alive():
                logging.warning("截图服务未能优雅退出，强制终止。")
                capture_proc.terminate()
        
        logging.info("正在清理内核对象...")
        for p in pointers: UnmapViewOfFile(p)
        for h in handles: CloseHandle(h)
        logging.info("主进程退出。")

if __name__ == '__main__':
    main()