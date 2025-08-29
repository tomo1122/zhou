import numpy as np

from abc import ABC, abstractmethod

from app.core.config import MergedConfig


class BaseCaptureEngine(ABC):
    """截图引擎的抽象基类"""
    def __init__(self, config: MergedConfig):
        self.config = config
    
    @abstractmethod
    def start(self):
        """启动引擎，建立与截图源的连接并获取初始信息（如分辨率）"""
        pass


    @abstractmethod
    def stop(self):
        """停止引擎，断开连接并释放所有资源"""
        pass


    @abstractmethod
    def capture_frame_into_buffer(self, dest_buffer) -> int:
        """高性能的截图方法。将一帧图像直接捕获到调用者提供的 ctypes 缓冲区中。"""
        pass