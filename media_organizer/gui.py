"""Dark-mode desktop GUI (CustomTkinter)."""
from __future__ import annotations

import os
import queue
import sys
import threading
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from . import APP_NAME, __version__
from .core import (DONE, DUPLICATE, ERROR, OUT_OF_RANGE, PLANNED, Options, Plan, build_plan,
                   execute, latest_undo, summary_text, undo, write_reports)

ACCENT = "#4f8cff"
BG = "#16181d"
PANEL = "#1f222a"
FG = "#e6e8ee"
MUTED = "#9aa3b2"
STATUS_COLORS = {PLANNED: "#7fb3ff", DONE: "#5fd38d", DUPLICATE: "#e0b450",
                 OUT_OF_RANGE: "#8a8f99", ERROR: "#ff6b6b"}


class App(ctk.CTk):
    def __init__(self, screenshot: str | None = None, preset: tuple[str, str] | None = None):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title(f"{APP_NAME} {__version__}")
        self.geometry("1180x760")
        self.minsize(960, 620)
        self.configure(fg_color=BG)
        self.plan: Plan | None = None
        self.q: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self._build()
        self.after(60, self._pump)
        if preset:
            self.src_var.set(preset[0])
            self.out_var.set(preset[1])
        if screenshot:
            self.after(400, self.on_preview)
            self.after(500, lambda: self._screenshot_when_idle(screenshot))

    # ------------------------------------------------------------------ layout
    def _build(self):
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=20, pady=(16, 6))
        ctk.CTkLabel(head, text="📷  " + APP_NAME, font=ctk.CTkFont(size=24, weight="bold"),
                     text_color=FG).pack(side="left")
        ctk.CTkLabel(head, text="Sort photos & videos into  YYYY / MM / DD.MM",
                     text_color=MUTED, font=ctk.CTkFont(size=13)).pack(side="left", padx=14, pady=(6, 0))

        card = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=12)
        card.pack(fill="x", padx=20, pady=6)
        card.grid_columnconfigure(1, weight=1)
        self.src_var = ctk.StringVar()
        self.out_var = ctk.StringVar()
        for r, (label, var, cmd) in enumerate((
                ("Source folder", self.src_var, self._pick_src),
                ("Output folder", self.out_var, self._pick_out))):
            ctk.CTkLabel(card, text=label, text_color=MUTED, width=110, anchor="w").grid(
                row=r, column=0, padx=(16, 8), pady=(12 if r == 0 else 6, 6), sticky="w")
            ctk.CTkEntry(card, textvariable=var, height=34).grid(
                row=r, column=1, sticky="ew", pady=(12 if r == 0 else 6, 6))
            ctk.CTkButton(card, text="Browse…", width=96, height=34, command=cmd,
                          fg_color="#2c313c", hover_color="#3a404d").grid(
                row=r, column=2, padx=16, pady=(12 if r == 0 else 6, 6))

        opts = ctk.CTkFrame(card, fg_color="transparent")
        opts.grid(row=2, column=0, columnspan=3, sticky="ew", padx=16, pady=(4, 14))
        ctk.CTkLabel(opts, text="Mode", text_color=MUTED).pack(side="left")
        self.mode = ctk.CTkSegmentedButton(opts, values=["Copy", "Move"])
        self.mode.set("Copy")
        self.mode.pack(side="left", padx=(8, 18))
        ctk.CTkLabel(opts, text="Duplicates", text_color=MUTED).pack(side="left")
        self.dups = ctk.CTkSegmentedButton(opts, values=["Skip", "Keep"])
        self.dups.set("Skip")
        self.dups.pack(side="left", padx=(8, 18))
        ctk.CTkLabel(opts, text="Years", text_color=MUTED).pack(side="left")
        self.y_from = ctk.CTkEntry(opts, width=70, placeholder_text="from")
        self.y_from.pack(side="left", padx=(8, 4))
        self.y_to = ctk.CTkEntry(opts, width=70, placeholder_text="to")
        self.y_to.pack(side="left", padx=(0, 18))
        self.recursive = ctk.CTkCheckBox(opts, text="Include sub-folders")
        self.recursive.select()
        self.recursive.pack(side="left", padx=(0, 14))
        self.fname = ctk.CTkCheckBox(opts, text="Dates from filenames")
        self.fname.select()
        self.fname.pack(side="left")

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=20, pady=(6, 4))
        self.btn_preview = ctk.CTkButton(bar, text="🔍  Scan & Preview", height=38, width=170,
                                         command=self.on_preview)
        self.btn_preview.pack(side="left")
        self.btn_run = ctk.CTkButton(bar, text="▶  Organize", height=38, width=150,
                                     fg_color="#2f9e62", hover_color="#278553",
                                     command=self.on_run, state="disabled")
        self.btn_run.pack(side="left", padx=10)
        self.btn_cancel = ctk.CTkButton(bar, text="Cancel", height=38, width=90, state="disabled",
                                        fg_color="#5a2f35", hover_color="#733a42",
                                        command=lambda: self.cancel.set())
        self.btn_cancel.pack(side="left")
        ctk.CTkButton(bar, text="↶  Undo last run", height=38, width=140,
                      fg_color="#2c313c", hover_color="#3a404d",
                      command=self.on_undo).pack(side="right")
        ctk.CTkButton(bar, text="📂  Open output", height=38, width=130,
                      fg_color="#2c313c", hover_color="#3a404d",
                      command=self.on_open).pack(side="right", padx=10)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=20, pady=6)
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        tree_card = ctk.CTkFrame(body, fg_color=PANEL, corner_radius=12)
        tree_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self._style_tree()
        cols = ("status", "original", "date", "source", "dest")
        self.tree = ttk.Treeview(tree_card, columns=cols, show="headings", style="Dark.Treeview")
        for c, t, w in (("status", "Status", 90), ("original", "Original file", 220),
                        ("date", "Date", 130), ("source", "Date source", 240),
                        ("dest", "Destination", 170)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=int(w * self._scale), anchor="w",
                             stretch=c in ("original", "source", "dest"))
        for st, color in STATUS_COLORS.items():
            self.tree.tag_configure(st, foreground=color)
        vs = ctk.CTkScrollbar(tree_card, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)
        vs.pack(side="right", fill="y", pady=10, padx=(0, 4))

        side = ctk.CTkFrame(body, fg_color=PANEL, corner_radius=12)
        side.grid(row=0, column=1, sticky="nsew")
        ctk.CTkLabel(side, text="Summary", font=ctk.CTkFont(size=15, weight="bold"),
                     text_color=FG).pack(anchor="w", padx=14, pady=(12, 4))
        self.summary = ctk.CTkTextbox(side, fg_color="#181b21", text_color=FG, wrap="word",
                                      font=ctk.CTkFont(family="Consolas", size=12))
        self.summary.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.summary.insert("end", "Choose a source and output folder, then click\n"
                                   "“Scan & Preview”. Nothing is changed until you\n"
                                   "click “Organize”.")
        self.summary.configure(state="disabled")

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.pack(fill="x", padx=20, pady=(2, 14))
        self.progress = ctk.CTkProgressBar(foot, height=10)
        self.progress.set(0)
        self.progress.pack(fill="x")
        self.status = ctk.CTkLabel(foot, text="Ready.", text_color=MUTED, anchor="w")
        self.status.pack(fill="x", pady=(4, 0))

    def _style_tree(self):
        try:
            self._scale = ctk.ScalingTracker.get_window_scaling(self)
        except Exception:
            self._scale = self.winfo_fpixels("1i") / 96
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("Dark.Treeview", background="#181b21", fieldbackground="#181b21",
                    foreground=FG, rowheight=int(26 * self._scale), borderwidth=0,
                    font=("Segoe UI", 10))
        s.configure("Dark.Treeview.Heading", background="#262a33", foreground=MUTED,
                    relief="flat", font=("Segoe UI Semibold", 10))
        s.map("Dark.Treeview", background=[("selected", "#2d4a80")])
        s.map("Dark.Treeview.Heading", background=[("active", "#30353f")])

    # ------------------------------------------------------------------ helpers
    def _pick_src(self):
        d = filedialog.askdirectory(title="Choose source folder")
        if d:
            self.src_var.set(os.path.normpath(d))
            if not self.out_var.get():
                self.out_var.set(os.path.normpath(d) + " - organized")

    def _pick_out(self):
        d = filedialog.askdirectory(title="Choose output folder")
        if d:
            self.out_var.set(os.path.normpath(d))

    def _read_options(self) -> Options | None:
        src, out = self.src_var.get().strip(), self.out_var.get().strip()
        if not src or not Path(src).is_dir():
            messagebox.showerror(APP_NAME, "Please choose an existing source folder.")
            return None
        if not out:
            messagebox.showerror(APP_NAME, "Please choose an output folder.")
            return None

        def year(e):
            v = e.get().strip()
            return int(v) if v.isdigit() else None

        return Options(source=Path(src), output=Path(out), mode=self.mode.get().lower(),
                       recursive=bool(self.recursive.get()), year_from=year(self.y_from),
                       year_to=year(self.y_to), duplicates=self.dups.get().lower(),
                       use_filename_dates=bool(self.fname.get()))

    def _set_busy(self, busy: bool):
        self.busy = busy
        self.btn_preview.configure(state="disabled" if busy else "normal")
        self.btn_run.configure(state="disabled" if busy or not self.plan else "normal")
        self.btn_cancel.configure(state="normal" if busy else "disabled")

    def _progress(self, i, n, msg):
        self.q.put(("progress", i, n, msg))

    def _pump(self):
        try:
            while True:
                ev = self.q.get_nowait()
                if ev[0] == "progress":
                    _, i, n, msg = ev
                    self.progress.set(i / max(n, 1))
                    self.status.configure(text=f"{msg}   ({i}/{n})")
                elif ev[0] == "done":
                    ev[1]()
        except queue.Empty:
            pass
        self.after(50, self._pump)

    def _thread(self, work, finish):
        self.cancel.clear()
        self._set_busy(True)

        def run():
            try:
                res = work()
                self.q.put(("done", lambda: finish(res, None)))
            except Exception as e:  # pragma: no cover
                self.q.put(("done", lambda: finish(None, e)))
        threading.Thread(target=run, daemon=True).start()

    def _show(self, plan: Plan, header: str):
        self.tree.delete(*self.tree.get_children())
        out = plan.options.output
        def order(x):
            return (0 if x.dest else 1, str(x.dest.parent) if x.dest else "",
                    x.date.dt if x.date else 0, x.suffix, x.src.name)
        for it in sorted(plan.items, key=order):
            dest = str(it.dest.relative_to(out)) if it.dest else (it.note or "-")
            self.tree.insert("", "end", tags=(it.status,), values=(
                it.status, it.src.name,
                it.date.dt.strftime("%Y-%m-%d %H:%M") if it.date else "",
                it.date.label() if it.date else it.note, dest))
        for p in plan.ignored:
            self.tree.insert("", "end", tags=(OUT_OF_RANGE,),
                             values=("skipped", p.name, "", "not a media file", "-"))
        self.summary.configure(state="normal")
        self.summary.delete("1.0", "end")
        self.summary.insert("end", header + "\n\n" + summary_text(plan))
        self.summary.configure(state="disabled")

    # ------------------------------------------------------------------ actions
    def on_preview(self):
        opts = self._read_options()
        if not opts:
            return

        def finish(plan, err):
            self._set_busy(False)
            if err:
                messagebox.showerror(APP_NAME, str(err))
                return
            self.plan = plan
            n = sum(1 for it in plan.items if it.status == PLANNED)
            verb = "moved" if opts.mode == "move" else "copied"
            self._show(plan, f"PREVIEW: {n} file(s) will be {verb}.")
            self.status.configure(text=f"Preview ready: {n} file(s) to {opts.mode}.")
            self._set_busy(False)

        self._thread(lambda: build_plan(opts, self._progress, self.cancel), finish)

    def on_run(self):
        if not self.plan:
            return
        opts = self._read_options()
        if not opts:
            return
        n = sum(1 for it in self.plan.items if it.status == PLANNED)
        verb = "MOVE" if opts.mode == "move" else "copy"
        if not messagebox.askyesno(APP_NAME, f"{verb} {n} file(s) into\n{opts.output} ?"):
            return
        # honour option changes made after preview
        self.plan.options.mode = opts.mode
        plan = self.plan

        def work():
            manifest = execute(plan, self._progress, self.cancel)
            return write_reports(plan, manifest, executed=True)

        def finish(report, err):
            self.plan = None
            self._set_busy(False)
            if err:
                messagebox.showerror(APP_NAME, str(err))
                return
            self._show(plan, f"DONE. Report:\n{report}")
            self.status.configure(text=f"Finished. Report saved to {report}")

        self._thread(work, finish)

    def on_open(self):
        out = self.out_var.get().strip()
        if out and Path(out).exists():
            os.startfile(out)  # type: ignore[attr-defined]

    def on_undo(self):
        out = self.out_var.get().strip()
        m = latest_undo(Path(out)) if out else None
        if not m:
            messagebox.showinfo(APP_NAME, "No previous run found in the output folder.")
            return
        if not messagebox.askyesno(APP_NAME, f"Revert run {m.name}?"):
            return

        def finish(res, err):
            self._set_busy(False)
            messagebox.showinfo(APP_NAME, f"Undo result: {res or err}")
        self._thread(lambda: undo(m, self._progress), finish)

    # ------------------------------------------------------------------ docs helper
    def _screenshot_when_idle(self, path: str):
        if self.busy or not self.plan:
            self.after(300, lambda: self._screenshot_when_idle(path))
            return
        self.after(800, lambda: self._grab(path))

    def _grab(self, path: str):
        self.update()
        try:
            img = self._print_window()
        except Exception:
            from PIL import ImageGrab
            x, y = self.winfo_rootx(), self.winfo_rooty()
            w, h = self.winfo_width(), self.winfo_height()
            img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
        img.save(path)
        self.destroy()

    def _print_window(self):
        """Render the window via Win32 PrintWindow (works without screen access)."""
        import ctypes
        from ctypes import wintypes

        from PIL import Image

        u32, g32 = ctypes.windll.user32, ctypes.windll.gdi32
        hwnd = int(self.wm_frame(), 16)
        r = wintypes.RECT()
        u32.GetWindowRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        hdc = u32.GetWindowDC(hwnd)
        mdc = g32.CreateCompatibleDC(hdc)
        bmp = g32.CreateCompatibleBitmap(hdc, w, h)
        g32.SelectObject(mdc, bmp)
        ok = u32.PrintWindow(hwnd, mdc, 2)  # PW_RENDERFULLCONTENT

        class BMI(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]
        bmi = BMI(ctypes.sizeof(BMI), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        g32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bmi), 0)
        g32.DeleteObject(bmp)
        g32.DeleteDC(mdc)
        u32.ReleaseDC(hwnd, hdc)
        if not ok:
            raise OSError("PrintWindow failed")
        return Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)


def _enable_dpi_awareness():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    shot = preset = None
    if "--screenshot" in argv:
        i = argv.index("--screenshot")
        shot, preset = argv[i + 1], (argv[i + 2], argv[i + 3])
    _enable_dpi_awareness()
    App(screenshot=shot, preset=preset).mainloop()


if __name__ == "__main__":
    main()
