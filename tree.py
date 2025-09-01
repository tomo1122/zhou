import os

# 要排除的目录名
EXCLUDE_DIRS = {".venv", ".git", "__pycache__", ".pytest_cache", "zhou"}

def print_tree(root, prefix=""):
    # 获取目录下的所有文件和文件夹
    items = sorted(os.listdir(root))
    items = [i for i in items if i not in EXCLUDE_DIRS]  # 过滤掉不需要的目录

    for index, name in enumerate(items):
        path = os.path.join(root, name)
        connector = "└── " if index == len(items) - 1 else "├── "
        print(prefix + connector + name)

        if os.path.isdir(path):
            # 如果是目录，继续递归
            extension = "    " if index == len(items) - 1 else "│   "
            print_tree(path, prefix + extension)


if __name__ == "__main__":
    import sys

    # 获取要展示的目录，默认为当前目录
    start_path = sys.argv[1] if len(sys.argv) > 1 else "."

    print(start_path)
    print_tree(start_path)
