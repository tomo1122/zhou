import time
import textwrap
import logging
import subprocess

from typing import Optional, IO, Tuple

from app.control.engine.base import BaseController


logger = logging.getLogger(__name__)


class MaaTouchAdapter(BaseController):
    def __init__(self, device_serial: str):
        """
        初始化 MaaTouch 适配器。
        
        Args:
            device_serial: 设备的 adb 序列号 (例如 '127.0.0.1:16384')。
        """
        self.device_serial = device_serial
        self._process: Optional[subprocess.Popen] = None
        self.stdin: Optional[IO] = None
        self.is_connected: bool = False


    def _run_adb(self, command: str) -> str:
        """执行一个 adb 命令字符串并返回其标准输出。"""

        full_command = f"adb {command}"
        logger.debug(f"Executing ADB command: {full_command}")
        try:
            result = subprocess.run(
                full_command.split(),
                capture_output=True,
                text=True,
                encoding='utf-8',
                check=True
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"执行 ADB 命令失败: {full_command}\n错误: {e.stderr.strip()}")
        except FileNotFoundError:
            raise FileNotFoundError("找不到 'adb' 命令。请确保 adb 在您的系统 PATH 环境变量中。")


    def connect(self):
        if self.is_connected:
            return
        logger.info(f"正在通过 adb 连接到 {self.device_serial}...")
        self._run_adb(f'connect {self.device_serial}')
        
        logger.info("正在获取 Android ID...")
        android_id = self._run_adb(f'-s {self.device_serial} shell settings get secure android_id')
        if not android_id:
            raise RuntimeError("获取 Android ID 失败，请检查设备连接和权限。")
            
        logger.info("正在启动 MaaTouch 输入流...")
        create_cmd_str = (
            f'adb -s {self.device_serial} shell '
            f'"export CLASSPATH=/data/local/tmp/{android_id}; app_process /data/local/tmp com.shxyke.MaaTouch.App"'
        )
        
        try:
            self._process = subprocess.Popen(
                create_cmd_str,
                # 使用 shell=True 来正确处理带引号和分号的复杂命令
                shell=True, 
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.stdin = self._process.stdin
            time.sleep(1) 
            
            if self._process.poll() is not None:
                raise RuntimeError("MaaTouch 进程启动失败")

            self.is_connected = True
            logger.info("MaaTouch 已连接。")
        except Exception as e:
            self.close()
            raise ConnectionError(f"连接 MaaTouch 失败: {e}")


    def close(self):
        if not self.is_connected and not self._process:
            return
        
        logger.info("正在关闭 MaaTouch 连接...")
        if self._process:
            if self.stdin and not self.stdin.closed:
                try: 
                    self.stdin.close()
                except Exception: 
                    pass
            
            if self._process.poll() is None:
                self._process.terminate()
                try: 
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._process.kill()
            self._process = None
            self.stdin = None
        
        self.is_connected = False
        logger.info("MaaTouch 连接已关闭。")


    def _write(self, data: str):
        if not self.is_connected or not self.stdin or self.stdin.closed:
            raise IOError("MaaTouch 未连接或输入流已关闭。")
        try:
            # 确保写入的数据以换行符结尾
            if not data.endswith('\n'):
                data += '\n'
            self.stdin.write(data.encode('utf-8'))
            self.stdin.flush()
        except BrokenPipeError:
            self.is_connected = False
            raise IOError("写入失败，MaaTouch 连接可能已断开。")


    def deploy(self, start_pos: Tuple[int, int], end_pos: Tuple[int, int], direction: str, slide_length: int = 200):
        deploy_cmd = textwrap.dedent(f'''
            d 0 1800 100 1 
            u 0
            d 0 {start_pos[0]} {start_pos[1]} 1
            w 10
            m 0 {end_pos[0]} {end_pos[1]} 1
            k 4 d
            k 4 u
            c
            w 500
            u 0
            c
        ''')
        time.sleep(1)
        self._write(deploy_cmd)
        
        if direction == 'left':   end_slide = (max(end_pos[0] - slide_length, 0), end_pos[1])
        elif direction == 'right':  end_slide = (min(end_pos[0] + slide_length, 1920), end_pos[1])
        elif direction == 'up':     end_slide = (end_pos[0], max(end_pos[1] - slide_length, 0))
        elif direction == 'down':   end_slide = (end_pos[0], min(end_pos[1] + slide_length, 1080))
        else: return

        slide_cmd = textwrap.dedent(f'''
            d 1 {end_pos[0]} {end_pos[1]} 1
            w 10
            m 1 {end_slide[0]} {end_slide[1]} 1
            c
            w 500
            u 1
            c
        ''')
        time.sleep(0.5) 
        self._write(slide_cmd)


    def skill(self, pos: Tuple[int, int]):
        '''
        点击干员后固定点击坐标 (1265, 605)
        干员技能没好时点击 (1265, 605) 会卡住
        点击 1000 50 恢复为选中干员的状态 (大概是剩余血量的位置)
        '''
        skill_cmd = textwrap.dedent(f'''
            d 0 1800 100 1 
            u 0                      
            d 1 {pos[0]} {pos[1]} 1
            u 1
            w 20
            k 4 d
            k 4 u
            c
            w 100
            d 2 1265 605 1
            u 2
            c
            w 500
            d 3 1000 50 1
            u 3  
            c
        ''')
        time.sleep(1)
        self._write(skill_cmd)

    def recall(self, pos: Tuple[int, int]):
        '''
        点击干员后固定点击坐标 (920, 325)
        '''
        recall_cmd = textwrap.dedent(f'''
            d 0 1800 100 1 
            u 0
            d 1 {pos[0]} {pos[1]} 1
            u 1
            w 10
            k 4 d
            k 4 u
            c 
            w 500
            d 2 920 325 1
            u 2
            c
        ''')
        time.sleep(1)
        self._write(recall_cmd)

    def toggle_pause(self):
        pause_cmd = textwrap.dedent(f'''
            d 0 1800 100 1 
            u 0
            c
        ''')
        self._write(pause_cmd)

    def next_frame(self, delay: int = 33):
        next_frame_cmd = textwrap.dedent(f'''
            d 0 1800 100 1
            u 0
            w {delay}
            k 4 d
            k 4 u
            c
        ''')
        time.sleep(1)
        self._write(next_frame_cmd)


if __name__ == "__main__":
    device_serial = '127.0.0.1:16384'
    maatouch = MaaTouchAdapter(device_serial)
    maatouch.connect()

    maatouch.deploy([100, 1000], [1090, 600], 'left')
    maatouch.deploy([280, 1000], [1250, 600], 'left')
    maatouch.deploy([450, 1000], [1425, 560], 'left')
    maatouch.deploy([790, 1000], [1600, 600], 'left')
    maatouch.deploy([950, 1000], [1090, 350], 'left')
    maatouch.deploy([1140, 1000], [1250, 350], 'left')
    maatouch.deploy([1300, 1000], [1420, 350], 'left')
    
    time.sleep(3)

    maatouch.recall([1090, 600])
    maatouch.recall([1250, 600])
    maatouch.recall([1425, 560])
    maatouch.recall([1600, 600])
    maatouch.recall([1090, 350])
    maatouch.recall([1250, 350])
    maatouch.recall([1420, 350])

    for _ in range(100):
        print(_)
        # maatouch.deploy([1820, 1000], [1430, 580], 'left')
        maatouch.next_frame(delay=12)
        # maatouch.skill([640, 490])
        # maatouch.toggle_pause()
        # maatouch.recall([1330, 266])
        # time.sleep(1)


    time.sleep(1)
    maatouch.close()