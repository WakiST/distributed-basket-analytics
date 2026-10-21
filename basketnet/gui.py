"""Графический клиент Tkinter; сеть работает вне потока интерфейса."""

import argparse
import json
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .client import Client
from .tasks import OPERATIONS
from .samples import DESCRIPTIONS, SAMPLES

LABELS = {"queued": "В очереди", "running": "Выполняется", "completed": "Завершено", "error": "Ошибка"}
COLORS = {"background": "#f2f5f8", "surface": "#ffffff", "navy": "#142c3e",
          "text": "#203748", "muted": "#708393", "border": "#e2e9ef", "accent": "#087f78"}



class Application:
    def __init__(self, root, host="127.0.0.1", port=9000, notification_port=9002):
        self.root = root
        self.notification_port = notification_port
        self.inbox = queue.Queue()
        self.stop = threading.Event()
        self.handles, self.latest, self.history = {}, {}, {}
        self.monitor_busy = False
        self.refresh_timer = None
        root.title("BasketNet - Аналитика торговых транзакций")
        root.geometry("1280x850")
        root.minsize(1080, 800)
        root.configure(bg=COLORS["background"])
        self.configure_styles()

        header = tk.Frame(root, bg=COLORS["navy"], padx=28, pady=14)
        header.pack(fill="x")
        mark = tk.Canvas(header, width=44, height=44, bg=COLORS["navy"], highlightthickness=0)
        mark.pack(side="left", padx=(0, 14))
        for start, end in (((10, 12), (32, 12)), ((10, 12), (21, 34)), ((32, 12), (21, 34))):
            mark.create_line(*start, *end, fill="#54c7b8", width=2)
        for x, y in ((10, 12), (32, 12), (21, 34)):
            mark.create_oval(x - 5, y - 5, x + 5, y + 5, fill="#80e0cc", outline="")
        branding = tk.Frame(header, bg=COLORS["navy"])
        branding.pack(side="left")
        tk.Label(branding, text="BasketNet", font=("Segoe UI", 25, "bold"), fg="white",
                 bg=COLORS["navy"]).pack(anchor="w")
        tk.Label(branding, text="АНАЛИТИКА ТОРГОВЫХ ТРАНЗАКЦИЙ", font=("Segoe UI", 9),
                 fg="#acc4d3", bg=COLORS["navy"]).pack(anchor="w")
        self.connection_label = tk.Label(header, text="●  Подключение…", padx=16, pady=9,
                                         bg="#254355", fg="#cbdce5", font=("Segoe UI", 10))
        self.connection_label.pack(side="right")

        outer = ttk.Frame(root, padding=(26, 15, 26, 12))
        outer.pack(fill="both", expand=True)
        connection = ttk.Frame(outer)
        connection.pack(fill="x", pady=(0, 15))
        self.host, self.port = tk.StringVar(value=host), tk.StringVar(value=str(port))
        ttk.Label(connection, text="СЕРВЕР", style="Eyebrow.TLabel").pack(side="left", padx=(0, 10))
        ttk.Entry(connection, textvariable=self.host, width=22).pack(side="left")
        ttk.Label(connection, text="Порт", style="Muted.TLabel").pack(side="left", padx=(14, 8))
        ttk.Entry(connection, textvariable=self.port, width=7).pack(side="left")
        ttk.Button(connection, text="Обновить", command=self.refresh, style="Quiet.TButton").pack(side="left", padx=10)
        self.worker_summary = ttk.Label(connection, text="Ожидание воркеров", style="Muted.TLabel")
        self.worker_summary.pack(side="right")

        metrics = ttk.Frame(outer)
        metrics.pack(fill="x", pady=(0, 14))
        self.metric_values = {}
        for index, (status, title, note, color) in enumerate((
            ("queued", "В очереди", "Ожидают свободного узла", "#9b6a22"),
            ("running", "Выполняется", "Сейчас обрабатываются", "#3575be"),
            ("completed", "Завершено", "Результаты готовы", "#087f78"),
            ("error", "Ошибки", "Требуют внимания", "#bf5261"),
        )):
            metrics.columnconfigure(index, weight=1, uniform="metrics")
            card = tk.Frame(metrics, bg=COLORS["surface"], highlightbackground=COLORS["border"], highlightthickness=1)
            card.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 6, 0 if index == 3 else 6))
            tk.Frame(card, bg=color, width=3).pack(side="left", fill="y")
            content = tk.Frame(card, bg="white", padx=16, pady=8)
            content.pack(fill="both", expand=True)
            tk.Label(content, text=title, font=("Segoe UI", 10), fg=COLORS["muted"], bg="white").pack(anchor="w")
            value = tk.Label(content, text="-", font=("Segoe UI", 25, "bold"), fg=color, bg="white")
            value.pack(anchor="w")
            self.metric_values[status] = value
            tk.Label(content, text=note, font=("Segoe UI", 9), fg=COLORS["muted"], bg="white").pack(anchor="w")

        self.footer = ttk.Label(outer, text="Готов к работе. Результаты можно сохранить в JSON.", style="Muted.TLabel")
        self.footer.pack(side="bottom", anchor="w", pady=(12, 0), fill="x")
        workspace = ttk.Frame(outer)
        workspace.pack(fill="both", expand=True)
        workspace.columnconfigure(0, weight=0, minsize=355)
        workspace.columnconfigure(1, weight=1)
        workspace.rowconfigure(0, weight=1)
        form = ttk.Frame(workspace, style="Card.TFrame", padding=20)
        form.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        form.grid_propagate(False)
        ttk.Label(form, text="Новое задание", style="Section.TLabel").pack(anchor="w")
        ttk.Label(form, text="Настройте расчёт и отправьте в очередь", style="CardMuted.TLabel").pack(anchor="w", pady=(3, 16))
        controls = ttk.Frame(form, style="Card.TFrame")
        controls.pack(fill="x")
        controls.columnconfigure(0, weight=1)
        ttk.Label(controls, text="ОПЕРАЦИЯ", style="CardEyebrow.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 6))
        ttk.Label(controls, text="ПРИОРИТЕТ", style="CardEyebrow.TLabel").grid(row=0, column=1, sticky="w", padx=(12, 0), pady=(0, 6))
        self.operation = tk.StringVar(value="basket_affinity")
        combo = ttk.Combobox(controls, textvariable=self.operation, values=OPERATIONS, state="readonly", width=20)
        combo.grid(row=1, column=0, sticky="ew")
        combo.bind("<<ComboboxSelected>>", lambda event: self.example())
        self.priority = tk.StringVar(value="5")
        ttk.Spinbox(controls, from_=0, to=9, textvariable=self.priority, width=5).grid(row=1, column=1, sticky="ew", padx=(12, 0))
        self.operation_hint = ttk.Label(form, text="", style="CardMuted.TLabel", wraplength=310)
        self.operation_hint.pack(anchor="w", fill="x", pady=(12, 14))
        ttk.Label(form, text="ВХОДНЫЕ ДАННЫЕ · JSON", style="CardEyebrow.TLabel").pack(anchor="w", pady=(0, 8))
        form_footer = ttk.Frame(form, style="Card.TFrame")
        form_footer.pack(side="bottom", fill="x")
        buttons = ttk.Frame(form_footer, style="Card.TFrame")
        buttons.pack(fill="x", pady=(10, 12))
        ttk.Button(buttons, text="Пример", command=self.example, style="Quiet.TButton").pack(side="left")
        ttk.Button(buttons, text="Открыть JSON", command=self.load_json, style="Quiet.TButton").pack(side="left", padx=(7, 0))
        self.submit_button = ttk.Button(form_footer, text="Отправить задание  →", command=self.submit, style="Accent.TButton")
        self.submit_button.pack(fill="x")
        ttk.Label(form_footer, text="9 - срочно  ·  0 - обычный приоритет", style="CardMuted.TLabel").pack(anchor="center", pady=(10, 0))
        self.editor = self.textbox(form, height=3, readonly=False)

        right = ttk.Frame(workspace)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(0, weight=1, minsize=185)
        right.rowconfigure(1, weight=2, minsize=205)
        right.columnconfigure(0, weight=1)
        tasks = ttk.Frame(right, style="Card.TFrame", padding=(18, 14))
        tasks.grid(row=0, column=0, sticky="nsew", pady=(0, 14))
        heading = ttk.Frame(tasks, style="Card.TFrame")
        heading.pack(fill="x", pady=(0, 10))
        ttk.Label(heading, text="Мои задания", style="Section.TLabel").pack(side="left")
        self.task_count = ttk.Label(heading, text="0 заданий", style="CardMuted.TLabel")
        self.task_count.pack(side="right")
        self.task_tree = self.tree(tasks, ("id", "operation", "priority", "status", "attempt"),
                                   ("ID", "Операция", "Приоритет", "Статус", "Попытка"), (78, 142, 75, 112, 65), height=4)
        self.task_tree.bind("<<TreeviewSelect>>", lambda event: self.show_selected())
        self.empty_tasks = tk.Label(self.task_tree, text="Пока нет заданий\nОтправьте первый расчёт в панели слева",
                                    bg="white", fg=COLORS["muted"], font=("Segoe UI", 11), justify="center")
        self.empty_tasks.place(relx=0.5, rely=0.58, anchor="center")

        details = ttk.Frame(right, style="Card.TFrame", padding=(18, 14))
        details.grid(row=1, column=0, sticky="nsew")
        actions = ttk.Frame(details, style="Card.TFrame")
        actions.pack(fill="x", pady=(0, 10))
        ttk.Label(actions, text="Детали задания", style="Section.TLabel").pack(side="left")
        self.save_result_button = ttk.Button(actions, text="Сохранить результат", command=self.save_result, style="Quiet.TButton", state="disabled")
        self.save_result_button.pack(side="right")
        self.save_handle_button = ttk.Button(actions, text="ID и токен", command=self.save_handle, style="Quiet.TButton", state="disabled")
        self.save_handle_button.pack(side="right", padx=6)
        notebook = ttk.Notebook(details)
        notebook.pack(fill="both", expand=True)
        summary, results, events, workers = [ttk.Frame(notebook, style="Card.TFrame") for _ in range(4)]
        for frame, label in ((summary, "Результат"), (results, "JSON"), (events, "События"), (workers, "Воркеры")):
            notebook.add(frame, text=label)
        self.summary_text = self.textbox(summary, height=6, readonly=True, prose=True)
        self.result_text = self.textbox(results, height=6, readonly=True)
        self.event_text = self.textbox(events, height=6, readonly=True, prose=True)
        self.worker_tree = self.tree(workers, ("id", "state", "task", "heartbeat", "completed", "reason"),
                                     ("Воркер", "Состояние", "Задание", "Пульс, с", "Готово", "Причина"),
                                     (130, 90, 80, 70, 60, 170), height=4)
        self.replace(self.summary_text, "Выберите задание\n\nЗдесь появятся результат расчёта, исполнитель и номер попытки.")
        self.replace(self.result_text, "Результат в формате JSON появится после отправки задания.")
        self.replace(self.event_text, "Изменения статуса появятся здесь автоматически.")
        self.example()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)
        self.refresh_timer = root.after(150, self.refresh)

    def configure_styles(self):
        """Единые цвета, размеры и состояния элементов интерфейса."""
        root = self.root
        style = ttk.Style(root)
        style.theme_use("clam")
        for widget in ("TEntry", "TCombobox", "TSpinbox"):
            root.option_add(f"*{widget}.font", ("Segoe UI", 10))
        root.option_add("*TCombobox*Listbox.font", ("Segoe UI", 11))
        style.configure("TFrame", background=COLORS["background"])
        style.configure("Card.TFrame", background="white")
        style.configure("TLabel", background=COLORS["background"], foreground=COLORS["text"], font=("Segoe UI", 10))
        style.configure("Muted.TLabel", foreground=COLORS["muted"], font=("Segoe UI", 9))
        style.configure("Eyebrow.TLabel", foreground=COLORS["muted"], font=("Segoe UI", 9, "bold"))
        style.configure("CardMuted.TLabel", background="white", foreground=COLORS["muted"], font=("Segoe UI", 9))
        style.configure("CardEyebrow.TLabel", background="white", foreground=COLORS["muted"], font=("Segoe UI", 9, "bold"))
        style.configure("Section.TLabel", background="white", foreground=COLORS["text"], font=("Segoe UI", 13, "bold"))
        style.configure("TButton", font=("Segoe UI", 10), padding=(12, 8), borderwidth=0)
        style.configure("Quiet.TButton", background="#edf3f6", foreground="#37576b", borderwidth=0)
        style.map("Quiet.TButton", background=[("active", "#dce9ee")], foreground=[("disabled", "#a3b3bd")])
        style.configure("Accent.TButton", background=COLORS["accent"], foreground="white", padding=(15, 12), font=("Segoe UI", 11, "bold"))
        style.map("Accent.TButton", background=[("disabled", "#9ebfba"), ("active", "#06675f")], foreground=[("disabled", "#edf5f3")])
        for name in ("TEntry", "TCombobox", "TSpinbox"):
            style.configure(name, padding=7, bordercolor=COLORS["border"], lightcolor=COLORS["border"],
                            darkcolor=COLORS["border"], fieldbackground="white", foreground=COLORS["text"], arrowsize=13)
        style.map("TCombobox", fieldbackground=[("readonly", "white")], selectbackground=[("readonly", "white")],
                  selectforeground=[("readonly", COLORS["text"])])
        style.configure("Treeview", rowheight=33, font=("Segoe UI", 10), borderwidth=0, background="white",
                        fieldbackground="white", foreground=COLORS["text"])
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"), background="#f3f6f8", foreground=COLORS["muted"],
                        padding=(6, 9), relief="flat", borderwidth=0)
        style.map("Treeview", background=[("selected", "#d9efea")], foreground=[("selected", "#15554c")])
        style.configure("TNotebook", background="white", borderwidth=0, tabmargins=0)
        style.configure("TNotebook.Tab", padding=(13, 8), background="white", foreground=COLORS["muted"], borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", "#e5f3ef")], foreground=[("selected", COLORS["accent"])])
        style.configure("Vertical.TScrollbar", background="#d6e0e6", troughcolor="#f5f8fa", borderwidth=0, arrowsize=11)

    def textbox(self, parent, height, readonly=False, prose=False):
        frame = ttk.Frame(parent, style="Card.TFrame")
        frame.pack(fill="both", expand=True)
        text = tk.Text(frame, height=height, width=1, wrap="word", font=("Segoe UI" if prose else "Consolas", 10),
                       bg="white" if readonly else "#f6f8fb", fg=COLORS["text"], insertbackground=COLORS["accent"],
                       selectbackground="#ccebe3", relief="flat", padx=12, pady=12, undo=not readonly,
                       spacing1=2, spacing3=3, highlightthickness=1, highlightbackground=COLORS["border"],
                       highlightcolor=COLORS["accent"])
        text.tag_configure("title", font=("Segoe UI", 15, "bold"), foreground=COLORS["accent"])
        bar = ttk.Scrollbar(frame, command=text.yview)
        text.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        if readonly:
            text.configure(state="disabled")
        return text

    def tree(self, parent, columns, headings, widths, height):
        frame = ttk.Frame(parent, style="Card.TFrame")
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=columns, show="headings", height=height)
        for column, heading, width in zip(columns, headings, widths):
            tree.heading(column, text=heading)
            tree.column(column, width=width, minwidth=45)
        scroll = ttk.Scrollbar(frame, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        for tag, color in (("completed", "#f0f8f4"), ("running", "#edf4fd"), ("queued", "#fcf8ee"), ("error", "#fdeef0")):
            tree.tag_configure(tag, background=color)
        return tree

    def client(self):
        port = int(self.port.get())
        if not 1 <= port <= 65535:
            raise ValueError("Порт должен быть от 1 до 65535")
        return Client(self.host.get().strip(), port, self.notification_port)

    def background(self, target):
        threading.Thread(target=target, daemon=True).start()

    def example(self):
        self.operation_hint.configure(text=DESCRIPTIONS[self.operation.get()])
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", json.dumps(SAMPLES[self.operation.get()], ensure_ascii=False, indent=2))
        self.editor.yview_moveto(0)

    def load_json(self):
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json"), ("Все файлы", "*.*")])
        if path:
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
                self.editor.delete("1.0", "end")
                self.editor.insert("1.0", json.dumps(data, ensure_ascii=False, indent=2))
            except (ValueError, OSError) as exc:
                messagebox.showerror("Ошибка файла", str(exc))

    def submit(self):
        try:
            client, operation = self.client(), self.operation.get()
            data, priority = json.loads(self.editor.get("1.0", "end")), int(self.priority.get())
        except (ValueError, OSError) as exc:
            messagebox.showerror("Проверьте данные", str(exc))
            return
        self.submit_button.configure(state="disabled")
        self.submit_button.configure(text="Отправка…")

        def work():
            try:
                handle = client.submit(operation, data, priority)
                self.inbox.put(("submitted", (handle, client)))
            except Exception as exc:
                self.inbox.put(("submit_error", str(exc)))
        self.background(work)

    def watch(self, handle, client):
        version = 0
        while not self.stop.is_set():
            try:
                for event in client.subscribe(handle, after_version=version, stop=self.stop):
                    version = event["version"]
                    self.inbox.put(("event", event))
                    if event["status"] in ("completed", "error"):
                        return
            except ValueError as exc:
                self.inbox.put(("notice", f"Подписка {handle['task_id'][:8]}: {exc}"))
                return
            except (OSError, EOFError) as exc:
                self.inbox.put(("notice", f"Восстановление подписки {handle['task_id'][:8]}: {exc}"))
            if self.stop.wait(2):
                return

    def refresh(self):
        if self.refresh_timer is not None:
            self.root.after_cancel(self.refresh_timer)
            self.refresh_timer = None
        if self.stop.is_set() or self.monitor_busy:
            return
        try:
            client = self.client()
        except ValueError as exc:
            self.connection_label.configure(text=str(exc))
            self.refresh_timer = self.root.after(2000, self.refresh)
            return
        self.monitor_busy = True

        def work():
            try:
                self.inbox.put(("monitor", client.monitor()))
            except Exception as exc:
                self.inbox.put(("offline", str(exc)))
        self.background(work)

    def poll(self):
        for _ in range(200):
            try:
                kind, data = self.inbox.get_nowait()
            except queue.Empty:
                break
            if kind in ("submitted", "submit_error"):
                self.submit_button.configure(state="normal", text="Отправить задание  →")
                if kind == "submit_error":
                    messagebox.showerror("Отправка не удалась", data)
                else:
                    handle, client = data
                    self.handles[handle["task_id"]] = handle
                    self.footer.configure(text=f"Задание {handle['task_id'][:8]} принято. Ожидаем уведомления брокера.")
                    self.background(lambda h=handle, c=client: self.watch(h, c))
            elif kind == "event":
                task_id = data["task_id"]
                self.latest[task_id] = data
                self.history.setdefault(task_id, []).append(data)
                values = (task_id[:8], data["operation"], data["priority"], LABELS[data["status"]], data["attempt"])
                if self.task_tree.exists(task_id):
                    self.task_tree.item(task_id, values=values, tags=(data["status"],))
                else:
                    self.empty_tasks.place_forget()
                    self.task_tree.insert("", 0, iid=task_id, values=values, tags=(data["status"],))
                    self.task_tree.selection_set(task_id)
                self.task_count.configure(text=f"Всего: {len(self.latest)}")
                self.show_selected()
            elif kind in ("monitor", "offline"):
                self.monitor_busy = False
                if kind == "offline":
                    self.connection_label.configure(text="●  Брокер недоступен", foreground="#ffc4cb", background="#583d49")
                    self.footer.configure(text=data)
                    self.worker_summary.configure(text="Нет связи с сервером")
                    for label in self.metric_values.values():
                        label.configure(text="-")
                else:
                    self.connection_label.configure(text="●  Брокер подключён", foreground="#9de0ca", background="#21493f")
                    counts = data["tasks"]
                    for status, label in self.metric_values.items():
                        label.configure(text=str(counts[status]))
                    active = sum(w["state"] != "dead" for w in data["workers"])
                    busy = sum(w["state"] == "busy" for w in data["workers"])
                    self.worker_summary.configure(text=f"Узлов в сети: {active}  ·  Занято: {busy}")
                    for item in self.worker_tree.get_children():
                        self.worker_tree.delete(item)
                    states = {"idle": "Свободен", "busy": "Работает", "dead": "Отключён"}
                    for worker in data["workers"]:
                        self.worker_tree.insert("", "end", values=(worker["worker_id"], states[worker["state"]],
                            (worker["task_id"] or "-")[:8], worker["heartbeat_age"], worker["completed"], worker["reason"] or "-"))
                self.refresh_timer = self.root.after(2000, self.refresh)
            elif kind == "notice":
                self.footer.configure(text=data)
        if not self.stop.is_set():
            self.root.after(100, self.poll)

    def selected(self):
        selection = self.task_tree.selection()
        return selection[0] if selection else None

    @staticmethod
    def replace(widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def show_selected(self):
        task_id = self.selected()
        if not task_id or task_id not in self.latest:
            return
        event = self.latest[task_id]
        self.save_result_button.configure(state="normal")
        self.save_handle_button.configure(state="normal")
        self.replace(self.summary_text, self.describe_result(event))
        self.summary_text.tag_add("title", "1.0", "1.end")
        self.replace(self.result_text, json.dumps(event, ensure_ascii=False, indent=2))
        lines = []
        for item in self.history[task_id]:
            clock = time.strftime("%H:%M:%S", time.localtime(item["timestamp"]))
            lines.append(f"{clock}  {LABELS[item['status']]}  ·  попытка {item['attempt']}  ·  {item['worker_id'] or '-'}\n{item.get('reason') or item.get('error') or ''}")
        self.replace(self.event_text, "\n".join(lines))

    @staticmethod
    def describe_result(event):
        """Понятное описание результата; полные данные доступны на вкладке JSON."""
        lines = [LABELS[event["status"]],
                 f"{event['operation']}  ·  попытка {event['attempt']}  ·  узел: {event['worker_id'] or '-'}", ""]
        if event["status"] == "error":
            lines.append(event.get("error") or "Не удалось выполнить задание.")
        elif event["status"] == "queued":
            lines.append("Задание ожидает свободного воркера. Статус обновится автоматически.")
        elif event["status"] == "running":
            lines.append("Воркер выполняет расчёт. Результат появится после подтверждения выполнения.")
        else:
            result, operation = event["result"], event["operation"]
            if operation == "basket_affinity":
                lines.append(f"Чеков: {result['basket_count']}  ·  Уникальных товаров: {result['unique_products']}")
                for pair in result["pairs"][:10]:
                    a, b = pair["products"]
                    lines.extend(["", f"{a} + {b}  |  Совместных покупок: {pair['count']}",
                                  f"Доля чеков: {pair['support']:.1%}  ·  Сила связи: {pair['lift']:.2f}",
                                  f"Вероятность {a} → {b}: {pair['confidence_a_to_b']:.1%}"])
                if not result["pairs"]:
                    lines.append("Совместные покупки с заданными условиями не найдены.")
                elif len(result["pairs"]) > 10:
                    lines.extend(["", "Показаны первые 10 пар. Полный список доступен на вкладке JSON."])
            elif operation == "sum" and isinstance(result, dict):
                for key, title in (("subtotal", "Сумма до скидок"), ("item_discount", "Скидки по позициям"),
                                   ("order_discount", "Скидка на чек"), ("discount", "Общая скидка"), ("total", "К оплате")):
                    lines.append(f"{title}: {result[key]}")
            elif operation == "count_words":
                lines.append(f"Всего слов: {result['total']}  ·  Уникальных: {result['unique']}")
                lines.extend(f"{word}: {count}" for word, count in list(result["frequencies"].items())[:20])
                if result["unique"] > 20:
                    lines.append("Показаны первые 20 слов. Полный список доступен на вкладке JSON.")
            elif operation == "hash":
                lines.extend(["Контрольная сумма SHA-256", result["digest"]])
            elif operation == "sort":
                lines.append(f"Отсортировано позиций: {len(result)}")
                for position, item in enumerate(result[:20], 1):
                    value = " · ".join(f"{key}: {value}" for key, value in item.items()) if isinstance(item, dict) else str(item)
                    lines.append(f"{position}. {value}")
                if len(result) > 20:
                    lines.append("Показаны первые 20 позиций. Полный список доступен на вкладке JSON.")
            else:
                lines.append(f"Результат: {result}")
        return "\n".join(lines)

    def save(self, value, filename):
        path = filedialog.asksaveasfilename(defaultextension=".json", initialfile=filename, filetypes=[("JSON", "*.json")])
        if path:
            try:
                Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError as exc:
                messagebox.showerror("Ошибка сохранения", str(exc))

    def save_result(self):
        task_id = self.selected()
        if task_id:
            self.save(self.latest[task_id], f"result-{task_id[:8]}.json")

    def save_handle(self):
        task_id = self.selected()
        if task_id:
            self.save(self.handles[task_id], f"task-{task_id[:8]}.json")

    def close(self):
        self.stop.set()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="Графический клиент BasketNet")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--notification-port", type=int, default=9002)
    args = parser.parse_args()
    root = tk.Tk()
    Application(root, args.host, args.port, args.notification_port)
    root.mainloop()


if __name__ == "__main__":
    main()
