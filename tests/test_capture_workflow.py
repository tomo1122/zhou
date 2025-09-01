import time
import logging

import pytest
import numpy as np

from multiprocessing import Process, Event, Queue

from app.core.config import get_config
from app.core.ipc.triple_shared_buffer import TripleSharedBuffer
from app.perception.capture_process import run_capture_process
from app.perception.engines.mumu import MumuCaptureEngine
from tests.conftest import check_connection_task, verifying_consumer_task, performance_consumer_task


logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] [%(processName)s] %(message)s')
logger = logging.getLogger(__name__)


@pytest.fixture(scope="module") 
def test_config():
    """提供一个在整个测试模块中共享的配置对象。"""
    return get_config()


@pytest.fixture(scope="module")
def ipc_params(test_config):
    """提供 IPC 缓冲区的标准参数。"""
    engine = MumuCaptureEngine(test_config)
    engine.start()
    height, width = engine.height, engine.width
    engine.stop()
    logger.info(f"成功从模拟器获取到分辨率: {width}x{height}")
        
    return {"height": height, "width": width, "channels": 4}


@pytest.fixture
def unique_ipc_params(ipc_params):
    """为每个测试创建一个带有唯一 name_prefix 的参数字典。"""
    params = ipc_params.copy()
    params["name_prefix"] = f"pytest_ipc_{time.time_ns()}"
    return params


@pytest.fixture
def triple_buffer(unique_ipc_params):
    """一个核心的 fixture，负责在每个测试前后创建和销毁 TripleSharedBuffer。"""
    buffer = None
    try:
        logger.debug(f"Fixture 'triple_buffer': 正在创建 IPC 缓冲区 (prefix: {unique_ipc_params['name_prefix']}).")
        buffer = TripleSharedBuffer(**unique_ipc_params, create=True)
        buffer.creation_params = unique_ipc_params # 附加参数以方便访问
        yield buffer
    finally:
        if buffer:
            logger.debug(f"Fixture 'triple_buffer': 正在关闭和清理 IPC 缓冲区 (prefix: {buffer.creation_params['name_prefix']}).")
            buffer.close_and_unlink()


def test_ipc_buffer_creation_and_connection(triple_buffer):
    """测试项 1: 验证 TripleSharedBuffer 能否被主进程创建并被子进程成功连接。"""
    assert triple_buffer is not None, "主进程创建 TripleSharedBuffer 失败"
    
    p = Process(target=check_connection_task, args=(triple_buffer.creation_params,))
    p.start()
    p.join(timeout=5)
    
    assert p.exitcode == 0, "子进程连接到 TripleSharedBuffer 失败"
    logger.info("测试 'test_ipc_buffer_creation_and_connection' 通过。")


def test_producer_writes_to_buffer(test_config, triple_buffer):
    """测试项 2: 验证真实的生产者进程是否能成功向共享缓冲区写入数据。"""
    stop_event = Event()
    
    initial_data = np.copy(triple_buffer.get_read_buffer())
    assert np.all(initial_data == 0), "缓冲区初始状态不为零"

    producer_proc = Process(
        target=run_capture_process, 
        name="TestProducer", 
        args=(MumuCaptureEngine, test_config, triple_buffer.creation_params, stop_event)
    )
    producer_proc.start()
    
    time.sleep(2)
    stop_event.set()
    producer_proc.join(timeout=5)
    assert producer_proc.exitcode == 0, f"生产者进程未能正常退出 (exitcode: {producer_proc.exitcode})"

    final_data = np.copy(triple_buffer.get_read_buffer())
    assert np.any(final_data != 0), "生产者运行后，缓冲区内容未被修改"
    logger.info("测试 'test_producer_writes_to_buffer' 通过。")


def test_consumer_reads_from_buffer(triple_buffer):
    """测试项 3: 主进程先手动写入数据，然后验证消费者子进程能否成功读取到。"""
    EXPECTED_VALUE = 123
    write_buffer = triple_buffer.get_write_buffer()
    write_buffer.fill(EXPECTED_VALUE)
    triple_buffer.done_writing()

    stop_event = Event()
    consumer_proc = Process(
        target=verifying_consumer_task, 
        name="TestConsumer", 
        args=(triple_buffer.creation_params, stop_event, EXPECTED_VALUE)
    )
    consumer_proc.start()
    consumer_proc.join(timeout=5)
    stop_event.set()
    
    assert consumer_proc.exitcode == 0, "验证消费者进程未能成功读取数据"
    logger.info("测试 'test_consumer_reads_from_buffer' 通过。")


@pytest.mark.performance
def test_end_to_end_workflow_performance(test_config, triple_buffer):
    """测试项 4: 综合测试，同时运行真实生产者和测试消费者，验证整个工作流的性能。"""
    stop_event = Event()
    result_queue = Queue()
    params = triple_buffer.creation_params
    
    expected_min_fps = test_config.fps * 0.90
    test_duration = test_config.perf_test_duration

    producer_proc = Process(
        target=run_capture_process, 
        name="PerfProducer", 
        args=(MumuCaptureEngine, test_config, params, stop_event)
    )
    consumer_proc = Process(
        target=performance_consumer_task, 
        name="PerfConsumer", 
        args=(params, stop_event, result_queue)
    )
    
    producer_proc.start()
    consumer_proc.start()
    
    logger.info(f"性能测试运行中... 持续 {test_duration} 秒。")
    time.sleep(test_duration)
    
    stop_event.set()
    producer_proc.join(timeout=5)
    consumer_proc.join(timeout=5)
    
    assert producer_proc.exitcode == 0, f"生产者进程在性能测试中未能正常退出 (exitcode: {producer_proc.exitcode})"
    assert consumer_proc.exitcode == 0, f"消费者进程在性能测试中未能正常退出 (exitcode: {consumer_proc.exitcode})"

    results = result_queue.get(timeout=2)
    assert results is not None, "未能从消费者获取性能测试结果"
    
    actual_fps = results['fps']
    
    print("\n--- 端到端性能测试结果 ---")
    print(f"目标 FPS (来自配置): {test_config.fps}")
    print(f"要求最低 FPS (90%): {expected_min_fps:.2f}")
    print(f"测试持续时间: {results['duration']:.2f} 秒")
    print(f"消费者接收帧数: {results['frames']}")
    print(f"消费者实际平均 FPS: {actual_fps:.2f}")
    print("----------------------------")
    
    assert actual_fps >= expected_min_fps, \
        f"端到端性能未达标。实际 FPS ({actual_fps:.2f}) 低于要求的最低 FPS ({expected_min_fps:.2f})。"
    logger.info("测试 'test_end_to_end_workflow_performance' 通过。")