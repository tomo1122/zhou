import logging

import win32gui

from typing import Tuple, Optional


logger = logging.getLogger(__name__)


class WindowHelper:
    """
    一个封装了与 Windows 窗口交互的通用工具类。

    它负责查找窗口句柄、获取渲染区域的屏幕坐标、以及在
    屏幕绝对坐标和应用虚拟坐标之间进行转换。
    """
    def __init__(self,
                 main_window_title: str,
                 render_window_class: str,
                 target_resolution: Tuple[int, int] = (1920, 1080)):
        """
        初始化 WindowHelper。

        Args:
            main_window_title (str): 目标主窗口的标题。
            render_window_class (str): 目标渲染子窗口的类名。
            target_resolution (Tuple[int, int]): 应用内部使用的虚拟分辨率，用于坐标转换。
        """
        self.main_window_title = main_window_title
        self.render_window_class = render_window_class
        self.target_resolution = target_resolution

        self.main_hwnd: Optional[int] = None
        self.render_hwnd: Optional[int] = None
        self.render_area: Tuple[int, int, int, int] = (0, 0, 0, 0)  # (x, y, width, height)
        self.is_connected: bool = False


    def connect(self) -> None:
        """
        查找主窗口和渲染子窗口，并建立连接。

        Raises:
            ConnectionError: 如果找不到主窗口、渲染子窗口，或无法获取有效的渲染区域。
        """
        if self.is_connected:
            return

        logger.info(f"正在查找主窗口: '{self.main_window_title}'...")
        self.main_hwnd = win32gui.FindWindow(None, self.main_window_title)
        if self.main_hwnd == 0:
            raise ConnectionError(f"找不到主窗口 '{self.main_window_title}'。请确保应用已运行。")

        logger.info(f"正在查找渲染子窗口 (class: '{self.render_window_class}')...")
        self.render_hwnd = self._find_render_window_recursive(self.main_hwnd)
        if self.render_hwnd == 0:
            raise ConnectionError(f"在主窗口下找不到类名为 '{self.render_window_class}' 的渲染子窗口。")

        if not self.update_render_area():
            raise ConnectionError("无法获取有效的渲染区域尺寸。窗口可能未完全加载或尺寸为零。")

        self.is_connected = True
        logger.info(f"成功连接到目标窗口 (主窗口: {self.main_hwnd}, 渲染窗口: {self.render_hwnd})。")


    def disconnect(self) -> None:
        """断开连接并重置所有状态。"""
        self.main_hwnd = None
        self.render_hwnd = None
        self.is_connected = False
        logger.info("WindowHelper 连接已断开。")


    def _find_render_window_recursive(self, parent_hwnd: int) -> int:
        """递归地在父窗口下查找具有指定类名的子窗口。"""
        result_hwnd = [0]

        def callback(hwnd, _):
            if result_hwnd[0] != 0:
                return
            if win32gui.GetClassName(hwnd) == self.render_window_class:
                result_hwnd[0] = hwnd
            else:
                found_hwnd = self._find_render_window_recursive(hwnd)
                if found_hwnd != 0:
                    result_hwnd[0] = found_hwnd

        try:
            win32gui.EnumChildWindows(parent_hwnd, callback, None)
        except win32gui.error:
            # 某些窗口可能不允许枚举子窗口，忽略错误并继续
            pass
        return result_hwnd[0]


    def update_render_area(self) -> bool:
        """
        获取并更新渲染子窗口的最新屏幕位置和大小。

        Returns:
            bool: 如果成功获取到有效的（宽度和高度>0）区域，则返回 True，否则 False。
        """
        if not self.render_hwnd:
            return False
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
            self.disconnect()
            return False


    def transform_virtual_to_screen(self, virtual_pos: Tuple[int, int]) -> Optional[Tuple[int, int]]:
        """
        将虚拟坐标 (例如，作战计划中的坐标) 转换为屏幕绝对坐标。
        这是【控制器】需要的。

        Args:
            virtual_pos: (x, y) 格式的虚拟坐标元组。

        Returns:
            (x, y) 格式的屏幕绝对坐标元组，如果未连接或渲染区域无效则返回 None。
        """
        if not self.is_connected:
            return None

        area_x, area_y, area_w, area_h = self.render_area
        if area_w == 0 or area_h == 0:
            return None

        target_w, target_h = self.target_resolution
        virtual_x, virtual_y = virtual_pos

        # 计算相对比例
        relative_x_ratio = virtual_x / target_w
        relative_y_ratio = virtual_y / target_h

        # 应用到实际渲染区域
        screen_x = int(area_x + relative_x_ratio * area_w)
        screen_y = int(area_y + relative_y_ratio * area_h)

        return screen_x, screen_y


    def transform_screen_to_virtual(self, screen_pos: Tuple[int, int]) -> Optional[Tuple[int, int]]:
        """
        将屏幕绝对坐标 (例如，鼠标点击事件的坐标) 转换为虚拟坐标。
        这是【录制器】需要的。

        Args:
            screen_pos: (x, y) 格式的屏幕绝对坐标元组。

        Returns:
            (x, y) 格式的虚拟坐标元组。如果屏幕坐标不在渲染区域内，则返回 None。
        """
        if not self.is_connected:
            return None

        area_x, area_y, area_w, area_h = self.render_area
        screen_x, screen_y = screen_pos

        # 检查点击是否在渲染区域内
        if not (area_x <= screen_x < area_x + area_w and area_y <= screen_y < area_y + area_h):
            return None

        # 计算在渲染区域内的相对坐标
        relative_x, relative_y = screen_x - area_x, screen_y - area_y
        
        target_w, target_h = self.target_resolution
        
        # 缩放到目标虚拟分辨率
        virtual_x = int((relative_x / area_w) * target_w)
        virtual_y = int((relative_y / area_h) * target_h)

        return virtual_x, virtual_y
    

    def is_foreground_window(self) -> bool:
        """
        检查目标主窗口当前是否为系统的焦点窗口。

        Returns:
            bool: 如果主窗口是焦点窗口，则返回 True，否则返回 False。
                  如果尚未连接，也返回 False。
        """
        if not self.is_connected or not self.main_hwnd:
            return False
        
        try:
            current_foreground_hwnd = win32gui.GetForegroundWindow()
            return current_foreground_hwnd == self.main_hwnd
        except win32gui.error:
            # 如果窗口已失效，GetForegroundWindow 可能出错
            return False