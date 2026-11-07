from __future__ import annotations

import ctypes
import copy
import os
import queue
import threading
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import messagebox, ttk

from . import __version__
from .credentials import data_dir, load_settings, save_settings
from .model import CAMPUS, Settings, floor_key, now_china, parse_seats, seat_key
from .runner import ReservationRunner

BLUE = "#2357d9"
INK = "#18243d"
MUTED = "#68758b"
BACKGROUND = "#f3f6fc"


class SeatApp:
    def __init__(self, root: tk.Tk, *, preview: bool = False, no_autostart: bool = False, test_seats: bool = False):
        self.root = root
        self.preview = preview
        self.events: queue.Queue[dict] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.closing = False
        self.pending_autostart = None
        self.poll_timer = None
        self.pending_start = None
        self.test_mode = False
        self.job_finished = False
        self.form_locked = False
        self.catalogs: dict[str, dict] = {}
        self.picker_window = None
        self.settings = Settings() if preview else Settings.new_account()
        self.load_error = ""
        if not preview:
            try:
                self.settings = load_settings()
            except Exception:
                self.load_error = "已有配置无法解密或读取。请重新填写并保存；加密配置绑定当前 Windows 用户。"
        self.school_settings = copy.deepcopy(self.settings)
        self.school_dialog = None
        self.vars = {name: tk.StringVar(value=getattr(self.settings, name))
                     for name in ("account", "password", "campus", "floor", "seats", "booking_date", "start_time", "end_time", "release_time")}
        self.vars["date_mode"] = tk.StringVar(value="明天（每次启动自动计算）" if self.settings.date_mode == "tomorrow" else "指定日期")
        self.autostart = tk.BooleanVar(value=self.settings.autostart)
        self.floor_seats = dict(self.settings.floor_seats)
        self.selected_floor_key = self.settings.location_key()
        self.catalog_text = tk.StringVar(value="尚未读取本层座位，可先测试并读取。")
        self.phase = tk.StringVar(value="请填写预约配置")
        self.countdown = tk.StringVar(value="22:30")
        self.result_text = tk.StringVar(value="先填写账号、楼层和使用时段。测试会点击选座、确认时段，再读取本层座位。")
        self.plan_text = tk.StringVar(value=f"学校：{self.settings.school_name or '未设置学校'}\n预约日期：明天\n更换账号不会自动切换学校入口。")
        self.form_widgets: list[tuple[tk.Widget, str]] = []
        self._build()
        for field in ("campus", "floor"):
            self.vars[field].trace_add("write", self.floor_changed)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll_timer = self.root.after(100, self.poll)
        if self.load_error:
            self.log(self.load_error)
        else:
            self.log("已就绪。每次运行使用全新的独立浏览器会话，关闭后清除临时登录状态。")
        if test_seats:
            self.phase.set("即将开始选座测试")
            self.result_text.set("正在准备测试登录、确认时段和读取座位图。")
            self.stop_button.configure(state="normal")
            self.pending_autostart = self.root.after(500, lambda: self.start(test_mode=True, save=False))
        elif self.settings.account and self.settings.password and self.settings.seats:
            try:
                plan = self.settings.plan()
                self.plan_text.set(plan.summary())
                if self.settings.autostart and not preview and not no_autostart:
                    self.phase.set("即将自动启动")
                    self.result_text.set("已读取保存的配置，3 秒后自动启动。点击停止可取消。")
                    self.stop_button.configure(state="normal")
                    self.pending_autostart = self.root.after(3000, lambda: self.start(save=False))
            except ValueError as exc:
                self.log(str(exc))

    def _build(self) -> None:
        self.root.title(f"学习通 · 图书馆自动预约 v{__version__}")
        self.root.geometry("1000x900")
        self.root.minsize(960, 880)
        self.root.configure(bg=BACKGROUND)
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=BACKGROUND)
        style.configure("Card.TFrame", background="white")
        style.configure("TLabel", background="white", foreground=INK, font=("Microsoft YaHei UI", 10))
        style.configure("Muted.TLabel", foreground=MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("Section.TLabel", font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("TEntry", padding=4, font=("Microsoft YaHei UI", 10))
        style.configure("TCombobox", padding=4, font=("Microsoft YaHei UI", 10))
        style.configure("TButton", padding=(12, 9), font=("Microsoft YaHei UI", 10))
        style.configure("Small.TButton", padding=(7, 5), font=("Microsoft YaHei UI", 9))
        style.configure("Primary.TButton", background=BLUE, foreground="white", borderwidth=0)
        style.map("Primary.TButton", background=[("active", "#1946b8"), ("disabled", "#a9b9df")])
        style.configure("TCheckbutton", background="white", foreground=INK, font=("Microsoft YaHei UI", 9))

        header = tk.Frame(self.root, bg="#182c51", padx=24, pady=18)
        header.pack(fill="x")
        self.school_button = ttk.Button(header, text="学校与放号规则…", command=self.edit_school)
        self.school_button.pack(side="right")
        tk.Label(header, text="图书馆自动预约", fg="white", bg="#182c51", font=("Microsoft YaHei UI", 20, "bold")).pack(anchor="w")
        self.school_text = tk.StringVar(value=self.school_settings.school_name or '未设置学校 · 请先选择学校入口')
        tk.Label(header, textvariable=self.school_text, fg="#c2cfea", bg="#182c51", font=("Microsoft YaHei UI", 10)).pack(anchor="w", pady=(4, 0))

        content = ttk.Frame(self.root, padding=(20, 18))
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=3)
        content.columnconfigure(1, weight=2)
        content.rowconfigure(0, weight=1)
        content.rowconfigure(2, weight=1)
        form = ttk.Frame(content, style="Card.TFrame", padding=12)
        form.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="预约配置", style="Section.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))
        self._entry(form, "学习通账号", "account", 1)
        self._entry(form, "学习通密码", "password", 2, password=True)
        ttk.Label(form, text="手机号或超星号；账号和密码仅在本机加密保存。", style="Muted.TLabel").grid(row=3, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self._combo(form, "校区 / 馆名", "campus", 4, (CAMPUS,), editable=True)
        self._combo(form, "楼层 / 区域", "floor", 5, tuple(f"{i}F" for i in range(1, 8)), editable=True)
        self._entry(form, "候选座位号", "seats", 6)
        ttk.Label(form, text="每层单独记住；按优先级填写本层编号，最多 8 个。", style="Muted.TLabel").grid(row=7, column=0, columnspan=3, sticky="w", pady=(0, 6))
        directory = ttk.Frame(form, style="Card.TFrame")
        directory.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        directory.columnconfigure(1, weight=1)
        self.pick_button = ttk.Button(directory, text="从本层座位中选", style="Small.TButton", command=self.choose_seats, state="disabled")
        self.pick_button.grid(row=0, column=0, sticky="w", padx=(0, 10))
        ttk.Label(directory, textvariable=self.catalog_text, style="Muted.TLabel", wraplength=285).grid(row=0, column=1, sticky="w")
        self._combo(form, "预约日期", "date_mode", 9, ("明天（每次启动自动计算）", "指定日期"))
        date_input = self._entry(form, "指定日期", "booking_date", 10)
        self.date_entry = date_input
        date_combo = self.form_widgets[-2][0]
        date_combo.bind("<<ComboboxSelected>>", lambda _: self.date_mode_changed())
        ttk.Label(form, text="使用时段").grid(row=11, column=0, sticky="w", padx=(0, 14), pady=(0, 7))
        times = ttk.Frame(form, style="Card.TFrame")
        times.grid(row=11, column=1, columnspan=2, sticky="ew", pady=(0, 7))
        times.columnconfigure(0, weight=1)
        times.columnconfigure(2, weight=1)
        for column, name, begin, end in ((0, "start_time", 0, 1440), (2, "end_time", 0, 1440)):
            combo = ttk.Combobox(times, textvariable=self.vars[name], state="normal", width=10,
                                 values=tuple(f"{m // 60:02d}:{m % 60:02d}" for m in range(begin, end, 30)))
            combo.grid(row=0, column=column, sticky="ew")
            self.form_widgets.append((combo, "normal"))
        ttk.Label(times, text="至").grid(row=0, column=1, padx=7)
        self._entry(form, "开放预约时间", "release_time", 12)
        ttk.Label(form, text="开放时间是开始预约的时间；上方时段是到馆使用时间。", style="Muted.TLabel", wraplength=440).grid(row=13, column=0, columnspan=3, sticky="w", pady=(1, 10))
        check = ttk.Checkbutton(form, text="下次打开程序自动启动预约", variable=self.autostart)
        check.grid(row=14, column=0, columnspan=3, sticky="w")
        self.form_widgets.append((check, "normal"))
        self.date_mode_changed()

        status = ttk.Frame(content, style="Card.TFrame", padding=20)
        status.grid(row=0, column=1, sticky="nsew")
        ttk.Label(status, text="运行状态", style="Section.TLabel").pack(anchor="w")
        ttk.Label(status, textvariable=self.phase, foreground=BLUE, font=("Microsoft YaHei UI", 11, "bold"), wraplength=285).pack(anchor="w", pady=(16, 5))
        ttk.Label(status, textvariable=self.countdown, font=("Consolas", 32, "bold"), foreground=INK).pack(anchor="w", pady=(8, 4))
        ttk.Label(status, text="北京时间 · 开放前显示倒计时", style="Muted.TLabel").pack(anchor="w")
        ttk.Separator(status).pack(fill="x", pady=18)
        ttk.Label(status, textvariable=self.plan_text, wraplength=285, justify="left").pack(anchor="w")
        ttk.Separator(status).pack(fill="x", pady=18)
        ttk.Label(status, textvariable=self.result_text, wraplength=285, justify="left").pack(anchor="w")
        ttk.Label(status, text="等待期间请保持程序、浏览器及网络开启。", style="Muted.TLabel", wraplength=285).pack(anchor="w", side="bottom", pady=(16, 0))

        buttons = ttk.Frame(content, padding=(0, 12, 0, 12))
        buttons.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.save_button = ttk.Button(buttons, text="保存配置", command=self.save)
        self.save_button.pack(side="left")
        self.test_button = ttk.Button(buttons, text="测试并读取本层座位", command=lambda: self.start(test_mode=True))
        self.test_button.pack(side="left", padx=9)
        self.start_button = ttk.Button(buttons, text="保存并启动预约", style="Primary.TButton", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(buttons, text="停止 / 关闭浏览器", command=self.stop, state="disabled")
        self.stop_button.pack(side="right")
        self.logs_button = ttk.Button(buttons, text="查看日志", command=self.open_logs)
        self.logs_button.pack(side="right", padx=(0, 8))

        logs = ttk.Frame(content, style="Card.TFrame", padding=(14, 10))
        logs.grid(row=2, column=0, columnspan=2, sticky="nsew")
        ttk.Label(logs, text="运行记录", style="Section.TLabel").pack(anchor="w", pady=(0, 7))
        self.log_text = tk.Text(logs, height=5, wrap="word", font=("Microsoft YaHei UI", 9), relief="flat", bg="#f8faff", fg=INK, state="disabled", padx=8, pady=7)
        self.log_text.pack(fill="both", expand=True)

    def _entry(self, parent, label, field, row, password=False):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 14), pady=(0, 7))
        entry = ttk.Entry(parent, textvariable=self.vars[field], show="●" if password else "")
        entry.grid(row=row, column=1, columnspan=2, sticky="ew", pady=(0, 7))
        self.form_widgets.append((entry, "normal"))
        return entry

    def _combo(self, parent, label, field, row, values, *, editable=False):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 14), pady=(0, 7))
        state = "normal" if editable else "readonly"
        combo = ttk.Combobox(parent, textvariable=self.vars[field], values=values, state=state)
        combo.grid(row=row, column=1, columnspan=2, sticky="ew", pady=(0, 7))
        self.form_widgets.append((combo, state))
        return combo

    def current_location_key(self):
        from .schools import location_key
        return location_key(self.school_settings.entry_url, self.vars['campus'].get(), self.vars['floor'].get(),
                            self.school_settings.third_level, self.school_settings.room_id)

    def edit_school(self):
        if self.form_locked or (self.worker and self.worker.is_alive()):
            return
        if self.school_dialog is not None and self.school_dialog.window.winfo_exists():
            self.school_dialog.window.lift()
            return
        from .school_gui import SchoolDialog
        self.school_dialog = SchoolDialog(self.root, self.collect(), self.apply_school)

    def apply_school(self, settings):
        old = self.selected_floor_key
        self.floor_seats[old] = self.vars['seats'].get()
        self.school_settings = copy.deepcopy(settings)
        self._applying_school = True
        self.vars['campus'].set(settings.campus)
        self.vars['floor'].set(settings.floor)
        self._applying_school = False
        self.vars['release_time'].set(settings.release_time)
        self.selected_floor_key = settings.location_key()
        self.vars['seats'].set(self.floor_seats.get(self.selected_floor_key, ''))
        region = f' / {settings.third_level}' if settings.third_level else ''
        self.school_text.set(f'{settings.school_name} / {settings.campus} / {settings.floor}{region}')
        self.update_catalog_hint()
        self.log('学校与放号规则已应用；请核对候选座位和到馆时段，再保存账号。')

    def date_mode_changed(self) -> None:
        if self.vars["date_mode"].get() == "指定日期":
            if not self.vars["booking_date"].get():
                self.vars["booking_date"].set((now_china().date() + timedelta(days=1)).isoformat())
            self.date_entry.configure(state="normal")
        else:
            self.date_entry.configure(state="disabled")

    def floor_changed(self, *_args) -> None:
        if getattr(self, '_applying_school', False):
            return
        # A manually edited location invalidates a previously selected exact room.
        if (self.vars['campus'].get() != self.school_settings.campus
                or self.vars['floor'].get() != self.school_settings.floor):
            self.school_settings.third_level = ''
            self.school_settings.room_id = 0
            self.school_settings.campus = self.vars['campus'].get()
            self.school_settings.floor = self.vars['floor'].get()
        key = self.current_location_key()
        if key == self.selected_floor_key:
            return
        self.floor_seats[self.selected_floor_key] = self.vars["seats"].get()
        self.selected_floor_key = key
        self.vars["seats"].set(self.floor_seats.get(key, ""))
        if self.picker_window is not None and self.picker_window.winfo_exists():
            self.picker_window.destroy()
        self.update_catalog_hint()
        self.log(f"已切换到 {self.vars['floor'].get()}，使用该层单独保存的候选座位。")

    def update_catalog_hint(self) -> None:
        catalog = self.catalogs.get(self.selected_floor_key)
        if catalog:
            self.catalog_text.set(f"{catalog['floor']} · 已读取 {len(catalog['labels'])} 个座位\n{catalog['day']} · {catalog.get('interval', '')}")
        else:
            self.catalog_text.set("尚未读取本层座位，可先测试并读取。")
        self.pick_button.configure(state="normal" if catalog and not self.form_locked else "disabled")

    def remember_catalog(self, event: dict) -> None:
        self.catalogs[event.get('location_key') or self.catalog_key(event['campus'], event['floor'])] = event
        self.update_catalog_hint()

    def validate_catalog(self, settings: Settings) -> None:
        catalog = self.catalogs.get(settings.location_key())
        if not catalog:
            return
        labels = {seat_key(label) for label in catalog["labels"]}
        missing = [label for label in parse_seats(settings.seats) if seat_key(label) not in labels]
        if missing:
            raise ValueError(f"{settings.floor} 没有座位 {','.join(missing)}。请从本层实际座位中选择。")

    def choose_seats(self) -> None:
        catalog = self.catalogs.get(self.selected_floor_key)
        if not catalog:
            return
        if self.picker_window is not None and self.picker_window.winfo_exists():
            self.picker_window.lift()
            return
        window = tk.Toplevel(self.root)
        self.picker_window = window
        window.title(f"{catalog['floor']} · 选择候选座位")
        window.geometry("670x510")
        window.minsize(620, 480)
        window.transient(self.root)
        window.grab_set()
        body = ttk.Frame(window, padding=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(2, weight=1)
        body.rowconfigure(2, weight=1)
        ttk.Label(body, text=f"{catalog['campus']} {catalog['floor']}：{len(catalog['labels'])} 个实际座位", style="Section.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(body, text=f"已确认 {catalog['day']} {catalog.get('interval', '')}；正式预约重新检查空闲状态。", style="Muted.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(5, 12))
        left = ttk.Frame(body)
        left.grid(row=2, column=0, sticky="nsew")
        search = tk.StringVar()
        ttk.Entry(left, textvariable=search).pack(fill="x", pady=(0, 6))
        available_box = tk.Frame(left)
        available_box.pack(fill="both", expand=True)
        available = tk.Listbox(available_box, exportselection=False, font=("Consolas", 11), width=20)
        scroll = ttk.Scrollbar(available_box, command=available.yview)
        available.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        available.pack(side="left", fill="both", expand=True)
        ttk.Label(left, text="输入编号筛选，双击加入候选。", style="Muted.TLabel").pack(anchor="w", pady=(5, 0))
        right = ttk.Frame(body)
        right.grid(row=2, column=2, sticky="nsew")
        ttk.Label(right, text="候选顺序（最多 8 个）").pack(anchor="w", pady=(0, 6))
        chosen = tk.Listbox(right, exportselection=False, font=("Consolas", 11), width=20)
        chosen.pack(fill="both", expand=True)
        valid = {seat_key(label): label for label in catalog["labels"]}
        try:
            current = parse_seats(self.vars["seats"].get())
        except ValueError:
            current = ()
        for label in current:
            if seat_key(label) in valid:
                chosen.insert("end", valid[seat_key(label)])
        def filter_list(*_):
            available.delete(0, "end")
            for label in catalog["labels"]:
                if search.get().strip() in label:
                    available.insert("end", label)
        def add(*_):
            selection = available.curselection()
            if not selection:
                return
            label = available.get(selection[0])
            if label in chosen.get(0, "end"):
                return
            if chosen.size() >= 8:
                messagebox.showinfo("候选已满", "最多选择 8 个候选座位。", parent=window)
                return
            chosen.insert("end", label)
        def remove():
            if chosen.curselection():
                chosen.delete(chosen.curselection()[0])
        def move(step):
            if not chosen.curselection():
                return
            index = chosen.curselection()[0]
            target = index + step
            if 0 <= target < chosen.size():
                label = chosen.get(index)
                chosen.delete(index)
                chosen.insert(target, label)
                chosen.selection_set(target)
        controls = ttk.Frame(body, padding=8)
        controls.grid(row=2, column=1)
        for text, command in (("加入 →", add), ("移除", remove), ("上移", lambda: move(-1)), ("下移", lambda: move(1))):
            ttk.Button(controls, text=text, command=command, style="Small.TButton").pack(fill="x", pady=4)
        def apply():
            if not chosen.size():
                messagebox.showinfo("请选择座位", "至少选择一个本层座位。", parent=window)
                return
            self.vars["seats"].set(",".join(chosen.get(0, "end")))
            self.floor_seats[self.selected_floor_key] = self.vars["seats"].get()
            window.destroy()
        ttk.Button(body, text="使用这些候选座位", style="Primary.TButton", command=apply).grid(row=3, column=0, columnspan=3, sticky="e", pady=(14, 0))
        available.bind("<Double-Button-1>", add)
        search.trace_add("write", filter_list)
        filter_list()

    def collect(self) -> Settings:
        values = {name: var.get().strip() if name != "password" else var.get()
                  for name, var in self.vars.items()}
        values["date_mode"] = "tomorrow" if values["date_mode"].startswith("明天") else "specific"
        self.floor_seats[self.selected_floor_key] = values["seats"]
        advanced = {name: getattr(self.school_settings, name) for name in
                    ('school_name', 'entry_url', 'third_level', 'room_id', 'release_days_before', 'days_ahead', 'time_unit')}
        return Settings(**values, **advanced, autostart=self.autostart.get(), floor_seats=dict(self.floor_seats))

    def catalog_key(self, campus, floor):
        from .schools import location_key
        return location_key(self.school_settings.entry_url, campus, floor,
                            self.school_settings.third_level, self.school_settings.room_id)

    def log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{now_china():%H:%M:%S}  {text}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def save(self) -> None:
        try:
            settings = self.collect()
            plan = settings.plan()
            self.validate_catalog(settings)
            save_settings(settings)
        except Exception as exc:
            text = str(exc) if isinstance(exc, ValueError) else "加密保存失败，请检查本机配置目录是否可写。"
            messagebox.showerror("配置未保存", text, parent=self.root)
            return
        self.settings = settings
        self.plan_text.set(plan.summary())
        self.result_text.set("各层候选座位已分别保存。可以测试选座，或启动自动预约。")
        self.log("配置已保存；账号和密码未以明文写入配置文件。")

    def start(self, *, test_mode=False, save=True) -> None:
        if self.pending_autostart is not None:
            self.root.after_cancel(self.pending_autostart)
            self.pending_autostart = None
        if self.worker is not None and self.worker.is_alive():
            if self.test_mode and self.job_finished:
                self.pending_start = {"test_mode": test_mode, "save": save}
                self.stop_event.set()
                self.set_running(True)
                self.phase.set("正在结束上次测试")
                self.log("关闭测试浏览器后，将按当前配置启动新会话。")
            return
        try:
            settings = self.collect()
            settings.validate_account()
            plan = settings.plan(for_preview=test_mode)
            if not test_mode:
                self.validate_catalog(settings)
            if save and not test_mode:
                save_settings(settings)
        except Exception as exc:
            text = str(exc) if isinstance(exc, ValueError) else "加密保存失败，请检查本机配置目录是否可写。"
            messagebox.showerror("请检查配置", text, parent=self.root)
            self.phase.set("请检查配置")
            self.stop_button.configure(state="disabled")
            return
        self.stop_event = threading.Event()
        self.test_mode = test_mode
        self.job_finished = False
        self.phase.set("正在启动")
        self.result_text.set("测试会登录、点击本层选座、选择并确认时段，再读取座位；不会提交预约。" if test_mode else "按保存的配置执行；成功后核对预约记录并停止。")
        self.countdown.set("--:--:--")
        self.plan_text.set(f"测试：{settings.campus} {settings.floor}\n预设时段：{plan.interval}\n选座 → 选择时段 → 确认 → 读取座位\n明日未开放时使用今天可选的测试时段" if test_mode else plan.summary())
        self.set_running(True)
        self.log("启动本层登录和选座测试。" if test_mode else "启动自动预约任务。")
        self.worker = threading.Thread(target=self.work, args=(settings, plan, test_mode), daemon=True, name="seat-reservation")
        self.worker.start()

    def work(self, settings, plan, test_mode) -> None:
        log_path = data_dir() / "logs" / f"run-{now_china():%Y%m%d-%H%M%S-%f}.log"
        protocol = getattr(self, 'use_protocol_preview', False) or bool(settings.room_id)
        if protocol:
            from .protocol_runner import ProtocolRunner
            runner = ProtocolRunner(self.events.put, self.stop_event, log_path=log_path, keep_open=False)
        else:
            runner = ReservationRunner(self.events.put, self.stop_event, log_path=log_path)
        try:
            runner.run(settings, plan, preview=test_mode)
        except Exception:
            self.events.put({"kind": "result", "code": "ERROR", "text": "程序未能启动，请检查运行环境。"})

    def open_logs(self) -> None:
        folder = data_dir() / "logs"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))
        except OSError:
            messagebox.showerror("无法打开日志目录", f"请手动查看：{folder}", parent=self.root)

    def set_running(self, running: bool, *, browser_open: bool = False) -> None:
        self.form_locked = running
        self.school_button.configure(state='disabled' if running else 'normal')
        for widget, original in self.form_widgets:
            widget.configure(state="disabled" if running else original)
        if not running:
            self.date_mode_changed()
        for button in (self.save_button, self.test_button, self.start_button):
            button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running or browser_open else "disabled")
        self.update_catalog_hint()

    def stop(self) -> None:
        self.pending_start = None
        if self.pending_autostart is not None:
            self.root.after_cancel(self.pending_autostart)
            self.pending_autostart = None
            self.phase.set("已取消自动启动")
            self.result_text.set("可以修改配置，或手动启动预约。")
            self.stop_button.configure(state="disabled")
        if self.worker is not None and self.worker.is_alive():
            self.stop_event.set()
            self.phase.set("正在关闭浏览器")
            self.log("已请求停止；当前页面操作结束后关闭本次浏览器会话。")

    def poll(self) -> None:
        self.poll_timer = None
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event["kind"]
            if kind == "phase":
                self.phase.set(event["text"])
            elif kind == "log":
                self.log(event["text"])
            elif kind == "countdown":
                seconds = event["seconds"]
                self.countdown.set(f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}")
            elif kind == "catalog":
                self.remember_catalog(event)
            elif kind == "result":
                names = {"SUCCESS": "预约成功，记录已核对", "EXISTING": "目标日期已有预约", "LOGIN_READY": "登录测试完成",
                         "PREVIEW_READY": "本层选座测试完成",
                         "UNAVAILABLE": "候选座位未约到", "UNKNOWN": "请核对预约结果", "ERROR": "任务已停止", "STOPPED": "已停止"}
                self.phase.set(names.get(event["code"], "任务结束"))
                self.result_text.set(event["text"])
                self.job_finished = True
                if self.test_mode:
                    self.set_running(False, browser_open=self.worker is not None and self.worker.is_alive())
                if event["code"] in {"SUCCESS", "UNKNOWN", "ERROR"}:
                    self.root.bell()
            elif kind == "closed":
                self.log(event["text"])
        if self.worker is not None and not self.worker.is_alive():
            self.worker = None
            self.set_running(False)
            if self.pending_start is not None and not self.closing:
                arguments = self.pending_start
                self.pending_start = None
                self.start(**arguments)
        if self.closing and self.worker is None:
            self.destroy_window()
            return
        self.poll_timer = self.root.after(100, self.poll)

    def destroy_window(self) -> None:
        if self.poll_timer is not None:
            self.root.after_cancel(self.poll_timer)
            self.poll_timer = None
        self.root.destroy()

    def close(self) -> None:
        self.closing = True
        if self.school_dialog is not None and self.school_dialog.window.winfo_exists():
            self.school_dialog.close()
        self.stop()
        if self.worker is None:
            self.destroy_window()
        else:
            self.phase.set("正在关闭程序和浏览器")


def main(*, preview: bool = False, no_autostart: bool = False, test_seats: bool = False) -> None:
    mutex = None
    kernel = None
    if os.name == "nt" and not preview:
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        mutex = kernel.CreateMutexW(None, False, "Local\\SeatFlow")
        if ctypes.get_last_error() == 183:
            kernel.CloseHandle(mutex)
            ctypes.windll.user32.MessageBoxW(None, "预约程序已经打开，请查看现有窗口。", "图书馆自动预约", 0x40)
            return
    try:
        root = tk.Tk()
        SeatApp(root, preview=preview, no_autostart=no_autostart, test_seats=test_seats)
        root.mainloop()
    finally:
        if mutex and kernel:
            kernel.CloseHandle(mutex)
