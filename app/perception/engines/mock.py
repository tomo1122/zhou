
import logging
import numpy as np
import time

logger = logging.getLogger(__name__)

class MockCaptureEngine:
    """A mock capture engine that generates numpy arrays for testing."""

    def __init__(self, width: int = 1920, height: int = 1080, channels: int = 4):
        self._width = width
        self._height = height
        self._channels = channels
        self.frame_count = 0
        logger.info(f"Initialized MockCaptureEngine with resolution {self._width}x{self._height}")

    def start(self):
        """Starts the mock engine."""
        self.frame_count = 0
        logger.info("MockCaptureEngine started.")

    def stop(self):
        """Stops the mock engine."""
        logger.info("MockCaptureEngine stopped.")

    def capture_frame(self) -> np.ndarray:
        """Generates and returns a mock frame with a 24-bit counter in the first pixel."""
        image = np.zeros((self._height, self._width, self._channels), dtype=np.uint8)
        
        # Store the frame count as a 24-bit integer in the RGB channels of the top-left pixel
        # This allows counting up to 2^24 - 1 (over 16 million)
        count = self.frame_count
        # Assuming BGR order for pixel, which is common, but RGB is used for clarity.
        # Let's use RGB: pixel[0,0] = [R, G, B]
        image[0, 0, 0] = count & 0xFF         # B channel = low byte
        image[0, 0, 1] = (count >> 8) & 0xFF  # G channel = mid byte
        image[0, 0, 2] = (count >> 16) & 0xFF # R channel = high byte
        image[0, 0, 3] = 255                  # A channel = opaque

        self.frame_count += 1
        return image

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height
