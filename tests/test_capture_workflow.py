import time
import logging

import pytest
import numpy as np

from pathlib import Path
from multiprocessing import Process, Event, Queue

from app.core.config import get_config
from app.core.ipc.triple_shared_buffer import TripleSharedBuffer
from app.perception.capture_process import run_capture


logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] [%(processName)s] %(message)s')
logger = logging.getLogger(__name__)


@pytest.fixture
def test_config():
    """提供一个用于测试的配置对象。"""
    config = get_config()
    return config


@pytest.fixture
def ipc_params():
    """提供 IPC 缓冲区的标准参数。使用较小分辨率以加快测试速度。"""
    return {"name_prefix": f"pytest_ipc_{time.time_ns()}", "height": 1080, "width": 1920, "channels": 4}


@pytest.fixture
def triple_buffer(ipc_params):
    """一个核心的 fixture，负责在每个测试前后创建和销毁 TripleSharedBuffer。"""
    buffer = None
    try:
        logger.debug(f"Fixture 'triple_buffer': 正在创建 IPC 缓冲区 (prefix: {ipc_params['name_prefix']}).")
        buffer = TripleSharedBuffer(**ipc_params, create=True)
        yield buffer
    finally:
        if buffer:
            logger.debug(f"Fixture 'triple_buffer': 正在关闭和清理 IPC 缓冲区 (prefix: {ipc_params['name_prefix']}).")
            buffer.close_and_unlink()


def test_ipc_buffer_creation_and_connection(triple_buffer):
    """测试项 1: 验证 TripleSharedBuffer 能否被主进程创建并被子进程成功连接。"""
    assert triple_buffer is not None, "主进程创建 TripleSharedBuffer 失败"
    
    # 这是一个需要在子进程中运行的辅助函数
    def _check_connection_task(ipc_params):
        client_buffer = None
        try:
            client_buffer = TripleSharedBuffer(**ipc_params, create=False)
            assert client_buffer is not None, "子进程未能创建 TripleSharedBuffer 实例"
            assert client_buffer.np_arrays is not None, "子进程未能正确附加到 numpy 数组"
            exit(0) # 成功退出
        except Exception as e:
            logger.error(f"子进程连接测试失败: {e}", exc_info=True)
            exit(1) # 失败退出
        finally:
            if client_buffer:
                client_buffer.close()

    # 启动子进程进行连接测试
    p = Process(target=_check_connection_task, args=(triple_buffer.get_params(),))
    p.start()
    p.join(timeout=5)
    
    assert p.exitcode == 0, "子进程连接到 TripleSharedBuffer 失败"
    logger.info("测试 'test_ipc_buffer_creation_and_connection' 通过。")


def test_producer_writes_to_buffer(test_config, triple_buffer):
    """测试项 2: 验证真实的生产者进程(run_capture)是否能成功向共享缓冲区写入数据。"""
    stop_event = Event()
    
    # 检查写入前缓冲区的状态 (应该是全零)
    latest_idx_before = triple_buffer.np_latest_idx[0]
    initial_data = np.copy(triple_buffer.np_arrays[latest_idx_before])
    assert np.all(initial_data == 0), "缓冲区初始状态不为零"

    # 启动生产者进程
    producer_proc = Process(target=run_capture, name="TestProducer", args=(test_config, triple_buffer.get_params(), stop_event))
    producer_proc.start()
    
    # 运行一小段时间，给生产者足够的时间写入至少一帧
    time.sleep(2)
    
    stop_event.set()
    producer_proc.join(timeout=3)
    assert producer_proc.exitcode == 0, f"生产者进程未能正常退出 (exitcode: {producer_proc.exitcode})"

    # 检查写入后缓冲区的内容
    latest_idx_after = triple_buffer.np_latest_idx[0]
    final_data = np.copy(triple_buffer.np_arrays[latest_idx_after])
    assert np.any(final_data != 0), "生产者运行后，缓冲区内容未被修改"
    logger.info("测试 'test_producer_writes_to_buffer' 通过。")


def test_consumer_reads_from_buffer(triple_buffer):
    """测试项 3: 主进程先手动写入数据，然后验证消费者子进程能否成功读取到。"""
    
    # 这是一个专为本测试实现的、简单的消费者任务
    def _verifying_consumer_task(ipc_params, stop_event, expected_value):
        consumer_buffer = None
        try:
            consumer_buffer = TripleSharedBuffer(**ipc_params, create=False)
            # 等待最多3秒，直到读到非零数据
            start_wait = time.time()
            frame_data_is_correct = False
            while time.time() - start_wait < 3:
                read_buffer = consumer_buffer.get_read_buffer()
                if np.all(read_buffer == expected_value):
                    frame_data_is_correct = True
                    break
                time.sleep(0.01)
            
            assert frame_data_is_correct, f"消费者未能读取到期望值 {expected_value}"
            exit(0)
        except Exception as e:
            logger.error(f"验证消费者任务失败: {e}", exc_info=True)
            exit(1)
        finally:
            if consumer_buffer:
                consumer_buffer.close()

    # 1. 主进程手动向缓冲区写入一帧可识别的数据
    EXPECTED_VALUE = 123
    write_buffer = triple_buffer.get_write_buffer()
    write_buffer.fill(EXPECTED_VALUE)
    triple_buffer.done_writing()

    # 2. 启动消费者进程进行验证
    stop_event = Event()
    consumer_proc = Process(target=_verifying_consumer_task, name="TestConsumer", args=(triple_buffer.get_params(), stop_event, EXPECTED_VALUE))
    consumer_proc.start()
    
    consumer_proc.join(timeout=5)
    stop_event.set() # 虽然join后设置意义不大，但保持良好习惯
    
    assert consumer_proc.exitcode == 0, "验证消费者进程未能成功读取数据"
    logger.info("测试 'test_consumer_reads_from_buffer' 通过。")


@pytest.mark.performance
def test_end_to_end_workflow_performance(test_config, triple_buffer):
    """测试项 4: 综合测试，同时运行真实生产者和测试消费者，验证整个工作流的性能。"""

    # 这是一个用于性能测试的消费者，它会通过 Queue 返回结果
    def _performance_consumer_task(ipc_params, stop_event, result_queue):
        consumer_buffer = None
        frames_received = 0
        try:
            consumer_buffer = TripleSharedBuffer(**ipc_params, create=False)
            last_processed_idx = -1
            start_time = time.perf_counter()

            while not stop_event.is_set():
                current_idx = consumer_buffer.np_latest_idx[0]
                if current_idx != last_processed_idx:
                    frames_received += 1
                    last_processed_idx = current_idx
                    # 模拟少量处理
                    _ = consumer_buffer.np_arrays[current_idx][0, 0, 0]
                else:
                    time.sleep(0.0001)
            
            end_time = time.perf_counter()
            duration = end_time - start_time
            fps = frames_received / duration if duration > 0 else 0
            result_queue.put({"frames": frames_received, "duration": duration, "fps": fps})
            exit(0)
        except Exception as e:
            logger.error(f"性能测试消费者任务失败: {e}", exc_info=True)
            result_queue.put(None) # 发送失败信号
            exit(1)
        finally:
            if consumer_buffer:
                consumer_buffer.close()

    stop_event = Event()
    result_queue = Queue()
    params = triple_buffer.get_params()
    
    expected_min_fps = test_config.fps * 0.90 # 允许10%的性能波动
    test_duration = test_config.perf_test_duration

    # 启动生产者和消费者
    producer_proc = Process(target=run_capture, name="PerfProducer", args=(test_config, params, stop_event))
    consumer_proc = Process(target=_performance_consumer_task, name="PerfConsumer", args=(params, stop_event, result_queue))
    
    producer_proc.start()
    consumer_proc.start()
    
    logger.info(f"性能测试运行中... 持续 {test_duration} 秒。")
    time.sleep(test_duration)
    
    # 停止所有进程
    stop_event.set()
    producer_proc.join(timeout=5)
    consumer_proc.join(timeout=5)
    
    assert producer_proc.exitcode == 0, f"生产者进程在性能测试中未能正常退出 (exitcode: {producer_proc.exitcode})"
    assert consumer_proc.exitcode == 0, f"消费者进程在性能测试中未能正常退出 (exitcode: {consumer_proc.exitcode})"

    # 从队列中获取性能测试结果
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