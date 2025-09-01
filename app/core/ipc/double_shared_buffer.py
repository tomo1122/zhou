import ctypes
import logging

from multiprocessing import shared_memory


logger = logging.getLogger(__name__)


class FrameData(ctypes.Structure):
    """定义一个 C-Style 结构体，用于在共享内存中原子性地存储帧数据"""
    # 紧凑内存布局
    _pack_ = 1      
    _fields_ = [
        ("total_frames", ctypes.c_longlong),
        ("logical_frame", ctypes.c_int),
        ("cycle_index", ctypes.c_int),
        ("timestamp", ctypes.c_double),
    ]


class DoubleSharedBuffer:
    """
    一个 基于双缓冲的 状态驱动的 IPC机制
    核心目标是保证数据的新鲜度，分享一个小型、固定的数据结构（状态）

    
    ### 设计模式

    1.  **创建者/附加者 (Creator/Attacher) 模式**:
        -   **创建者**: 由一个主进程（协调者）以 `create=True` 模式实例化。它负责实际创建操作系统级的共享内存段。
        -   **附加者**: 所有子进程（生产者、消费者）都以 `create=False` 模式实例化，并通过共享的 `name_prefix` "地址"来附加到已存在的内存段上。

    2.  **双缓冲机制 (Double Buffering)**:
        -   系统维护两个独立的数据缓冲区（槽0和槽1）和一个用于指向最新缓冲区的共享索引 `latest_idx`。

        
    ### 工作流程

    1.  **初始化**:
        -   创建者进程创建了2个用于存储 `FrameData` 的共享内存块和1个用于存储"最新槽索引" (`latest_idx`) 的共享内存块。
        -   `latest_idx` 被初始化为 `0`。
        -   生产者内部的写入指针 (`_producer_write_idx`) 也初始化为 `0`。

    2.  **生产者循环 (例如: `Ruler` 进程)**:
        -   调用 `.set()` 方法发布一个新状态。
        -   内部逻辑如下：
            a. 将新的状态数据完整地写入到当前指向的**写入槽** (例如，`槽[0]`)。这个槽此时对消费者是不可见的。
            b. 数据写完后，**原子性地**更新共享的 `latest_idx` 的值为刚刚写完的槽的索引 (更新为 `0`)。这个操作对所有消费者立刻可见。
            c. 将生产者自己的内部写入指针切换到另一个槽 (切换到 `槽[1]`)，为下一次写入做准备。

    3.  **消费者循环 (例如: `Commander` 进程)**:
        -   在一个高效的循环中，调用 `.get()` 方法。
        -   内部逻辑如下：
            a. **原子性地**读取共享的 `latest_idx` 的值 (例如，读到 `0`)。
            b. 根据读取到的索引，直接从对应的**读取槽** (例如，`槽[0]`) 拷贝数据。
            c. 返回数据的**一个副本**。这确保了消费者获取到的数据在其后续处理过程中不会被生产者再次修改，保证了数据的一致性。
        -   由于读取的槽永远不会是生产者当前正在写入的槽，消费者永远不会读到"撕裂"（只写了一半）的数据。

    4.  **资源清理**:
        -   所有使用该缓冲区的进程在退出时都**必须**调用 `close()` 来释放自己与共享内存的连接。
        -   只有**创建者**进程在最后才应该调用 `close_and_unlink()`，这个方法会请求操作系统彻底销毁这些共享内存段，避免资源泄露。
    
    """
    def __init__(self, name_prefix: str, create: bool = False):
        """
        初始化共享状态对象。

        Args:
            name_prefix (str): 一个唯一的名称前缀，用于标识所有相关的共享内存段。
                               创建者和所有附加者必须使用完全相同的 `name_prefix`。
            create (bool):     `True` 表示作为创建者创建共享内存，`False` 表示作为
                               附加者连接到已存在的共享内存。
        """
        if not name_prefix:
            raise ValueError("`name_prefix` 不能为空。")

        self.name_prefix = name_prefix
        self._is_creator = create
        self._data_size = ctypes.sizeof(FrameData)
        self._idx_size = ctypes.sizeof(ctypes.c_int)

        self._idx_shm: shared_memory.SharedMemory = None
        self._data_shms: list[shared_memory.SharedMemory] = []

        self.latest_idx_view: ctypes.c_int = None
        self.data_views: list[FrameData] = []
        
        # 仅生产者使用的内部状态
        self._producer_write_idx = 0
        self._attach_or_create()

    def _attach_or_create(self):
        """内部方法，创建或附加到共享内存。"""
        try:
            # 1. 创建/附加索引内存
            idx_name = f"{self.name_prefix}_state_idx"
            self._idx_shm = shared_memory.SharedMemory(name=idx_name, create=self._is_creator, size=self._idx_size)
            self.latest_idx_view = ctypes.c_int.from_buffer(self._idx_shm.buf)
            
            # 2. 创建/附加两个数据槽
            for i in range(2):
                data_name = f"{self.name_prefix}_state_buf_{i}"
                shm = shared_memory.SharedMemory(name=data_name, create=self._is_creator, size=self._data_size)
                self._data_shms.append(shm)
                self.data_views.append(FrameData.from_buffer(shm.buf))

            if self._is_creator:
                # 初始化状态
                self.latest_idx_view.value = 0
                for view in self.data_views:
                    view.total_frames = -1
                    view.logical_frame = -1
                    view.cycle_index = -1
                    view.timestamp = 0.0

        except Exception as e:
            logger.error(f"创建或附加 SharedState 内存失败 (prefix='{self.name_prefix}'). "
                         f"创建者模式={self._is_creator}. 错误: {e}", exc_info=True)
            if self._is_creator:
                self.close_and_unlink()
            else:
                self.close()
            raise


    def set(self, total_frames: int, logical_frame: int, cycle_index: int, timestamp: float):
        """
        [生产者调用] 写入新状态到备用缓冲区，然后原子性地切换索引。
        """
        # 1. 写入数据到当前的写入槽
        write_view = self.data_views[self._producer_write_idx]
        write_view.total_frames = total_frames
        write_view.logical_frame = logical_frame
        write_view.cycle_index = cycle_index
        write_view.timestamp = timestamp

        # 2. 原子性地更新最新索引，让消费者看到新数据
        self.latest_idx_view.value = self._producer_write_idx

        # 3. 切换到另一个缓冲区供下次写入
        self._producer_write_idx = 1 - self._producer_write_idx


    def get(self) -> FrameData:
        """
        [消费者调用] 获取最新的状态数据副本。此操作无锁且极快。
        """
        # 1. 原子性地读取最新数据的索引
        idx = self.latest_idx_view.value
        
        # 2. 从对应的槽中读取数据
        latest_view = self.data_views[idx]
        
        # 3. 返回一个数据副本，确保线程安全和数据一致性
        return FrameData(
            latest_view.total_frames,
            latest_view.logical_frame,
            latest_view.cycle_index,
            latest_view.timestamp
        )


    def close(self):
        """关闭当前进程对共享内存的连接。"""
        # 在关闭共享内存句柄之前，先显式地删除 ctypes 视图引用
        self.latest_idx_view = None
        self.data_views.clear()

        if self._idx_shm:
            self._idx_shm.close()
        for shm in self._data_shms:
            shm.close()
        self._data_shms.clear()


    def close_and_unlink(self):
        """关闭连接并请求销毁共享内存段 (仅由创建者调用)。"""
        self.close()
        if self._is_creator:
            logger.info(f"创建者进程正在注销 SharedState 共享内存 (prefix: {self.name_prefix}).")
            try:
                shared_memory.SharedMemory(name=f"{self.name_prefix}_state_idx").unlink()
                for i in range(2):
                    shared_memory.SharedMemory(name=f"{self.name_prefix}_state_buf_{i}").unlink()
            except FileNotFoundError:
                pass