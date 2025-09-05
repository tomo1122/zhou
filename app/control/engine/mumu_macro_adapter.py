import time
import logging

import pyautogui
import win32gui
import win32api
import win32con

from typing import Tuple, Optional

from app.control.engine.base import BaseController


logger = logging.getLogger(__name__)

pyautogui.MINIMUM_SLEEP = 0.004
if not hasattr(win32con, 'XBUTTON1'):
    win32con.XBUTTON1 = 0x0001
if not hasattr(win32con, 'XBUTTON2'):
    win32con.XBUTTON2 = 0x0002


class MumuMacroController(BaseController):
    """
    一个通过模拟键鼠宏来控制MuMu模拟器的控制器
    """
    def __init__(self, mumu_window_title: str = "MuMu模拟器12", 
                 render_window_class: str = "nemuwin",
                 target_resolution: Tuple[int, int] = (1920, 1080),
                 action_delay: float = 0.2, 
                 **kwargs):
        """
        初始化 MuMu 宏控制器。

        Args:
            mumu_window_title (str): MuMu模拟器主窗口的标题。
            render_window_class (str): MuMu渲染子窗口的类名。
            target_resolution (Tuple[int, int]): 计划中使用的虚拟分辨率。
            action_delay (float): 动作步骤之间的微小延迟。
            **kwargs: 忽略其他参数。
        """
        self.main_window_title = mumu_window_title
        self.render_window_class = render_window_class
        self.target_resolution = target_resolution
        self.action_delay = action_delay
        
        self.main_hwnd = None
        self.render_hwnd = None
        self.render_area: Tuple[int, int, int, int] = (0, 0, 0, 0) # (x, y, width, height)
        self.is_connected = False
        # 不要太频繁的调用active_window
        self.last_active_window_time = -1


    def _find_render_window_recursive(self, parent_hwnd: int) -> int:
        """递归查找渲染子窗口"""
        result_hwnd = [0]
        def callback(hwnd, _):
            if result_hwnd[0] != 0: return
            if win32gui.GetClassName(hwnd) == self.render_window_class:
                result_hwnd[0] = hwnd
            else: 
                found_hwnd = self._find_render_window_recursive(hwnd)
                if found_hwnd != 0: result_hwnd[0] = found_hwnd
        try:
            win32gui.EnumChildWindows(parent_hwnd, callback, None)
        except win32gui.error:
            pass 
        return result_hwnd[0]


    def _update_render_area(self) -> bool:
        """获取并更新渲染子窗口的屏幕位置和大小。"""
        if not self.render_hwnd: return False
        try:
            rect = win32gui.GetWindowRect(self.render_hwnd)
            x, y, right, bottom = rect
            width, height = right - x, bottom - y
            if width > 0 and height > 0:
                self.render_area = (x, y, width, height)
                logger.debug(f"渲染区域更新: {self.render_area}")
                return True
            return False
        except win32gui.error:
            logger.warning("更新渲染区域失败，窗口可能已关闭。")
            self.is_connected = False
            return False


    def _transform_to_screen_coords(self, virtual_x: int, virtual_y: int) -> Optional[Tuple[int, int]]:
        """
        将1920x1080的虚拟坐标转换为真实的屏幕坐标
        """
        area_x, area_y, area_w, area_h = self.render_area
        if area_w == 0 or area_h == 0:
            return None
        
        target_w, target_h = self.target_resolution
        
        # 计算相对比例
        relative_x_ratio = virtual_x / target_w
        relative_y_ratio = virtual_y / target_h

        # 应用到实际渲染区域
        screen_x = int(area_x + relative_x_ratio * area_w)
        screen_y = int(area_y + relative_y_ratio * area_h)
        
        return screen_x, screen_y


    def connect(self):
        if self.is_connected: return
        
        logger.info(f"正在查找主窗口: '{self.main_window_title}'...")
        self.main_hwnd = win32gui.FindWindow(None, self.main_window_title)
        if self.main_hwnd == 0:
            raise ConnectionError(f"找不到主窗口 '{self.main_window_title}'。")
        
        logger.info(f"正在查找渲染子窗口 (class: '{self.render_window_class}')...")
        self.render_hwnd = self._find_render_window_recursive(self.main_hwnd)
        if self.render_hwnd == 0:
            raise ConnectionError(f"在主窗口下找不到渲染子窗口 '{self.render_window_class}'。")

        if not self._update_render_area():
             raise ConnectionError("无法获取有效的渲染区域尺寸。")

        self.is_connected = True
        logger.info(f"成功连接到MuMu模拟器 (主窗口: {self.main_hwnd}, 渲染窗口: {self.render_hwnd})。")


    def close(self):
        self.main_hwnd = None
        self.render_hwnd = None
        self.is_connected = False
        logger.info("MuMu宏控制器连接已关闭。")


    def _activate_window(self):
        if not self.is_connected or not self.main_hwnd:
            raise IOError("控制器未连接，无法激活窗口。")
        if time.time() - self.last_active_window_time < 5:
            return
        self.last_active_window_time = time.time()
        try:
            win32gui.SetForegroundWindow(self.main_hwnd)
            time.sleep(0.1)
            self._update_render_area() 
        except:
            pass


    def deploy(self, start_pos: Tuple[int, int], end_pos: Tuple[int, int], direction: str, slide_length: int = 200):
        """
        实现两段式部署：
        1. 右键拖拽：从手牌区 (start_pos) 放置到战场 (end_pos)。
        2. 左键拖拽：从战场 (end_pos) 按 direction 滑动以设置朝向。
        """
        self._activate_window()

        # 步骤 1: 放置干员 (右键拖拽)
        screen_start = self._transform_to_screen_coords(*start_pos)
        screen_end = self._transform_to_screen_coords(*end_pos)

        if not screen_start or not screen_end:
            logger.error(f"坐标转换失败: {start_pos}->{screen_start}, {end_pos}->{screen_end}")
            return

        logger.debug(f"部署-放置: {start_pos} -> {end_pos} (屏幕: {screen_start} -> {screen_end})")
        pyautogui.click(*screen_start)
        time.sleep(0.5)
        pyautogui.dragTo(*screen_end, button='right', duration=0.5)
        time.sleep(0.5) 

        # 步骤 2: 设定朝向 (左键拖拽)
        target_w, target_h = self.target_resolution
        if direction == 'left':   end_slide_virtual = (max(end_pos[0] - slide_length, 0), end_pos[1])
        elif direction == 'right':  end_slide_virtual = (min(end_pos[0] + slide_length, target_w), end_pos[1])
        elif direction == 'up':     end_slide_virtual = (end_pos[0], max(end_pos[1] - slide_length, 0))
        elif direction == 'down':   end_slide_virtual = (end_pos[0], min(end_pos[1] + slide_length, target_h))
        else: 
            logger.warning(f"未知的部署方向 '{direction}'，跳过朝向设定。")
            return
            
        screen_slide_end = self._transform_to_screen_coords(*end_slide_virtual)
        if not screen_slide_end:
            logger.error(f"朝向坐标转换失败: {end_slide_virtual}->{screen_slide_end}")
            return
            
        logger.debug(f"部署-朝向 ({direction}): {end_pos} -> {end_slide_virtual} (屏幕: {screen_end} -> {screen_slide_end})")
        # 鼠标当前应该还在 screen_end 位置
        pyautogui.dragTo(*screen_slide_end, duration=0.5)


    def skill(self, pos: Tuple[int, int]):
        self._activate_window()
        screen_pos = self._transform_to_screen_coords(*pos)
        if not screen_pos: return
        
        logger.debug(f"技能: {pos} (屏幕: {screen_pos})")
        pyautogui.moveTo(*screen_pos)
        time.sleep(self.action_delay)
        win32api.mouse_event(win32con.MOUSEEVENTF_XDOWN, 0, 0, win32con.XBUTTON2, 0)
        time.sleep(self.action_delay)
        win32api.mouse_event(win32con.MOUSEEVENTF_XUP, 0, 0, win32con.XBUTTON2, 0)


    def recall(self, pos: Tuple[int, int]):
        self._activate_window()
        screen_pos = self._transform_to_screen_coords(*pos)
        if not screen_pos: return

        logger.debug(f"撤退: {pos} (屏幕: {screen_pos})")
        pyautogui.moveTo(*screen_pos)
        time.sleep(self.action_delay)
        win32api.mouse_event(win32con.MOUSEEVENTF_XDOWN, 0, 0, win32con.XBUTTON1, 0)
        time.sleep(self.action_delay)
        win32api.mouse_event(win32con.MOUSEEVENTF_XUP, 0, 0, win32con.XBUTTON1, 0)


    def toggle_pause(self):
        # 发送空格键 支持后台发送
        vk_code = 0x20
        scan_code = win32api.MapVirtualKey(vk_code, 0)
        lParam_down = (scan_code << 16) | 1
        lParam_up = (scan_code << 16) | 0xC0000001
        win32gui.PostMessage(self.main_hwnd, win32con.WM_KEYDOWN, vk_code, lParam_down)
        win32gui.PostMessage(self.main_hwnd, win32con.WM_KEYUP, vk_code, lParam_up)


    def next_frame(self, **kwargs):
        delay = kwargs.get("delay", 13)
        key_char = None
        vk_code = None

        if delay == 99:
            key_char = "y"
            vk_code = 0x59  # 'Y' 键的虚拟键码
        elif delay == 33:
            key_char = "t"
            vk_code = 0x54  # 'T' 键的虚拟键码
        elif delay == 12:
            key_char = "r"
            vk_code = 0x52  # 'R' 键的虚拟键码
        else:
            raise RuntimeError(f"mumu_macro_adapter 收到了一个不合法的delay： {delay}")

        logger.debug(f"下一帧 (delay={delay}, key='{key_char}') - 后台发送")
        
        # 检查窗口句柄是否存在
        if not self.main_hwnd:
             logger.error("无法发送下一帧指令，因为主窗口句柄丢失。")
             # 可以考虑重新连接或抛出异常
             raise IOError("控制器未连接或窗口已关闭。")

        # 使用 PostMessage 后台发送按键消息
        scan_code = win32api.MapVirtualKey(vk_code, 0)
        lParam_down = (scan_code << 16) | 1
        lParam_up = (scan_code << 16) | 0xC0000001
        
        win32gui.PostMessage(self.main_hwnd, win32con.WM_KEYDOWN, vk_code, lParam_down)
        win32gui.PostMessage(self.main_hwnd, win32con.WM_KEYUP, vk_code, lParam_up)

if __name__ == "__main__":
    print('start')
    mumu_control = MumuMacroController()
    mumu_control.connect()
    mumu_control.toggle_pause()