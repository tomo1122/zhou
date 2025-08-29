# pure_python_producer.py (修正版)
import ctypes
import mmap
import os
import signal
import time
import numpy as np

# 和中继进程共享的常量
SHM_BUFFER_FILENAME = "relay_test_buffer.tmp"
SHM_FLAG_FILENAME = "relay_test_flag.tmp"
INT_SIZE = ctypes.sizeof(ctypes.c_int32)
BUFFER_SIZE = 1920 * 1080 * 4

stop_producer = False

def handle_signal(signum, frame):
    global stop_producer
    print("[Producer] Received signal, shutting down.")
    stop_producer = True

def main():
    global stop_producer
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    f_buffer, m_buffer = None, None
    f_flag, m_flag = None, None

    try:
        # --- 创建共享内存 ---
        f_buffer = open(SHM_BUFFER_FILENAME, "w+b")
        f_buffer.seek(BUFFER_SIZE - 1); f_buffer.write(b'\0')
        f_buffer.flush() # <--- 修正点 1
        m_buffer = mmap.mmap(f_buffer.fileno(), 0)

        f_flag = open(SHM_FLAG_FILENAME, "w+b")
        f_flag.seek(INT_SIZE - 1); f_flag.write(b'\0')
        f_flag.flush() # <--- 修正点 2
        m_flag = mmap.mmap(f_flag.fileno(), 0)

        flag = ctypes.c_int32.from_buffer(m_flag)
        flag.value = 0 # 初始为0，等待中继进程准备好

        source_np_buffer = np.ndarray((1080, 1920, 4), dtype=np.uint8, buffer=m_buffer)
        frame_count = 0

        print("[Producer] Ready and waiting for Relay to start.")
        while not stop_producer:
            if flag.value != 0: # 等待中继进程发出“准备好”的信号
                time.sleep(0.001)
                continue
            
            # --- 生成新数据 ---
            source_np_buffer.fill(frame_count % 256) # 简单地用递增值填充
            
            # --- 发送信号 ---
            flag.value = 1 # 1 表示“数据已就绪”
            frame_count += 1
            if frame_count % 100 == 0:
                print(f"[Producer] Sent frame {frame_count}")


    except Exception as e:
        print(f"[Producer] Error: {e}")
    finally:
        print("[Producer] Cleaning up.")
        # 在关闭mmap前，解除numpy数组的引用
        source_np_buffer = None
        flag = None
        if m_buffer: m_buffer.close()
        if f_buffer: f_buffer.close()
        if m_flag: m_flag.close()
        if f_flag: f_flag.close()
        try:
            if os.path.exists(SHM_BUFFER_FILENAME): os.remove(SHM_BUFFER_FILENAME)
            if os.path.exists(SHM_FLAG_FILENAME): os.remove(SHM_FLAG_FILENAME)
        except OSError:
            pass
        print("[Producer] Stopped.")

if __name__ == "__main__":
    main()