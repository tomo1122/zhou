import mmap
import os
import tempfile
import numpy as np
import logging

logger = logging.getLogger(__name__)

class TripleSharedBuffer:
    def __init__(self, name_prefix: str, height: int, width: int, channels: int = 4, dtype=np.uint8, create: bool = False):
        """
        Initializes a triple buffer using memory-mapped files for robust cross-process sharing.

        Args:
            name_prefix (str): A unique prefix for the backing files.
            height (int): The height of the image frame.
            width (int): The width of the image frame.
            channels (int): The number of channels in the image (e.g., 4 for RGBA).
            dtype: The numpy data type for the frame elements.
            create (bool): True if this process is creating the buffers, False if attaching to existing ones.
        """
        self.name_prefix = name_prefix
        self.shape = (height, width, channels)
        self.dtype = dtype
        self.frame_size = int(np.prod(self.shape) * np.dtype(self.dtype).itemsize)
        self._is_creator = create
        self._int_size = np.dtype(np.int32).itemsize

        # --- Shared State (using mmap for index) ---
        self.idx_file = None
        self.idx_mmap = None
        self.np_latest_idx = None
        self._setup_index_mmap()

        # --- Buffers (using mmap for frames) ---
        self.frame_files = []
        self.frame_mmaps = []
        self.np_arrays = []
        self._attach_or_create_buffers()

        # --- Producer-side State ---
        self._producer_write_idx = 0
        self._producer_free_idx = 1

    def _get_temp_dir(self):
        # Using a consistent temporary directory helps in locating files.
        return tempfile.gettempdir()

    def _setup_index_mmap(self):
        """Creates or attaches to the shared memory for the latest buffer index."""
        idx_filename = os.path.join(self._get_temp_dir(), f"{self.name_prefix}_latest_idx.tmp")
        
        if self._is_creator:
            # Create the file and allocate space
            self.idx_file = open(idx_filename, "wb+")
            self.idx_file.seek(self._int_size - 1)
            self.idx_file.write(b'\0')
            self.idx_file.flush()
        else:
            # Attach to the existing file
            self.idx_file = open(idx_filename, "rb+")

        self.idx_mmap = mmap.mmap(self.idx_file.fileno(), self._int_size)
        self.np_latest_idx = np.ndarray((1,), dtype=np.int32, buffer=self.idx_mmap)
        
        if self._is_creator:
            self.np_latest_idx[0] = 2 # Initial value points to the third buffer

    def _attach_or_create_buffers(self):
        """Creates or attaches to the three shared memory frame buffers."""
        for i in range(3):
            frame_filename = os.path.join(self._get_temp_dir(), f"{self.name_prefix}_buf_{i}.tmp")
            
            if self._is_creator:
                # Create the file and allocate space
                f = open(frame_filename, "wb+")
                f.seek(self.frame_size - 1)
                f.write(b'\0')
                f.flush()
            else:
                # Attach to the existing file
                f = open(frame_filename, "rb+")
            
            self.frame_files.append(f)
            m = mmap.mmap(f.fileno(), self.frame_size)
            self.frame_mmaps.append(m)
            self.np_arrays.append(np.ndarray(self.shape, dtype=self.dtype, buffer=m))

    def get_write_buffer(self) -> np.ndarray:
        """Returns the numpy array that the producer should write to."""
        return self.np_arrays[self._producer_write_idx]

    def done_writing(self):
        """Signals that the producer has finished writing to the buffer."""
        new_latest = self._producer_write_idx
        new_write = self._producer_free_idx
        
        # Read the value from shared memory and immediately cast it to a native Python int.
        # This prevents the producer's internal state from being "contaminated" by numpy types.
        new_free = int(self.np_latest_idx[0])
        
        # Atomically update the latest index for the consumer
        self.np_latest_idx[0] = new_latest

        # Swap producer's write and free buffers using pure Python integers
        self._producer_write_idx = new_write
        self._producer_free_idx = new_free

    def get_read_buffer(self) -> np.ndarray:
        """Returns the latest complete frame buffer for the consumer to read from."""
        idx = self.np_latest_idx[0]
        return self.np_arrays[idx]

    def close_and_unlink(self):
        """Closes all resources and unlinks backing files if this is the creator."""
        logger.info(f"Closing mmap buffers in process {os.getpid()}. Creator: {self._is_creator}")
        # Close mmaps first
        if self.idx_mmap: self.idx_mmap.close()
        for m in self.frame_mmaps: m.close()

        # Then close file handles
        if self.idx_file: self.idx_file.close()
        for f in self.frame_files: f.close()

        # Unlink backing files (only by the creator to prevent race conditions)
        if self._is_creator:
            logger.info("Creator process is unlinking backing files.")
            idx_filename = os.path.join(self._get_temp_dir(), f"{self.name_prefix}_latest_idx.tmp")
            try: os.remove(idx_filename)
            except OSError as e: logger.warning(f"Could not remove {idx_filename}: {e}")
            
            for i in range(3):
                frame_filename = os.path.join(self._get_temp_dir(), f"{self.name_prefix}_buf_{i}.tmp")
                try: os.remove(frame_filename)
                except OSError as e: logger.warning(f"Could not remove {frame_filename}: {e}")

        # Clear local references
        self.frame_files.clear()
        self.frame_mmaps.clear()
        self.np_arrays.clear()