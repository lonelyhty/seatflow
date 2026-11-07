"""School settings dialog; its discovery action is strictly read only."""
from __future__ import annotations

import copy
import queue
import threading
import tkinter as tk
from dataclasses import replace
from tkinter import ttk

from .schools import SchoolEntry, validate_school_target


SCHOOL_CHOICES = ('请选择学校', '河北工业大学（内置）', '其他学校（配置入口）')


class SchoolDialog:
    def __init__(self, parent, settings, apply):
        self.settings = copy.deepcopy(settings)
        self.apply_callback = apply
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.worker = None
        self.catalog = None
        self.catalog_entry = None
        self.timer = None
        self.window = tk.Toplevel(parent)
        self.window.title("学校与放号规则")
        self.window.geometry("920x800")
        self.window.minsize(820, 760)
        self.window.transient(parent)
        self.window.grab_set()
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind('<Escape>', lambda _: self.close())
        names = ("school_name", "entry_url", "campus", "floor", "third_level", "room_id",
                 "release_time", "release_days_before", "days_ahead", "time_unit")
        self.vars = {name: tk.StringVar(value=getattr(settings, name) if name != 'room_id' or settings.room_id else '')
                     for name in names}
        self.last_entry = settings.entry_url
        body = ttk.Frame(self.window, padding=18)
        body.pack(fill='both', expand=True)
        body.columnconfigure(1, weight=1)
        body.columnconfigure(3, weight=1)
        body.rowconfigure(12, weight=1)
        ttk.Label(body, text="先选择学校入口，再登录查询房间。每个账号独立保存学校配置。", wraplength=830).grid(
            row=0, column=0, columnspan=4, sticky='w', pady=(0, 12))
        choice = 0
        if settings.entry_url:
            choice = 1 if SchoolEntry.parse(settings.entry_url).is_hebut else 2
        self.school_choice = tk.StringVar(value=SCHOOL_CHOICES[choice])
        ttk.Label(body, text='选择学校').grid(row=1, column=0, sticky='w', padx=(0, 10), pady=5)
        self.school_selector = ttk.Combobox(body, textvariable=self.school_choice,
                                           values=SCHOOL_CHOICES, state='readonly')
        self.school_selector.grid(row=1, column=1, columnspan=3, sticky='ew', pady=5)
        self.school_selector.bind('<<ComboboxSelected>>', self.choose_school)
        self.entries = []
        for row, name, label in ((2, 'school_name', '学校名称（备注）'), (3, 'entry_url', '预约入口链接')):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky='w', padx=(0, 10), pady=5)
            entry = ttk.Entry(body, textvariable=self.vars[name])
            entry.grid(row=row, column=1, columnspan=3, sticky='ew', pady=5)
            self.entries.append(entry)
        ttk.Label(body, text="其他学校：从学习通本校图书馆页面复制完整链接。只改名称或账号不会切换学校。", wraplength=830).grid(
            row=4, column=0, columnspan=4, sticky='w', pady=(0, 4))
        self.entry_status = tk.StringVar()
        ttk.Label(body, textvariable=self.entry_status, wraplength=830).grid(
            row=5, column=0, columnspan=4, sticky='w', pady=(0, 8))
        self.vars['entry_url'].trace_add('write', self.entry_changed)
        self.entry_changed()
        pairs = ((('campus', '校区 / 馆名'), ('floor', '楼层 / 二级区域')),
                 (('third_level', '三级区域（可空）'), ('room_id', '房间编号（可空）')),
                 (('release_time', '放号时间（北京）'), ('release_days_before', '提前几天放号')),
                 (('days_ahead', '可预约未来天数'), ('time_unit', '时段步长（分钟）')))
        for row, pair in enumerate(pairs, 6):
            for col, (name, label) in enumerate(pair):
                ttk.Label(body, text=label).grid(row=row, column=col*2, sticky='w', padx=(0, 10), pady=5)
                entry = ttk.Entry(body, textvariable=self.vars[name])
                entry.grid(row=row, column=col*2+1, sticky='ew', padx=(0, 18 if col == 0 else 0), pady=5)
                self.entries.append(entry)
        ttk.Label(body, text="例如：提前 1 天、22:30 放号；0 表示使用当天放号。最终以学校房间规则为准。", wraplength=830).grid(
            row=10, column=0, columnspan=4, sticky='w', pady=(4, 8))
        actions = ttk.Frame(body)
        actions.grid(row=11, column=0, columnspan=4, sticky='ew', pady=(0, 8))
        self.query_button = ttk.Button(actions, text='登录并查询该校房间', command=self.query)
        self.query_button.pack(side='left')
        self.status = tk.StringVar(value='查询使用当前账号编辑框中的学习通账号和密码；无需提供 Cookie。')
        ttk.Label(body, textvariable=self.status, wraplength=830).grid(row=13, column=0, columnspan=4, sticky='w', pady=10)
        table_box = ttk.Frame(body)
        table_box.grid(row=12, column=0, columnspan=4, sticky='nsew')
        table_box.rowconfigure(0, weight=1)
        table_box.columnconfigure(0, weight=1)
        columns = ('id', 'campus', 'floor', 'third_level', 'capacity')
        self.table = ttk.Treeview(table_box, columns=columns, show='headings', selectmode='browse', height=6)
        for name, label, width in zip(columns, ('房间编号', '校区 / 馆名', '楼层 / 二级区域', '三级区域', '座位数'), (90, 210, 150, 180, 75)):
            self.table.heading(name, text=label)
            self.table.column(name, width=width, minwidth=65)
        scroll = ttk.Scrollbar(table_box, command=self.table.yview)
        horizontal = ttk.Scrollbar(table_box, orient='horizontal', command=self.table.xview)
        self.table.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        self.table.grid(row=0, column=0, sticky='nsew')
        scroll.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        self.table.bind('<<TreeviewSelect>>', self.select_room)
        footer = ttk.Frame(body)
        footer.grid(row=14, column=0, columnspan=4, sticky='ew')
        self.apply_button = ttk.Button(footer, text='应用到当前账号', command=self.apply)
        self.apply_button.pack(side='right')
        ttk.Button(footer, text='关闭', command=self.close).pack(side='right', padx=8)
        self.timer = self.window.after(100, self.poll)

    def collect(self):
        values = {name: var.get().strip() for name, var in self.vars.items()}
        for name in ('room_id', 'release_days_before', 'days_ahead', 'time_unit'):
            try:
                values[name] = int(values[name] or '0')
            except ValueError:
                raise ValueError('房间编号、提前天数、可预约天数和步长需要填写整数。') from None
        values['entry_url'] = validate_school_target(values['school_name'], values['entry_url']).url
        settings = replace(self.settings, **values)
        return settings

    def entry_changed(self, *_):
        value = self.vars['entry_url'].get().strip()
        if value != self.last_entry:
            self.last_entry = value
            for name in ('campus', 'floor', 'third_level', 'room_id', 'release_time',
                         'release_days_before', 'days_ahead', 'time_unit'):
                self.vars[name].set('')
        try:
            entry = SchoolEntry.parse(value) if value else None
        except ValueError as error:
            self.entry_status.set(str(error))
        else:
            if entry is None:
                self.entry_status.set('尚未设置入口：请先选择学校；其他学校需粘贴本校预约链接。')
            elif entry.is_hebut:
                self.school_choice.set(SCHOOL_CHOICES[1])
                self.entry_status.set('当前入口：河北工业大学。修改学校名称不会改变这个入口。')
            else:
                self.school_choice.set(SCHOOL_CHOICES[2])
                self.entry_status.set('当前入口：自定义学校应用。请登录查询房间，核对是否属于目标学校。')
        if not self.catalog:
            return
        self.catalog = None
        self.catalog_entry = None
        for item in self.table.get_children():
            self.table.delete(item)
        self.status.set('入口已修改，请重新查询该校房间。')

    def choose_school(self, _event=None):
        if self.worker and self.worker.is_alive():
            return
        if self.school_choice.get() == SCHOOL_CHOICES[1]:
            self.restore()
            return
        from .model import Settings
        draft = Settings.new_account()
        for name in self.vars:
            self.vars[name].set(getattr(draft, name) if name in ('school_name', 'entry_url') else '')
        self.catalog = None
        self.catalog_entry = None
        for item in self.table.get_children():
            self.table.delete(item)
        self.status.set('请粘贴目标学校的完整预约入口，再登录查询房间；每所学校首次接入需要提供入口。')

    def restore(self):
        if self.worker and self.worker.is_alive():
            return
        from .model import Settings
        default = Settings()
        for name in self.vars:
            self.vars[name].set(getattr(default, name) if name != 'room_id' else '')
        self.catalog = None
        self.catalog_entry = None
        for item in self.table.get_children():
            self.table.delete(item)
        self.status.set('已恢复河北工大入口和默认规则，请按需要查询房间。')

    def query(self):
        if self.worker and self.worker.is_alive():
            return
        try:
            settings = self.collect()
            settings.validate_account()
        except ValueError as error:
            self.status.set(str(error))
            return
        self.catalog = None
        for item in self.table.get_children():
            self.table.delete(item)
        self.query_button.configure(state='disabled')
        self.school_selector.configure(state='disabled')
        self.apply_button.configure(state='disabled')
        for entry in self.entries:
            entry.configure(state='disabled')
        self.status.set('正在登录并读取房间，请稍候；本次不提交预约。')
        self.worker = threading.Thread(target=self.work, args=(settings,), daemon=True)
        self.catalog_entry = settings.entry_url
        self.worker.start()

    def work(self, settings):
        from .school_catalog import read_school_catalog
        from .protocol_api import ProtocolQueryError
        from .protocol_session import ProtocolError
        from .account_lock import AccountBusy
        try:
            self.events.put(('catalog', read_school_catalog(settings, stop=self.stop)))
        except (ValueError, ProtocolError, ProtocolQueryError, AccountBusy) as error:
            self.events.put(('error', str(error)))
        except Exception as error:
            self.events.put(('error', f'房间查询未完成（{type(error).__name__}）。请检查入口、网络和账号登录方式。'))

    def poll(self):
        self.timer = None
        try:
            kind, value = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            for entry in self.entries:
                entry.configure(state='normal')
            self.query_button.configure(state='normal')
            self.school_selector.configure(state='readonly')
            self.apply_button.configure(state='normal')
            if kind == 'catalog':
                self.catalog = value
                for room in value['rooms']:
                    self.table.insert('', 'end', iid=str(room['id']), values=tuple(
                        room[key] for key in ('id', 'campus', 'floor', 'third_level', 'capacity')))
                self.status.set(f"查询完成：{len(value['rooms'])} 个房间。选择一行填入准确名称和编号，再核对放号规则。")
            else:
                self.status.set(value)
        self.timer = self.window.after(100, self.poll)

    def select_room(self, _event=None):
        selection = self.table.selection()
        if not selection or not self.catalog:
            return
        room = next(room for room in self.catalog['rooms'] if str(room['id']) == selection[0])
        for name in ('campus', 'floor', 'third_level'):
            self.vars[name].set(room[name])
        self.vars['room_id'].set(str(room['id']))
        defaults = self.catalog['default_rules']
        for source, target in (('reserveBeforeTime', 'release_time'), ('reserveBeforeDay', 'release_days_before'),
                               ('reserveBeforeDay', 'days_ahead'), ('timeUnit', 'time_unit')):
            if source in defaults:
                self.vars[target].set(str(defaults[source]))
        self.status.set('已填入所选房间及学校默认放号规则。请核对；正式预约会再次检查该房间的实际规则。')

    def apply(self):
        try:
            settings = self.collect()
            settings.validate_school()
            from .model import parse_time
            parse_time(settings.release_time)
            self.apply_callback(settings)
        except ValueError as error:
            self.status.set(str(error))
            return
        self.close()

    def close(self):
        self.stop.set()
        if self.timer is not None:
            self.window.after_cancel(self.timer)
        self.window.destroy()
