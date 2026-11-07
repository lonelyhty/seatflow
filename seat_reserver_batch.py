"""Multi-account desktop app and unattended command-line entry point."""
from __future__ import annotations

import argparse
from pathlib import Path

from seat_reserver.batch import BatchRunner, OK_RESULTS, RESULT_NAMES
from seat_reserver.batch_model import load_batch, masked_account
from seat_reserver.model import now_china
from seat_reserver.protocol_runner import ProtocolRunner
from seat_reserver.runner import ReservationRunner


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="学习通图书馆多学校、多账号并发预约")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--run", action="store_true", help="读取加密账号列表，提前登录并到点预约")
    actions.add_argument("--test-login", action="store_true", help="批量登录测试，不提交预约")
    actions.add_argument("--test-seats", action="store_true", help="批量选座测试，不提交预约")
    actions.add_argument("--check", action="store_true", help="仅检查预约配置，不打开浏览器")
    actions.add_argument("--preview", action="store_true", help="打开空白多账号窗口，不读取配置")
    parser.add_argument("--config", type=Path, help="加密配置文件路径；默认使用本机 batch.json")
    parser.add_argument("--max-parallel", type=int, help="同时运行上限（不会将超出的账号排队）")
    parser.add_argument("--release-time", help="本批次统一开放时间，HH:MM 或 HH:MM:SS（北京时间）")
    parser.add_argument("--per-profile-release", action="store_true", help="使用各账号保存的放号时间，适合多个学校同批运行")
    parser.add_argument("--protocol", action="store_true", help="使用直接 HTTP 协议模式；需要人工验证时停止")
    display = parser.add_mutually_exclusive_group()
    display.add_argument("--headless", action="store_true", default=None, help="不显示浏览器窗口")
    display.add_argument("--headed", action="store_true", help="显示浏览器，以便人工处理验证码")
    args = parser.parse_args(argv)
    if args.per_profile_release and args.release_time is not None:
        parser.error('--per-profile-release 不能与 --release-time 同时使用')
    if not any((args.run, args.test_login, args.test_seats, args.check)):
        from seat_reserver.batch_gui import main as gui_main
        options = {'per_profile_release': True} if args.per_profile_release else {}
        gui_main(preview=args.preview, config_path=args.config, protocol=args.protocol, **options)
        return 0
    try:
        config = load_batch(args.config)
        if args.max_parallel is not None:
            config.max_parallel = args.max_parallel
        if args.release_time is not None:
            config.release_time = args.release_time
            config.use_profile_release = False
        if args.per_profile_release:
            config.use_profile_release = True
        if args.headless is not None or args.headed:
            config.headless = bool(args.headless) and not args.headed
        mode = "login" if args.test_login else "preview" if args.test_seats else "reserve"
        jobs = config.jobs(mode)
    except Exception as exc:
        print(str(exc) if isinstance(exc, ValueError) else f"无法读取本机加密配置（{type(exc).__name__}）。请在多账号窗口中检查。")
        return 2
    if args.check:
        release = "各账号独立放号时间" if config.use_profile_release else f"统一放号时间 {config.release_time}"
        print(f"配置检查通过：{len(jobs)} 个启用账号，上限 {config.max_parallel}，{release}（北京时间）。")
        for index, (profile, plan) in enumerate(jobs, 1):
            print(f"{index}. {profile.settings.school_name} · {masked_account(profile.settings.account)} · {plan.summary()} · 触发 {plan.trigger:%Y-%m-%d %H:%M:%S}")
        return 0

    def emit(event):
        if event["kind"] in {"phase", "result", "release", "batch_started", "batch_done", "log"}:
            print(f"{now_china():%H:%M:%S} [{event.get('name', '批次')}] {event.get('text', '')}", flush=True)

    runner = BatchRunner(emit, runner_factory=ProtocolRunner if args.protocol else ReservationRunner)
    interrupted = False
    try:
        runner.start(config, mode=mode)
        while not runner.wait(0.2):
            pass
    except KeyboardInterrupt:
        interrupted = True
        print("正在停止全部账号；提交过的任务请核对结果。", flush=True)
        runner.stop()
        while not runner.wait(0.2):
            pass
    except Exception as exc:
        print(f"无法启动批次（{type(exc).__name__}）。", flush=True)
        runner.stop()
        return 2
    finally:
        runner.join()
    states = runner.snapshot()
    for state in states:
        print(f"{state['name']} · {state['account']} · {RESULT_NAMES.get(state['code'], state['code'])}")
    print(f"结果文件：{runner.report_path}")
    return 130 if interrupted else 0 if all(s["code"] in OK_RESULTS for s in states) else 2


if __name__ == "__main__":
    raise SystemExit(main())
