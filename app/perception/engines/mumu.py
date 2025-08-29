import logging

from ctypes import WinDLL, c_int, c_wchar_p, POINTER, c_ubyte, byref

from app.core.config import MumuConfig
from app.perception.engines.base import BaseCaptureEngine


logger = logging.getLogger(__name__)


class MumuCaptureEngine(BaseCaptureEngine):
    """
    依赖DLL进行截图的Engine
    """
    def __init__(self, config: MumuConfig):
        super().__init__(config)

        self.dll = None
        self.handle = None
        self.buffer = None
        self.dll_path = config.mumu_dll_path
        self.mumu_base_path = self.config.mumu_base_path
        self.mumu_instance_index = self.config.mumu_instance_index
        
        self._width = c_int()
        self._height = c_int()

    def start(self):
        logger.info(f'模拟器地址: {self.mumu_base_path}')
        logger.info(f'开始连接到模拟器实例 {self.mumu_instance_index}')

        # 设置 DLL 函数原型
        self.dll = WinDLL(self.dll_path)
        self.dll.nemu_connect.argtypes = [c_wchar_p, c_int]
        self.dll.nemu_connect.restype = c_int
        self.dll.nemu_disconnect.argtypes = [c_int]
        self.dll.nemu_capture_display.argtypes = [c_int, c_int, c_int, POINTER(c_int), POINTER(c_int), POINTER(c_ubyte)]
        self.dll.nemu_capture_display.restype = c_int

        # 连接到模拟器
        self.handle = self.dll.nemu_connect(self.mumu_base_path, self.mumu_instance_index)
        if self.handle == 0:
            raise ConnectionError("连接模拟器失败: 1. 检查配置 2.检查模拟器是否已启动")
        
        # 获取分辨率
        result = self.dll.nemu_capture_display(self.handle, 0, 0, byref(self._width), byref(self._height), None)
        if result != 0:
            self.stop()
            raise RuntimeError(f"获取模拟器分辨率失败, 错误码: {result}")
        logger.info(f"成功连接到模拟器, 当前分辨率: {self.width}x{self.height}")

    def stop(self):
        """
        断开模拟器连接
        """
        if self.handle and self.dll:
            self.dll.nemu_disconnect(self.handle)
            self.handle = None
        logger.info("断开模拟器连接")


    def capture_frame_into_buffer(self, dest_buffer):
        """
        将捕获到的一帧数据放入内存缓冲区中
        避免重复分配内存地址和数据的拷贝
        """
        if not self.handle:
            raise ConnectionError("未连接到模拟器")
        
        buffer_size = len(dest_buffer)
        result = self.dll.nemu_capture_display(self.handle, 0, buffer_size, byref(self._width), byref(self._height), dest_buffer)
        
        # result == 0 成功
        return result

    @property
    def width(self) -> int:
        return self._width.value

    @property
    def height(self) -> int:
        return self._height.value