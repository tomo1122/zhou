import os
import shutil
import glob
import tempfile
import logging

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

def clean_pycache(root_dir):
    logging.info(f"Cleaning __pycache__ directories in {root_dir}...")
    for dirpath, dirnames, filenames in os.walk(root_dir):
        if '__pycache__' in dirnames:
            pycache_path = os.path.join(dirpath, '__pycache__')
            logging.info(f"Deleting: {pycache_path}")
            try:
                shutil.rmtree(pycache_path)
            except OSError as e:
                logging.error(f"Error deleting {pycache_path}: {e}")

def clean_mmap_tmp_files():
    logging.info(f"Cleaning mmap temporary files in {tempfile.gettempdir()}...")
    # Assuming name_prefix is "test_capture" as used in test_capture_ipc.py
    patterns = [
        os.path.join(tempfile.gettempdir(), "test_capture_latest_idx.tmp"),
        os.path.join(tempfile.gettempdir(), "test_capture_buf_*.tmp")
    ]
    
    for pattern in patterns:
        for file_path in glob.glob(pattern):
            logging.info(f"Deleting: {file_path}")
            try:
                os.remove(file_path)
            except OSError as e:
                logging.error(f"Error deleting {file_path}: {e}")

def delete_specific_file(file_path):
    logging.info(f"Deleting specific file: {file_path}...")
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            logging.info(f"Successfully deleted: {file_path}")
        else:
            logging.info(f"File not found, skipping: {file_path}")
    except OSError as e:
        logging.error(f"Error deleting {file_path}: {e}")

if __name__ == "__main__":
    project_root = os.path.dirname(os.path.abspath(__file__))
    
    logging.info("Starting cleanup process...")
    
    clean_pycache(project_root)
    clean_mmap_tmp_files()
    delete_specific_file(os.path.join(project_root, "app", "core", "ipc", "triple_shared_buffer.py"))
    
    logging.info("Cleanup process finished.")
