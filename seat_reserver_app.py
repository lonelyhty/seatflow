from __future__ import annotations

import argparse

from seat_reserver.gui import main as run_gui


def main(argv=None):
    parser = argparse.ArgumentParser(description="SeatFlow 学习通图书馆预约")
    parser.add_argument("--preview", action="store_true", help="打开空白界面，不读取配置或自动执行")
    parser.add_argument("--no-autostart", action="store_true", help="读取已有配置但等待手动启动")
    parser.add_argument("--test-seats", action="store_true", help="读取已有配置并启动选座测试，不启动正式预约")
    arguments = parser.parse_args(argv)
    run_gui(preview=arguments.preview, no_autostart=arguments.no_autostart, test_seats=arguments.test_seats)


if __name__ == "__main__":
    main()
