import logging
from ctypes import WinDLL, c_int, c_wchar_p, POINTER, c_ubyte, byref
import numpy as np

# Assuming app is in python path
from app.core.config import MumuConfig

logger = logging.getLogger(__name__)

class MumuCaptureEngine:
    """A capture engine for MuMu Player using its screen capture DLL."""

    def __init__(self, config: MumuConfig):
        """Initializes the MuMuPlayer capture engine from a config object."""
        self.config = config
        self.dll_path = config.mumu_dll_path
        
        self.handle = None
        self._width = c_int()
        self._height = c_int()
        self.buffer = None
        self.dll = None

    def start(self):
        """Loads the DLL, connects to the emulator, and prepares for capture."""
        try:
            logger.info(f"Loading MuMu DLL from: {self.dll_path}")
            self.dll = WinDLL(self.dll_path)
            self._setup_dll_functions()
        except (FileNotFoundError, OSError) as e:
            logger.error(f"Failed to load MuMu DLL: {e}")
            raise RuntimeError(f"Failed to load MuMu DLL from {self.dll_path}") from e

        logger.info(f"Connecting to MuMu instance {self.config.mumu_instance_index} at {self.config.mumu_base_path}")
        self.handle = self.dll.nemu_connect(self.config.mumu_base_path, self.config.mumu_instance_index)
        if self.handle == 0:
            raise ConnectionError("Failed to connect to the emulator. Check if it is running and the config is correct.")

        result = self.dll.nemu_capture_display(self.handle, 0, 0, byref(self._width), byref(self._height), None)
        if result != 0:
            self.stop()
            raise RuntimeError(f"Failed to get emulator resolution. Error code: {result}")
        
        logger.info(f"Successfully connected to emulator. Resolution: {self.width}x{self.height}")

    def stop(self):
        """Disconnects from the emulator."""
        if self.handle and self.dll:
            self.dll.nemu_disconnect(self.handle)
            self.handle = None
            logger.info("Disconnected from the emulator.")

    def _setup_dll_functions(self):
        """Sets up the argument and return types for the DLL functions."""
        self.dll.nemu_connect.argtypes = [c_wchar_p, c_int]
        self.dll.nemu_connect.restype = c_int
        self.dll.nemu_disconnect.argtypes = [c_int]
        self.dll.nemu_capture_display.argtypes = [c_int, c_int, c_int, POINTER(c_int), POINTER(c_int), POINTER(c_ubyte)]
        self.dll.nemu_capture_display.restype = c_int

    def capture_frame_into_buffer(self, dest_buffer):
        """
        Captures a frame directly into a provided ctypes buffer.
        This is the core, high-performance capture call.
        """
        if not self.handle:
            raise ConnectionError("Emulator is not connected.")
        
        buffer_size = len(dest_buffer)
        result = self.dll.nemu_capture_display(self.handle, 0, buffer_size, byref(self._width), byref(self._height), dest_buffer)
        
        if result != 0:
            logger.error(f"Failed to capture frame directly. Error code: {result}")
            # In a tight loop, raising an exception might be too slow.
            # Consider returning the error code instead.
            return result
        return 0

    def capture_frame(self) -> np.ndarray:
        """Convenience method to capture a frame and return it as a flipped numpy array."""
        buffer_size = self.width * self.height * 4
        buffer = (c_ubyte * buffer_size)()
        
        if self.capture_frame_into_buffer(buffer) == 0:
            img_np = np.frombuffer(buffer, dtype=np.uint8).reshape((self.height, self.width, 4))
            img_flipped = img_np[::-1, :, :]
            return img_flipped.copy()
        return None

    @property
    def width(self) -> int:
        return self._width.value

    @property
    def height(self) -> int:
        return self._height.value