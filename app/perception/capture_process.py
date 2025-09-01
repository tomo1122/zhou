import ctypes
import signal
import logging

from typing import Type
from multiprocessing.synchronize import Event as SyncEvent 

from app.core.config import MergedConfig
from app.core.ipc.triple_shared_buffer import TripleSharedBuffer
from app.perception.engines.base import BaseCaptureEngine 


logger = logging.getLogger(__name__)


def run_capture_process(engine_class: Type[BaseCaptureEngine], config: MergedConfig, ipc_params: dict, stop_event: SyncEvent ):
    """
    一个通用的生产者进程函数。

    它接收一个引擎类作为参数，负责实例化该引擎，并运行一个高效的
    截图 -> 写入共享内存的循环。

    Args:
        engine_class (Type[BaseCaptureEngine]): 要实例化的截图引擎类 
        config (MergedConfig): 应用程序配置。
        ipc_params (dict): TripleSharedBuffer 的参数。
        stop_event (Event): 用于停止进程的事件。
    """
    # 忽略 Ctrl + C 信号
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    
    logger.info(f"捕获进程已启动，使用引擎: {engine_class.__name__}")
    ipc_buffer = None
    engine = None
    try:
        ipc_buffer = TripleSharedBuffer(**ipc_params, create=False)
        engine = engine_class(config)
        engine.start()

        buffer_size = engine.width * engine.height * 4
        temp_ctypes_buffer = (ctypes.c_ubyte * buffer_size)()

        logger.info("引擎已启动，开始捕获循环...")
        while not stop_event.is_set():
            result = engine.capture_frame_into_buffer(temp_ctypes_buffer)

            # 从临时缓冲区高效拷贝到 TripleSharedBuffer 的写入区
            write_buffer = ipc_buffer.get_write_buffer()
            ctypes.memmove(
                write_buffer.ctypes.data, 
                temp_ctypes_buffer,
                buffer_size
            )
            
            ipc_buffer.done_writing()
            
    except Exception as e:
        logger.critical(f"捕获进程中发生未处理的异常: {e}", exc_info=True)
    finally:
        if engine:
            engine.stop()
        if ipc_buffer:
            ipc_buffer.close()
        logger.info("捕获进程已关闭。")