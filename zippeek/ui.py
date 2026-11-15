"""Dark Russian UI. Archive members are never extracted or executed."""
import ctypes
import json
import os
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .archive import inspect_archive, verify, format_size
from .instance import TITLE

BG, CARD, FG, MUTED = '#111214', '#1b1d21', '#eef0f3', '#989da8'
EDGE, ACCENT = '#30343b', '#92b9ff'
GOOD, WARN, BAD = '#9cdbb0', '#e9cc87', '#f0a1a1'
SETTINGS = Path(os.environ.get('APPDATA', str(Path.home()))) / 'ZipPeek' / 'settings.json'
LIMITS = {'1 ГиБ': 1024**3, '4 ГиБ': 4*1024**3, '16 ГиБ': 16*1024**3, 'Без лимита': None}
STATUS = {'ok': 'Целостность OK', 'error': 'Ошибка', 'encrypted': 'Нужен пароль',
          'unsupported': 'Метод не поддержан'}


def clean(text):
    return ''.join(char if char >= ' ' else ' ' for char in text)


def summary(check, count):
    if check.complete:
        return f'Целостность подтверждена · записей: {count}', GOOD
    issues = len(check.errors)
    verified = sum(value == 'ok' for value in check.statuses.values())
    state = 'Остановлено' if check.cancelled else 'Достигнут лимит' if check.limited else 'Проверка завершена'
    return f'{state} · OK: {verified} / {count} · ошибки: {issues} · не проверено: {count-verified-issues}', BAD if issues else WARN


def filtered_members(archive, check, query='', attention=False):
    if archive is None:
        return []
    query = query.casefold()
    return [member for member in archive.members
            if query in member.name.casefold()
            and (not attention or check is None or check.statuses.get(member.index) != 'ok')]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(TITLE)
        self.geometry('1000x740')
        self.minsize(820, 640)
        self.configure(bg=BG)
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.busy = False
        self.archive = None
        self.check = None
        self.filter_job = None
        self.insert_job = None
        self.poll_job = None
        self.generation = 0
        self.shown = {}
        try:
            saved = json.loads(SETTINGS.read_text(encoding='utf-8'))
            if not isinstance(saved, dict):
                saved = {}
        except (OSError, ValueError):
            saved = {}
        self.configure_style()
        pad = tk.Frame(self, bg=BG)
        pad.pack(fill='both', expand=True, padx=24, pady=20)
        head = tk.Frame(pad, bg=BG)
        head.pack(fill='x')
        tk.Label(head, text='ZipPeek', bg=BG, fg=FG, font=('Segoe UI Semibold', 24)).pack(side='left')
        tk.Label(head, text='БЕЗ РАСПАКОВКИ', bg='#1e2930', fg='#9abac3',
                 font=('Segoe UI Semibold', 8), padx=10, pady=5).pack(side='right')
        tk.Label(pad, text='Содержимое ZIP, размер после распаковки и проверка целостности.',
                 bg=BG, fg=MUTED, font=('Segoe UI', 10)).pack(anchor='w', pady=(3,18))
        panel = tk.Frame(pad, bg=CARD, padx=16, pady=14)
        panel.pack(fill='x')
        tk.Label(panel, text='ZIP-АРХИВ', bg=CARD, fg=MUTED, font=('Segoe UI Semibold', 8)).pack(anchor='w', pady=(0,7))
        line = tk.Frame(panel, bg=CARD)
        line.pack(fill='x')
        self.path = tk.StringVar(value=str(saved.get('path', '')))
        self.path_entry = ttk.Entry(line, textvariable=self.path)
        self.path_entry.pack(side='left', fill='x', expand=True)
        self.browse = ttk.Button(line, text='Выбрать…', command=self.choose, cursor='hand2')
        self.browse.pack(side='left', padx=(8,0))
        self.load_button = ttk.Button(line, text='Открыть', command=self.load)
        self.load_button.pack(side='left', padx=(8,0))
        actions = tk.Frame(panel, bg=CARD)
        actions.pack(fill='x', pady=(12,0))
        self.check_button = ttk.Button(actions, text='Проверить целостность', style='Primary.TButton',
                                       command=self.start_check, state='disabled', cursor='hand2')
        self.check_button.pack(side='left')
        self.stop_button = ttk.Button(actions, text='Остановить', state='disabled', command=self.stop.set)
        self.stop_button.pack(side='left', padx=8)
        saved_limit = saved.get('limit')
        self.limit = tk.StringVar(value=saved_limit if isinstance(saved_limit, str) and saved_limit in LIMITS else '4 ГиБ')
        self.limit_box = ttk.Combobox(actions, textvariable=self.limit, values=list(LIMITS), state='readonly', width=12)
        self.limit_box.pack(side='right')
        self.limit_box.bind('<MouseWheel>', lambda _event: 'break')
        tk.Label(actions, text='Лимит чтения', bg=CARD, fg=MUTED).pack(side='right', padx=8)
        track = tk.Frame(pad, bg=BG, height=3)
        track.pack(fill='x', pady=(0,14))
        track.pack_propagate(False)
        self.progress = ttk.Progressbar(track, style='Scan.Horizontal.TProgressbar', mode='determinate')
        self.progress.pack(fill='both', expand=True)
        metrics = tk.Frame(pad, bg=BG)
        metrics.pack(fill='x', pady=(0,12))
        self.total = tk.StringVar(value='—')
        self.packed = tk.StringVar(value='—')
        self.unpacked = tk.StringVar(value='—')
        for column, (title, variable) in enumerate([('ЗАПИСЕЙ', self.total), ('АРХИВ НА ДИСКЕ', self.packed),
                                                   ('ПОСЛЕ РАСПАКОВКИ', self.unpacked)]):
            box = tk.Frame(metrics, bg=CARD, padx=12, pady=10)
            box.grid(row=0, column=column, sticky='nsew', padx=(0,8) if column<2 else 0)
            metrics.columnconfigure(column, weight=1, uniform='metric')
            tk.Label(box, text=title, bg=CARD, fg=MUTED, font=('Segoe UI Semibold', 8)).pack(anchor='w')
            tk.Label(box, textvariable=variable, bg=CARD, fg=FG, font=('Segoe UI Semibold', 15)).pack(anchor='w', pady=(3,0))
        self.status = tk.StringVar(value='Выберите архив. Список содержимого не проверяет данные файлов.')
        self.status_label = tk.Label(pad, textvariable=self.status, bg=BG, fg=MUTED, anchor='w', justify='left', wraplength=900)
        self.status_label.pack(fill='x', pady=(0,10))
        pad.bind('<Configure>', lambda event: self.status_label.configure(wraplength=max(300, event.width)))
        filter_line = tk.Frame(pad, bg=BG)
        filter_line.pack(fill='x', pady=(0,8))
        tk.Label(filter_line, text='Содержимое', bg=BG, fg=FG, font=('Segoe UI Semibold', 11)).pack(side='left')
        tk.Label(filter_line, text='Фильтр по имени', bg=BG, fg=MUTED).pack(side='left', padx=(20,8))
        self.query = tk.StringVar()
        self.attention = tk.BooleanVar(value=False)
        ttk.Checkbutton(filter_line, text='Требуют внимания', variable=self.attention,
                        command=self.render, cursor='hand2').pack(side='right', padx=(12,0))
        ttk.Entry(filter_line, textvariable=self.query).pack(side='left', fill='x', expand=True)
        self.query.trace_add('write', self.filter_changed)
        self.path.trace_add('write', self.invalidate)
        self.footer = tk.Frame(pad, bg=BG)
        self.footer.pack(side='bottom', fill='x', pady=(12,0))
        tk.Label(self.footer, text='День 84 / 365 · финал недели «Файлы»', bg=BG, fg=MUTED,
                 font=('Segoe UI', 8)).pack(side='left')
        tk.Label(self.footer, text='v1.0.0 · darkshade', bg=BG, fg=MUTED, font=('Segoe UI', 8)).pack(side='right')
        lower = tk.Frame(pad, bg=BG)
        lower.pack(side='bottom', fill='x', pady=(10,0))
        ttk.Button(lower, text='Копировать имя', command=self.copy_name).pack(side='left')
        self.report_button = ttk.Button(lower, text='Копировать отчёт', command=self.copy_report,
                                       state='disabled', cursor='hand2')
        self.report_button.pack(side='right', padx=(10,0))
        self.detail = tk.StringVar(value='Выберите запись, чтобы увидеть подробности')
        ttk.Entry(lower, textvariable=self.detail, state='readonly').pack(side='left', fill='x', expand=True, padx=(10,0))
        table = tk.Frame(pad, bg=EDGE, padx=1, pady=1)
        table.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(table, columns=('name','size','packed','status'), show='headings', selectmode='browse')
        for key, text, width in [('name','Имя внутри архива',400), ('size','Размер',110),
                                  ('packed','Сжато',110), ('status','Проверка',180)]:
            self.tree.heading(key, text=text, anchor='w')
            self.tree.column(key, width=width, minwidth=80, stretch=key=='name')
        vertical = ttk.Scrollbar(table, orient='vertical', command=self.tree.yview)
        horizontal = ttk.Scrollbar(table, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        for tag, color in [('ok',GOOD), ('error',BAD), ('encrypted',WARN), ('unsupported',WARN)]:
            self.tree.tag_configure(tag, foreground=color)
        self.tree.tag_configure('even', background='#1e2126')
        self.tree.bind('<<TreeviewSelect>>', self.select)
        self.empty = tk.Label(self.tree, text='Выберите ZIP-архив\n\nФайлы останутся внутри архива', bg=CARD,
                              fg=MUTED, font=('Segoe UI', 11), justify='center')
        self.empty.place(relx=.5, rely=.52, anchor='center')
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.poll_job = self.after(80, self.poll)

    def configure_style(self):
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('.', background=BG, foreground=FG, fieldbackground=CARD,
                        font=('Segoe UI', 10), bordercolor=EDGE, lightcolor=EDGE, darkcolor=EDGE)
        style.configure('TEntry', padding=(10, 8), borderwidth=1, relief='flat',
                        insertcolor=FG, selectbackground='#354e74', selectforeground=FG)
        style.map('TEntry', bordercolor=[('focus', '#698bbe')],
                  fieldbackground=[('disabled', '#202226'), ('readonly', '#17191d')],
                  foreground=[('disabled', '#6e7480'), ('readonly', MUTED)])
        style.configure('TButton', padding=(13, 8), background='#292d34', foreground=FG,
                        borderwidth=0, relief='flat', focusthickness=0)
        style.map('TButton', background=[('disabled', '#202226'), ('pressed', '#373e48'),
                                        ('active', '#343a43')],
                  foreground=[('disabled', '#626873')], bordercolor=[('focus', '#698bbe')])
        style.configure('Primary.TButton', background=ACCENT, foreground='#101a2a',
                        font=('Segoe UI Semibold', 10))
        style.map('Primary.TButton', background=[('disabled', '#283346'), ('pressed', '#739cdd'),
                                                ('active', '#aecbff')],
                  foreground=[('disabled', '#6c7c94'), ('!disabled', '#101a2a')])
        style.configure('Treeview', background=CARD, fieldbackground=CARD,
                        foreground=FG, rowheight=34, borderwidth=0, relief='flat')
        style.configure('Treeview.Heading', background='#252930', foreground=MUTED,
                        font=('Segoe UI Semibold', 9), padding=(10, 9), borderwidth=0, relief='flat')
        style.map('Treeview.Heading', background=[('active', '#2b3038')])
        style.map('Treeview', background=[('selected', '#30486b')], foreground=[('selected', '#ffffff')])
        for orient in ('Vertical', 'Horizontal'):
            name = orient + '.TScrollbar'
            style.configure(name, background='#383d46', troughcolor=CARD, arrowcolor=MUTED,
                            borderwidth=0, arrowsize=12, gripcount=0, relief='flat')
            style.map(name, background=[('active', '#505967'), ('pressed', '#637083')])
        style.configure('Scan.Horizontal.TProgressbar', background=ACCENT, troughcolor=BG,
                        borderwidth=0, lightcolor=ACCENT, darkcolor=ACCENT, thickness=3)
        style.configure('TCombobox', padding=6, arrowcolor=FG)
        style.map('TCombobox', fieldbackground=[('readonly',CARD)], foreground=[('readonly',FG)])
        style.configure('TCheckbutton', background=BG, foreground=MUTED, padding=(4,6),
                        indicatorbackground=CARD, indicatorforeground=FG)
        style.map('TCheckbutton', background=[('active',BG)], foreground=[('selected',FG)],
                  indicatorbackground=[('selected','#5278ad')])

    def choose(self):
        path = filedialog.askopenfilename(parent=self, filetypes=[('ZIP-архивы','*.zip'), ('Все файлы','*.*')])
        if path:
            self.path.set(path)
            self.load()

    def invalidate(self, *_):
        if self.busy:
            return
        self.archive = self.check = None
        self.clear()
        self.total.set('—')
        self.packed.set('—')
        self.unpacked.set('—')
        self.check_button.configure(state='disabled')
        self.report_button.configure(state='disabled')
        self.status.set('Нажмите «Открыть», чтобы прочитать содержимое.')
        self.status_label.configure(fg=MUTED)
        self.empty.configure(text='Архив ещё не открыт')

    def clear(self):
        if self.insert_job is not None:
            self.after_cancel(self.insert_job)
            self.insert_job = None
        self.generation += 1
        self.tree.delete(*self.tree.get_children())
        self.shown.clear()
        self.empty.place(relx=.5, rely=.52, anchor='center')
        self.detail.set('Выберите запись, чтобы увидеть подробности')

    def set_busy(self, busy):
        self.busy = busy
        for widget in (self.path_entry, self.load_button, self.browse):
            widget.configure(state='disabled' if busy else 'normal')
        self.check_button.configure(state='normal' if self.archive and not busy else 'disabled')
        self.report_button.configure(state='normal' if self.archive and not busy else 'disabled')
        self.stop_button.configure(state='normal' if busy else 'disabled')
        self.limit_box.configure(state='disabled' if busy else 'readonly')
        if busy:
            self.progress.configure(mode='indeterminate')
            self.progress.start(15)
        else:
            self.progress.stop()
            self.progress.configure(mode='determinate', value=0)

    def work(self, kind, operation):
        self.stop.clear()
        self.set_busy(True)
        def worker():
            try:
                result = operation()
                self.events.put((kind, result))
            except InterruptedError:
                self.events.put(('cancelled', None))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def load(self):
        if self.busy:
            return
        path = self.path.get().strip().strip('"')
        if not path:
            return
        self.archive = self.check = None
        self.clear()
        self.total.set('—')
        self.packed.set('—')
        self.unpacked.set('—')
        self.empty.configure(text='Читаю список содержимого…')
        self.status.set('Читаю заголовки архива…')
        self.status_label.configure(fg=MUTED)
        self.work('loaded', lambda: inspect_archive(path, self.stop))

    def start_check(self):
        if self.busy or self.archive is None:
            return
        self.check = None
        self.render()
        self.status.set('Проверяю данные и CRC…')
        self.status_label.configure(fg=ACCENT)
        snapshot = self.archive
        limit = LIMITS[self.limit.get()]
        last = [0.0]
        def progress(index, read_bytes):
            now = time.monotonic()
            if now-last[0] > .15:
                last[0] = now
                self.events.put(('progress', (index, read_bytes)))
        self.work('checked', lambda: verify(snapshot, self.stop, limit, progress))

    def poll(self):
        self.poll_job = None
        for _ in range(40):
            try:
                kind, data = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'progress':
                self.status.set(f'Проверка записи {data[0]+1} · прочитано: {format_size(data[1])}')
                continue
            if kind == 'loaded':
                self.archive = data
                self.total.set(str(len(data.members)))
                self.packed.set(format_size(data.packed))
                self.unpacked.set(format_size(data.size))
                self.status.set('Содержимое прочитано. Целостность ещё не проверена.')
                self.render()
            elif kind == 'checked':
                self.check = data
                text, color = summary(data, len(self.archive.members))
                self.status.set(text)
                self.status_label.configure(fg=color)
                self.render()
            elif kind == 'cancelled':
                self.status.set('Чтение содержимого остановлено. Архив не проверен.')
                self.status_label.configure(fg=MUTED)
                self.empty.configure(text='Чтение остановлено\n\nНажмите «Открыть», чтобы начать заново')
            else:
                self.check = None
                self.status.set('Ошибка: ' + data)
                self.status_label.configure(fg=BAD)
                self.render()
                if self.archive is None:
                    self.empty.configure(text='Архив не прочитан\n\nОн может быть повреждён или недоступен')
            self.set_busy(False)
        self.poll_job = self.after(80, self.poll)

    def filter_changed(self, *_):
        if self.filter_job is not None:
            self.after_cancel(self.filter_job)
        self.filter_job = self.after(160, self.render)

    def render(self):
        if self.filter_job is not None:
            self.after_cancel(self.filter_job)
            self.filter_job = None
        self.clear()
        if self.archive is None:
            return
        query = self.query.get()
        members = filtered_members(self.archive, self.check, query, self.attention.get())
        if not members:
            if not self.archive.members:
                text = 'Архив пуст'
            elif query:
                text = 'Нет совпадений с выбранными фильтрами'
            else:
                text = 'Все записи прошли проверку'
            self.empty.configure(text=text)
            return
        self.empty.place_forget()
        self.insert_batch(members, 0, self.generation)

    def insert_batch(self, members, offset, generation):
        if generation != self.generation:
            return
        self.insert_job = None
        for position, member in enumerate(members[offset:offset+200], start=offset):
            state = self.check.statuses.get(member.index) if self.check else None
            label = STATUS.get(state, 'Нужен пароль' if member.encrypted else 'Не проверено')
            tags = (('even',) if position%2==0 else ()) + ((state,) if state else ())
            key = str(member.index)
            self.tree.insert('', 'end', iid=key, values=(clean(member.name), format_size(member.size),
                                                       format_size(member.packed), label), tags=tags)
            self.shown[key] = member
        if offset+200 < len(members):
            self.insert_job = self.after(1, self.insert_batch, members, offset+200, generation)

    def select(self, _event=None):
        selected = self.tree.selection()
        member = self.shown.get(selected[0]) if selected else None
        if member:
            error = self.check.errors.get(member.index, '') if self.check else ''
            self.detail.set(clean(member.name + (' · '+error if error else f' · метод {member.method}')))
        else:
            self.detail.set('Выберите запись, чтобы увидеть подробности')
        return member

    def copy_name(self):
        member = self.select()
        if member:
            self.set_clipboard(member.name, 'Имя скопировано')

    def set_clipboard(self, text, success):
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
        except (tk.TclError, UnicodeError):
            self.detail.set('Не удалось скопировать. Буфер обмена может быть занят — повторите.')
            return False
        self.detail.set(success)
        return True

    def report_text(self):
        if self.archive is None:
            return ''
        members = filtered_members(self.archive, self.check, self.query.get(), self.attention.get())
        state = summary(self.check, len(self.archive.members))[0] if self.check else 'Целостность не проверена'
        lines = ['ZipPeek', 'Архив: '+json.dumps(self.archive.path, ensure_ascii=False), state,
                 'Состояние окна: '+clean(self.status.get()),
                 f'На диске: {format_size(self.archive.packed)} · после распаковки (заявлено): {format_size(self.archive.size)}',
                 f'Записей всего: {len(self.archive.members)} · в отчёте: {len(members)}',
                 'Фильтр имени: '+json.dumps(self.query.get(), ensure_ascii=False),
                 'Требуют внимания: '+('да' if self.attention.get() else 'нет'), '',
                 '№\tРезультат\tРазмер\tСжато\tИмя (JSON)\tПодробности (JSON)']
        for member in members:
            status = self.check.statuses.get(member.index) if self.check else None
            label = STATUS.get(status, 'Нужен пароль' if member.encrypted else 'Не проверено')
            error = self.check.errors.get(member.index, '') if self.check else ''
            lines.append(f'{member.index+1}\t{label}\t{member.size}\t{member.packed}\t'
                         +json.dumps(member.name, ensure_ascii=False)+'\t'+json.dumps(error, ensure_ascii=False))
        return '\n'.join(lines)

    def copy_report(self):
        if self.archive is None or self.busy:
            return
        text = self.report_text()
        self.set_clipboard(text, 'Отчёт скопирован · учтены текущие фильтры')

    def close(self):
        self.stop.set()
        self.progress.stop()
        for callback in (self.filter_job, self.insert_job, self.poll_job):
            if callback is not None:
                self.after_cancel(callback)
        self.filter_job = self.insert_job = self.poll_job = None
        try:
            SETTINGS.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS.write_text(json.dumps({'path':self.path.get(), 'limit':self.limit.get()}, ensure_ascii=True), encoding='utf-8')
        except (OSError, UnicodeError, ValueError):
            pass
        finally:
            self.destroy()


def run():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except OSError:
        pass
    App().mainloop()
