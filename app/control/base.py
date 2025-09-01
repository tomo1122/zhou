from abc import ABC, abstractmethod
from typing import Tuple, Optional


class BaseController(ABC):
    """控制器的抽象基类，定义了所有具体控制器必须实现的标准操作接口"""
    @abstractmethod
    def deploy(self, start_pos: Tuple[int, int], end_pos: Tuple[int, int], direction: str):
        """
        部署一个单位。

        Args:
            start_pos: 卡牌在手牌区的起始拖拽坐标 (像素坐标)。
            end_pos: 战场上的目标部署坐标 (像素坐标)。
            direction: 部署后的朝向 ('up', 'down', 'left', 'right')。
        """
        pass

    @abstractmethod
    def skill(self, pos: Tuple[int, int]):
        """在指定坐标激活技能。"""
        pass

    @abstractmethod
    def recall(self, pos: Tuple[int, int]):
        """在指定坐标撤退一个单位。"""
        pass

    @abstractmethod
    def toggle_pause(self):
        """切换游戏的暂停/继续状态。"""
        pass

    @abstractmethod
    def next_frame(self, delay: int = 13):
        """
        在暂停状态下，使游戏前进一帧。
        
        Args:
            delay: 点击屏幕暂停 和 发送back按键事件 之间的间隔时长
        """
        pass