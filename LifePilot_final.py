import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
import sqlite3
import hashlib
import secrets
import csv
import json
from pathlib import Path
from datetime import date, datetime, timedelta
import shutil

APP_NAME = "LifePilot"
CURRENCY_DEFAULT = "PHP"

# Keep the database in one stable location so running a copied script from a
# different folder does not accidentally create a second, empty account database.
APP_DATA_DIR = Path.home() / ".lifepilot"
try:
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH = APP_DATA_DIR / "lifepilot.db"
    previous_db = Path(__file__).resolve().with_name("lifepilot.db")
    if not DB_PATH.exists() and previous_db.exists() and previous_db.resolve() != DB_PATH.resolve():
        shutil.copy2(previous_db, DB_PATH)
except OSError:
    # Fallback for computers where the home directory is not writable.
    DB_PATH = Path(__file__).resolve().with_name("lifepilot.db")


def password_hash(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt), 210_000).hex()
    return salt, digest


class Database:
    def __init__(self, path=DB_PATH):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.setup()

    def setup(self):
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL COLLATE NOCASE UNIQUE,
            salt TEXT NOT NULL, password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS settings(
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            display_name TEXT NOT NULL DEFAULT '',
            currency TEXT NOT NULL DEFAULT 'PHP',
            opening_balance REAL NOT NULL DEFAULT 0,
            monthly_budget REAL NOT NULL DEFAULT 0,
            warning_percent INTEGER NOT NULL DEFAULT 80,
            theme TEXT NOT NULL DEFAULT 'light'
        );
        CREATE TABLE IF NOT EXISTS tasks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title TEXT NOT NULL, due_date TEXT, priority TEXT NOT NULL DEFAULT 'Medium',
            category TEXT NOT NULL DEFAULT 'Personal', estimate INTEGER NOT NULL DEFAULT 0,
            repeat_rule TEXT NOT NULL DEFAULT '', done INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, done_at TEXT
        );
        CREATE TABLE IF NOT EXISTS transactions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind TEXT NOT NULL, amount REAL NOT NULL, category TEXT NOT NULL,
            tx_date TEXT NOT NULL, description TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS goals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title TEXT NOT NULL, target REAL NOT NULL, unit TEXT NOT NULL,
            due_date TEXT, progress REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS habits(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL, days TEXT NOT NULL DEFAULT '0,1,2,3,4,5,6'
        );
        CREATE TABLE IF NOT EXISTS habit_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            habit_id INTEGER NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
            log_date TEXT NOT NULL, UNIQUE(habit_id, log_date)
        );
        CREATE TABLE IF NOT EXISTS journal(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title TEXT NOT NULL DEFAULT '', mood TEXT NOT NULL DEFAULT 'Neutral',
            body TEXT NOT NULL, entry_date TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title TEXT NOT NULL, event_date TEXT NOT NULL,
            start_time TEXT NOT NULL, end_time TEXT NOT NULL, location TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS reminders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title TEXT NOT NULL, remind_at TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0,
            notified INTEGER NOT NULL DEFAULT 0
        );
        """)
        self.conn.commit()

    def rows(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def row(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    def run(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur.lastrowid

    def register(self, username, password, display_name):
        username = username.strip()
        if len(username) < 2:
            raise ValueError("Username must contain at least 2 characters.")
        if len(username) > 40:
            raise ValueError("Username must be 40 characters or fewer.")
        if any(ch.isspace() for ch in username):
            raise ValueError("Username cannot contain spaces.")
        if len(password) < 8:
            raise ValueError("Password must contain at least 8 characters.")
        salt, digest = password_hash(password)
        try:
            # Create the account and settings together so a partial registration
            # is not left behind if one of the database writes fails.
            with self.conn:
                cur = self.conn.execute(
                    "INSERT INTO users(username,salt,password_hash) VALUES(?,?,?)",
                    (username, salt, digest))
                uid = cur.lastrowid
                self.conn.execute(
                    "INSERT INTO settings(user_id,display_name) VALUES(?,?)",
                    (uid, display_name.strip() or username))
        except sqlite3.IntegrityError as exc:
            if "username" in str(exc).lower():
                raise ValueError("That username is already in use. Choose another one.") from exc
            raise
        return uid

    def login(self, username, password):
        u = self.row("SELECT * FROM users WHERE username=? COLLATE NOCASE",
                     (username.strip(),))
        if not u:
            return None
        _, digest = password_hash(password, u["salt"])
        return u["id"] if secrets.compare_digest(digest, u["password_hash"]) else None


class LifePilot(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LifePilot — Personal Life Management")
        self.geometry("1120x760")
        self.minsize(850, 580)
        self.db = Database()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.user_id = None
        self.username = ""
        self.settings = {}
        self.style = ttk.Style(self)
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass
        self.configure(bg="#f3f6fb")
        self.show_auth()
        self.after(15000, self.check_reminders)

    def clear(self):
        for w in self.winfo_children():
            w.destroy()

    def show_auth(self):
        self.user_id = None
        self.clear()
        outer = tk.Frame(self, bg="#f3f6fb")
        outer.pack(fill="both", expand=True)
        box = tk.Frame(outer, bg="white", padx=34, pady=30,
                       highlightbackground="#dce4ef", highlightthickness=1)
        box.place(relx=.5, rely=.45, anchor="center")
        tk.Label(box, text="LifePilot", font=("Segoe UI", 27, "bold"),
                 fg="#15335b", bg="white").pack(anchor="w")
        tk.Label(box, text="Organize your life. Move forward.", font=("Segoe UI", 11),
                 fg="#65758b", bg="white").pack(anchor="w", pady=(2, 22))
        tabs = ttk.Frame(box)
        tabs.pack(fill="x", pady=(0, 12))
        self.auth_mode = tk.StringVar(value="login")
        ttk.Radiobutton(tabs, text="Log in", value="login", variable=self.auth_mode,
                        command=self.auth_fields).pack(side="left", padx=(0, 12))
        ttk.Radiobutton(tabs, text="Create account", value="register", variable=self.auth_mode,
                        command=self.auth_fields).pack(side="left")
        self.auth_area = ttk.Frame(box)
        self.auth_area.pack(fill="x")
        self.auth_fields()
        tk.Label(box, text="Your account and data are saved on this computer. Use Create account the first time you open LifePilot.",
                 wraplength=360, justify="left", bg="white", fg="#65758b",
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(18, 0))
        tk.Label(box, text=f"Database: {DB_PATH}", wraplength=360, justify="left",
                 bg="white", fg="#8a96a8", font=("Segoe UI", 8)).pack(anchor="w", pady=(6, 0))

    def auth_fields(self):
        for w in self.auth_area.winfo_children():
            w.destroy()
        mode = self.auth_mode.get()
        self.auth_name = tk.StringVar()
        self.auth_display = tk.StringVar()
        self.auth_password = tk.StringVar()
        self.auth_confirm = tk.StringVar()
        ttk.Label(self.auth_area, text="Username").pack(anchor="w")
        ttk.Entry(self.auth_area, textvariable=self.auth_name, width=40).pack(fill="x", pady=(3, 10))
        if mode == "register":
            ttk.Label(self.auth_area, text="Display name (optional)").pack(anchor="w")
            ttk.Entry(self.auth_area, textvariable=self.auth_display).pack(fill="x", pady=(3, 10))
        ttk.Label(self.auth_area, text="Password (8+ characters for new accounts)").pack(anchor="w")
        ttk.Entry(self.auth_area, textvariable=self.auth_password, show="•").pack(fill="x", pady=(3, 10))
        if mode == "register":
            ttk.Label(self.auth_area, text="Confirm password").pack(anchor="w")
            ttk.Entry(self.auth_area, textvariable=self.auth_confirm, show="•").pack(fill="x", pady=(3, 12))
        ttk.Button(self.auth_area, text="Create account" if mode == "register" else "Log in",
                   command=self.authenticate).pack(fill="x", ipady=5)

    def authenticate(self):
        username = self.auth_name.get().strip()
        password = self.auth_password.get()
        try:
            if self.auth_mode.get() == "register":
                if password != self.auth_confirm.get():
                    raise ValueError("The passwords do not match.")
                uid = self.db.register(username, password, self.auth_display.get())
            else:
                uid = self.db.login(username, password)
                if not uid:
                    raise ValueError("Wrong username or password.")
            self.user_id = uid
            self.username = username
            self.load_settings()
            self.show_main()
        except ValueError as e:
            messagebox.showerror("LifePilot", str(e), parent=self)
        except sqlite3.Error as e:
            messagebox.showerror(
                "Database problem",
                "LifePilot could not access its database. Close other LifePilot windows, "
                "then reopen the app. If the problem continues, send this message to your helper:\n\n"
                + str(e),
                parent=self,
            )

    def on_close(self):
        try:
            self.db.conn.close()
        except (AttributeError, sqlite3.Error):
            pass
        self.destroy()

    def load_settings(self):
        r = self.db.row("SELECT * FROM settings WHERE user_id=?", (self.user_id,))
        if not r:
            self.db.run("INSERT INTO settings(user_id,display_name) VALUES(?,?)",
                        (self.user_id, self.username))
            r = self.db.row("SELECT * FROM settings WHERE user_id=?", (self.user_id,))
        self.settings = dict(r)

    def show_main(self):
        self.clear()
        self.root_frame = ttk.Frame(self)
        self.root_frame.pack(fill="both", expand=True)
        header = tk.Frame(self.root_frame, bg="#15335b", height=72)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="LifePilot", bg="#15335b", fg="white",
                 font=("Segoe UI", 21, "bold")).pack(side="left", padx=22)
        tk.Label(header, text=f"Welcome, {self.settings.get('display_name') or self.username}",
                 bg="#15335b", fg="#dbeafe", font=("Segoe UI", 11)).pack(side="left", padx=14)
        ttk.Button(header, text="Log out", command=self.show_auth).pack(side="right", padx=18, pady=16)
        self.notebook = ttk.Notebook(self.root_frame)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=12)
        pages = [
            ("Today", self.page_today), ("Tasks", self.page_tasks),
            ("Calendar", self.page_calendar), ("Money", self.page_money),
            ("Goals", self.page_goals), ("Habits", self.page_habits),
            ("Journal", self.page_journal), ("Reports", self.page_reports),
            ("Settings", self.page_settings), ("Search", self.page_search),
        ]
        self.pages = {}
        for title, fn in pages:
            frame = ttk.Frame(self.notebook, padding=12)
            self.notebook.add(frame, text=title)
            self.pages[title] = frame
            fn(frame)

    def rebuild_page(self, title, fn=None):
        frame = self.pages[title]
        for w in frame.winfo_children():
            w.destroy()
        (fn or getattr(self, "page_" + title.lower()))(frame)

    def heading(self, parent, title, subtitle=None):
        ttk.Label(parent, text=title, font=("Segoe UI", 19, "bold")).pack(anchor="w")
        if subtitle:
            ttk.Label(parent, text=subtitle, foreground="#65758b").pack(anchor="w", pady=(0, 8))

    def tree(self, parent, columns, headings, height=10):
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True, pady=6)
        tree = ttk.Treeview(wrap, columns=columns, show="headings", height=height)
        for col, head in zip(columns, headings):
            tree.heading(col, text=head)
            tree.column(col, width=130, anchor="w")
        sy = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        sx = ttk.Scrollbar(wrap, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        tree.grid(row=0, column=0, sticky="nsew")
        sy.grid(row=0, column=1, sticky="ns")
        sx.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        return tree

    def form_dialog(self, title, fields, initial=None):
        """fields: (key,label,type,default); returns dict or None."""
        initial = initial or {}
        win = tk.Toplevel(self)
        win.title(title)
        win.transient(self)
        win.grab_set()
        win.resizable(False, False)
        frame = ttk.Frame(win, padding=18)
        frame.pack(fill="both", expand=True)
        vars_ = {}
        for row, (key, label, typ, default) in enumerate(fields):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=6)
            value = initial.get(key, default)
            if typ == "choice":
                if key not in initial and isinstance(default, (list, tuple)) and default:
                    value = default[0]
                var = tk.StringVar(value=str(value))
                widget = ttk.Combobox(frame, textvariable=var, values=default, state="readonly", width=30)
            else:
                var = tk.StringVar(value=str(value if value is not None else ""))
                widget = ttk.Entry(frame, textvariable=var, width=34)
            widget.grid(row=row, column=1, sticky="ew", pady=6)
            vars_[key] = var
        result = {"value": None}
        buttons = ttk.Frame(frame)
        buttons.grid(row=len(fields), column=0, columnspan=2, sticky="e", pady=(14, 0))
        def submit():
            result["value"] = {k: v.get().strip() for k, v in vars_.items()}
            win.destroy()
        ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="Save", command=submit).pack(side="right")
        win.bind("<Return>", lambda _e: submit())
        win.bind("<Escape>", lambda _e: win.destroy())
        win.update_idletasks()
        win.geometry(f"+{self.winfo_rootx()+180}+{self.winfo_rooty()+100}")
        self.wait_window(win)
        return result["value"]

    def confirm_delete(self, what):
        return messagebox.askyesno("Confirm delete", f"Delete this {what}? This cannot be undone.", parent=self)

    def page_today(self, f):
        self.heading(f, "Today", date.today().strftime("%A, %B %d, %Y"))
        uid = self.user_id
        tasks = self.db.rows("""SELECT * FROM tasks WHERE user_id=? AND done=0
                              ORDER BY CASE priority WHEN 'Critical' THEN 0 WHEN 'High' THEN 1
                              WHEN 'Medium' THEN 2 ELSE 3 END, COALESCE(due_date,'9999-12-31')""", (uid,))
        month = date.today().strftime("%Y-%m")
        tx = self.db.rows("SELECT kind,amount FROM transactions WHERE user_id=? AND substr(tx_date,1,7)=?", (uid, month))
        inc = sum(x["amount"] for x in tx if x["kind"] == "Income")
        exp = sum(x["amount"] for x in tx if x["kind"] == "Expense")
        s = self.settings
        cards = ttk.Frame(f)
        cards.pack(fill="x", pady=12)
        stats = [
            ("Open tasks", str(len(tasks))),
            ("Overdue tasks", str(sum(1 for t in tasks if t["due_date"] and t["due_date"] < date.today().isoformat()))),
            ("Income this month", self.money(inc)),
            ("Expenses this month", self.money(exp)),
            ("Recorded balance", self.money(s.get("opening_balance", 0) + self.db.row(
                "SELECT COALESCE(SUM(CASE WHEN kind='Income' THEN amount ELSE -amount END),0) n FROM transactions WHERE user_id=?",
                (uid,))["n"])),
        ]
        for title, val in stats:
            card = ttk.LabelFrame(cards, text=title, padding=12)
            card.pack(side="left", fill="both", expand=True, padx=4)
            ttk.Label(card, text=val, font=("Segoe UI", 13, "bold")).pack()
        ttk.Label(f, text="Suggested next task", font=("Segoe UI", 13, "bold")).pack(anchor="w", pady=(14, 4))
        if tasks:
            t = tasks[0]
            due = f" · due {t['due_date']}" if t["due_date"] else ""
            ttk.Label(f, text=f"{t['title']}{due} · {t['priority']} priority",
                      wraplength=850).pack(anchor="w")
        else:
            ttk.Label(f, text="You're all caught up. Add a task in the Tasks tab.").pack(anchor="w")
        ttk.Label(f, text="Upcoming reminders", font=("Segoe UI", 13, "bold")).pack(anchor="w", pady=(18, 4))
        reminders = self.db.rows("SELECT * FROM reminders WHERE user_id=? AND done=0 ORDER BY remind_at LIMIT 5", (uid,))
        for r in reminders:
            ttk.Label(f, text=f"• {r['remind_at']} — {r['title']}").pack(anchor="w")
        if not reminders:
            ttk.Label(f, text="No pending reminders.").pack(anchor="w")

    def money(self, amount):
        cur = self.settings.get("currency", CURRENCY_DEFAULT)
        try:
            return f"{cur} {float(amount):,.2f}"
        except (ValueError, TypeError):
            return f"{cur} 0.00"

    def page_tasks(self, f):
        self.heading(f, "Tasks", "Add, prioritize, complete, and remove your tasks.")
        controls = ttk.Frame(f); controls.pack(fill="x", pady=5)
        ttk.Button(controls, text="＋ Add task", command=self.add_task).pack(side="left", padx=(0, 6))
        ttk.Button(controls, text="Mark selected complete / reopen", command=self.toggle_task).pack(side="left", padx=6)
        ttk.Button(controls, text="Edit selected", command=self.edit_task).pack(side="left", padx=6)
        ttk.Button(controls, text="Delete selected", command=self.delete_task).pack(side="left", padx=6)
        self.task_tree = self.tree(f, ("id","title","due","priority","category","estimate","status"),
                                   ("ID","Task","Due date","Priority","Category","Minutes","Status"))
        self.refresh_tasks()

    def refresh_tasks(self):
        if not hasattr(self, "task_tree") or not self.task_tree.winfo_exists(): return
        self.task_tree.delete(*self.task_tree.get_children())
        rows = self.db.rows("SELECT * FROM tasks WHERE user_id=? ORDER BY done, COALESCE(due_date,'9999-12-31'), id",
                            (self.user_id,))
        for x in rows:
            self.task_tree.insert("", "end", values=(x["id"],x["title"],x["due_date"] or "—",
                x["priority"],x["category"],x["estimate"],"Completed" if x["done"] else "Open"))

    def selected_id(self, tree):
        sel = tree.selection()
        if not sel:
            messagebox.showinfo("LifePilot", "Select a row first.", parent=self); return None
        return int(tree.item(sel[0], "values")[0])

    def add_task(self):
        d = self.form_dialog("Add task", [
            ("title","Task title","text",""),("due_date","Due date (YYYY-MM-DD, optional)","text",""),
            ("priority","Priority","choice",["Low","Medium","High","Critical"]),
            ("category","Category","choice",["School","Work","Personal","Family","Health","Financial","Errands","Projects","Other"]),
            ("estimate","Estimated minutes","text","30"),
            ("repeat_rule","Repeat","choice",["None","Daily","Weekly","Monthly"])])
        if not d: return
        if not d["title"]: return messagebox.showerror("LifePilot","Task title is required.")
        if d["due_date"] and not self.valid_date(d["due_date"]): return messagebox.showerror("LifePilot","Use YYYY-MM-DD for the due date.")
        try: estimate = max(0, int(d["estimate"] or 0))
        except ValueError: return messagebox.showerror("LifePilot","Minutes must be a whole number.")
        repeat = "" if d["repeat_rule"] == "None" else d["repeat_rule"]
        self.db.run("INSERT INTO tasks(user_id,title,due_date,priority,category,estimate,repeat_rule) VALUES(?,?,?,?,?,?,?)",
                    (self.user_id,d["title"],d["due_date"] or None,d["priority"],d["category"],estimate,repeat))
        self.refresh_tasks(); self.rebuild_page("Today", self.page_today)

    def toggle_task(self):
        ident = self.selected_id(self.task_tree)
        if ident is None: return
        row = self.db.row("SELECT * FROM tasks WHERE id=? AND user_id=?", (ident,self.user_id))
        done = 0 if row["done"] else 1
        self.db.run("UPDATE tasks SET done=?,done_at=? WHERE id=? AND user_id=?",
                    (done,datetime.now().isoformat(timespec="seconds") if done else None,ident,self.user_id))
        self.refresh_tasks(); self.rebuild_page("Today", self.page_today)

    def edit_task(self):
        ident = self.selected_id(self.task_tree)
        if ident is None: return
        x = self.db.row("SELECT * FROM tasks WHERE id=? AND user_id=?", (ident,self.user_id))
        d = self.form_dialog("Edit task", [
            ("title","Task title","text",x["title"]),("due_date","Due date (YYYY-MM-DD, optional)","text",x["due_date"] or ""),
            ("priority","Priority","choice",["Low","Medium","High","Critical"]),
            ("category","Category","choice",["School","Work","Personal","Family","Health","Financial","Errands","Projects","Other"]),
            ("estimate","Estimated minutes","text",x["estimate"]),
            ("repeat_rule","Repeat","choice",["None","Daily","Weekly","Monthly"])],
            {"priority":x["priority"],"category":x["category"],
             "repeat_rule":x["repeat_rule"] or "None"})
        if not d: return
        if not d["title"]: return messagebox.showerror("LifePilot","Task title is required.")
        if d["due_date"] and not self.valid_date(d["due_date"]): return messagebox.showerror("LifePilot","Use YYYY-MM-DD for the due date.")
        try: estimate=max(0,int(d["estimate"] or 0))
        except ValueError: return messagebox.showerror("LifePilot","Minutes must be a whole number.")
        self.db.run("UPDATE tasks SET title=?,due_date=?,priority=?,category=?,estimate=?,repeat_rule=? WHERE id=? AND user_id=?",
            (d["title"],d["due_date"] or None,d["priority"],d["category"],estimate,
             "" if d["repeat_rule"]=="None" else d["repeat_rule"],ident,self.user_id))
        self.refresh_tasks()

    def delete_task(self):
        ident = self.selected_id(self.task_tree)
        if ident is not None and self.confirm_delete("task"):
            self.db.run("DELETE FROM tasks WHERE id=? AND user_id=?", (ident,self.user_id)); self.refresh_tasks()

    @staticmethod
    def valid_date(s):
        try: datetime.strptime(s, "%Y-%m-%d"); return True
        except (ValueError, TypeError): return False

    def page_calendar(self, f):
        self.heading(f, "Calendar & reminders", "Add events and reminders. LifePilot checks reminders while the app is open.")
        buttons=ttk.Frame(f); buttons.pack(fill="x", pady=5)
        ttk.Button(buttons,text="＋ Add event",command=self.add_event).pack(side="left",padx=(0,6))
        ttk.Button(buttons,text="＋ Add reminder",command=self.add_reminder).pack(side="left",padx=6)
        ttk.Button(buttons,text="Mark reminder done",command=self.complete_reminder).pack(side="left",padx=6)
        ttk.Button(buttons,text="Delete selected item",command=self.delete_calendar_item).pack(side="left",padx=6)
        ttk.Label(f,text="Events",font=("Segoe UI",13,"bold")).pack(anchor="w",pady=(10,0))
        self.event_tree=self.tree(f,("id","title","date","start","end","location"),("ID","Event","Date","Start","End","Location"),height=7)
        self.refresh_events()
        ttk.Label(f,text="Reminders",font=("Segoe UI",13,"bold")).pack(anchor="w",pady=(10,0))
        self.rem_tree=self.tree(f,("id","title","when","status"),("ID","Reminder","Date and time","Status"),height=5)
        self.refresh_reminders()

    def add_event(self):
        today=date.today().isoformat()
        d=self.form_dialog("Add event",[("title","Event title","text",""),("event_date","Date (YYYY-MM-DD)","text",today),
          ("start_time","Start time (HH:MM)","text","09:00"),("end_time","End time (HH:MM)","text","10:00"),("location","Location (optional)","text","")])
        if not d:return
        if not d["title"] or not self.valid_date(d["event_date"]):return messagebox.showerror("LifePilot","Enter a title and a valid date.")
        try:
            datetime.strptime(d["start_time"],"%H:%M"); datetime.strptime(d["end_time"],"%H:%M")
        except ValueError:return messagebox.showerror("LifePilot","Use 24-hour time, for example 14:30.")
        if d["end_time"]<=d["start_time"]:return messagebox.showerror("LifePilot","End time must be after start time.")
        self.db.run("INSERT INTO events(user_id,title,event_date,start_time,end_time,location) VALUES(?,?,?,?,?,?)",
            (self.user_id,d["title"],d["event_date"],d["start_time"],d["end_time"],d["location"]))
        self.refresh_events()

    def add_reminder(self):
        d=self.form_dialog("Add reminder",[("title","Reminder","text",""),("remind_at","When (YYYY-MM-DD HH:MM)","text",datetime.now().strftime("%Y-%m-%d %H:%M"))])
        if not d:return
        try: datetime.strptime(d["remind_at"],"%Y-%m-%d %H:%M")
        except ValueError:return messagebox.showerror("LifePilot","Use YYYY-MM-DD HH:MM, for example 2026-10-22 18:30.")
        if not d["title"]:return messagebox.showerror("LifePilot","Reminder title is required.")
        self.db.run("INSERT INTO reminders(user_id,title,remind_at) VALUES(?,?,?)",(self.user_id,d["title"],d["remind_at"]))
        self.refresh_reminders()

    def refresh_events(self):
        if not hasattr(self,"event_tree") or not self.event_tree.winfo_exists():return
        self.event_tree.delete(*self.event_tree.get_children())
        for x in self.db.rows("SELECT * FROM events WHERE user_id=? ORDER BY event_date,start_time",(self.user_id,)):
            self.event_tree.insert("","end",values=(x["id"],x["title"],x["event_date"],x["start_time"],x["end_time"],x["location"]))

    def refresh_reminders(self):
        if not hasattr(self,"rem_tree") or not self.rem_tree.winfo_exists():return
        self.rem_tree.delete(*self.rem_tree.get_children())
        for x in self.db.rows("SELECT * FROM reminders WHERE user_id=? ORDER BY remind_at",(self.user_id,)):
            status="Done" if x["done"] else ("Due" if x["remind_at"]<=datetime.now().strftime("%Y-%m-%d %H:%M") else "Scheduled")
            self.rem_tree.insert("","end",values=(x["id"],x["title"],x["remind_at"],status))

    def complete_reminder(self):
        ident=self.selected_id(self.rem_tree)
        if ident is not None:
            self.db.run("UPDATE reminders SET done=1 WHERE id=? AND user_id=?",(ident,self.user_id));self.refresh_reminders()

    def delete_calendar_item(self):
        # Delete whichever selected row is active in the calendar tab.
        widget=self.focus_get()
        if widget == getattr(self,"event_tree",None): tree=self.event_tree; table="events"
        else:
            # Prefer selected reminder; otherwise event.
            if getattr(self,"rem_tree",None) and self.rem_tree.selection():tree=self.rem_tree;table="reminders"
            else:tree=self.event_tree;table="events"
        ident=self.selected_id(tree)
        if ident is not None and self.confirm_delete("item"):
            self.db.run(f"DELETE FROM {table} WHERE id=? AND user_id=?",(ident,self.user_id))
            self.refresh_events();self.refresh_reminders()

    def page_money(self, f):
        self.heading(f,"Money","Track recorded income and expenses. This is not connected to your bank.")
        row=ttk.Frame(f);row.pack(fill="x",pady=5)
        ttk.Button(row,text="＋ Add transaction",command=self.add_transaction).pack(side="left",padx=(0,6))
        ttk.Button(row,text="Edit selected",command=self.edit_transaction).pack(side="left",padx=6)
        ttk.Button(row,text="Delete selected",command=self.delete_transaction).pack(side="left",padx=6)
        self.money_tree=self.tree(f,("id","date","type","category","amount","description"),("ID","Date","Type","Category","Amount","Description"))
        self.refresh_money()

    def refresh_money(self):
        if not hasattr(self,"money_tree") or not self.money_tree.winfo_exists():return
        self.money_tree.delete(*self.money_tree.get_children())
        for x in self.db.rows("SELECT * FROM transactions WHERE user_id=? ORDER BY tx_date DESC,id DESC",(self.user_id,)):
            self.money_tree.insert("","end",values=(x["id"],x["tx_date"],x["kind"],x["category"],self.money(x["amount"]),x["description"]))

    def add_transaction(self):
        d=self.form_dialog("Add transaction",[("kind","Type","choice",["Expense","Income"]),("amount","Amount","text",""),
          ("category","Category","choice",["Food","Transportation","Education","Housing","Bills","Shopping","Health","Entertainment","Savings","Other"]),
          ("tx_date","Date (YYYY-MM-DD)","text",date.today().isoformat()),("description","Description (optional)","text","")])
        if not d:return
        try:amount=float(d["amount"])
        except ValueError:return messagebox.showerror("LifePilot","Enter a valid amount.")
        if amount<=0 or not self.valid_date(d["tx_date"]):return messagebox.showerror("LifePilot","Amount must be above zero and date must be YYYY-MM-DD.")
        self.db.run("INSERT INTO transactions(user_id,kind,amount,category,tx_date,description) VALUES(?,?,?,?,?,?)",
          (self.user_id,d["kind"],amount,d["category"],d["tx_date"],d["description"]))
        self.refresh_money();self.rebuild_page("Today",self.page_today);self.rebuild_page("Reports",self.page_reports)

    def edit_transaction(self):
        ident=self.selected_id(self.money_tree)
        if ident is None:return
        x=self.db.row("SELECT * FROM transactions WHERE id=? AND user_id=?",(ident,self.user_id))
        d=self.form_dialog("Edit transaction",[("kind","Type","choice",["Expense","Income"]),("amount","Amount","text",x["amount"]),
          ("category","Category","text",x["category"]),("tx_date","Date (YYYY-MM-DD)","text",x["tx_date"]),("description","Description","text",x["description"])],
          {"kind":x["kind"]})
        if not d:return
        try:amount=float(d["amount"])
        except ValueError:return messagebox.showerror("LifePilot","Enter a valid amount.")
        if amount<=0 or not self.valid_date(d["tx_date"]):return messagebox.showerror("LifePilot","Check amount and date.")
        self.db.run("UPDATE transactions SET kind=?,amount=?,category=?,tx_date=?,description=? WHERE id=? AND user_id=?",
          (d["kind"],amount,d["category"],d["tx_date"],d["description"],ident,self.user_id));self.refresh_money()

    def delete_transaction(self):
        ident=self.selected_id(self.money_tree)
        if ident is not None and self.confirm_delete("transaction"):
            self.db.run("DELETE FROM transactions WHERE id=? AND user_id=?",(ident,self.user_id));self.refresh_money()

    def page_goals(self,f):
        self.heading(f,"Goals","Turn a big goal into measurable progress.")
        bar=ttk.Frame(f);bar.pack(fill="x",pady=5)
        ttk.Button(bar,text="＋ Add goal",command=self.add_goal).pack(side="left",padx=(0,6))
        ttk.Button(bar,text="Record progress",command=self.progress_goal).pack(side="left",padx=6)
        ttk.Button(bar,text="Delete selected",command=self.delete_goal).pack(side="left",padx=6)
        self.goal_tree=self.tree(f,("id","title","progress","target","unit","due","percent"),("ID","Goal","Progress","Target","Unit","Target date","Complete"))
        self.refresh_goals()

    def add_goal(self):
        d=self.form_dialog("Add goal",[("title","Goal title","text",""),("target","Target number","text","1"),("unit","Unit (pages, tasks, PHP...)","text","steps"),("due_date","Target date (YYYY-MM-DD, optional)","text","")])
        if not d:return
        try:target=float(d["target"])
        except ValueError:return messagebox.showerror("LifePilot","Target must be a number.")
        if not d["title"] or target<=0 or (d["due_date"] and not self.valid_date(d["due_date"])):return messagebox.showerror("LifePilot","Check the title, target, and date.")
        self.db.run("INSERT INTO goals(user_id,title,target,unit,due_date) VALUES(?,?,?,?,?)",(self.user_id,d["title"],target,d["unit"] or "steps",d["due_date"] or None))
        self.refresh_goals()

    def refresh_goals(self):
        if not hasattr(self,"goal_tree") or not self.goal_tree.winfo_exists():return
        self.goal_tree.delete(*self.goal_tree.get_children())
        for x in self.db.rows("SELECT * FROM goals WHERE user_id=? ORDER BY id DESC",(self.user_id,)):
            self.goal_tree.insert("","end",values=(x["id"],x["title"],x["progress"],x["target"],x["unit"],x["due_date"] or "—",f"{min(100,x['progress']/x['target']*100):.0f}%"))

    def progress_goal(self):
        ident=self.selected_id(self.goal_tree)
        if ident is None:return
        n=simpledialog.askfloat("Record progress","How much progress would you like to add?",minvalue=0.01,parent=self)
        if n is not None:
            self.db.run("UPDATE goals SET progress=progress+? WHERE id=? AND user_id=?",(n,ident,self.user_id));self.refresh_goals()

    def delete_goal(self):
        ident=self.selected_id(self.goal_tree)
        if ident is not None and self.confirm_delete("goal"):
            self.db.run("DELETE FROM goals WHERE id=? AND user_id=?",(ident,self.user_id));self.refresh_goals()

    def page_habits(self,f):
        self.heading(f,"Habits","Choose the days you plan to do each habit, then log today's completion.")
        row=ttk.Frame(f);row.pack(fill="x",pady=5)
        ttk.Button(row,text="＋ Add habit",command=self.add_habit).pack(side="left",padx=(0,6))
        ttk.Button(row,text="Mark selected done today",command=self.log_habit).pack(side="left",padx=6)
        ttk.Button(row,text="Delete selected",command=self.delete_habit).pack(side="left",padx=6)
        self.habit_tree=self.tree(f,("id","name","days","today","week"),("ID","Habit","Scheduled days","Today","Last 7 days"))
        self.refresh_habits()

    def add_habit(self):
        d=self.form_dialog("Add habit",[("name","Habit name","text",""),("days","Days (0=Sun ... 6=Sat, comma-separated)","text","1,2,3,4,5,6,0")])
        if not d:return
        try:days=sorted(set(int(x.strip()) for x in d["days"].split(",") if x.strip()!=""))
        except ValueError:return messagebox.showerror("LifePilot","Enter days as numbers from 0 to 6.")
        if not d["name"] or not days or any(x<0 or x>6 for x in days):return messagebox.showerror("LifePilot","Enter a name and at least one day from 0 to 6.")
        self.db.run("INSERT INTO habits(user_id,name,days) VALUES(?,?,?)",(self.user_id,d["name"],",".join(map(str,days))))
        self.refresh_habits()

    def refresh_habits(self):
        if not hasattr(self,"habit_tree") or not self.habit_tree.winfo_exists():return
        self.habit_tree.delete(*self.habit_tree.get_children())
        today=date.today(); daynames=["Sun","Mon","Tue","Wed","Thu","Fri","Sat"]
        for h in self.db.rows("SELECT * FROM habits WHERE user_id=? ORDER BY id DESC",(self.user_id,)):
            days=[int(x) for x in h["days"].split(",") if x]
            # Python weekday: Monday=0; convert to Sunday=0.
            sunday_today=(today.weekday()+1)%7
            done=self.db.row("SELECT 1 FROM habit_logs WHERE habit_id=? AND log_date=?",(h["id"],today.isoformat()))
            n=self.db.row("SELECT COUNT(*) n FROM habit_logs WHERE habit_id=? AND log_date>=?",(h["id"],(today-timedelta(days=6)).isoformat()))["n"]
            self.habit_tree.insert("","end",values=(h["id"],h["name"],", ".join(daynames[x] for x in days),
                "Done" if done else ("Scheduled" if sunday_today in days else "Rest day"),f"{n}/7"))

    def log_habit(self):
        ident=self.selected_id(self.habit_tree)
        if ident is None:return
        h=self.db.row("SELECT * FROM habits WHERE id=? AND user_id=?",(ident,self.user_id))
        exists=self.db.row("SELECT id FROM habit_logs WHERE habit_id=? AND log_date=?",(ident,date.today().isoformat()))
        if exists:
            self.db.run("DELETE FROM habit_logs WHERE id=? AND habit_id=?", (exists["id"], ident))
        else:
            self.db.run("INSERT INTO habit_logs(habit_id,log_date) VALUES(?,?)", (ident, date.today().isoformat()))
        self.refresh_habits()

    def delete_habit(self):
        ident=self.selected_id(self.habit_tree)
        if ident is not None and self.confirm_delete("habit"):
            self.db.run("DELETE FROM habits WHERE id=? AND user_id=?",(ident,self.user_id));self.refresh_habits()

    def page_journal(self,f):
        self.heading(f,"Journal","Personal entries and mood notes are stored in your local LifePilot database.")
        row=ttk.Frame(f);row.pack(fill="x",pady=5)
        ttk.Button(row,text="＋ New entry",command=self.add_journal).pack(side="left",padx=(0,6))
        ttk.Button(row,text="View selected",command=self.view_journal).pack(side="left",padx=6)
        ttk.Button(row,text="Delete selected",command=self.delete_journal).pack(side="left",padx=6)
        self.journal_tree=self.tree(f,("id","date","title","mood","preview"),("ID","Date","Title","Mood","Preview"))
        self.refresh_journal()

    def add_journal(self):
        d=self.form_dialog("New journal entry",[("title","Title (optional)","text",""),("mood","Mood","choice",["Neutral","Happy","Excited","Calm","Motivated","Tired","Sad","Stressed","Anxious","Frustrated","Other"]),("body","Entry text","text","")])
        if not d:return
        if not d["body"]:return messagebox.showerror("LifePilot","Write something before saving.")
        self.db.run("INSERT INTO journal(user_id,title,mood,body,entry_date) VALUES(?,?,?,?,?)",
                    (self.user_id,d["title"],d["mood"],d["body"],date.today().isoformat()))
        self.refresh_journal()

    def refresh_journal(self):
        if not hasattr(self,"journal_tree") or not self.journal_tree.winfo_exists():return
        self.journal_tree.delete(*self.journal_tree.get_children())
        for x in self.db.rows("SELECT * FROM journal WHERE user_id=? ORDER BY entry_date DESC,id DESC",(self.user_id,)):
            preview=x["body"].replace("\n"," ")[:90]
            self.journal_tree.insert("","end",values=(x["id"],x["entry_date"],x["title"] or "Untitled",x["mood"],preview))

    def view_journal(self):
        ident=self.selected_id(self.journal_tree)
        if ident is None:return
        x=self.db.row("SELECT * FROM journal WHERE id=? AND user_id=?",(ident,self.user_id))
        win=tk.Toplevel(self);win.title(x["title"] or "Journal entry");win.geometry("560x430")
        ttk.Label(win,text=f"{x['entry_date']} · {x['mood']}",padding=10).pack(anchor="w")
        text=tk.Text(win,wrap="word",padx=12,pady=12);text.pack(fill="both",expand=True)
        text.insert("1.0",x["body"]);text.configure(state="disabled")

    def delete_journal(self):
        ident=self.selected_id(self.journal_tree)
        if ident is not None and self.confirm_delete("journal entry"):
            self.db.run("DELETE FROM journal WHERE id=? AND user_id=?",(ident,self.user_id));self.refresh_journal()

    def page_reports(self,f):
        self.heading(f,"Reports","A summary of the information you have recorded.")
        uid=self.user_id
        tasks=self.db.rows("SELECT done,due_date,estimate FROM tasks WHERE user_id=?",(uid,))
        tx=self.db.rows("SELECT kind,amount,tx_date FROM transactions WHERE user_id=?",(uid,))
        goals=self.db.rows("SELECT title,progress,target,unit,due_date FROM goals WHERE user_id=?",(uid,))
        today=date.today().isoformat();month=date.today().strftime("%Y-%m")
        inc=sum(x["amount"] for x in tx if x["kind"]=="Income" and x["tx_date"].startswith(month))
        exp=sum(x["amount"] for x in tx if x["kind"]=="Expense" and x["tx_date"].startswith(month))
        done=sum(x["done"] for x in tasks);overdue=sum(1 for x in tasks if not x["done"] and x["due_date"] and x["due_date"]<today)
        box=ttk.LabelFrame(f,text="Productivity",padding=12);box.pack(fill="x",pady=8)
        ttk.Label(box,text=f"Total tasks: {len(tasks)}    Completed: {done}    Open: {len(tasks)-done}    Overdue: {overdue}").pack(anchor="w")
        rate=(done/len(tasks)*100) if tasks else 0
        ttk.Label(box,text=f"Task completion rate: {rate:.0f}%    Estimated open work: {sum(x['estimate'] for x in tasks if not x['done'])} minutes").pack(anchor="w",pady=(5,0))
        box=ttk.LabelFrame(f,text=f"Money this month ({month})",padding=12);box.pack(fill="x",pady=8)
        ttk.Label(box,text=f"Income: {self.money(inc)}    Expenses: {self.money(exp)}    Net: {self.money(inc-exp)}").pack(anchor="w")
        box=ttk.LabelFrame(f,text="Goals",padding=12);box.pack(fill="both",expand=True,pady=8)
        if not goals: ttk.Label(box,text="No goals recorded yet.").pack(anchor="w")
        for g in goals:
            p=min(100,g["progress"]/g["target"]*100) if g["target"] else 0
            ttk.Label(box,text=f"{g['title']}: {g['progress']:g}/{g['target']:g} {g['unit']} ({p:.0f}%)").pack(anchor="w",pady=3)
        ttk.Button(f,text="Export transactions to CSV",command=self.export_csv).pack(anchor="w",pady=5)

    def export_csv(self):
        path=filedialog.asksaveasfilename(parent=self,defaultextension=".csv",filetypes=[("CSV files","*.csv")],initialfile="lifepilot-transactions.csv")
        if not path:return
        rows=self.db.rows("SELECT tx_date,kind,category,amount,description FROM transactions WHERE user_id=? ORDER BY tx_date DESC",(self.user_id,))
        try:
            with open(path,"w",newline="",encoding="utf-8-sig") as f:
                writer=csv.writer(f);writer.writerow(["Date","Type","Category","Amount","Description"])
                writer.writerows([x["tx_date"],x["kind"],x["category"],f"{x['amount']:.2f}",x["description"]] for x in rows)
            messagebox.showinfo("LifePilot","CSV export completed.",parent=self)
        except OSError as e:messagebox.showerror("LifePilot",f"Could not export: {e}",parent=self)

    def page_settings(self,f):
        self.heading(f,"Settings","Update your display name, currency, and budget.")
        s=self.settings
        form=ttk.Frame(f);form.pack(anchor="nw",fill="x",pady=8)
        fields=[("Display name", "display_name", s.get("display_name","")),
                ("Currency code (e.g. PHP, USD)", "currency", s.get("currency","PHP")),
                ("Opening balance", "opening_balance", str(s.get("opening_balance",0))),
                ("Monthly budget", "monthly_budget", str(s.get("monthly_budget",0))),
                ("Budget warning percent", "warning_percent", str(s.get("warning_percent",80)))]
        self.setting_vars={}
        for i,(label,key,val) in enumerate(fields):
            ttk.Label(form,text=label).grid(row=i,column=0,sticky="w",padx=(0,12),pady=7)
            var=tk.StringVar(value=str(val));self.setting_vars[key]=var
            ttk.Entry(form,textvariable=var,width=36).grid(row=i,column=1,sticky="w",pady=7)
        ttk.Button(form,text="Save settings",command=self.save_settings).grid(row=len(fields),column=1,sticky="w",pady=10)
        ttk.Separator(f).pack(fill="x",pady=12)
        ttk.Label(f,text=f"Signed in as: {self.username}").pack(anchor="w")
        ttk.Label(f,text=f"Database file: {DB_PATH}",wraplength=850).pack(anchor="w",pady=4)
        ttk.Button(f,text="Export full backup (JSON)",command=self.export_backup).pack(anchor="w",pady=8)
        ttk.Button(f,text="Import full backup (JSON)",command=self.import_backup).pack(anchor="w",pady=4)
        ttk.Label(f,text="Keep the database file and backups safe. The database is local to this computer.",
                  foreground="#65758b",wraplength=850).pack(anchor="w",pady=8)

    def save_settings(self):
        d={k:v.get().strip() for k,v in self.setting_vars.items()}
        try:
            opening=float(d["opening_balance"] or 0);budget=float(d["monthly_budget"] or 0);warning=int(d["warning_percent"] or 80)
            if budget<0 or not 1<=warning<=100:raise ValueError
            cur=d["currency"].upper()
            if len(cur)!=3 or not cur.isalpha():raise ValueError
        except ValueError:
            return messagebox.showerror("LifePilot","Check your currency code, amounts, and warning percent (1–100).")
        self.db.run("""UPDATE settings SET display_name=?,currency=?,opening_balance=?,monthly_budget=?,warning_percent=?
                    WHERE user_id=?""",(d["display_name"] or self.username,cur,opening,budget,warning,self.user_id))
        self.load_settings();messagebox.showinfo("LifePilot","Settings saved.");self.rebuild_page("Today",self.page_today)

    def export_backup(self):
        path=filedialog.asksaveasfilename(parent=self,defaultextension=".json",filetypes=[("JSON files","*.json")],initialfile="lifepilot-backup.json")
        if not path:return
        tables=["settings","tasks","transactions","goals","habits","habit_logs","journal","events","reminders"]
        data={"format":"LifePilot backup v1","username":self.username,"tables":{}}
        try:
            for table in tables:
                if table=="settings":rows=self.db.rows("SELECT * FROM settings WHERE user_id=?",(self.user_id,))
                elif table=="habit_logs":
                    rows=self.db.rows("""SELECT hl.* FROM habit_logs hl JOIN habits h ON h.id=hl.habit_id WHERE h.user_id=?""",(self.user_id,))
                else:rows=self.db.rows(f"SELECT * FROM {table} WHERE user_id=?",(self.user_id,))
                data["tables"][table]=[dict(r) for r in rows]
            Path(path).write_text(json.dumps(data,indent=2),encoding="utf-8")
            messagebox.showinfo("LifePilot","Backup exported.")
        except (OSError,sqlite3.Error) as e:messagebox.showerror("LifePilot",f"Backup failed: {e}")

    def import_backup(self):
        path=filedialog.askopenfilename(parent=self,filetypes=[("JSON files","*.json")])
        if not path:return
        if not messagebox.askyesno("Import backup","This will replace this account's current records. Continue?",parent=self):return
        try:
            data=json.loads(Path(path).read_text(encoding="utf-8"))
            if data.get("format")!="LifePilot backup v1":raise ValueError("This is not a LifePilot backup.")
            tables=data["tables"]
            self.db.conn.execute("BEGIN")
            # Delete child records first.
            for table in ["habit_logs","habits","tasks","transactions","goals","journal","events","reminders"]:
                if table=="habit_logs":
                    self.db.conn.execute("DELETE FROM habit_logs WHERE habit_id IN (SELECT id FROM habits WHERE user_id=?)",(self.user_id,))
                else:self.db.conn.execute(f"DELETE FROM {table} WHERE user_id=?",(self.user_id,))
            # Map old habit ids to newly inserted habit ids.
            mapping={}
            for row in tables.get("settings",[]):
                self.db.conn.execute("""UPDATE settings SET display_name=?,currency=?,opening_balance=?,monthly_budget=?,warning_percent=?,theme=?
                    WHERE user_id=?""",(row.get("display_name",self.username),row.get("currency","PHP"),row.get("opening_balance",0),
                    row.get("monthly_budget",0),row.get("warning_percent",80),row.get("theme","light"),self.user_id))
            for table in ["tasks","transactions","goals","journal","events","reminders"]:
                for row in tables.get(table,[]):
                    cols=[c for c in row if c not in ("id","user_id","created_at","done_at","notified")]
                    vals=[row[c] for c in cols]
                    self.db.conn.execute(f"INSERT INTO {table}(user_id,{','.join(cols)}) VALUES({','.join(['?']*(len(cols)+1))})",[self.user_id]+vals)
            for row in tables.get("habits",[]):
                cur=self.db.conn.execute("INSERT INTO habits(user_id,name,days) VALUES(?,?,?)",(self.user_id,row["name"],row["days"]))
                mapping[row["id"]]=cur.lastrowid
            for row in tables.get("habit_logs",[]):
                if row["habit_id"] in mapping:
                    self.db.conn.execute("INSERT OR IGNORE INTO habit_logs(habit_id,log_date) VALUES(?,?)",(mapping[row["habit_id"]],row["log_date"]))
            self.db.conn.commit();self.load_settings();messagebox.showinfo("LifePilot","Backup imported. Reopen tabs to refresh.")
        except Exception as e:
            self.db.conn.rollback();messagebox.showerror("LifePilot",f"Could not import backup: {e}")

    def page_search(self,f):
        self.heading(f,"Search","Search your tasks, transactions, goals, habits, journal, events, and reminders.")
        bar=ttk.Frame(f);bar.pack(fill="x",pady=6)
        self.search_var=tk.StringVar()
        entry=ttk.Entry(bar,textvariable=self.search_var);entry.pack(side="left",fill="x",expand=True)
        ttk.Button(bar,text="Search",command=self.run_search).pack(side="left",padx=6)
        self.search_tree=self.tree(f,("type","date","title","details"),("Type","Date","Title","Details"))
        entry.bind("<Return>",lambda _e:self.run_search())

    def run_search(self):
        if not hasattr(self,"search_tree") or not self.search_tree.winfo_exists():return
        q=self.search_var.get().strip().lower()
        self.search_tree.delete(*self.search_tree.get_children())
        if not q:return
        sources=[
            ("Task","SELECT title,COALESCE(due_date,''),category FROM tasks WHERE user_id=?"),
            ("Goal","SELECT title,COALESCE(due_date,''),unit FROM goals WHERE user_id=?"),
            ("Habit","SELECT name,'','' FROM habits WHERE user_id=?"),
            ("Journal","SELECT title,entry_date,mood||' '||body FROM journal WHERE user_id=?"),
            ("Event","SELECT title,event_date,location FROM events WHERE user_id=?"),
            ("Reminder","SELECT title,remind_at,'' FROM reminders WHERE user_id=?"),
            ("Money","SELECT category,tx_date,description FROM transactions WHERE user_id=?"),
        ]
        for typ,sql in sources:
            for r in self.db.rows(sql,(self.user_id,)):
                if q in " ".join(str(x or "") for x in r).lower():
                    self.search_tree.insert("","end",values=(typ,r[1],r[0],r[2]))

    def check_reminders(self):
        if self.user_id:
            now=datetime.now().strftime("%Y-%m-%d %H:%M")
            due=self.db.rows("SELECT * FROM reminders WHERE user_id=? AND done=0 AND notified=0 AND remind_at<=?",(self.user_id,now))
            for r in due:
                self.db.run("UPDATE reminders SET notified=1 WHERE id=?",(r["id"],))
                messagebox.showinfo("LifePilot reminder",r["title"],parent=self)
            if due and hasattr(self,"rem_tree"):self.refresh_reminders()
        self.after(15000,self.check_reminders)


if __name__ == "__main__":
    app = LifePilot()
    app.mainloop()
