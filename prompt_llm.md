---

#### **第一阶段：环境搭建与项目初始化 (Setup)**

1.  **创建项目结构**:
    *   根据设计文档，创建以下目录结构：`zhou_v2/app`, `zhou_v2/configs`, `zhou_v2/plans`, `zhou_v2/assets`。
    *   在 `app/` 下创建子目录：`perception`, `analysis`, `control`, `core`。
    *   在每个目录和子目录中创建 `__init__.py` 文件，使其成为Python包。
    *   创建 `run.py` 文件在项目根目录。

2.  **设置依赖管理**:
    *   使用命令 `uv add `添加核心依赖：`numpy`, `pillow`, `pyyaml`, `pydantic`。
    *   查看 pyproject.toml 确保依赖安装完成

3.  **创建核心配置系统 (`app/core/config.py`)**:
    *   实现一个配置加载器，能够读取 `configs/` 目录下的所有 `.yaml` 文件并合并它们。
    *   使用 `Pydantic` 为 `settings.yaml` 和 `mumu.yaml` 创建基本的数据模型。
    *   **测试**: 创建一个简单的 `test_config.py`，确保配置能够被正确加载、合并，并且Pydantic校验能正常工作（例如，对一个错误的配置项报错）。

#### **第二阶段：感知层与图像流IPC (Capture & Image IPC)**

1.  **实现 `Capture` 进程**:
    *   将旧项目中的 `CaptureService` (位于 `capture_service.py`) 的核心逻辑迁移到 `app/perception/engines/mumu.py` 中。
    *   创建一个 `app/perception/capture_process.py` 文件，定义 `run_capture` 函数。此函数将负责初始化 `mumu` 引擎，并进入一个主循环。

2.  **实现 `TripleSharedBuffer` (`app/core/ipc/triple_shared_buffer.py`)**:
    *   这是最关键的一步。你需要实现一个类 `TripleSharedBuffer`，它负责：
        *   `__init__`: 根据图像的 `height`, `width`, `channels` 计算大小，并创建三块 `multiprocessing.shared_memory.SharedMemory`。
        *   `get_write_buffer()`: 为生产者（`Capture`）提供一个可写入的 `numpy` 数组视图。
        *   `done_writing()`: 生产者调用此方法，原子地更新内部索引，标记一个缓冲区已写完并可供读取。
        *   `get_read_buffer()`: 为消费者提供一个指向最新完整缓冲区的只读 `numpy` 数组视图。
    *   **关键点**: 索引交换的原子性是核心，可以使用 `multiprocessing.Value` 或 `Array` 来安全地管理索引。

3.  **集成与测试 (最重要的测试环节)**:
    *   **创建测试脚本 `test_capture_ipc.py`**:
        *   这个脚本将扮演 `run.py` 的早期角色。
        *   **主进程**: 初始化 `TripleSharedBuffer`。
        *   **启动 `Capture` 进程**: 将 `TripleSharedBuffer` 对象传递给它。在 `run_capture` 的循环中，捕获图像并将其写入共享缓冲区。
        *   **启动一个 `MockConsumer` 进程**: 这个消费者进程在循环中，不断地从 `TripleSharedBuffer` 读取最新的图像，并可以简单地打印出图像的形状或均值，以验证数据完整性。
        *   **验证**: 运行此测试脚本，观察消费者是否能持续、无误地打印出图像信息。检查内存使用情况，并确保在 `Ctrl+C` 后，共享内存被正确`unlink()`。

#### **第三阶段：分析层 (Ruler) 与索引流IPC (Ruler & Index IPC)**

1.  **实现 `Ruler` 进程逻辑**:
    *   将旧项目 `ruler/utils.py` 和 `core/engine.py` 中与费用条分析相关的逻辑（`find_cost_bar_roi`, `_get_raw_filled_pixel_width`, `get_logical_frame_from_calibration` 等）迁移到 `app/analysis/ruler_process.py` 的一个 `FrameAnalyzer` 类中。
    *   `FrameAnalyzer` 在初始化时，需要加载一个指定的**校准文件** (`.json`)。
    *   创建 `run_ruler` 函数。它接收 `TripleSharedBuffer` 作为输入。在主循环中，它会：
        1.  从共享缓冲区读取最新图像。
        2.  调用 `FrameAnalyzer` 处理图像，使用已加载的校准数据计算出逻辑总帧数。

2.  **实现校准工具 (`app/analysis/calibrator.py`)**:
    *   将旧项目 `ruler/services/calibration_manager.py` 中的 `calibrate` 函数及其辅助逻辑迁移到 `app/analysis/calibrator.py` 中。
    *   创建一个 `run_calibration` 函数，它接收 `TripleSharedBuffer` 作为输入。
    *   该函数会引导用户完成校准过程（例如，提示用户“请在游戏暂停状态下开始”），然后执行校准算法，并将结果保存为 `.json` 文件。

3.  **实现广播事件IPC (`app/core/ipc/broadcast_event.py`)**:
    *   创建一个类 `BroadcastEvent`，它封装 `multiprocessing.Condition` 和 `multiprocessing.Value`。
    *   提供方法：
        *   `set(value)`: 更新共享值并调用 `notify_all()`。
        *   `wait()`: 等待事件通知。
        *   `get_value()`: 获取当前共享值。

4.  **集成与测试 (分两步)**:
    *   **第一步：测试校准模式 (`test_calibration.py` 或通过 `run.py calibrate`)**
        *   在 `run.py` 中添加 `argparse`，以支持 `calibrate` 子命令。
        *   当执行 `python run.py calibrate` 时：
            *   主进程初始化 `TripleSharedBuffer`。
            *   启动 `Capture` 进程。
            *   启动一个**校准进程**，运行 `run_calibration` 函数，并将图像缓冲区传递给它。
        *   **验证**: 在连接模拟器的情况下运行校准命令。程序应能成功生成一个 `.json` 校准文件，并且文件内容看起来是合理的（包含像素映射表等）。

    *   **第二步：测试 `Ruler` 的执行模式 (`test_ruler_ipc.py`)**
        *   这个测试脚本现在需要**前置条件**：必须先有一个校准文件。
        *   **主进程**: 初始化 `TripleSharedBuffer` 和 `BroadcastEvent` (用于帧索引)。
        *   **启动 `Capture` 进程** (同上)。
        *   **启动 `Ruler` 进程**: 将图像缓冲区、帧索引广播事件对象，以及**校准文件的路径**传递给它。`Ruler` 在计算出新帧数后，调用 `broadcast_event.set(frame_index)`。
        *   **启动 `MockCommander` 进程**: (同上一版)。
        *   **验证**: 运行测试脚本。`MockCommander` 应该能正确地打印出由校准数据转换而来的、连续递增的逻辑帧数。


#### **第四阶段：控制层 (Commander)**

1.  **实现 `Commander` 进程**:
    *   将旧项目 `core/orchestrator.py` 和 `maatouch.py` 的逻辑迁移到 `app/control/commander_process.py`。
    *   将 `maatouch.py` 的逻辑封装成一个 `MaaTouchController` 类，可以放在 `app/control/` 下的一个 `drivers` 子目录中。
    *   将旧的 `Orchestrator` 逻辑重构为一个新的 `Commander` 类。它的 `run` 方法将：
        1.  加载作战计划 (从 `plans/` 目录)。
        2.  接收 `BroadcastEvent` 对象。
        3.  在主循环中，等待帧索引更新，并与作战计划比对，当时机成熟时，调用 `MaaTouchController` 执行操作。
        *   **重要**: 将旧的暂停、逐帧逻辑也一并迁移过来。

2.  **集成与测试 (`test_commander.py` 或在 `run.py` 中进行)**:
    *   这是第一次进行接近完整的端到端测试。
    *   创建一个简单的 `battle_plan.yaml`，例如，在第100帧时执行一个打印操作或一个无害的 `maatouch` 点击。
    *   **启动 `run.py`**: 配置 `run.py` 来启动 `Capture`, `Ruler`, 和 `Commander` 三个进程，并正确初始化和传递IPC对象。
    *   **连接模拟器**: 确保MaaTouch服务已在模拟器中准备就绪。
    *   **验证**: 运行 `run.py`。观察日志。当 `Ruler` 计算出的总帧数达到100时，`Commander` 的日志应该显示它触发了动作，并且模拟器上应该能看到对应的点击效果。

#### **第五阶段：状态监控与生命周期管理**

1.  **实现 `StateMonitor` 进程**:
    *   创建 `app/analysis/state_monitor_process.py`。
    *   这里需要你（LLM）根据旧项目代码和新需求，实现一个基于模板匹配（如OpenCV的 `cv2.matchTemplate`）的状态检测器。
    *   它接收 `TripleSharedBuffer` 作为输入，在循环中对图像进行匹配，识别出“胜利”、“失败”等状态。

2.  **实现 `LifecycleManager` 进程**:
    *   创建 `app/control/lifecycle_manager_process.py`。
    *   这个进程的逻辑相对简单，它订阅由 `StateMonitor` 产生的**状态流**（这是第二个 `BroadcastEvent` 实例）。
    *   当接收到 `GameState.FAILED` 时，它会调用 `MaaTouchController` 点击“重试”按钮。

3.  **集成与测试 (`test_lifecycle.py` 或扩展 `run.py`)**:
    *   **主进程**: 初始化两个 `BroadcastEvent`：一个给 `Ruler`，一个给 `StateMonitor`。
    *   **启动所有进程**: `Capture`, `Ruler`, `Commander`, `StateMonitor`, `LifecycleManager`。
    *   **准备测试素材**: 在 `assets/templates/` 中放入“失败”画面的截图作为模板。
    *   **验证**: 手动操作游戏，使其进入失败画面。观察 `StateMonitor` 的日志是否识别出状态变化，以及 `LifecycleManager` 是否执行了重试操作。
