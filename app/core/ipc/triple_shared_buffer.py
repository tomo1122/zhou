import logging

import numpy as np

from multiprocessing import shared_memory


logger = logging.getLogger(__name__)


class TripleSharedBuffer:
    """
    一个基于 `multiprocessing.shared_memory` 实现的高性能、零拷贝、无锁的三缓冲 IPC  模块。
    核心目标是保证数据的新鲜度，并最大限度地减少延迟。

    ### 设计模式

    1.  **创建者/附加者 (Creator/Attacher) 模式**:
        -   **创建者**: 由一个主进程（协调者）在启动时以 `create=True` 模式实例化本类。它负责实际创建操作系统级的共享内存段。
        -   **附加者**: 所有子进程（如生产者、消费者）都以 `create=False` 模式实例化本类，并通过一个共享的 `name_prefix` "地址"来附加到已存在的内存段上。

    2.  **三缓冲机制 (Triple Buffering)**:
        -   系统维护三个独立的缓冲区，确保任何时候读写操作都不会在同一个缓冲区上发生冲突，从而实现无锁读写。
        -   **写入缓冲 (Write Buffer)**: 生产者当前正在写入的区域。
        -   **读取缓冲 (Read Buffer)**: 消费者当前正在读取的、包含最新完整数据的区域。
        -   **空闲缓冲 (Free Buffer)**: 备用区域，一旦生产者写完，它将成为下一个写入缓冲。

    ### 工作流程

    1.  **初始化**:
        -   创建者进程创建了3个用于图像帧的共享内存块和1个用于存储"最新帧索引"的共享内存块。
        -   "最新帧索引" (`np_latest_idx`) 被初始化为 `2`，这样消费者一开始就可以安全地从 `Buffer[2]` 读取（初始为空白帧）。
        -   生产者内部的写入指针 (`_producer_write_idx`) 初始化为 `0`，空闲指针 (`_producer_free_idx`) 初始化为 `1`。

    2.  **生产者循环**:
        -   调用 `get_write_buffer()` 获取当前可用的写入缓冲区 (例如，`Buffer[0]`)。
        -   将新捕获的数据（如一帧图像）填入该缓冲区。
        -   调用 `done_writing()`。这是机制的核心：
            a. 它会原子性地将共享的"最新帧索引" (`np_latest_idx`) 更新为刚刚写完的缓冲区的索引 (变为 `0`)。
            b. 它会"回收"消费者之前正在读取的旧缓冲区，作为自己下一个空闲缓冲区。
            c. 它会将自己之前的空闲缓冲区，作为下一个写入目标。
            d. 这个索引交换过程极快，且对消费者是立刻可见的。

    3.  **消费者循环**:
        -   在一个高效的循环中，消费者不断地读取共享的"最新帧索引" (`np_latest_idx`) 的值。
        -   根据读取到的索引值，直接从对应的缓冲区（如 `Buffer[0]`）读取数据。
        -   由于读取的缓冲区永远不会是生产者正在写入的那个，所以消费者永远不会读到"撕裂"（只写了一半）的帧。
        -   **数据新鲜度保证**: 如果消费者处理一帧的速度慢于生产者的速度，它可能会错过中间几帧。当它处理完当前帧后，会直接去读取最新的"最新帧索引"，从而自动跳到最新的数据，丢弃所有已经过时的数据。

    4.  **资源清理**:
        -   所有使用该缓冲区的进程在退出时都**必须**调用 `close()` 来释放自己与共享内存的连接。
        -   只有**创建者**进程在最后才应该调用 `close_and_unlink()`，这个方法会请求操作系统在所有连接都断开后，彻底销毁这些共享内存段，避免资源泄露。
    """

    def __init__(self, name_prefix: str, height: int, width: int, channels: int = 4, dtype=np.uint8, create: bool = False):
        """
        初始化三缓冲对象。

        Args:
            name_prefix (str): 一个唯一的名称前缀，用于标识所有相关的共享内存段。
                               创建者和所有附加者必须使用完全相同的 `name_prefix`。
            height (int):      图像帧的高度。
            width (int):       图像帧的宽度。
            channels (int):    图像帧的通道数 (例如，4代表RGBA)
            dtype:             Numpy 数据类型，例如 np.uint8。
            create (bool):     `True` 表示作为创建者创建共享内存，`False` 表示作为附加者连接到已存在的共享内存。
        """
        # name_prefix 不能为空，这是进程间通信的"地址"，至关重要。
        if not name_prefix:
            raise ValueError("`name_prefix` 不能为空字符串或 None。")

        # 基础属性
        self.name_prefix = name_prefix
        self.shape = (height, width, channels)
        self.dtype = dtype
        self.frame_size = int(np.prod(self.shape) * np.dtype(self.dtype).itemsize)
        self._is_creator = create
        self._int_size = np.dtype(np.int32).itemsize

        # 共享内存对象句柄
        self.idx_shm = None      # 用于存储"最新帧索引"的共享内存对象
        self.frame_shms = []     # 用于存储三个图像帧缓冲区的共享内存对象列表

        # 指向共享内存的 Numpy 数组视图
        self.np_latest_idx = None # 指向"最新帧索引"的 Numpy 数组
        self.np_arrays = []       # 指向三个图像帧缓冲区的 Numpy 数组列表

        # 调用内部方法来创建或附加到共享内存
        self._attach_or_create_buffers()

        # 仅生产者使用的内部状态指针 (私有)
        self._producer_write_idx = 0  # 指向生产者当前应写入的缓冲区索引
        self._producer_free_idx = 1   # 指向生产者下一个可用的空闲缓冲区索引

        # 如果是创建者，则需要进行初始化设置
        if self._is_creator:
            # 将"最新帧索引"初始化为2，确保消费者初始读取时不会与生产者冲突
            self.np_latest_idx[0] = 2
            # 将所有图像缓冲区填充为0，确保一个干净的初始状态
            for arr in self.np_arrays:
                arr.fill(0)

    def _attach_or_create_buffers(self):
        """
        一个内部辅助方法，负责创建或附加到所有需要的共享内存段。
        """
        try:
            # 1. 设置用于"最新帧索引"的共享内存
            idx_name = f"{self.name_prefix}_latest_idx"
            self.idx_shm = shared_memory.SharedMemory(name=idx_name, create=self._is_creator, size=self._int_size)
            # 创建一个 Numpy 数组视图，直接操作这块内存
            self.np_latest_idx = np.ndarray((1,), dtype=np.int32, buffer=self.idx_shm.buf)

            # 2. 循环创建或附加三个图像帧缓冲区
            for i in range(3):
                frame_name = f"{self.name_prefix}_buf_{i}"
                shm = shared_memory.SharedMemory(name=frame_name, create=self._is_creator, size=self.frame_size)
                self.frame_shms.append(shm)
                # 同样为每个图像缓冲区创建 Numpy 数组视图
                self.np_arrays.append(np.ndarray(self.shape, dtype=self.dtype, buffer=shm.buf))

        except Exception as e:
            logger.error(f"创建或附加共享内存失败 (prefix='{self.name_prefix}'). "
                         f"创建者模式={self._is_creator}. 错误: {e}", exc_info=True)
            # 如果在创建过程中失败，必须尝试清理已部分创建的资源
            if self._is_creator:
                self.close_and_unlink()
            else:
                self.close()
            raise

    def get_write_buffer(self) -> np.ndarray:
        """
        [生产者调用] 获取一个当前可以安全写入数据的缓冲区。
        """
        return self.np_arrays[self._producer_write_idx]

    def done_writing(self):
        """
        [生产者调用] 通知系统已完成对写入缓冲区的操作，并发布这一新帧。
        这是三缓冲索引交换的核心逻辑。
        """
        # 1. 确定刚刚写完的缓冲区索引，这将成为新的"最新"帧
        new_latest = self._producer_write_idx
        # 2. 确定下一个要写入的缓冲区索引 (当前的空闲缓冲区)
        new_write = self._producer_free_idx
        # 3. 从共享内存中读取消费者刚刚"用完并放弃"的旧缓冲区索引，它将成为新的空闲区
        new_free = int(self.np_latest_idx[0])

        # 4. **原子操作**: 更新共享的"最新帧索引"，让所有消费者立刻看到新帧
        self.np_latest_idx[0] = new_latest

        # 5. 更新生产者内部的指针，为下一次写入做准备
        self._producer_write_idx = new_write
        self._producer_free_idx = new_free

    def get_read_buffer(self) -> np.ndarray:
        """
        [消费者调用] 获取当前最新、最完整的帧数据以供读取。此操作无锁且极快。
        """
        # 读取共享的最新帧索引
        idx = self.np_latest_idx[0]
        return self.np_arrays[idx]

    def close(self):
        """
        关闭当前进程对共享内存段的连接。
        **所有**使用此缓冲区的进程（包括创建者和附加者）在退出时都**必须**调用此方法。
        这不会销毁共享内存本身，只是断开当前进程的连接。
        """
        if self.idx_shm:
            self.idx_shm.close()
        for shm in self.frame_shms:
            shm.close()

    def close_and_unlink(self):
        """
        关闭连接，并请求操作系统销毁共享内存段。
        此方法应该**仅由创建者进程**在所有子进程都结束后调用，以确保资源被彻底清理。
        """
        # 首先，关闭当前进程的连接
        self.close()

        # 如果是创建者，则继续请求操作系统删除这些共享内存段
        if self._is_creator:
            logger.info(f"创建者进程正在注销共享内存 (prefix: {self.name_prefix}).")
            try:
                # 重新创建一个临时的 SharedMemory 对象来调用 unlink，这是标准做法
                shared_memory.SharedMemory(name=f"{self.name_prefix}_latest_idx").unlink()
            except FileNotFoundError:
                # 如果已经被其他方式清理或从未成功创建，忽略错误
                pass
            
            for i in range(3):
                try:
                    shared_memory.SharedMemory(name=f"{self.name_prefix}_buf_{i}").unlink()
                except FileNotFoundError:
                    pass

        # 清理本地的引用，有助于垃圾回收
        self.frame_shms.clear()
        self.np_arrays.clear()