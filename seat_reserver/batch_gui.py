from __future__ import annotations

import copy
import os
import queue
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from . import __version__
from .account_lock import AccountBusy, AccountLease
from .batch import BatchRunner, OK_RESULTS
from .batch_model import BatchSettings, Profile, load_batch, masked_account, save_batch
from .credentials import data_dir
from .gui import BACKGROUND, BLUE, INK, SeatApp
from .model import floor_key, now_china
from .model import Settings
from .protocol_runner import ProtocolRunner
from .runner import ReservationRunner


class ProfileEditor(SeatApp):
    """Reuse the verified floor picker, but never launch a real booking here."""
    def __init__(self, root, profile: Profile, commit, *, per_profile_release=True, protocol=False):
        self.profile = copy.deepcopy(profile)
        self.commit_profile = commit
        super().__init__(root, preview=True, no_autostart=True)
        root.title(f"编辑账号 · {profile.name}")
        settings = profile.settings
        self.school_settings = copy.deepcopy(settings)
        self.school_text.set(settings.school_name or '未设置学校 · 请先选择学校入口')
        self.use_protocol_preview = protocol
        self._applying_school = True
        for name, var in self.vars.items():
            if name != "date_mode":
                var.set(getattr(settings, name))
        self._applying_school = False
        self.vars["date_mode"].set("明天（每次启动自动计算）" if settings.date_mode == "tomorrow" else "指定日期")
        self.floor_seats = dict(settings.floor_seats)
        self.selected_floor_key = settings.location_key()
        self.vars["seats"].set(settings.seats)
        self.autostart.set(False)
        self.date_mode_changed()
        self.start_button.pack_forget()
        self.save_button.configure(text="保存到账号列表")
        for widget, _ in self.form_widgets:
            if isinstance(widget, ttk.Checkbutton):
                widget.grid_remove()
        self.vars["release_time"].set(settings.release_time)
        release = next(w for w, _ in self.form_widgets
                       if isinstance(w, ttk.Entry) and str(w.cget("textvariable")) == str(self.vars["release_time"]))
        if not per_profile_release:
            release.configure(state="disabled")
            self.form_widgets = [(w, "disabled" if w == release else state) for w, state in self.form_widgets]
        form = self.form_widgets[0][0].master
        for widget in form.grid_slaves(row=0):
            widget.grid_remove()
        name_row = ttk.Frame(form, style="Card.TFrame")
        name_row.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 12))
        ttk.Label(name_row, text="账号备注").pack(side="left", padx=(0, 10))
        self.name_var = tk.StringVar(value=profile.name)
        ttk.Entry(name_row, textvariable=self.name_var, width=22).pack(side="left", fill="x", expand=True)
        self.enabled_var = tk.BooleanVar(value=profile.enabled)
        ttk.Checkbutton(name_row, text="启用", variable=self.enabled_var).pack(side="right", padx=(10, 0))
        self.phase.set("编辑账号与座位")
        self.plan_text.set("每个账号可以设置不同学校、放号时间、房间和到馆时段。\n主窗口勾选各账号独立放号时间后生效。")
        self.result_text.set("可以先测试并读取本层座位，再保存到账号列表。此窗口的测试不会提交预约。")

    def start(self, *, test_mode=True, save=False):
        super().start(test_mode=True, save=False)

    def save(self):
        try:
            settings = self.collect()
            settings.autostart = False
            settings.plan(for_preview=True)
            self.validate_catalog(settings)
            profile = Profile(self.name_var.get().strip(), settings, self.enabled_var.get(), self.profile.id)
            self.commit_profile(profile)
        except Exception as exc:
            messagebox.showerror("未保存", str(exc) if isinstance(exc, ValueError) else "无法加密保存账号列表。", parent=self.root)
            return
        self.close()


class BatchApp:
    def __init__(self, root: tk.Tk, *, preview=False, config_path: Path | None = None,
                 protocol=False, per_profile_release=None):
        self.root = root
        self.config_path = config_path
        self.preview = preview
        self.events = queue.Queue()
        self.runner: BatchRunner | None = None
        self.editor: ProfileEditor | None = None
        self.closing = False
        self.timer = None
        self.load_failed = False
        try:
            self.config = BatchSettings() if preview else load_batch(config_path)
        except Exception:
            self.config = BatchSettings()
            self.load_failed = True
        self.max_var = tk.StringVar(value=str(self.config.max_parallel))
        self.release_var = tk.StringVar(value=self.config.release_time)
        self.headed_var = tk.BooleanVar(value=not self.config.headless)
        self.protocol_var = tk.BooleanVar(value=protocol)
        self.profile_release_var = tk.BooleanVar(value=self.config.use_profile_release if per_profile_release is None else per_profile_release)
        self.summary_var = tk.StringVar(value="添加账号，先测试登录，再提前启动预约。")
        self.widgets = []
        self._build()
        self.refresh()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.timer = root.after(100, self.poll)
        if self.load_failed:
            self.log("已有多账号配置无法解密或读取，已停止保存和启动。请检查配置文件与 Windows 用户。")
            self.summary_var.set("配置读取失败；请先修复配置文件，再重新打开程序。")
        else:
            self.log("已载入本机账号列表，等待手动启动。登录测试不会提交预约。")

    def _build(self):
        root = self.root
        root.title(f"学习通 · 多学校多账号预约 v{__version__}")
        root.geometry("1220x800")
        root.minsize(1080, 710)
        root.configure(bg=BACKGROUND)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background=BACKGROUND)
        style.configure("TLabel", background=BACKGROUND, foreground=INK, font=("Microsoft YaHei UI", 10))
        style.configure("TButton", padding=(10, 8), font=("Microsoft YaHei UI", 10))
        style.configure("TCheckbutton", background=BACKGROUND, font=("Microsoft YaHei UI", 10))
        style.configure("Treeview", rowheight=31, font=("Microsoft YaHei UI", 10))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))
        header = tk.Frame(root, bg="#182c51", padx=20, pady=16)
        header.pack(fill="x")
        tk.Label(header, text="多账号并发预约", bg="#182c51", fg="white", font=("Microsoft YaHei UI", 20, "bold")).pack(anchor="w")
        tk.Label(header, text="学校分别配置  |  独立登录 · 各校放号时间 · 每个账号分别核对结果",
                 bg="#182c51", fg="#c2cfea", font=("Microsoft YaHei UI", 10)).pack(anchor="w", pady=(5, 0))
        body = ttk.Frame(root, padding=16)
        body.pack(fill="both", expand=True)
        options = ttk.Frame(body)
        options.pack(fill="x")
        ttk.Label(options, text="同时运行上限").pack(side="left")
        capacity = ttk.Spinbox(options, from_=1, to=64, width=5, textvariable=self.max_var)
        capacity.pack(side="left", padx=(8, 22))
        ttk.Label(options, text="统一开放时间（北京时间）").pack(side="left")
        release = ttk.Entry(options, textvariable=self.release_var, width=10)
        release.pack(side="left", padx=(8, 22))
        headed = ttk.Checkbutton(options, text="网页模式显示浏览器", variable=self.headed_var)
        headed.pack(side="left")
        self.widgets.extend([capacity, release, headed])
        self.button(options, "保存设置", self.save_options, side="right")
        modes = ttk.Frame(body)
        modes.pack(fill="x", pady=(8, 0))
        self.protocol_check = ttk.Checkbutton(modes, text="本次使用协议模式（直接请求）", variable=self.protocol_var)
        self.protocol_check.pack(side="left")
        self.widgets.append(self.protocol_check)
        release_mode = ttk.Checkbutton(modes, text="使用各账号独立放号时间", variable=self.profile_release_var)
        release_mode.pack(side='left', padx=(14, 0))
        self.widgets.append(release_mode)
        self.profile_release_var.trace_add('write', lambda *_: self.refresh())
        ttk.Label(modes, text="需要验证码或人工验证时停止并提示。", foreground=BLUE).pack(side="left", padx=(15, 0))
        toolbar = ttk.Frame(body)
        toolbar.pack(fill="x", pady=(14, 10))
        self.button(toolbar, "添加账号", self.add)
        self.button(toolbar, "编辑所选", self.edit)
        self.button(toolbar, "启用 / 停用", self.toggle)
        self.button(toolbar, "删除所选", self.delete)
        ttk.Button(toolbar, text="查看使用说明", command=self.open_help).pack(side="right")
        table_frame = ttk.Frame(body)
        table_frame.pack(fill="both", expand=True)
        columns = ("enabled", "name", "account", "school", "floor", "release", "seats", "interval", "date", "status", "countdown")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended", height=10)
        specs = [("enabled", "启用", 48), ("name", "备注", 110), ("account", "账号", 106), ("school", "学校", 130),
                 ("floor", "楼层 / 区域", 100), ("release", "放号时间", 140),
                 ("seats", "候选座位", 125), ("interval", "使用时段", 108), ("date", "预约日期", 90),
                 ("status", "运行状态", 220), ("countdown", "倒计时", 94)]
        for key, title, width in specs:
            self.table.heading(key, text=title)
            self.table.column(key, width=width, minwidth=width, stretch=key in {"name", "status"})
        self.table.tag_configure("good", foreground="#15803d")
        self.table.tag_configure("bad", foreground="#b42318")
        self.table.tag_configure("ready", foreground=BLUE)
        ybar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        xbar = ttk.Scrollbar(table_frame, orient="horizontal", command=self.table.xview)
        self.table.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.table.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.table.bind("<Double-1>", lambda _: self.edit())
        self.table.bind("<<TreeviewSelect>>", self.show_selected)
        self.detail_var = tk.StringVar(value="选中一行可查看该账号的结果说明。")
        ttk.Label(body, textvariable=self.detail_var, wraplength=1120).pack(anchor="w", pady=(9, 3))
        ttk.Label(body, textvariable=self.summary_var, foreground=BLUE, wraplength=1120).pack(anchor="w", pady=(4, 9))
        actions = ttk.Frame(body)
        actions.pack(fill="x")
        self.button(actions, "测试启用账号登录", lambda: self.start("login"))
        self.button(actions, "启动全部启用账号预约", lambda: self.start("reserve"))
        self.stop_selected = ttk.Button(actions, text="停止所选账号", command=self.stop_selected_jobs, state="disabled")
        self.stop_selected.pack(side="left", padx=5)
        self.stop_all = ttk.Button(actions, text="停止全部", command=self.stop, state="disabled")
        self.stop_all.pack(side="left", padx=5)
        ttk.Button(actions, text="查看结果与日志", command=self.open_logs).pack(side="right")
        self.log_text = tk.Text(body, height=7, state="disabled", wrap="word", bg="#f8faff", fg=INK,
                                font=("Microsoft YaHei UI", 9), relief="flat", padx=9, pady=9)
        self.log_text.pack(fill="x", pady=(14, 0))

    def button(self, parent, text, command, side="left"):
        button = ttk.Button(parent, text=text, command=command)
        button.pack(side=side, padx=(0, 7))
        self.widgets.append(button)
        return button

    def busy(self):
        return self.runner is not None and self.runner.running

    def editor_open(self):
        return self.editor is not None and self.editor.root.winfo_exists()

    def collect(self):
        result = copy.deepcopy(self.config)
        try:
            result.max_parallel = int(self.max_var.get())
        except ValueError as exc:
            raise ValueError("同时运行上限请填写整数。") from exc
        result.release_time = self.release_var.get().strip()
        result.headless = not self.headed_var.get()
        result.use_profile_release = self.profile_release_var.get()
        result.validate()
        return result

    def persist(self, config):
        if self.preview:
            raise ValueError("预览窗口不保存账号，请从正式入口打开。")
        if self.load_failed:
            raise ValueError("已有配置未能读取，请先修复配置后重新打开程序。")
        save_batch(config, self.config_path)
        self.config = config

    def save_options(self):
        if self.busy():
            return
        try:
            self.persist(self.collect())
            self.log("账号和运行设置已加密保存。")
        except Exception as exc:
            self.error(exc)

    def selected(self):
        ids = set(self.table.selection())
        return [p for p in self.config.profiles if p.id in ids]

    def add(self):
        self.open_editor(Profile(name=f"账号 {len(self.config.profiles) + 1}", settings=Settings.new_account()))

    def edit(self):
        selected = self.selected()
        if selected:
            self.open_editor(selected[0])

    def open_editor(self, profile):
        if self.busy() or self.closing or self.load_failed:
            return
        if self.editor_open():
            self.editor.root.lift()
            return
        profile = copy.deepcopy(profile)
        per_profile_release = self.profile_release_var.get()
        if not per_profile_release:
            profile.settings.release_time = self.release_var.get().strip()
        window = tk.Toplevel(self.root)
        self.editor = ProfileEditor(window, profile, self.commit, per_profile_release=per_profile_release,
                                    protocol=self.protocol_var.get())
        window.transient(self.root)

    def commit(self, profile):
        config = self.collect()
        index = next((i for i, p in enumerate(config.profiles) if p.id == profile.id), None)
        if index is None:
            config.profiles.append(profile)
        else:
            config.profiles[index] = profile
        self.persist(config)
        self.refresh()
        self.table.selection_set(profile.id)

    def toggle(self):
        if self.busy() or self.editor_open():
            return
        selected = {p.id for p in self.selected()}
        try:
            config = self.collect()
            for profile in config.profiles:
                if profile.id in selected:
                    profile.enabled = not profile.enabled
            self.persist(config)
            self.refresh()
        except Exception as exc:
            self.error(exc)

    def delete(self):
        if self.busy() or self.editor_open():
            return
        selected = {p.id for p in self.selected()}
        if not selected:
            return
        if not messagebox.askyesno("删除账号配置", f"从本机列表删除选中的 {len(selected)} 个账号配置？", parent=self.root):
            return
        try:
            config = self.collect()
            config.profiles = [p for p in config.profiles if p.id not in selected]
            self.persist(config)
            self.refresh()
        except Exception as exc:
            self.error(exc)

    def refresh(self):
        selected = self.table.selection()
        for key in self.table.get_children():
            self.table.delete(key)
        for profile in self.config.profiles:
            s = profile.settings
            self.table.insert("", "end", iid=profile.id, values=("是" if profile.enabled else "否", profile.name,
                              masked_account(s.account), s.school_name or '未设置学校', s.floor,
                              f"提前{s.release_days_before}天 {s.release_time if self.profile_release_var.get() else self.release_var.get()}",
                              s.seats, f"{s.start_time}–{s.end_time}",
                              "明天" if s.date_mode == "tomorrow" else s.booking_date, "待启动", "—"))
        self.table.selection_set([key for key in selected if self.table.exists(key)])
        if not self.busy() and not self.load_failed:
            release = "使用各账号独立放号时间" if self.profile_release_var.get() else f"统一放号时间 {self.release_var.get()}"
            self.summary_var.set(f"共 {len(self.config.profiles)} 个账号，启用 {sum(p.enabled for p in self.config.profiles)} 个。{release}（北京时间）。")

    def start(self, mode):
        if self.busy() or self.closing or self.load_failed:
            return
        if self.editor_open():
            self.summary_var.set("请先保存并关闭账号编辑窗口及其测试浏览器。")
            self.editor.root.lift()
            return
        try:
            config = self.collect()
            config.jobs(mode)
            self.persist(config)
            self.refresh()
            factory = ProtocolRunner if self.protocol_var.get() else ReservationRunner
            self.runner = BatchRunner(self.events.put, runner_factory=factory)
            self.runner.start(config, mode=mode)
        except Exception as exc:
            self.error(exc)
            return
        self.set_running(True)
        self.log("本批次使用协议模式。" if self.protocol_var.get() else "本批次使用网页模式。")
        self.log("开始批量登录测试；本次不提交预约。" if mode == "login" else "开始正式预约批次：各账号提前登录，分别等待已配置的放号时间。")

    def set_running(self, running):
        for widget in self.widgets:
            widget.configure(state="disabled" if running else "normal")
        self.stop_selected.configure(state="normal" if running else "disabled")
        self.stop_all.configure(state="normal" if running else "disabled")

    def stop_selected_jobs(self):
        if self.runner:
            for profile in self.selected():
                self.runner.stop(profile.id)
            self.log("已请求停止所选账号；其他账号继续运行。")

    def stop(self):
        if self.runner:
            self.runner.stop()
            self.log("已请求停止全部账号，正在等待各账号任务结束。")

    def show_selected(self, _event=None):
        if not self.runner:
            return
        ids = set(self.table.selection())
        state = next((s for s in self.runner.snapshot() if s["id"] in ids), None)
        if state:
            delay = f"；触发晚于设定时间 {state['delay_ms']} ms" if state["delay_ms"] is not None else ""
            self.detail_var.set(f"{state['name']}：{state['text'] or state['phase']}{delay}")

    def log(self, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{now_china():%H:%M:%S}  {text}\n")
        if int(self.log_text.index("end-1c").split(".")[0]) > 500:
            self.log_text.delete("1.0", "100.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def poll(self):
        self.timer = None
        for _ in range(500):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event["kind"] in {"log", "result", "release", "batch_done"}:
                self.log(f"[{event.get('name', '批次')}] {event.get('text', '')}")
            if event["kind"] == "batch_done":
                self.set_running(False)
        if self.runner:
            states = self.runner.snapshot()
            for state in states:
                if not self.table.exists(state["id"]):
                    continue
                self.table.set(state["id"], "status", state["phase"])
                seconds = state["seconds"]
                countdown = "—" if seconds is None or state["code"] else f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
                self.table.set(state["id"], "countdown", countdown)
                tag = "good" if state["code"] in OK_RESULTS else "bad" if state["code"] else "ready" if state["ready"] else ""
                self.table.item(state["id"], tags=(tag,))
            success = sum(s["code"] in OK_RESULTS for s in states)
            attention = sum(bool(s["code"]) and s["code"] not in OK_RESULTS for s in states)
            active = sum(not s["closed"] for s in states)
            ready = sum(s["ready"] and not s["code"] for s in states)
            self.summary_var.set(f"本批次 {len(states)} 个账号 · 运行中 {active} · 已就绪 {ready} · 通过 / 成功 {success} · 需查看 {attention}")
            self.show_selected()
        if self.closing and not self.busy() and not self.editor_open():
            self.destroy()
            return
        self.timer = self.root.after(100, self.poll)

    def open_logs(self):
        folder = self.runner.report_path.parent if self.runner and self.runner.report_path else data_dir() / "batch-runs"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))
        except OSError:
            messagebox.showerror("无法打开", f"请手动查看：{folder}", parent=self.root)

    def open_help(self):
        os.startfile(str(Path(__file__).with_name("BATCH_README.md")))

    def error(self, exc):
        messagebox.showerror("请检查配置", str(exc) if isinstance(exc, ValueError) else f"本机操作失败（{type(exc).__name__}）。", parent=self.root)

    def close(self):
        self.closing = True
        if self.editor_open():
            self.editor.close()
        self.stop()
        if not self.busy() and not self.editor_open():
            self.destroy()

    def destroy(self):
        if self.timer is not None:
            self.root.after_cancel(self.timer)
            self.timer = None
        self.root.destroy()


def main(*, preview=False, config_path=None, protocol=False, per_profile_release=None):
    root = tk.Tk()
    root.withdraw()
    try:
        with AccountLease("@application:batch-editor"):
            BatchApp(root, preview=preview, config_path=config_path, protocol=protocol,
                     per_profile_release=per_profile_release)
            root.deiconify()
            root.mainloop()
    except AccountBusy:
        messagebox.showinfo("多账号预约", "多账号窗口已经打开，请使用已有窗口。", parent=root)
        root.destroy()
