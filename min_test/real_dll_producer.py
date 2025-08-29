# real_dll_producer.py
import ctypes
import mmap
import os
import signal
import time
import logging

from ctypes import wintypes, windll

# --- 和中继进程共享的常量 (确保匹配) ---
SHM_BUFFER_FILENAME = "relay_test_buffer.tmp"
SHM_FLAG_FILENAME = "relay_test_flag.tmp"
INT_SIZE = ctypes.sizeof(ctypes.c_int32)

# --- Capture Service DLL (从之前的测试中复制过来) ---
class CaptureService:
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

stop_producer = False

def handle_signal(signum, frame):
    global stop_producer
    logging.info(f"Received signal {signum}, shutting down.")
    stop_producer = True

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [RealProducer] %(message)s')
    global stop_producer
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    service = None
    f_buffer, m_buffer = None, None
    f_flag, m_flag = None, None
    flag = None

    try:
        BASE_PATH = "C:/software/MuMuPlayer-12.0"
        DLL_PATH = "C:/software/MuMuPlayer-12.0/shell/sdk/external_renderer_ipc.dll"
        service = CaptureService(base_path=BASE_PATH, dll_path=DLL_PATH)
        service.connect()
        buffer_size = service.width.value * service.height.value * 4

        # --- 创建共享内存 ---
        f_buffer = open(SHM_BUFFER_FILENAME, "w+b")
        f_buffer.seek(buffer_size - 1); f_buffer.write(b'\0'); f_buffer.flush()
        m_buffer = mmap.mmap(f_buffer.fileno(), 0)

        f_flag = open(SHM_FLAG_FILENAME, "w+b")
        f_flag.seek(INT_SIZE - 1); f_flag.write(b'\0'); f_flag.flush()
        m_flag = mmap.mmap(f_flag.fileno(), 0)
        
        flag = ctypes.c_int32.from_buffer(m_flag)
        flag.value = 0 # 初始为0，等待中继

        shm_buffer_ctypes = (ctypes.c_ubyte * buffer_size).from_buffer(m_buffer)

        logging.info("Ready and waiting for Relay to start.")
        while not stop_producer:
            if flag.value != 0:
                time.sleep(0.001)
                continue
            
            service.capture_frame_raw()
            ctypes.memmove(shm_buffer_ctypes, service.buffer, buffer_size)
            
            flag.value = 1
    
    except Exception as e:
        logging.critical(f"Error: {e}", exc_info=True)
    finally:
        logging.info("Cleaning up.")
        if flag is not None:
             flag.value = 2 # Signal shutdown to relay
        if service: service.disconnect()
        shm_buffer_ctypes = None
        flag = None
        if m_buffer: m_buffer.close()
        if f_buffer: f_buffer.close()
        if m_flag: m_flag.close()
        if f_flag: f_flag.close()
        try:
            if os.path.exists(SHM_BUFFER_FILENAME): os.remove(SHM_BUFFER_FILENAME)
            if os.path.exists(SHM_FLAG_FILENAME): os.remove(SHM_FLAG_FILENAME)
        except OSError:
            pass
        logging.info("Stopped.")

if __name__ == "__main__":
    main()