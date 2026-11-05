"""Attendance Automation - desktop app (Tkinter). Watches ONE WhatsApp group and fills the tracker."""
import os, queue, shutil, sys, time, tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, date, time as dtime
from tkinter import ttk, filedialog, messagebox, simpledialog
import qrcode
from PIL import ImageTk
from core import (Store, IST, APP_DIR, clean_id, sender_alias, classify, fmt_time, is_checkin, attendance_kind, parse_timing, suggest, parse_chat,
                  build_import_ops, stated_time, load_checkins, save_checkins, load_attendance_messages,
                  save_attendance_messages, expand_ids, plan_fill, norm)
from tracker import Tracker
from bridge import Bridge
from autostart import set_start_on_login


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Attendance Automation"); self.geometry("900x620")
        self.store, self.bridge = Store(), Bridge()
        self.pending, self.last_msg, self.ready_at = [], 0, None
        self.group_map, self.rows, self.sheet_names = {}, [], []
        self.members, self.show_all = [], False
        self.session_msgs = []                          # group messages received this run (memory only)
        self.senders = {}      # people seen posting in the group (names/ids only, kept in memory)
        self.cl_last = self.store["cl_last_date"]
        self.cl_tick = 0
        self.checkins = load_checkins(); self.checkin_ids = {c["id"] for c in self.checkins}   # linked people's check-ins only
        self.attendance_message_load_error = None
        try:
            self.attendance_entries = load_attendance_messages()
        except (OSError, ValueError) as error:
            self.attendance_entries = []
            self.attendance_message_load_error = error
        self.attendance_message_save_error = None
        self.attendance_entry_by_id = {entry["id"]: entry for entry in self.attendance_entries}
        self.message_after = None
        self.older_active, self.fill_job, self.want_members_ui = False, None, False
        self.manual_fill_active, self.manual_history_pending = False, False
        self.scan_error, self.missing_months = None, set()
        self.connect_attempt, self.connect_qr_seen = 0, False
        self.tracker_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tracker")
        self.worker_results = queue.Queue()
        self.processing = False
        self.cl_job_active = False
        self.daily_catchup_date = None
        self.daily_catchup_retry_at = 0
        self.catchup_scan_active = False
        self.month_setup_date = None
        self.month_setup_active = False
        self.month_setup_retry_at = 0
        self.build_ui(); self.set_title()
        if self.attendance_message_load_error:
            self.log(f"Cannot load saved WhatsApp attendance messages: {self.attendance_message_load_error}. Existing file was left unchanged.")
        self.refresh_messages_tree()
        self.start_whatsapp()
        self.after(300, self.tick)
        self.protocol("WM_DELETE_WINDOW", self.close)

    def close(self):
        self.bridge.stop()
        self.tracker_worker.shutdown(wait=False, cancel_futures=True)
        self.destroy()

    def run_tracker(self, task, callback):
        """Run workbook I/O off the UI thread and deliver its result from tick()."""
        future = self.tracker_worker.submit(task)

        def finished(result):
            try:
                self.worker_results.put((callback, result.result(), None))
            except Exception as error:
                self.worker_results.put((callback, None, error))

        future.add_done_callback(finished)

    def set_title(self):
        self.title("Attendance Automation  -  " + ("TEST MODE (nothing is written)" if self.store["test_mode"] else "LIVE (writing to the tracker)"))

    # ---------- UI ----------
    def build_ui(self):
        nb = ttk.Notebook(self); nb.pack(fill="both", expand=True, padx=6, pady=6)
        t1, t2, t3, t4, t5 = (ttk.Frame(nb, padding=10) for _ in range(5))
        for t, n in ((t1, "1. Connect"), (t2, "2. Group & employees"), (t5, "Messages"), (t3, "3. Settings"), (t4, "Log")):
            nb.add(t, text=n)
        # connect
        self.status = tk.StringVar(value="Starting WhatsApp helper...")
        ttk.Label(t1, textvariable=self.status, font=("Segoe UI", 12, "bold")).pack(anchor="w")
        ttk.Label(t1, text="If a QR appears, scan the latest one: WhatsApp > Settings > Linked devices > Link a device.\nThe app only reads the one group you pick and never sends messages.").pack(anchor="w", pady=6)
        self.stats = tk.StringVar(value="")
        ttk.Label(t1, textvariable=self.stats).pack(anchor="w")
        self.qr_lbl = ttk.Label(t1, text="Waiting for WhatsApp to provide a QR code or restore the saved connection.",
                                width=64, anchor="center")
        self.qr_lbl.pack(pady=8)
        ttk.Button(t1, text="Restart WhatsApp helper", command=self.restart_whatsapp).pack(anchor="w")
        ttk.Button(t1, text="Re-link WhatsApp (one-time: scan QR again, brings the group's full history)", command=self.relink).pack(anchor="w", pady=6)
        # group & employees
        top = ttk.Frame(t2); top.pack(fill="x")
        ttk.Button(top, text="Load my groups", command=lambda: self.bridge.send(cmd="groups")).pack(side="left")
        self.group_cb = ttk.Combobox(top, width=40, state="readonly"); self.group_cb.pack(side="left", padx=6)
        ttk.Button(top, text="Use this group", command=self.pick_group).pack(side="left")
        self.group_lbl = tk.StringVar(value="Group: " + (self.store["group_name"] or "(none selected)"))
        ttk.Label(t2, textvariable=self.group_lbl, font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=4)
        row = ttk.Frame(t2); row.pack(fill="x", pady=(2, 0))
        ttk.Button(row, text="People who posted in the group", command=self.load_members).pack(side="left")
        ttk.Button(row, text="Show tracker employees only", command=self.load_all_members).pack(side="left", padx=6)
        ttk.Label(t2, text="Linking uses the messages people post in the group (required). Ask each team member to post any message, then press the button.\nSheet names come from the CURRENT month's tab only.").pack(anchor="w", pady=(4, 6))
        box = ttk.Frame(t2); box.pack(fill="both", expand=True)
        cv = tk.Canvas(box, highlightthickness=0); sb = ttk.Scrollbar(box, command=cv.yview)
        self.inner = ttk.Frame(cv); self.inner.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.create_window((0, 0), window=self.inner, anchor="nw"); cv.configure(yscrollcommand=sb.set)
        cv.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")
        self.save_links_button = ttk.Button(t2, text="Save links", command=self.save_links)
        self.save_links_button.pack(anchor="e", pady=6)
        # settings
        self.v_path = tk.StringVar(value=self.store["tracker_path"])
        self.v_cl = tk.StringVar(value=self.store["cl_time"])
        self.v_grace = tk.StringVar(value=str(self.store["grace_min"]))
        self.v_hist = tk.StringVar(value=str(self.store["hist_days"]))
        self.v_test = tk.BooleanVar(value=self.store["test_mode"])
        self.v_stated = tk.BooleanVar(value=self.store["use_stated"])
        self.v_start_on_login = tk.BooleanVar(value=self.store["start_on_login"])
        r = ttk.Frame(t3); r.pack(fill="x")
        ttk.Label(r, text="Tracker file (synced DataPattern folder)").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(r, textvariable=self.v_path, width=60).grid(row=0, column=1, padx=6)
        ttk.Button(r, text="Browse", command=lambda: self.v_path.set(filedialog.askopenfilename(filetypes=[("Excel", "*.xlsx")]) or self.v_path.get())).grid(row=0, column=2)
        ttk.Label(r, text="CL fill time (24h, IST)").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(r, textvariable=self.v_cl, width=8).grid(row=1, column=1, sticky="w", padx=6)
        ttk.Label(r, text="Grace minutes before 'Late'").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(r, textvariable=self.v_grace, width=8).grid(row=2, column=1, sticky="w", padx=6)
        ttk.Label(r, text="Days to fill from WhatsApp history (0 = today + yesterday)").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(r, textvariable=self.v_hist, width=8).grid(row=3, column=1, sticky="w", padx=6)
        ttk.Checkbutton(r, text="TEST MODE - show what would be written, change nothing", variable=self.v_test).grid(row=4, column=0, columnspan=3, sticky="w", pady=8)
        ttk.Checkbutton(r, text="Use the time written in the message when someone posts late (e.g. 'reached office at 9:09 AM')", variable=self.v_stated).grid(row=5, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Checkbutton(r, text="Start automatically when Windows starts, unlocks, or wakes", variable=self.v_start_on_login).grid(row=6, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Button(r, text="Save settings", command=self.save_settings).grid(row=7, column=0, sticky="w")
        # messages (attendance-related entries from the selected group are saved locally)
        self.shown = set()
        ttk.Label(t5, text="Attendance messages from the selected group. Linked status follows your saved tracker links; logout is display-only.").pack(anchor="w")
        bf = ttk.Frame(t5); bf.pack(anchor="w", pady=4)
        ttk.Button(bf, text="Fill empty days...", command=self.fill_empty).pack(side="left")
        ttk.Button(bf, text="Read earlier messages now (WhatsApp must be online)", command=self.read_older).pack(side="left", padx=8)
        ttk.Button(bf, text="Import exported chat file...", command=self.import_chat).pack(side="left")
        filter_row = ttk.Frame(t5); filter_row.pack(fill="x", pady=(0, 4))
        ttk.Label(filter_row, text="Show messages from (IST, YYYY-MM-DD HH:MM):").pack(side="left")
        self.message_after_text = tk.StringVar()
        ttk.Entry(filter_row, textvariable=self.message_after_text, width=20).pack(side="left", padx=6)
        ttk.Button(filter_row, text="Apply filter", command=self.apply_message_filter).pack(side="left")
        ttk.Button(filter_row, text="Clear", command=self.clear_message_filter).pack(side="left", padx=6)
        ttk.Button(filter_row, text="Set display name for selected sender...", command=self.set_selected_sender_name).pack(side="left", padx=6)
        mf = ttk.Frame(t5); mf.pack(fill="both", expand=True, pady=4)
        self.tree = ttk.Treeview(mf, columns=("date", "time", "who", "kind", "link", "text"), show="headings")
        for c, h, w in (("date", "Date", 90), ("time", "Time", 85), ("who", "Employee / WhatsApp name", 150),
                        ("kind", "Attendance", 120), ("link", "Tracker link", 85), ("text", "Message", 300)):
            self.tree.heading(c, text=h); self.tree.column(c, width=w, anchor="w")
        sb2 = ttk.Scrollbar(mf, command=self.tree.yview); hsb2 = ttk.Scrollbar(mf, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=sb2.set, xscrollcommand=hsb2.set)
        self.tree.pack(side="top", fill="both", expand=True); sb2.pack(side="right", fill="y"); hsb2.pack(side="bottom", fill="x")
        self.message_tree_items = {}
        self.message_entry_ids = {}
        # log
        self.log_box = tk.Text(t4, state="disabled", wrap="word"); self.log_box.pack(fill="both", expand=True)

    def log(self, s):
        self.log_box.config(state="normal")
        self.log_box.insert("end", f"{datetime.now(IST):%d %b %H:%M:%S}  {s}\n")
        if int(self.log_box.index("end-1c").split(".")[0]) > 1000:
            self.log_box.delete("1.0", "end-1000 lines")
        self.log_box.see("end"); self.log_box.config(state="disabled")

    def start_whatsapp(self):
        self.ready_at = None
        self.connect_attempt += 1
        self.connect_qr_seen = False
        attempt = self.connect_attempt
        self.qr_lbl.config(image="", text="Waiting for WhatsApp to provide a QR code or restore the saved connection.")
        self.bridge.start(self.store["group_jid"])
        self.after(30000, lambda: self.connect_timeout(attempt))

    def restart_whatsapp(self):
        self.status.set("Restarting WhatsApp helper...")
        self.log("Restart requested from the Connect tab.")
        self.start_whatsapp()

    def connect_timeout(self, attempt):
        if attempt != self.connect_attempt or self.ready_at or self.connect_qr_seen:
            return
        self.status.set("No QR received after 30 seconds. Check the Log, then restart or re-link.")
        self.qr_lbl.config(image="", text="No QR received yet.\nCheck Log for the disconnect reason. If no reason is shown, verify PC/phone internet, then restart helper.")
        self.log("Connect timeout: helper did not provide a QR or confirm a saved-session connection within 30 seconds.")

    # ---------- settings / linking ----------
    def save_settings(self):
        try:
            parse_timing(self.v_cl.get()); int(self.v_grace.get()); int(self.v_hist.get())
        except ValueError as e:
            messagebox.showerror("Settings", str(e)); return
        s = self.store
        try:
            set_start_on_login(self.v_start_on_login.get())
        except OSError as e:
            self.v_start_on_login.set(s["start_on_login"])
            messagebox.showerror("Automatic start", str(e))
            return
        was_test = s["test_mode"]
        tracker_path_changed = s["tracker_path"] != self.v_path.get()
        s["tracker_path"], s["cl_time"] = self.v_path.get(), self.v_cl.get()
        s["grace_min"], s["test_mode"], s["hist_days"] = int(self.v_grace.get()), self.v_test.get(), max(0, int(self.v_hist.get()))
        s["use_stated"], s["start_on_login"] = self.v_stated.get(), self.v_start_on_login.get()
        self.set_title()
        if was_test and not s["test_mode"]:             # going live: write today's messages already received this run
            today = datetime.now(IST).date()
            redo = [dict(m, retry=True) for m in self.session_msgs if datetime.fromtimestamp(m["ts"], IST).date() == today]
            if redo:
                self.pending += redo; self.last_msg = 0
                self.log(f"Going live: writing {len(redo)} of today's messages received while in test mode.")
        s.save()
        if tracker_path_changed:
            self.month_setup_date = None
            self.month_setup_retry_at = 0
            self.daily_catchup_date = None
        self.log(f"Settings saved (test mode {'ON' if s['test_mode'] else 'OFF'}, automatic start {'ON' if s['start_on_login'] else 'OFF'}).")
        self.check_month_setup()

    def pick_group(self):
        i = self.group_cb.current()
        if i < 0: return
        gid, name = list(self.group_map.items())[i]
        self.store["group_jid"], self.store["group_name"] = gid, name
        self.store.save(); self.group_lbl.set("Group: " + name)
        self.refresh_messages_tree()
        self.bridge.send(cmd="watch", group=gid); self.log(f"Watching group '{name}'. Other chats are ignored.")

    def load_members(self, show_all=False):
        if not self.store["group_jid"]:
            messagebox.showinfo("Group", "Pick a group first."); return
        if not os.path.exists(self.store["tracker_path"]):
            messagebox.showinfo("Tracker", "Set the tracker file in Settings first."); return
        self.log("Loading employee names from the tracker...")
        path = self.store["tracker_path"]

        def loaded(names, error):
            if error:
                messagebox.showerror("Tracker", str(error)); return
            self.sheet_names = names
            self.show_all = show_all
            if show_all:
                self.want_members_ui = True
                self.bridge.send(cmd="members", group=self.store["group_jid"])
            else:
                self.show_members()

        self.run_tracker(lambda: Tracker(path).names(), loaded)

    def load_all_members(self):
        self.load_members(show_all=True)

    def note_sender(self, ev):
        raw = {ev.get("sender") or "", ev.get("senderAlt") or ""} - {""}
        ids = {clean_id(r) for r in raw}
        if not ids: return
        rec = next((self.senders[i] for i in ids if i in self.senders), None) or {"ids": set(), "raw": set(), "name": ""}
        rec["ids"] |= ids; rec["raw"] |= raw; rec["posted"] = True
        if ev.get("pushName"): rec["name"] = ev["pushName"]
        for i in ids: self.senders[i] = rec

    def show_members(self):
        self.member_render_generation = getattr(self, "member_render_generation", 0) + 1
        generation = self.member_render_generation
        self.save_links_button.config(state="disabled")
        for w in self.inner.winfo_children(): w.destroy()
        self.rows = []
        for i, (h, w) in enumerate((("Name (WhatsApp)", 24), ("Number", 16), ("Sheet name", 24), ("Match", 7), ("Timing", 10))):
            ttk.Label(self.inner, text=h, width=w, font=("Segoe UI", 9, "bold")).grid(row=0, column=i, sticky="w")
        recs, seen = [], set()
        for rec in self.senders.values():
            if id(rec) not in seen: seen.add(id(rec)); recs.append(rec)
        for e in self.store["employees"]:                       # keep saved links visible
            if not any(set(e["ids"]) & r["ids"] for r in recs):
                recs.append({"ids": set(e["ids"]), "raw": set(), "name": e.get("label", "")})
        if self.show_all:                                       # also list silent members (names only where WhatsApp gave one)
            for m in self.members:
                ids = {clean_id(i) for i in m["ids"]}
                rec = next((r for r in recs if ids & r["ids"]), None)
                if rec is None: recs.append({"ids": ids, "raw": set(m["ids"]), "name": m["name"]})
                else:
                    rec["ids"] |= ids; rec["raw"] |= set(m["ids"]); rec["name"] = rec["name"] or m["name"]
            matched = []
            for rec in recs:
                old = next((e for e in self.store["employees"] if set(e["ids"]) & rec["ids"]), None)
                if old:
                    rec["tracker_name"] = old["name"]
                else:
                    suggestion, score, ambiguous = suggest(rec["name"], self.sheet_names) if rec["name"] else ("", 0, False)
                    if not suggestion or score < 0.6 or ambiguous:
                        continue
                    rec["tracker_name"] = suggestion
                matched.append(rec)
            recs = matched
        recs.sort(key=lambda r: (not r.get("posted"), not r["name"], r["name"].lower()))
        if not recs:
            message = ("No group members matched the tracker employee names. Ask team members to post in the group, "
                       "or use 'People who posted in the group' to link them manually."
                       if self.show_all else
                       "Nobody has posted in the group yet. Ask your team to post a message, then press the button again.")
            ttk.Label(self.inner, text=message, wraplength=700).grid(row=1, column=0, columnspan=5, sticky="w", pady=8)

        def render_batch(start):
            if generation != self.member_render_generation or not self.inner.winfo_exists():
                return
            end = min(start + 40, len(recs))
            for index in range(start, end):
                rec = recs[index]
                ids, name = sorted(rec["ids"]), rec["name"]
                old = next((e for e in self.store["employees"] if set(e["ids"]) & rec["ids"]), None)
                phone = next((("+" + clean_id(x)) for x in rec["raw"] if x.endswith("@s.whatsapp.net")), "(hidden id)" if rec["raw"] else "(saved link)")
                sug, sc, amb = suggest(name, self.sheet_names) if name else ("", 0, False)
                pre = old["name"] if old else rec.get("tracker_name", sug if sc >= 0.6 and not amb else "")
                nv, tv = tk.StringVar(value=pre), tk.StringVar(value=old["timing"] if old else "9:00 AM")
                row = index + 1
                ttk.Label(self.inner, text=("\u25cf " if rec.get("posted") else "") + (name or "(no name)"), width=24).grid(row=row, column=0, sticky="w")
                ttk.Label(self.inner, text=phone, width=16).grid(row=row, column=1, sticky="w")
                ttk.Combobox(self.inner, textvariable=nv, values=[""] + self.sheet_names, width=24, state="readonly").grid(row=row, column=2, padx=2)
                ttk.Label(self.inner, text=(f"{sc:.0%}" + ("?" if amb else "")) if sug else "", width=7).grid(row=row, column=3)
                ttk.Entry(self.inner, textvariable=tv, width=10).grid(row=row, column=4)
                self.rows.append((ids, name or "(no name)", nv, tv))
            if end < len(recs):
                self.after(1, lambda: render_batch(end))
            else:
                self.save_links_button.config(state="normal")
                self.log(f"Showing {len(self.rows)} tracker-matched group members. Choose sheet names from the current month, then Save links.")

        render_batch(0)

    def save_links(self):
        emps, used = [], set()
        for ids, saved, nv, tv in self.rows:
            n = nv.get().strip()
            if not n: continue
            if n in used:
                messagebox.showerror("Links", f"'{n}' is linked to two people."); return
            try: parse_timing(tv.get())
            except ValueError as e:
                messagebox.showerror("Timing", f"{n}: {e}"); return
            used.add(n); emps.append({"name": n, "ids": ids, "timing": tv.get().strip(), "label": saved})
        self.store["employees"] = emps
        if not self.store["cl_last_date"]:
            self.cl_last = self.store["cl_last_date"] = (datetime.now(IST).date() - timedelta(1)).isoformat()
        self.store.save()
        self.refresh_messages_tree()
        self.daily_catchup_date = None
        self.month_setup_date = None
        self.month_setup_retry_at = 0
        self.log(f"Saved {len(emps)} linked employees. Everyone else is ignored.")
        self.check_month_setup()
        if self.ready_at:
            self.after(100, self.check_daily_catchup)

    # ---------- events ----------
    def tick(self):
        for _ in range(100):
            try: event = self.bridge.q.get_nowait()
            except queue.Empty: break
            self.on_event(event)
        while not self.worker_results.empty():
            callback, result, error = self.worker_results.get()
            callback(result, error)
        if self.pending and not self.processing and time.time() - self.last_msg > 3: self.process()
        self.check_month_setup()
        self.check_daily_catchup()
        if time.time() - self.cl_tick > 30: self.cl_tick = time.time(); self.cl_check()
        self.after(300, self.tick)

    def on_event(self, ev):
        t = ev.get("type")
        if t == "status":
            self.status.set(ev.get("msg", "Starting WhatsApp helper..."))
        elif t == "qr":
            self.connect_qr_seen = True
            self.status.set("Scan the latest QR code with your phone (it refreshes until linked)")
            self.qr_lbl.img = ImageTk.PhotoImage(qrcode.make(ev["qr"]).resize((280, 280)))
            self.qr_lbl.config(image=self.qr_lbl.img, text="")
        elif t == "ready":
            self.ready_at = time.time(); self.older_active = False; self.status.set("Connected to WhatsApp")
            self.connect_qr_seen = False
            self.qr_lbl.config(image="", text="Connected using your saved WhatsApp session.")
            if self.store["group_jid"]: self.after(4000, lambda: self.bridge.send(cmd="members", group=self.store["group_jid"]))
            self.log("Connected. Daily tracker catch-up will start automatically...")
            self.auto_tries = 0
        elif t == "logged_out":
            self.status.set("WhatsApp login expired - starting a fresh link...")
            self.log("WhatsApp reported that the saved login has expired; requesting a fresh QR.")
            shutil.rmtree(os.path.join(APP_DIR, "wa_auth"), ignore_errors=True)
            self.start_whatsapp()
        elif t == "disconnect":
            self.ready_at = None
            self.daily_catchup_date = None
            self.connect_qr_seen = False
            self.log(f"WhatsApp disconnected (code {ev.get('code')}): {ev.get('reason', 'no reason provided')}")
            self.qr_lbl.config(image="", text="WhatsApp disconnected. Waiting for a retry or checking the Log.")
            if ev.get("code") == 428:
                self.status.set("WhatsApp ended the connection (428). Retrying; if it repeats, check internet and re-link.")
            else:
                self.status.set(f"WhatsApp disconnected (code {ev.get('code')}); see Log for reason.")
        elif t == "transport":
            event = ev.get("event", "websocket")
            if event == "websocket_upgrade":
                self.log(f"WhatsApp transport: secure WebSocket established (HTTP {ev.get('status')}).")
            elif event == "websocket_rejected":
                self.log(f"WhatsApp transport rejected the WebSocket (HTTP {ev.get('status')}): {ev.get('message', '')}")
            elif event == "websocket_close":
                reason = ev.get("reason") or "(server provided no close reason)"
                self.log(f"WhatsApp transport closed (WebSocket code {ev.get('code')}): {reason}")
            elif event == "websocket_error":
                self.log(f"WhatsApp transport error: {ev.get('message', 'unknown WebSocket error')}")
        elif t == "stopped":
            self.ready_at = None
            self.connect_qr_seen = False
            self.qr_lbl.config(
                image="",
                text="WhatsApp could not connect after several tries.\nCheck internet, then use Restart helper. Use Re-link only if you need a fresh QR.")
            if ev.get("code") == 428:
                self.status.set("Connection ended repeatedly (428). Check internet, then restart or re-link.")
                self.log("WhatsApp stopped retrying after repeated 428 disconnects. Check phone/PC internet; if it continues, use Re-link WhatsApp to request a fresh QR.")
            else:
                self.status.set("WhatsApp stopped retrying; see Log, then restart or re-link.")
                self.log(f"WhatsApp stopped retrying after repeated disconnects (code {ev.get('code')}).")
        elif t == "groups":
            self.group_map = {g["id"]: g["subject"] for g in ev["groups"]}
            self.group_cb["values"] = list(self.group_map.values())
        elif t == "members":
            self.members = ev["members"]
            self.refresh_messages_tree()
            if self.want_members_ui: self.want_members_ui = False; self.show_members()
        elif t == "older":
            st, got = ev.get("status"), ev.get("got", 0)
            if st == "progress":
                oldest = datetime.fromtimestamp(ev["oldest"], IST)
                self.status.set(f"Reading earlier messages: {got} received (back to {oldest:%d %b %H:%M})...")
                self.log(f"Received {got} earlier messages so far (back to {oldest:%d %b %H:%M})...")
            elif st == "waiting":
                self.log("Waiting for the first new message in the group to use as a starting point (post any message there). It continues by itself.")
            else:
                self.older_active = False
                self.status.set("Connected to WhatsApp")
                self.log({"not_connected": "WhatsApp is not connected yet. Wait for 'Connected to WhatsApp' on tab 1, then try again.",
                          "no_anchor": "Nothing to start from yet: wait for one new message in the group (or post one), then try again.",
                          "timeout": "WhatsApp did not send earlier messages (is your phone online and WhatsApp up to date?). Try again, or use 'Import exported chat file'.",
                          "done": "Finished reading earlier messages. Results are in the Messages tab."}.get(st, "older: " + str(ev))
                         + (f" ({got} messages received)" if got else ""))
                manual_history = self.manual_history_pending
                self.manual_history_pending = False
                if self.fill_job:
                    self.after(1500, self.fill_preview)
                elif not manual_history and not self.manual_fill_active:
                    self.after(1500, self.auto_fill)
        elif t == "stats":
            self.stats.set(f"Group messages received: {ev['seen']}    Other chats ignored: {ev['dropped']}" + ("" if ev["watching"] else "    (NO GROUP SELECTED)"))
        elif t == "msg": self.receive_messages([ev])
        elif t == "messages": self.receive_messages(ev.get("messages", []))
        elif t in ("error", "closed"):
            if t == "error":
                self.status.set("WhatsApp helper error - see the Log tab")
                self.log(f"WhatsApp helper: {ev.get('msg', 'unknown error')}")
                self.qr_lbl.config(image="", text="WhatsApp helper failed to start or connect.\nSee the Log tab for the specific error.")
            else:
                self.status.set("WhatsApp helper stopped unexpectedly - restart it or check the Log tab.")
                self.log(f"WhatsApp helper exited unexpectedly (exit code {ev.get('returncode')}).")
                self.qr_lbl.config(image="", text="Helper stopped before connecting. Check the Log tab, then restart the helper.")
                self.ready_at = None
                self.older_active = False
            if t == "closed" and self.fill_job: self.after(500, self.fill_preview)

    # ---------- attendance ----------
    def apply_async(self, ops, callback=None):
        if not ops:
            if callback: callback(True, [])
            return
        path = self.store["tracker_path"]
        if not os.path.exists(path):
            self.log("Tracker file not found - set it in Settings.")
            if callback: callback(False, [("error", "tracker file not found")] * len(ops))
            return
        test = self.store["test_mode"]
        def completed(results, error):
            if error:
                res = [("error", str(error))] * len(ops)
            else:
                res = results
            for op, (st, detail) in zip(ops, res):
                what = op["reported"] + " " if op["kind"] == "in" else ""
                note = f" ({op['note']})" if op.get("note") else ""
                if st == "written":
                    outcome = f"written and verified in {os.path.basename(path)} (cloud sync unverified)"
                elif st == "skipped" and detail.startswith("already "):
                    outcome = f"{detail} in local {os.path.basename(path)}; not overwritten"
                else:
                    outcome = f"{st} {detail}".strip()
                self.log(f"{'[TEST] ' if test else ''}{op['name']} {op['date']:%d %b}: {what}{op['status']}{note} -> {outcome}")
            if callback:
                callback(not any(state == "error" for state, _ in res), res)

        self.run_tracker(lambda: Tracker(path).update(ops, dry=test), completed)

    def process(self):
        msgs, self.pending = sorted(self.pending, key=lambda m: m["ts"]), []
        seen, ops, taken, entry_ops = set(self.store["seen_ids"]), [], set(), []
        today, groups, new_ci = datetime.now(IST).date(), self.id_groups(), False
        for m in msgs:
            if (m["id"] in seen or m["id"] in self.shown) and not m.get("retry"): continue
            seen.add(m["id"]); self.shown.add(m["id"])
            kind = attendance_kind(m.get("text", ""))
            if not kind:
                continue
            ids = {clean_id(m.get("sender")), clean_id(m.get("senderAlt"))} - {""}
            emp = self.who_is(ids, groups)
            d = datetime.fromtimestamp(m["ts"], IST)
            entry = self.attendance_entry_by_id.get(m["id"])
            if entry is None:
                continue
            entry["kind"] = kind
            op = None
            if not emp:
                entry["result"] = "Unlinked - tracker unchanged"
            elif kind == "Logged out":
                entry["result"] = "Display only - tracker unchanged"
            else:
                if m["id"] not in self.checkin_ids:        # keep for "Fill empty days"
                    self.checkin_ids.add(m["id"]); new_ci = True
                    self.checkins.append({"id": m["id"], "ts": m["ts"], "name": emp["name"], "text": m["text"][:200]})
                if d.weekday() >= 5: entry["result"] = "Ignored (weekend)"
                elif m.get("hist") and (today - d.date()).days > int(self.store["hist_days"]):
                    entry["result"] = "Old history - not written"
                elif m.get("manual_fill"): entry["result"] = "Saved for Fill empty days preview"
                elif (d.date(), emp["name"]) in taken: entry["result"] = "Ignored (already counted)"
                else:
                    taken.add((d.date(), emp["name"]))
                    t = (stated_time(m["text"], d) if self.store["use_stated"] else None) or d
                    op = {"kind": "in", "date": d.date(), "name": emp["name"], "reported": fmt_time(t),
                          "status": classify(t, emp["timing"], self.store["grace_min"]),
                          "note": f"time written in message; sent {fmt_time(d)}" if t is not d else ""}
                    ops.append(op)
                    entry_ops.append((entry, op))
                    entry["result"] = "Processing tracker entry"
            self.update_message_row(entry)
        if new_ci: save_checkins(self.checkins)
        self.persist_attendance_messages()
        self.processing = True

        def completed(ok, results):
            if (any(m.get("hist") for m in msgs) and not any(m.get("manual_fill") for m in msgs)
                    and not self.older_active
                    and not self.fill_job and not self.manual_fill_active and not self.manual_history_pending):
                self.after(800, self.auto_fill)
            result_by_op = dict(zip(map(id, ops), results))
            for entry, op in entry_ops:
                st, detail = result_by_op.get(id(op), ("error", "no tracker result returned"))
                note = f" ({op['note']})" if op.get("note") else ""
                entry["result"] = f"{op['reported']} {op['status']}{note} -> {st} {detail}".strip()
                self.update_message_row(entry)
            if entry_ops:
                self.persist_attendance_messages()
            if not self.store["test_mode"]:
                if ok:
                    self.store["seen_ids"] = list(seen); self.store.save()
                else:
                    self.pending = [dict(m, retry=True) for m in msgs if m["id"] not in self.store["seen_ids"]] + self.pending
                    self.last_msg = time.time() + 30
            self.processing = False

        self.apply_async(ops, completed)

    # ---------- automatic fill (no buttons) ----------
    def relink(self):
        if not messagebox.askyesno("Re-link WhatsApp", "This signs the app out of WhatsApp and shows a new QR code.\nScan it once (WhatsApp > Linked devices); WhatsApp then sends the group's full history.\n\nContinue?"): return
        self.bridge.stop(); self.ready_at = None; self.older_active = False
        shutil.rmtree(os.path.join(APP_DIR, "wa_auth"), ignore_errors=True)
        self.status.set("Starting a fresh WhatsApp link; waiting for QR...")
        self.qr_lbl.config(image="", text="Requesting a new QR code from WhatsApp...")
        self.after(800, self.start_whatsapp)

    def scan_empties(self, callback):
        """Blank weekdays in the configured WhatsApp history lookback for linked employees."""
        s = self.store
        today = datetime.now(IST).date()
        names = [e["name"] for e in s["employees"]]
        path = s["tracker_path"]

        def completed(result, error):
            if error:
                message = f"Cannot scan tracker for empty days: {error}"
                if message != self.scan_error:
                    self.log(message)
                    self.scan_error = message
                callback([])
                return
            self.scan_error = None
            empties, missing = result
            self.missing_months.intersection_update(missing)
            for month in missing:
                if month not in self.missing_months:
                    self.log(f"No tab '{month:%b - %y}' in the tracker - skipped (copy last month's tab and rename it).")
                self.missing_months.add(month)
            callback(empties)

        def scan():
            tracker = Tracker(path)
            empties, missing = tracker.empties(
                today - timedelta(days=max(1, int(s["hist_days"]))), today, names)
            return empties, missing

        self.run_tracker(scan, completed)

    def check_month_setup(self):
        """Create this month's tab, plus next month's on month-end, without requiring WhatsApp."""
        today = datetime.now(IST).date()
        path = self.store["tracker_path"]
        if (not path or not os.path.isfile(path) or self.month_setup_date == today
                or self.month_setup_active or time.time() < self.month_setup_retry_at):
            return
        self.month_setup_active = True
        employees = [dict(employee) for employee in self.store["employees"]]

        def prepare():
            tracker = Tracker(path)
            created = []
            if tracker.ensure_month(today, employees):
                created.append(today.replace(day=1))
            next_month = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
            if today == next_month - timedelta(days=1) and tracker.ensure_month(next_month, employees):
                created.append(next_month)
            return created

        def completed(created, error):
            self.month_setup_active = False
            if path != self.store["tracker_path"]:
                self.month_setup_date = None
                self.month_setup_retry_at = 0
                self.check_month_setup()
                return
            if error:
                self.month_setup_retry_at = time.time() + 60
                self.log(f"Could not prepare the monthly tracker tab: {error}. Will retry in one minute.")
                return
            self.month_setup_date = today
            for month in created:
                self.log(f"Created tracker tab '{month:%b - %y}' from the previous month layout.")

        self.run_tracker(prepare, completed)

    def auto_catchup(self):
        """Fill blank tracker days from locally saved check-ins without requesting WhatsApp history."""
        s = self.store
        if (not (s["employees"] and os.path.exists(s["tracker_path"]))
                or self.fill_job or self.manual_fill_active or self.manual_history_pending or self.catchup_scan_active): return
        self.catchup_scan_active = True
        self.scan_empties(self.catchup_from_empties)

    def catchup_from_empties(self, empties):
        self.catchup_scan_active = False
        if self.scan_error:
            self.daily_catchup_date = None
            self.daily_catchup_retry_at = time.time() + 60
            self.after(60000, self.check_daily_catchup)
            return
        self.daily_catchup_retry_at = 0
        self.fill_from_empties(empties)
        if empties:
            self.log(
                f"Daily catch-up checked {len(empties)} blank tracker entr{'y' if len(empties) == 1 else 'ies'} "
                "using locally saved check-ins. Earlier WhatsApp history is not requested automatically."
            )

    def check_daily_catchup(self):
        today = datetime.now(IST).date()
        if (self.store["employees"]
                and os.path.isfile(self.store["tracker_path"]) and self.daily_catchup_date != today
                and time.time() >= self.daily_catchup_retry_at
                and not self.fill_job and not self.manual_fill_active and not self.manual_history_pending
                and not self.catchup_scan_active):
            self.daily_catchup_date = today
            mode = "TEST MODE - tracker will not be changed" if self.store["test_mode"] else "LIVE - tracker updates enabled"
            lookback = max(1, int(self.store["hist_days"]))
            self.log(f"Starting offline-capable daily catch-up ({mode}; checking today and {lookback} day(s) back).")
            self.auto_catchup()

    def auto_fill(self):
        """Fill blank weekdays with check-ins we can find (saved + this run). Never marks CL for past days here."""
        s = self.store
        if (self.fill_job or self.manual_fill_active or self.manual_history_pending
                or getattr(self, "auto_fill_active", False)
                or not (s["employees"] and os.path.exists(s["tracker_path"]))): return
        self.auto_fill_active = True

        def scanned(empties):
            self.auto_fill_active = False
            self.fill_from_empties(empties)

        self.scan_empties(scanned)

    def fill_from_empties(self, empties, attempt=0):
        s = self.store
        if not empties: return
        ops, _ = plan_fill(empties, self.fill_events([]), {e["name"]: e["timing"] for e in s["employees"]},
                           s["grace_min"], s["use_stated"], False, cl_time=s["cl_time"])
        seen = self.__dict__.setdefault("auto_seen", set())
        if s["test_mode"]:
            ops = [o for o in ops if (o["date"], o["name"]) not in seen]
            seen.update((o["date"], o["name"]) for o in ops)
        if not ops: return
        self.log(f"Auto-fill: {len(ops)} missing check-in(s) found in saved/received messages.")

        def completed(ok, results):
            if (not ok and not s["test_mode"] and attempt < 2
                    and any(state == "error" for state, _ in results)):
                self.log(f"Auto-fill tracker write failed; retrying in one minute (attempt {attempt + 2} of 3).")
                self.after(60000, lambda: self.fill_from_empties(empties, attempt + 1))

        self.apply_async(ops, completed)

    # ---------- matching helpers ----------
    def id_groups(self):
        """Id-sets that are the same person (phone id vs hidden id), from posters and the group member list."""
        gs = [set(r["ids"]) for r in self.senders.values()]
        gs += [{clean_id(i) for i in m["ids"]} for m in self.members]
        return gs

    def who_is(self, ids, groups=None):
        ids = expand_ids(ids, self.id_groups() if groups is None else groups)
        return next((e for e in self.store["employees"] if ids & set(e["ids"])), None)

    def read_older(self, auto=False, floor_date=None):
        """Ask WhatsApp (your phone) for the group's earlier messages back to the start of the day(s) set in Settings."""
        if not (self.ready_at and self.store["group_jid"]):
            if not auto: messagebox.showinfo("Earlier messages", "Connect WhatsApp and select the group first.")
            return False
        if self.older_active:
            if not auto: self.log("Already asking WhatsApp for earlier messages - please wait.")
            return False
        if floor_date is None:
            floor_date = datetime.now(IST).date() - timedelta(days=max(1, int(self.store["hist_days"])))
        return self._request_older(auto, floor_date)

    def receive_messages(self, messages):
        if not messages: return
        if self.manual_fill_active or self.manual_history_pending or self.fill_job:
            for message in messages:
                if message.get("hist"):
                    message["manual_fill"] = True
        self.session_msgs = (self.session_msgs + messages)[-5000:]
        changed = False
        for message in messages:
            self.note_sender(message)
            kind = attendance_kind(message.get("text", ""))
            message_id = message.get("id")
            if not kind or not message_id:
                continue
            ids = {clean_id(message.get("sender")), clean_id(message.get("senderAlt"))} - {""}
            rec = next((self.senders[i] for i in ids if i in self.senders), None)
            entry = self.attendance_entry_by_id.get(message_id)
            if entry is not None and entry.get("group", "") != self.store["group_jid"]:
                entry = None
            if entry is None:
                entry = {
                    "id": message_id,
                    "ts": message["ts"],
                    "ids": sorted(ids),
                    "name": (rec or {}).get("name") or message.get("pushName", ""),
                    "text": str(message.get("text") or "")[:2000],
                    "kind": kind,
                    "result": "Pending tracker handling",
                    "group": self.store["group_jid"],
                }
                self.attendance_entries.append(entry)
                self.attendance_entry_by_id[message_id] = entry
                self.append_message_row(entry)
                changed = True
            else:
                entry["ids"] = sorted(set(entry.get("ids", [])) | ids)
                if not entry.get("name"):
                    entry["name"] = (rec or {}).get("name") or message.get("pushName", "")
                changed = True
                self.update_message_row(entry)
        self.pending.extend(messages)
        self.last_msg = time.time()
        if changed:
            self.attendance_entries = self.attendance_entries[-4000:]
            self.attendance_entry_by_id = {entry["id"]: entry for entry in self.attendance_entries}
            self.persist_attendance_messages()

    def message_row(self, entry):
        ids = set(entry.get("ids", []))
        employee = self.who_is(ids) if ids else None
        member_name = next(
            (m.get("name") for m in self.members if ids & {clean_id(i) for i in m.get("ids", [])} and m.get("name")),
            "",
        )
        sender = next((self.senders[i] for i in ids if i in self.senders), None)
        alias = sender_alias(ids, self.store["sender_aliases"])
        name = employee["name"] if employee else (alias or member_name or (sender or {}).get("name") or entry.get("name") or "(unknown)")
        try:
            sent = datetime.fromtimestamp(entry["ts"], IST)
            day, clock = sent.strftime("%Y-%m-%d"), sent.strftime("%I:%M %p").lstrip("0")
        except (OverflowError, OSError, TypeError, ValueError):
            day, clock = "Unknown date", ""
        linked = "Linked" if employee else "Unlinked"
        return (day, clock, name, entry.get("kind", ""), linked,
                " ".join(entry.get("text", "").split())[:2000])

    def refresh_messages_tree(self):
        if not hasattr(self, "message_tree_items"):
            return
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.message_tree_items.clear()
        self.message_entry_ids.clear()
        group = self.store["group_jid"]
        entries = [
            e for e in self.attendance_entries
            if (not group or e.get("group", "") == group) and self.message_matches_filter(e)
        ][-4000:]
        for entry in sorted(entries, key=lambda row: row.get("ts", 0)):
            item = self.tree.insert("", "end", values=self.message_row(entry))
            self.message_tree_items[entry["id"]] = item
            self.message_entry_ids[item] = entry["id"]

    def message_matches_filter(self, entry):
        return self.message_after is None or entry.get("ts", 0) >= self.message_after.timestamp()

    def apply_message_filter(self):
        value = self.message_after_text.get().strip()
        if not value:
            self.message_after = None
        else:
            try:
                self.message_after = datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=IST)
            except ValueError:
                messagebox.showerror("Messages filter", "Enter a date and time as YYYY-MM-DD HH:MM (24-hour time, IST).")
                return
        self.refresh_messages_tree()

    def clear_message_filter(self):
        self.message_after_text.set("")
        self.message_after = None
        self.refresh_messages_tree()

    def append_message_row(self, entry):
        group = self.store["group_jid"]
        if (group and entry.get("group", "") != group) or not self.message_matches_filter(entry):
            return
        item = self.tree.insert("", "end", values=self.message_row(entry))
        self.message_tree_items[entry["id"]] = item
        self.message_entry_ids[item] = entry["id"]
        if len(self.message_tree_items) > 4000:
            children = self.tree.get_children()
            for old_item in children[:-4000]:
                self.tree.delete(old_item)
            visible_items = set(self.tree.get_children())
            self.message_tree_items = {
                message_id: iid for message_id, iid in self.message_tree_items.items() if iid in visible_items
            }
            self.message_entry_ids = {
                iid: message_id for iid, message_id in self.message_entry_ids.items() if iid in visible_items
            }
        self.tree.yview_moveto(1)

    def update_message_row(self, entry):
        item = self.message_tree_items.get(entry["id"])
        if item and self.tree.exists(item):
            self.tree.item(item, values=self.message_row(entry))

    def set_selected_sender_name(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("Sender display name", "Select one of that sender's messages first.")
            return
        message_id = self.message_entry_ids.get(selected[0])
        entry = self.attendance_entry_by_id.get(message_id)
        if not entry or not entry.get("ids"):
            messagebox.showinfo("Sender display name", "This message has no sender ID to map.")
            return
        ids = sorted(set(entry["ids"]))
        current = sender_alias(ids, self.store["sender_aliases"])
        name = simpledialog.askstring(
            "Sender display name",
            "Enter a display name for this WhatsApp sender.\nThis does not link them to a tracker employee.",
            initialvalue=current,
            parent=self,
        )
        if name is None:
            return
        name = name.strip()
        if not name:
            messagebox.showerror("Sender display name", "Display name cannot be blank.")
            return
        aliases = dict(self.store["sender_aliases"])
        for sender_id in ids:
            aliases[clean_id(sender_id)] = name
        self.store["sender_aliases"] = aliases
        self.store.save()
        self.refresh_messages_tree()

    def persist_attendance_messages(self):
        if self.attendance_message_load_error:
            return False
        try:
            save_attendance_messages(self.attendance_entries)
        except OSError as error:
            if error != self.attendance_message_save_error:
                self.log(f"Cannot save WhatsApp attendance messages: {error}")
            self.attendance_message_save_error = error
            return False
        self.attendance_message_save_error = None
        return True

    def _request_older(self, auto, floor_date):
        if not (self.ready_at and self.store["group_jid"]) or self.older_active:
            return False
        first = floor_date
        floor = int(datetime.combine(first, dtime(0, 0), tzinfo=IST).timestamp() * 1000)
        self.older_active = True
        self.bridge.send(cmd="older", floor=floor)
        self.log(f"Asking WhatsApp for earlier messages in the group back to {first:%d %b} (your phone must be online)...")
        return True

    # ---------- fill empty days ----------
    def fill_empty(self):
        """Find blank weekdays in the tracker and fill them from WhatsApp history / saved check-ins / an exported chat."""
        s = self.store
        if self.manual_fill_active:
            return
        if not os.path.exists(s["tracker_path"]):
            messagebox.showinfo("Tracker", "Set the tracker file in Settings first."); return
        if not s["employees"]:
            messagebox.showinfo("Employees", "Link your employees first (tab 2)."); return
        self.manual_fill_active = True
        today = datetime.now(IST).date()
        top = tk.Toplevel(self); top.title("Fill empty days"); top.grab_set()

        def cancel():
            self.manual_fill_active = False
            self.fill_job = None
            top.destroy()

        top.protocol("WM_DELETE_WINDOW", cancel)
        v_from, v_to = tk.StringVar(value=str(today.replace(day=1))), tk.StringVar(value=str(today))
        v_ask, v_cl, chat = tk.BooleanVar(value=bool(self.ready_at)), tk.BooleanVar(value=True), {"file": ""}
        v_file = tk.StringVar(value="(none)")
        ttk.Label(top, text="Finds weekdays where Reported/Status are empty for your linked employees and fills them.").grid(row=0, column=0, columnspan=4, sticky="w", padx=8, pady=(8, 4))
        ttk.Label(top, text="From (YYYY-MM-DD)").grid(row=1, column=0, sticky="w", padx=8, pady=3)
        ttk.Entry(top, textvariable=v_from, width=12).grid(row=1, column=1)
        ttk.Label(top, text="to").grid(row=1, column=2); ttk.Entry(top, textvariable=v_to, width=12).grid(row=1, column=3, padx=8)
        ttk.Checkbutton(top, text="First ask WhatsApp for the group's earlier messages (phone must be online)", variable=v_ask).grid(row=2, column=0, columnspan=4, sticky="w", padx=8, pady=2)
        ttk.Checkbutton(top, text="Mark weekdays that are over and still have no check-in as CL", variable=v_cl).grid(row=3, column=0, columnspan=4, sticky="w", padx=8, pady=2)

        def pick():
            f = filedialog.askopenfilename(title="WhatsApp exported chat (.txt)", filetypes=[("Text", "*.txt"), ("All files", "*.*")])
            if f: chat["file"] = f; v_file.set(os.path.basename(f))
        ttk.Button(top, text="Add exported chat file (optional)...", command=pick).grid(row=4, column=0, columnspan=2, sticky="w", padx=8, pady=4)
        ttk.Label(top, textvariable=v_file).grid(row=4, column=2, columnspan=2, sticky="w")
        if s["test_mode"]:
            ttk.Label(top, text="TEST MODE is on: nothing is written, the Log shows what would be.", foreground="#b36b00").grid(row=5, column=0, columnspan=4, sticky="w", padx=8)

        def go():
            try:
                d1, d2 = date.fromisoformat(v_from.get()), date.fromisoformat(v_to.get())
                if d1 > d2: raise ValueError("'From' is after 'to'.")
            except Exception as e:
                messagebox.showerror("Fill empty days", str(e), parent=top); return
            find_button.config(state="disabled")
            ttk.Label(top, text="Scanning tracker...").grid(row=6, column=0, columnspan=3, sticky="w", padx=8)
            names = [e["name"] for e in s["employees"]]

            def scanned(result, error):
                if not self.manual_fill_active:
                    return
                if error:
                    find_button.config(state="normal")
                    messagebox.showerror("Fill empty days", str(error), parent=top)
                    return
                empties, missing = result
                top.destroy()
                for m0 in missing:
                    self.log(f"No tab '{m0:%b - %y}' in the tracker - skipped (copy last month's tab and rename it).")
                if not empties:
                    self.manual_fill_active = False
                    messagebox.showinfo("Fill empty days", "No empty weekdays found for your linked employees in that range."); return
                self.fill_job = {"empties": empties, "cl": v_cl.get(), "chat": chat["file"]}
                oldest = min(e["date"] for e in empties)
                self.log(f"Fill: {len(empties)} empty entries found, oldest {oldest:%d %b}.")
                if v_ask.get():
                    self.manual_history_pending = True
                    if self.read_older(floor_date=oldest):
                        return
                    self.manual_history_pending = False
                self.fill_preview()

            self.run_tracker(
                lambda: Tracker(s["tracker_path"]).empties(d1, d2, names),
                scanned)
        find_button = ttk.Button(top, text="Find and preview", command=go)
        find_button.grid(row=6, column=3, sticky="e", padx=8, pady=8)

    def chat_mapping(self, chat_msgs):
        """Chat-export name -> sheet name, using confirmed aliases, saved WhatsApp names, then close matches."""
        emps = self.store["employees"]
        by_label = {norm(e.get("label", "")): e["name"] for e in emps if e.get("label")}
        by_name = {norm(e["name"]): e["name"] for e in emps}
        names = [e["name"] for e in emps]
        out = {}
        for n in {m["name"] for m in chat_msgs}:
            if self.store["chat_aliases"].get(n) in names: out[n] = self.store["chat_aliases"][n]
            elif norm(n) in by_label: out[n] = by_label[norm(n)]
            elif norm(n) in by_name: out[n] = by_name[norm(n)]
            else:
                sg, sc, amb = suggest(n, names)
                if sg and sc >= 0.75 and not amb: out[n] = sg
        return out

    def fill_events(self, chat_msgs):
        """Every check-in we know of, as {name, dt, text}: saved + this run + exported chat."""
        ev, have, groups = [], set(), self.id_groups()
        for c in self.checkins:
            ev.append({"name": c["name"], "dt": datetime.fromtimestamp(c["ts"], IST), "text": c["text"]}); have.add(c["id"])
        for m in self.session_msgs:
            if m["id"] in have: continue
            emp = self.who_is({clean_id(m.get("sender")), clean_id(m.get("senderAlt"))} - {""}, groups)
            if emp: ev.append({"name": emp["name"], "dt": datetime.fromtimestamp(m["ts"], IST), "text": m["text"]})
        mp = self.chat_mapping(chat_msgs)
        for m in chat_msgs:
            if m["name"] in mp: ev.append({"name": mp[m["name"]], "dt": m["dt"], "text": m["text"]})
        if chat_msgs:
            miss = sorted({m["name"] for m in chat_msgs} - set(mp))
            self.log(f"Chat file: {len(mp)} names matched to your employees" + (f"; not matched (ignored): {', '.join(miss[:8])}" if miss else "."))
        return ev

    def fill_preview(self):
        job, self.fill_job = self.fill_job, None
        if not job: return
        s, chat_msgs = self.store, []
        if job["chat"]:
            try: chat_msgs = [m for m in parse_chat(job["chat"]) if is_checkin(m["text"])]
            except OSError as e: self.log(f"Cannot read chat file: {e}")
        ops, rows = plan_fill(job["empties"], self.fill_events(chat_msgs), {e["name"]: e["timing"] for e in s["employees"]},
                              s["grace_min"], s["use_stated"], job["cl"], cl_time=s["cl_time"])
        n_in, n_cl = sum(o["kind"] == "in" for o in ops), sum(o["kind"] == "cl" for o in ops)
        top = tk.Toplevel(self); top.title("Fill empty days - preview"); top.geometry("640x460"); top.grab_set()
        ttk.Label(top, text=f"{n_in} check-ins found, {n_cl} to mark CL, {len(rows) - len(ops)} left empty.", font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=8, pady=6)
        if s["test_mode"]: ttk.Label(top, text="TEST MODE: 'Apply' only logs what would be written.", foreground="#b36b00").pack(anchor="w", padx=8)
        fr = ttk.Frame(top); fr.pack(fill="both", expand=True, padx=8, pady=6)
        tv = ttk.Treeview(fr, columns=("d", "n", "r"), show="headings")
        for c, h, w in (("d", "Date", 90), ("n", "Employee", 170), ("r", "Result", 330)):
            tv.heading(c, text=h); tv.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(fr, command=tv.yview); tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")
        for d, n, r in rows: tv.insert("", "end", values=(f"{d:%a %d %b}", n, r))
        if n_in == 0 and not self.checkins and not self.session_msgs and not chat_msgs:
            ttk.Label(top, text="No check-in messages are available yet. Use 'Add exported chat file' (WhatsApp > group > Export chat > Without media).", foreground="#b36b00", wraplength=600).pack(anchor="w", padx=8)

        def apply():
            top.destroy()

            def completed(ok, results):
                self.manual_fill_active = False
                counts = {}
                for state, _ in results: counts[state] = counts.get(state, 0) + 1
                self.log("Fill empty days done: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) if counts else "Fill empty days: nothing to write.")
                messagebox.showinfo("Fill empty days", ("TEST MODE: nothing was written. See the Log tab." if s["test_mode"] else "Finished. See the Log tab for each result.") if ok else "Could not write - see the Log tab.")

            self.apply_async(ops, completed)

        def close_preview():
            self.manual_fill_active = False
            top.destroy()

        top.protocol("WM_DELETE_WINDOW", close_preview)
        bf = ttk.Frame(top); bf.pack(fill="x", padx=8, pady=8)
        ttk.Button(bf, text="Apply" if not s["test_mode"] else "Apply (test: log only)", command=apply, state="normal" if ops else "disabled").pack(side="right")
        ttk.Button(bf, text="Close", command=close_preview).pack(side="right", padx=6)

    def import_chat(self):
        """Fill earlier check-ins from a WhatsApp 'Export chat' file (names there are your saved contact names)."""
        path = self.store["tracker_path"]
        if not os.path.exists(path):
            messagebox.showinfo("Tracker", "Set the tracker file in Settings first."); return
        f = filedialog.askopenfilename(title="WhatsApp exported chat (.txt)", filetypes=[("Text", "*.txt"), ("All files", "*.*")])
        if not f: return
        self.log("Reading exported chat and tracker names...")

        def loaded(result, error):
            if error:
                messagebox.showerror("Import", str(error)); return
            names, msgs = result
            self.show_import_chat(names, msgs)

        self.run_tracker(
            lambda: (Tracker(path).names(), [m for m in parse_chat(f) if is_checkin(m["text"])]),
            loaded)

    def show_import_chat(self, names, msgs):
        if not msgs:
            messagebox.showinfo("Import", "No check-in messages (reached office / logged in) found in that file."); return
        today = datetime.now(IST).date()
        days = sorted({m["dt"].date() for m in msgs})
        dflt = today if today in days else days[-1]
        top = tk.Toplevel(self); top.title("Import chat"); top.grab_set()
        v_from, v_to = tk.StringVar(value=str(dflt)), tk.StringVar(value=str(dflt))
        ttk.Label(top, text=f"This file has check-ins from {days[0]:%d %b} to {days[-1]:%d %b}.").grid(row=0, column=0, columnspan=4, sticky="w", padx=8, pady=(8, 2))
        ttk.Label(top, text="Fill dates from (YYYY-MM-DD)").grid(row=1, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(top, textvariable=v_from, width=12).grid(row=1, column=1)
        ttk.Label(top, text="to").grid(row=1, column=2); ttk.Entry(top, textvariable=v_to, width=12).grid(row=1, column=3)
        known = {e["name"]: e["timing"] for e in self.store["employees"]}
        senders = sorted({m["name"] for m in msgs})
        sugg = {n: suggest(n, names) for n in senders}
        aliases = {k: v for k, v in self.store["chat_aliases"].items() if v in names}      # confirmed by you earlier
        claimed = set(aliases.values())
        best = {}                       # one chat name per sheet name: the closest match wins, others stay blank
        for n, (sg, sc, amb) in sugg.items():
            if n in aliases or sg in claimed: continue
            if sg and sc >= 0.6 and not amb and sc > best.get(sg, (0, ""))[0]: best[sg] = (sc, n)
        vars_ = {}
        for n in senders:
            sg = sugg[n][0]
            pre = aliases.get(n) or (sg if sg and best.get(sg, (0, ""))[1] == n else "")
            vars_[n] = (tk.StringVar(value=pre), tk.StringVar(value=known.get(pre, "9:00 AM")))
        v_all = tk.BooleanVar(value=False)
        box = ttk.Frame(top); box.grid(row=3, column=0, columnspan=4, padx=8, sticky="w")

        def render():
            for w in box.winfo_children(): w.destroy()
            for i, h in enumerate(("Name in the chat", "Sheet name (blank = ignore)", "Timing")):
                ttk.Label(box, text=h, font=("Segoe UI", 9, "bold")).grid(row=0, column=i, padx=6, sticky="w")
            r = 1
            for n in senders:
                nv, tv = vars_[n]
                if not (v_all.get() or nv.get() or n == "You" or sugg[n][1] >= 0.5): continue
                ttk.Label(box, text=n, width=28).grid(row=r, column=0, padx=6, sticky="w")
                ttk.Combobox(box, textvariable=nv, values=[""] + names, width=22, state="readonly").grid(row=r, column=1, padx=6, pady=1)
                ttk.Entry(box, textvariable=tv, width=10).grid(row=r, column=2); r += 1
        ttk.Checkbutton(top, text="Show all names in the chat (default shows likely team members and 'You')", variable=v_all, command=render).grid(row=2, column=0, columnspan=4, sticky="w", padx=8)
        render()

        def go():
            try:
                d1, d2 = date.fromisoformat(v_from.get()), date.fromisoformat(v_to.get())
                mapping = {n: nv.get() for n, (nv, _) in vars_.items() if nv.get()}
                timings = {nv.get(): tv.get() for nv, tv in vars_.values() if nv.get()}
                for t in timings.values(): parse_timing(t)
            except ValueError as e:
                messagebox.showerror("Import", str(e)); return
            top.destroy()
            for n, (nv, _) in vars_.items():                    # remember the links you confirmed
                if nv.get(): self.store["chat_aliases"][n] = nv.get()
                else: self.store["chat_aliases"].pop(n, None)
            self.store.save()
            ops = build_import_ops(msgs, mapping, timings, d1, d2, self.store["grace_min"], self.store["use_stated"])

            def completed(ok, _results):
                messagebox.showinfo("Import", f"{len(ops)} check-in(s) found for the selected people and dates.\n" +
                                    ("TEST MODE: nothing was written. See the Log tab." if self.store["test_mode"] else "See the Log tab for each result.") if ok else
                                    "Could not write - see the Log tab.")

            self.apply_async(ops, completed)
        ttk.Button(top, text="Import", command=go).grid(row=4, column=3, sticky="e", padx=8, pady=8)
        self.wait_window(top)

    def cl_check(self):
        s = self.store
        if (not (self.ready_at and time.time() - self.ready_at > 90 and s["employees"] and s["tracker_path"] and self.cl_last)
                or self.manual_fill_active or self.manual_history_pending):
            return
        now = datetime.now(IST)
        end = now.date() if now.time() >= parse_timing(s["cl_time"]) else now.date() - timedelta(1)
        days, d = [], date.fromisoformat(self.cl_last) + timedelta(1)
        while d <= end: days.append(d); d += timedelta(1)
        days = [x for x in days[-7:] if x.weekday() < 5]
        ops = [{"kind": "cl", "date": x, "name": e["name"], "reported": "", "status": "CL"} for x in days for e in s["employees"]]
        if self.cl_job_active: return
        self.cl_job_active = True

        def completed(ok, _results):
            self.cl_job_active = False
            if ok:
                self.cl_last = end.isoformat()
                if not s["test_mode"]: s["cl_last_date"] = self.cl_last; s.save()

        self.apply_async(ops, completed)


if __name__ == "__main__":
    import socket
    _lock = socket.socket()
    try: _lock.bind(("127.0.0.1", 47931))               # one copy only: two copies would fight over the WhatsApp link
    except OSError:
        _r = tk.Tk(); _r.withdraw()
        messagebox.showerror("Attendance Automation", "Attendance Automation is already running.\nClose the other window (check Task Manager) first.")
        sys.exit(1)
    App().mainloop()
