"""入口。用 D:\\PY310\\python.exe main.py 启动。"""
import sys

import ui


def main():
    if sys.version_info < (3, 9):
        print("需要 Python 3.9+")
        return 1
    ui.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())