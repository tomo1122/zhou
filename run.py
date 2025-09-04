import time
import logging
import argparse
import os
import re
from multiprocessing import Process, Event

from app.core.config import get_config
from app.core.ipc.triple_shared_buffer import TripleSharedBuffer
from app.core.ipc.double_shared_buffer import DoubleSharedBuffer
from app.perception.capture_process import run_capture_process
from app.perception.engines.mumu import MumuCaptureEngine
from app.analysis.ruler_process import run_ruler_process
from app.analysis.calibrator import CalibrationManager, run_calibration
from app.analysis.recorder_process import run_recorder_process
from app.control.commander_process import run_commander_process
from app.control.engine.maatouch_adapter import MaaTouchAdapter
from app.control.engine.mumu_macro_adapter import MumuMacroController


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [%(processName)s] %(message)s' 
)
logger = logging.getLogger(__name__)


def main_run(args):
    """
    执行主运行流程: 初始化IPC, 启动 Capture, Ruler 和 Commander 进程。
    """
    config = get_config()
    logging.getLogger().setLevel(config.log_level.upper())

    image_buffer: TripleSharedBuffer = None
    frame_data_buffer: DoubleSharedBuffer = None
    processes = []
    stop_event = Event()

    try:
        # 1. 初始化 IPC 资源
        logger.info("正在初始化 IPC 资源...")
        temp_engine = MumuCaptureEngine(config)
        temp_engine.start()
        height, width = temp_engine.height, temp_engine.width
        temp_engine.stop()
        logger.info(f"从模拟器获取到分辨率: {width}x{height}")

        image_ipc_params = {
            "name_prefix": f"ark_image_{time.time_ns()}",
            "height": height, "width": width, "channels": 4,
        }
        image_buffer = TripleSharedBuffer(**image_ipc_params, create=True)
        logger.info(f"图像流 IPC (TripleSharedBuffer) 创建成功。")

        frame_ipc_params = {
            "name_prefix": f"ark_frame_{time.time_ns()}",
        }
        frame_data_buffer = DoubleSharedBuffer(**frame_ipc_params, create=True)
        logger.info(f"帧索引流 IPC (DoubleSharedBuffer) 创建成功。")

        # 2. 创建所有子进程
        # 2.1 Capture 进程
        capture_proc = Process(
            target=run_capture_process,
            name="CaptureProcess",
            args=(MumuCaptureEngine, config, image_ipc_params, stop_event),
        )
        processes.append(capture_proc)

        # 2.2 Ruler 进程
        ruler_proc = Process(
            target=run_ruler_process,
            name="RulerProcess",
            args=(config, image_ipc_params, frame_ipc_params, stop_event),
        )
        processes.append(ruler_proc)

        # 2.3 Commander 进程
        plan_name = args.plan
        logger.info(f"即将执行作战计划: '{plan_name}'")

        # 根据配置选择控制器，这里以 MaaTouchAdapter 为例
        controller_kwargs = {}
        commander_proc = Process(
            target=run_commander_process,
            name="CommanderProcess",
            args=(
                config,
                frame_ipc_params,
                plan_name,
                MumuMacroController,
                controller_kwargs,
                stop_event,
            ),
        )
        processes.append(commander_proc)

        # 3. 启动所有进程
        for p in processes:
            p.start()

        logger.info("所有进程已启动。按 Ctrl+C 停止。")
        # 主进程在此阻塞，等待 Commander 进程结束或用户中断
        commander_proc.join() # 等待 commander 完成任务
        logger.info("Commander 进程已结束，开始关闭所有进程。")


    except KeyboardInterrupt:
        logger.info("接收到用户停止信号 (Ctrl+C)...")
    except Exception as e:
        logger.critical(f"主进程发生严重错误: {e}", exc_info=True)
    finally:
        # 4. 停止并清理所有子进程
        logger.info("正在停止所有子进程...")
        stop_event.set()

        for p in processes:
            try:
                p.join(timeout=5)
                if p.is_alive():
                    logger.warning(f"进程 {p.name} 未能在5秒内正常退出，将强制终止。")
                    p.terminate()
            except Exception as e:
                logger.error(f"关闭进程 {p.name} 时出错: {e}")

        logger.info("所有子进程已停止。")

        # 5. 清理 IPC 资源
        if image_buffer:
            logger.info("正在清理图像流 IPC 资源...")
            image_buffer.close_and_unlink()
        if frame_data_buffer:
            logger.info("正在清理帧索引流 IPC 资源...")
            frame_data_buffer.close_and_unlink()

        logger.info("所有资源已清理，程序退出。")


def main_calibrate(args):
    """执行校准流程"""
    config = get_config()
    logging.getLogger().setLevel(config.log_level.upper())
    engine = None
    try:
        logger.info("正在启动截图引擎...")
        engine = MumuCaptureEngine(config)
        engine.start()
        logger.info(f"引擎启动成功，分辨率: {engine.width}x{engine.height}")

        def on_progress(p: float):
            print(f"\r校准进度: [{'#' * int(p / 5)}{' ' * (20 - int(p / 5))}] {p:.1f}%", end='')

        calibration_result = run_calibration(engine, progress_callback=on_progress)
        print("\n校准完成！")

        manager = CalibrationManager(config)
        basename = f"profile_{int(time.time())}"
        saved_path = manager.save(calibration_result, basename)
        logger.info(f"成功保存校准文件: {saved_path}")

        # Automatically update active_calibration_profile in settings.yaml
        calibration_dir = os.path.join(os.path.dirname(__file__), 'calibration')
        
        # Ensure the directory exists before listing
        if not os.path.exists(calibration_dir):
            logger.warning(f"Calibration directory not found: {calibration_dir}")
        else:
            calibration_files = [f for f in os.path.listdir(calibration_dir) if f.startswith('profile_') and f.endswith('.json')]
            
            if calibration_files:
                # Sort by timestamp in filename (e.g., profile_1700000000_...)
                latest_calibration_file = sorted(calibration_files, key=lambda x: int(x.split('_')[1]), reverse=True)[0]
                new_profile_path = f"calibration/{latest_calibration_file}"

                settings_path = os.path.join(os.path.dirname(__file__), 'configs', 'settings.yaml')
                
                try:
                    with open(settings_path, 'r', encoding='utf-8') as f:
                        settings_content = f.read()

                    # Use regex to find and replace the active_calibration_profile line
                    # This regex handles various indentation and comments
                    updated_settings_content = re.sub(
                        r'^(active_calibration_profile:\s*).*

    except Exception as e:
        logger.critical(f"校准过程中发生严重错误: {e}", exc_info=True)
    finally:
        if engine:
            engine.stop()
        logger.info("校准流程结束。")


def main_record(args):
    """
    执行录制流程: 初始化IPC, 启动 Capture, Ruler 和 Recorder 进程。
    """
    config = get_config()
    logging.getLogger().setLevel(config.log_level.upper())

    image_buffer: TripleSharedBuffer = None
    frame_data_buffer: DoubleSharedBuffer = None
    processes = []
    stop_event = Event()

    try:
        # 1. 初始化 IPC 资源
        logger.info("正在初始化 IPC 资源以进行录制...")
        temp_engine = MumuCaptureEngine(config)
        temp_engine.start()
        height, width = temp_engine.height, temp_engine.width
        temp_engine.stop()
        logger.info(f"从模拟器获取到分辨率: {width}x{height}")

        image_ipc_params = {
            "name_prefix": f"ark_image_{time.time_ns()}",
            "height": height, "width": width, "channels": 4,
        }
        image_buffer = TripleSharedBuffer(**image_ipc_params, create=True)

        frame_ipc_params = {
            "name_prefix": f"ark_frame_{time.time_ns()}",
        }
        frame_data_buffer = DoubleSharedBuffer(**frame_ipc_params, create=True)
        logger.info("IPC 资源创建成功。")

        # 2. 创建录制所需的子进程
        # 2.1 Capture 进程 (提供图像)
        capture_proc = Process(
            target=run_capture_process,
            name="CaptureProcess",
            args=(MumuCaptureEngine, config, image_ipc_params, stop_event),
        )
        processes.append(capture_proc)

        # 2.2 Ruler 进程 (提供帧数)
        ruler_proc = Process(
            target=run_ruler_process,
            name="RulerProcess",
            args=(config, image_ipc_params, frame_ipc_params, stop_event),
        )
        processes.append(ruler_proc)

        # 2.3 Recorder 进程 (核心录制逻辑)
        plan_name = args.plan_name
        logger.info(f"即将开始录制，并保存为作战计划: '{plan_name}.yaml'")
        recorder_proc = Process(
            target=run_recorder_process,
            name="RecorderProcess",
            args=(config, frame_ipc_params, plan_name, stop_event),
        )
        processes.append(recorder_proc)

        # 3. 启动所有进程
        for p in processes:
            p.start()

        logger.info("录制进程已启动。请切换到模拟器窗口进行操作。")
        logger.info("按 Ctrl+C 停止录制并保存。")
        
        # 主进程在此阻塞，等待用户中断
        while not stop_event.is_set():
            time.sleep(1)

    except KeyboardInterrupt:
        logger.info("接收到用户停止信号 (Ctrl+C)，正在停止录制...")
    except Exception as e:
        logger.critical(f"录制主进程发生严重错误: {e}", exc_info=True)
    finally:
        # 4. 停止并清理所有子进程
        logger.info("正在停止所有录制相关进程...")
        stop_event.set()

        for p in processes:
            try:
                p.join(timeout=5)
                if p.is_alive():
                    p.terminate()
            except Exception as e:
                logger.error(f"关闭进程 {p.name} 时出错: {e}")

        logger.info("所有子进程已停止。")

        # 5. 清理 IPC 资源
        if image_buffer:
            image_buffer.close_and_unlink()
        if frame_data_buffer:
            frame_data_buffer.close_and_unlink()

        logger.info("录制流程结束，资源已清理。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="高性能明日方舟自动化框架")
    subparsers = parser.add_subparsers(dest="command", required=True, help="可执行的命令")

    # 'run' 命令
    parser_run = subparsers.add_parser("run", help="启动完整的自动化流程")
    parser_run.add_argument(
        "plan",
        type=str,
        help="要执行的作战计划文件名称 (位于 'plans' 目录下，不含.yaml后缀)"
    )
    parser_run.set_defaults(func=main_run)

    # 'calibrate' 命令
    parser_calibrate = subparsers.add_parser("calibrate", help="运行费用条校准程序")
    parser_calibrate.set_defaults(func=main_calibrate)

    # 'record' 命令 (新增)
    parser_record = subparsers.add_parser("record", help="启动录制模式以生成作战计划")
    parser_record.add_argument(
        "plan_name",
        type=str,
        help="要生成的作战计划文件名 (不含.yaml后缀)"
    )
    parser_record.set_defaults(func=main_record)


    args = parser.parse_args()
    args.func(args), 
                        r'\1' + new_profile_path, 
                        settings_content, 
                        flags=re.MULTILINE
                    )

                    if settings_content != updated_settings_content:
                        with open(settings_path, 'w', encoding='utf-8') as f:
                            f.write(updated_settings_content)
                        logger.info(f"Updated active_calibration_profile in {settings_path} to: {new_profile_path}")
                    else:
                        logger.info(f"active_calibration_profile in {settings_path} is already set to: {new_profile_path}")

                except FileNotFoundError:
                    logger.error(f"Settings file not found: {settings_path}")
                except Exception as e:
                    logger.error(f"Error updating settings file: {e}", exc_info=True)
            else:
                logger.info(f"No calibration profiles found in {calibration_dir} to set as active.")

    except Exception as e:
        logger.critical(f"校准过程中发生严重错误: {e}", exc_info=True)
    finally:
        if engine:
            engine.stop()
        logger.info("校准流程结束。")


def main_record(args):
    """
    执行录制流程: 初始化IPC, 启动 Capture, Ruler 和 Recorder 进程。
    """
    config = get_config()
    logging.getLogger().setLevel(config.log_level.upper())

    image_buffer: TripleSharedBuffer = None
    frame_data_buffer: DoubleSharedBuffer = None
    processes = []
    stop_event = Event()

    try:
        # 1. 初始化 IPC 资源
        logger.info("正在初始化 IPC 资源以进行录制...")
        temp_engine = MumuCaptureEngine(config)
        temp_engine.start()
        height, width = temp_engine.height, temp_engine.width
        temp_engine.stop()
        logger.info(f"从模拟器获取到分辨率: {width}x{height}")

        image_ipc_params = {
            "name_prefix": f"ark_image_{time.time_ns()}",
            "height": height, "width": width, "channels": 4,
        }
        image_buffer = TripleSharedBuffer(**image_ipc_params, create=True)

        frame_ipc_params = {
            "name_prefix": f"ark_frame_{time.time_ns()}",
        }
        frame_data_buffer = DoubleSharedBuffer(**frame_ipc_params, create=True)
        logger.info("IPC 资源创建成功。")

        # 2. 创建录制所需的子进程
        # 2.1 Capture 进程 (提供图像)
        capture_proc = Process(
            target=run_capture_process,
            name="CaptureProcess",
            args=(MumuCaptureEngine, config, image_ipc_params, stop_event),
        )
        processes.append(capture_proc)

        # 2.2 Ruler 进程 (提供帧数)
        ruler_proc = Process(
            target=run_ruler_process,
            name="RulerProcess",
            args=(config, image_ipc_params, frame_ipc_params, stop_event),
        )
        processes.append(ruler_proc)

        # 2.3 Recorder 进程 (核心录制逻辑)
        plan_name = args.plan_name
        logger.info(f"即将开始录制，并保存为作战计划: '{plan_name}.yaml'")
        recorder_proc = Process(
            target=run_recorder_process,
            name="RecorderProcess",
            args=(config, frame_ipc_params, plan_name, stop_event),
        )
        processes.append(recorder_proc)

        # 3. 启动所有进程
        for p in processes:
            p.start()

        logger.info("录制进程已启动。请切换到模拟器窗口进行操作。")
        logger.info("按 Ctrl+C 停止录制并保存。")
        
        # 主进程在此阻塞，等待用户中断
        while not stop_event.is_set():
            time.sleep(1)

    except KeyboardInterrupt:
        logger.info("接收到用户停止信号 (Ctrl+C)，正在停止录制...")
    except Exception as e:
        logger.critical(f"录制主进程发生严重错误: {e}", exc_info=True)
    finally:
        # 4. 停止并清理所有子进程
        logger.info("正在停止所有录制相关进程...")
        stop_event.set()

        for p in processes:
            try:
                p.join(timeout=5)
                if p.is_alive():
                    p.terminate()
            except Exception as e:
                logger.error(f"关闭进程 {p.name} 时出错: {e}")

        logger.info("所有子进程已停止。")

        # 5. 清理 IPC 资源
        if image_buffer:
            image_buffer.close_and_unlink()
        if frame_data_buffer:
            frame_data_buffer.close_and_unlink()

        logger.info("录制流程结束，资源已清理。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="高性能明日方舟自动化框架")
    subparsers = parser.add_subparsers(dest="command", required=True, help="可执行的命令")

    # 'run' 命令
    parser_run = subparsers.add_parser("run", help="启动完整的自动化流程")
    parser_run.add_argument(
        "plan",
        type=str,
        help="要执行的作战计划文件名称 (位于 'plans' 目录下，不含.yaml后缀)"
    )
    parser_run.set_defaults(func=main_run)

    # 'calibrate' 命令
    parser_calibrate = subparsers.add_parser("calibrate", help="运行费用条校准程序")
    parser_calibrate.set_defaults(func=main_calibrate)

    # 'record' 命令 (新增)
    parser_record = subparsers.add_parser("record", help="启动录制模式以生成作战计划")
    parser_record.add_argument(
        "plan_name",
        type=str,
        help="要生成的作战计划文件名 (不含.yaml后缀)"
    )
    parser_record.set_defaults(func=main_record)


    args = parser.parse_args()
    args.func(args)