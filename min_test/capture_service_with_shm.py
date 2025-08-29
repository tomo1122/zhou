# capture_service_with_shm.py
import logging
import ctypes
from ctypes import WinDLL, c_int, c_wchar_p, POINTER, c_ubyte, byref, wintypes, windll
import numpy as np
import signal
import time

logger = logging.getLogger(__name__)

# --- Windows API Definitions for IPC ---
FILE_MAP_ALL_ACCESS = 0xF001F; EVENT_ALL_ACCESS = 0x1F0003; INFINITE = 0xFFFFFFFF
kernel32 = windll.kernel32
OpenFileMappingW = kernel32.OpenFileMappingW; OpenFileMappingW.restype=wintypes.HANDLE; OpenFileMappingW.argtypes=[wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
MapViewOfFile = kernel32.MapViewOfFile; MapViewOfFile.restype=wintypes.LPVOID; MapViewOfFile.argtypes=[wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
OpenEventW = kernel32.OpenEventW; OpenEventW.restype=wintypes.HANDLE; OpenEventW.argtypes=[wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
SetEvent = kernel32.SetEvent; SetEvent.argtypes=[wintypes.HANDLE]
WaitForSingleObject = kernel32.WaitForSingleObject; WaitForSingleObject.argtypes=[wintypes.HANDLE, wintypes.DWORD]
UnmapViewOfFile = kernel32.UnmapViewOfFile; UnmapViewOfFile.argtypes=[wintypes.LPCVOID]
CloseHandle = kernel32.CloseHandle; CloseHandle.argtypes=[wintypes.HANDLE]

# --- Shared Memory Constants ---
SHM_BUFFER_NAME = "my_app_shm_buffer"
EVT_CAPTURE_DONE = "my_app_capture_done_event"
EVT_CONSUMER_READY = "my_app_consumer_ready_event"

stop_service = False
def handle_signal(signum, frame): global stop_service; stop_service = True

class CaptureService:
    def __init__(self, base_path: str, dll_path: str, instance_index: int = 0):
        self.emu_path = base_path
        self.dll_path = dll_path
        self.instance_index = instance_index
        self.handle = None
        self.width = c_int()
        self.height = c_int()
        self.buffer = None
        
        logger.info(f"正在加载MuMu DLL: {self.dll_path}")
        self.dll = WinDLL(self.dll_path)
        self._setup_functions()

    def _setup_functions(self):
        self.dll.nemu_connect.argtypes = [c_wchar_p, c_int]
        self.dll.nemu_connect.restype = c_int
        self.dll.nemu_disconnect.argtypes = [c_int]
        self.dll.nemu_capture_display.argtypes = [c_int, c_int, c_int, POINTER(c_int), POINTER(c_int), POINTER(c_ubyte)]
        self.dll.nemu_capture_display.restype = c_int

    def connect(self):
        logger.info(f"正在连接到MuMu实例 {self.instance_index} (路径: {self.emu_path})")
        self.handle = self.dll.nemu_connect(self.emu_path, self.instance_index)
        if self.handle == 0:
            raise ConnectionError("模拟器连接失败。")
        result = self.dll.nemu_capture_display(self.handle, 0, 0, byref(self.width), byref(self.height), None)
        if result != 0:
            self.disconnect()
            raise RuntimeError(f"获取分辨率失败, 错误代码: {result}")
        buffer_size = self.width.value * self.height.value * 4
        self.buffer = (c_ubyte * buffer_size)()
        logger.info(f"连接成功，分辨率: {self.width.value}x{self.height.value}")
        return self

    def disconnect(self):
        if self.handle:
            self.dll.nemu_disconnect(self.handle)
            self.handle = None
            logger.info("已断开连接。")

    def capture_frame_raw(self):
        """仅截图到内部 ctypes 缓冲区"""
        if not self.handle:
            raise ConnectionError("模拟器未连接")
        buffer_size = len(self.buffer)
        result = self.dll.nemu_capture_display(self.handle, 0, buffer_size, byref(self.width), byref(self.height), self.buffer)
        if result != 0:
            raise RuntimeError(f"截图失败, 错误码: {result}")
        # self.buffer 现在包含了最新的 BGRA 图像数据

    def run_capture_loop(self):
        """运行截图并写入共享内存的主循环"""
        global stop_service
        handles, pointers = [], []
        try:
            # 1. 连接到由主进程创建的内核对象
            logger.info("正在附加到共享内存和事件...")
            buffer_size = self.width.value * self.height.value * 4
            h_buffer = OpenFileMappingW(FILE_MAP_ALL_ACCESS, False, SHM_BUFFER_NAME)
            p_buffer = MapViewOfFile(h_buffer, FILE_MAP_ALL_ACCESS, 0, 0, buffer_size)
            h_evt_capture_done = OpenEventW(EVENT_ALL_ACCESS, False, EVT_CAPTURE_DONE)
            h_evt_consumer_ready = OpenEventW(EVENT_ALL_ACCESS, False, EVT_CONSUMER_READY)
            handles.extend([h_buffer, h_evt_capture_done, h_evt_consumer_ready])
            pointers.append(p_buffer)

            if not all(handles) or not all(pointers):
                raise RuntimeError("附加到内核对象失败。主进程是否已创建它们？")
            
            # 获取共享内存的目标缓冲区
            shm_buffer = (ctypes.c_ubyte * buffer_size).from_address(p_buffer)
            logger.info("附加成功，开始截图循环。")

            # 2. "乒乓同步" 循环
            while not stop_service:
                # 等待消费者准备好接收新的一帧
                WaitForSingleObject(h_evt_consumer_ready, INFINITE)
                if stop_service: break

                # 截图到我们自己的内部缓冲区
                self.capture_frame_raw()
                
                # 将内部缓冲区的数据复制到共享内存
                ctypes.memmove(shm_buffer, self.buffer, buffer_size)

                # 通知消费者，新的一帧已经准备好
                SetEvent(h_evt_capture_done)

        except Exception as e:
            logger.critical(f"截图循环中发生错误: {e}", exc_info=True)
        finally:
            logger.info("截图服务正在清理资源...")
            for p in pointers: UnmapViewOfFile(p)
            for h in handles: CloseHandle(h)

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [CaptureService] %(message)s')
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    
    # 模拟配置
    BASE_PATH = "C:/software/MuMuPlayer-12.0"
    DLL_PATH = "C:/software/MuMuPlayer-12.0/shell/sdk/external_renderer_ipc.dll"
    
    service = None
    try:
        service = CaptureService(base_path=BASE_PATH, dll_path=DLL_PATH)
        service.connect()
        service.run_capture_loop()
    except Exception as e:
        logger.error(f"启动服务失败: {e}")
    finally:
        if service:
            service.disconnect()
        logger.info("截图服务已停止。")