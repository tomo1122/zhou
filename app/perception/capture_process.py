import logging
import time
import ctypes
import numpy as np
from multiprocessing import Event

from app.core.config import MergedConfig
from app.core.ipc.triple_shared_buffer import TripleSharedBuffer
# 引入 MumuCaptureEngine，代替 Mock engine
from app.perception.engines.mumu import MumuCaptureEngine 
from app.perception.engines.mock import MockCaptureEngine

logger = logging.getLogger(__name__)

# 不再需要简单SHM的常量了

# 这个函数是新的核心，直接截图并写入 TripleSharedBuffer
def run_capture_direct_to_tsb(config: MergedConfig, ipc_params: dict, stop_event: Event):
    """
    真正的生产者进程：直接从DLL截图并写入高性能的TripleSharedBuffer。
    """
    logger.info("Direct Capture-to-TSB process started.")
    ipc_buffer = None
    engine = None
    try:
        # 1. 附加到由主进程创建的 TripleSharedBuffer
        ipc_buffer = TripleSharedBuffer(**ipc_params, create=False)
        
        # 2. 初始化截图引擎
        # 注意：这里我们直接使用 MumuCaptureEngine
        engine = MumuCaptureEngine(config)
        engine.start()

        # 获取一个临时的 ctypes 缓冲区，用于接收 DLL 的原始数据
        # 这样可以避免直接让 DLL 写入共享内存，更安全
        buffer_size = engine.width * engine.height * 4
        temp_ctypes_buffer = (ctypes.c_ubyte * buffer_size)()

        logger.info("Engine started. Starting capture loop...")
        while not stop_event.is_set():
            # a. DLL 截图到临时缓冲区
            result = engine.capture_frame_into_buffer(temp_ctypes_buffer)
            if result != 0:
                logger.error(f"Capture failed with code {result}, stopping.")
                break
            
            # b. 从临时缓冲区拷贝到 TripleSharedBuffer 的写入区
            write_buffer = ipc_buffer.get_write_buffer()
            # 使用 ctypes.memmove 进行高效拷贝
            ctypes.memmove(
                write_buffer.ctypes.data, 
                temp_ctypes_buffer,
                buffer_size
            )
            
            # c. 通知消费者新的一帧已就绪
            ipc_buffer.done_writing()
            
    except Exception as e:
        logger.critical(f"An unhandled exception in capture process: {e}", exc_info=True)
    finally:
        if engine: engine.stop()
        logger.info("Capture process shutting down.")


# 保留 run_capture_mock 用于测试
def run_capture_mock(config: MergedConfig, ipc_params: dict, stop_event: Event):
    """ The original simple implementation for the mock engine. """
    # 这个函数可以保持不变，因为它已经是直连模式了
    logger.info("Running in mock mode.")
    ipc_buffer = None
    engine = None
    try:
        ipc_buffer = TripleSharedBuffer(**ipc_params, create=False)
        engine = MockCaptureEngine(width=ipc_params['width'], height=ipc_params['height'])
        engine.start()
        
        while not stop_event.is_set():
            frame = engine.capture_frame()
            if frame is None: continue
            
            write_buffer = ipc_buffer.get_write_buffer()
            np.copyto(write_buffer, frame)
            ipc_buffer.done_writing()
            
    except Exception as e:
        logger.critical(f"An unhandled exception in mock capture process: {e}", exc_info=True)
    finally:
        if engine: engine.stop()
        logger.info("Mock capture process shutting down.")


def run_capture(config: MergedConfig, ipc_params: dict, stop_event: Event):
    """
    主入口：根据配置决定是运行真实截图还是模拟截图。
    """
    if config.source == 'mumu':
        # 调用我们新的直连函数
        run_capture_direct_to_tsb(config, ipc_params, stop_event)
    elif config.source == 'mock':
        run_capture_mock(config, ipc_params, stop_event)
    else:
        raise ValueError(f"Unsupported capture source: {config.source}")