"""python -m subtransjav.refine 入口：必须经 sys.exit 转发 main() 返回值，
否则行动层执行器的退出码（0=成功/1=全败/2=entries 非法/3=部分降级）不传播。"""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
