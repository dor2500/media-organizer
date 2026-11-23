"""Dark-mode desktop GUI (CustomTkinter) - paste folder path(s), optionally upload a person's photo, click one button, done."""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

import customtkinter as ctk
from PIL import Image, ImageOps

from . import APP_NAME, __version__
from .bidi import rtl, wrap
from .core import (DONE, DUPLICATE, ERROR, NO_FACE_MATCH, OUT_OF_RANGE, PLANNED, SKIPPED,
                   Options, Plan, build_plan, execute, latest_undo, organize_live, summarize,
                   undo, write_reports)
from .dates import SRC_FILENAME, SRC_FILESYSTEM, SRC_METADATA
from .updater import check_for_updates, download_and_apply

BG = "#0b0d10"
PANEL = "#111418"
FIELD = "#0d1014"
BORDER = "#232931"
BORDER_HI = "#323a45"
FG = "#d6dce4"
MUTED = "#6c7682"
ACCENT, ACCENT_H, ACCENT_DIM = "#19c3d6", "#14a6b7", "#0e4a52"
ON_ACCENT = "#03161a"
GREY, GREY_H = "#161a20", "#1e242c"
RED, RED_H = "#e5484d", "#ff5f64"
RADIUS = 3
MONO = "Consolas"          # replaced by Cascadia Mono at startup when installed
STATUS_COLORS = {PLANNED: "#19c3d6", DONE: "#3ddc97", DUPLICATE: "#d9a441",
                 NO_FACE_MATCH: "#56606b", OUT_OF_RANGE: "#56606b",
                 SKIPPED: "#56606b", ERROR: "#e5484d"}
STATUS_HE = {k: rtl(v) for k, v in {
    PLANNED: "מתוכנן", DONE: "בוצע", DUPLICATE: "כפול - דולג",
    NO_FACE_MATCH: "אין התאמת פנים", OUT_OF_RANGE: "מחוץ לטווח",
    SKIPPED: "לא מדיה", ERROR: "שגיאה"}.items()}
OUTPUT_SUFFIX = " - מסודר"


class mbox:
    """messagebox wrapper: native dialogs also lay Hebrew out with an LTR base."""
    @staticmethod
    def showinfo(title, msg):
        return messagebox.showinfo(title, rtl(msg))

    @staticmethod
    def showerror(title, msg):
        return messagebox.showerror(title, rtl(msg))

    @staticmethod
    def askyesno(title, msg):
        return messagebox.askyesno(title, rtl(msg))
FACE_MODES = {rtl(k): v for k, v in
              {"רגיש (יותר תוצאות)": 0.32, "רגיל (מומלץ)": 0.363, "קפדני (מדויק יותר)": 0.42}.items()}
FACE_DEFAULT = rtl("רגיל (מומלץ)")


def clean_path(text: str) -> str:
    """Accept pasted paths with quotes/spaces/file://."""
    t = (text or "").strip().strip('"').strip("'").strip()
    if t.lower().startswith("file:///"):
        from urllib.parse import unquote
        t = unquote(t[8:])
    if not t:
        return ""
    return os.path.normpath(t)


def parse_sources(raw: str) -> list[str]:
    """Split multi-line or semicolon-separated folder paths."""
    out: list[str] = []
    for chunk in raw.replace(";", "\n").splitlines():
        p = clean_path(chunk)
        if p:
            if os.path.isfile(p):
                p = os.path.dirname(p)
            if p not in out:
                out.append(p)
    return out


def hebrew_summary(plan: Plan, executed: bool) -> str:
    s = summarize(plan)
    st, src = s["status"], s["date_sources"]
    ok = st.get(DONE if executed else PLANNED, 0)
    rows = [
        ("סודרו" if executed else "יסודרו", ok),
        ("קבצי מדיה שנמצאו", s["total_media"]),
        ("כפולים זהים (דולגו)", st.get(DUPLICATE, 0)),
        ("תאריך זהה (מספר רץ)", s["collisions_renamed"]),
    ]
    if st.get(NO_FACE_MATCH):
        rows.append(("ללא התאמת פנים", st[NO_FACE_MATCH]))
    if st.get(OUT_OF_RANGE):
        rows.append(("מחוץ לטווח השנים", st[OUT_OF_RANGE]))
    if s["ignored_non_media"]:
        rows.append(("לא מדיה (לא נגעו)", s["ignored_non_media"]))
    if st.get(ERROR):
        rows.append(("שגיאות", st[ERROR]))
    lines = ["תוצאה" if executed else "תצוגה מקדימה"]
    lines += [f"{k}: {v}" for k, v in rows]
    lines += ["", "מקור התאריך",
              f"מטא-דאטה (EXIF/וידאו): {src.get(SRC_METADATA, 0)}",
              f"שם הקובץ: {src.get(SRC_FILENAME, 0)}",
              f"תאריך הקובץ במחשב: {src.get(SRC_FILESYSTEM, 0)}"]
    if s["years"]:
        lines += ["", "לפי שנה"]
        lines += [f"{y}: {cnt}" for y, cnt in sorted(s["years"].items(), reverse=True)]
    return "\n".join(lines)


class App(ctk.CTk):
    def __init__(self, screenshot: str | None = None, preset: tuple[str, str] | None = None,
                 person: str | None = None, auto_run: bool = False):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.title(f"מסדר תמונות - {APP_NAME} {__version__}")
        self.geometry("1220x840")
        self.minsize(980, 680)
        self.configure(fg_color=BG)
        self._set_icon()
        self.plan: Plan | None = None
        self.q: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.quiet = bool(screenshot)
        self._out_manual = False
        self._thumb_ref = None
        self._build()
        self.after(60, self._pump)
        if preset:
            self._set_sources_text(preset[0])
            if preset[1]:
                self._set_out(preset[1], manual=True)
        if person:
            self._set_person(person)
        if screenshot:
            self.after(500, self.on_organize_all if auto_run else self.on_preview)
            self.after(700, lambda: self._screenshot_when_idle(screenshot))

    # ------------------------------------------------------------------ layout
    def _set_icon(self):
        if sys.platform != "win32":
            return
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
        ico = base / "assets" / "icon.ico"
        if ico.exists():
            try:
                self.iconbitmap(str(ico))
            except Exception:
                pass

    def _build(self):
        """Right-to-left, flat, technical layout: one accent colour, thin borders, no emoji."""
        global MONO
        if "Cascadia Mono" in tkfont.families(self):
            MONO = "Cascadia Mono"
        ui = ctk.CTkFont(family="Segoe UI", size=14)
        ui_b = ctk.CTkFont(family="Segoe UI Semibold", size=14)
        mono = ctk.CTkFont(family=MONO, size=13)
        mono_s = ctk.CTkFont(family=MONO, size=12)

        def btn(parent, text, cmd, primary=False, **kw):
            kw.setdefault("height", 36)
            kw.setdefault("font", ui_b if primary else ui)
            if primary:
                style = dict(fg_color=ACCENT, hover_color=ACCENT_H, text_color=ON_ACCENT,
                             text_color_disabled="#0b5c66", border_width=0)
            else:
                style = dict(fg_color=GREY, hover_color=GREY_H, text_color=FG,
                             text_color_disabled="#3d454f", border_width=1, border_color=BORDER_HI)
            style.update(kw)
            return ctk.CTkButton(parent, text=rtl(text), command=cmd, corner_radius=RADIUS, **style)

        def field_entry(parent, var, placeholder, **kw):
            return ctk.CTkEntry(parent, textvariable=var, height=36, corner_radius=RADIUS,
                                fg_color=FIELD, border_color=BORDER, border_width=1,
                                text_color=FG, font=mono, placeholder_text=rtl(placeholder),
                                placeholder_text_color="#46505b", **kw)

        # ---- header
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=22, pady=(16, 10))
        ctk.CTkLabel(head, text="MEDIA ORGANIZER", text_color=FG,
                     font=ctk.CTkFont(family=MONO, size=22, weight="bold")).pack(side="right")
        ctk.CTkLabel(head, text=f"v{__version__}", text_color=ACCENT, font=mono_s).pack(
            side="right", padx=(0, 12), pady=(6, 0))
        ctk.CTkLabel(head, text=rtl("מיון תמונות וסרטונים לפי תאריך  /  שנה  /  חודש  /  יום.חודש"),
                     text_color=MUTED, font=ui).pack(side="right", padx=(0, 18), pady=(4, 0))
        self.btn_update = btn(head, "בדוק עדכונים", self.on_check_updates, width=120, height=30)
        self.btn_update.pack(side="left")
        self.btn_undo = btn(head, "בטל ריצה אחרונה", self.on_undo, width=130, height=30)
        self.btn_undo.pack(side="left", padx=8)

        # ---- input panel
        card = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=RADIUS, border_width=1, border_color=BORDER)
        card.pack(fill="x", padx=22, pady=(0, 6))
        card.grid_columnconfigure(2, weight=1)

        def step(row, num, title, hint, pady=(14, 6)):
            f = ctk.CTkFrame(card, fg_color="transparent")
            f.grid(row=row, column=0, columnspan=3, sticky="ew", padx=18, pady=pady)
            ctk.CTkLabel(f, text=num, text_color=ACCENT, font=ctk.CTkFont(family=MONO, size=13, weight="bold")
                         ).pack(side="right")
            ctk.CTkLabel(f, text=rtl(title), text_color=FG, font=ui_b).pack(side="right", padx=(0, 10))
            ctk.CTkLabel(f, text=rtl(hint), text_color=MUTED, font=ctk.CTkFont(family="Segoe UI", size=12)
                         ).pack(side="right", padx=(0, 12))

        step(0, "01", "תיקיות מקור", "נתיב אחד בכל שורה. אפשר כמה תיקיות.")
        self.src_box = ctk.CTkTextbox(card, height=56, font=mono, fg_color=FIELD, text_color=FG,
                                      border_width=1, border_color=BORDER, corner_radius=RADIUS, wrap="none")
        self.src_box.grid(row=1, column=2, sticky="ew", padx=(8, 18))
        self.src_box.bind("<KeyRelease>", lambda e: self._on_src_change())
        btn(card, "הדבק", self._paste, width=100, height=56).grid(row=1, column=1, padx=4)
        btn(card, "הוסף תיקייה", self._pick_src, width=120, height=56).grid(row=1, column=0, padx=(18, 4))

        step(2, "02", "תיקיית יעד", "נקבעת אוטומטית. אפשר לשנות.", pady=(16, 6))
        self.out_var = ctk.StringVar()
        self.out_entry = field_entry(card, self.out_var, "נתיב לתיקייה שבה יישמרו הקבצים המסודרים")
        self.out_entry.grid(row=3, column=2, sticky="ew", padx=(8, 18))
        self.out_entry.bind("<Key>", lambda e: setattr(self, "_out_manual", True))
        btn(card, "הדבק", self._paste_out, width=100).grid(row=3, column=1, padx=4)
        btn(card, "בחר יעד", self._pick_out, width=120).grid(row=3, column=0, padx=(18, 4))

        step(4, "03", "חיפוש פנים", "אופציונלי. כל התאמה מועתקת מיד ליעד.", pady=(16, 6))
        person_row = ctk.CTkFrame(card, fg_color="transparent")
        person_row.grid(row=5, column=0, columnspan=3, sticky="ew", padx=18)
        person_row.grid_columnconfigure(3, weight=1)
        self.thumb_lbl = ctk.CTkLabel(person_row, text="—", width=36, height=36, text_color=MUTED,
                                      fg_color=FIELD, corner_radius=RADIUS, font=mono)
        self.thumb_lbl.grid(row=0, column=4, padx=(8, 0))
        self.person_var = ctk.StringVar()
        field_entry(person_row, self.person_var, "ריק = כל הקבצים. או בחר תמונת ייחוס של האדם").grid(
            row=0, column=3, sticky="ew", padx=(8, 0))
        self.face_sens = ctk.CTkOptionMenu(person_row, values=list(FACE_MODES.keys()), width=160, height=36,
                                           corner_radius=RADIUS, font=ui, dropdown_font=ui,
                                           fg_color=GREY, button_color=GREY_H, button_hover_color=BORDER_HI,
                                           text_color=FG)
        self.face_sens.set(FACE_DEFAULT)
        self.face_sens.grid(row=0, column=2, padx=4)
        btn(person_row, "בחר תמונה", self._pick_person, width=120).grid(row=0, column=1, padx=4)
        btn(person_row, "נקה", self._clear_person, width=56).grid(row=0, column=0)

        # ---- action bar
        ctk.CTkFrame(card, height=1, fg_color=BORDER).grid(row=6, column=0, columnspan=3, sticky="ew",
                                                           padx=1, pady=(18, 0))
        act = ctk.CTkFrame(card, fg_color="transparent")
        act.grid(row=7, column=0, columnspan=3, sticky="ew", padx=18, pady=14)
        self.btn_all = btn(act, "", self.on_organize_all, primary=True, width=240, height=46,
                           font=ctk.CTkFont(family="Segoe UI Semibold", size=17))
        self.btn_all.configure(text=self._all_label())
        self.btn_all.pack(side="right")
        self.btn_preview = btn(act, "תצוגה מקדימה", self.on_preview, width=150, height=46)
        self.btn_preview.pack(side="right", padx=10)
        self.btn_cancel = btn(act, "עצור", lambda: self.cancel.set(), width=90, height=46)
        self.btn_cancel.pack(side="right")
        self.btn_open = btn(act, "פתח תיקיית יעד", self.on_open, width=150, height=46)
        self.btn_open.pack(side="left")

        # ---- advanced options (collapsible)
        self.adv_open = False
        self.adv_btn = ctk.CTkButton(self, text=self._adv_label(), fg_color="transparent",
                                     hover_color=PANEL, text_color=MUTED, anchor="e", width=200,
                                     corner_radius=RADIUS, font=ui, command=self._toggle_adv)
        self.adv_btn.pack(anchor="e", padx=22, pady=(2, 0))
        self.adv = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=RADIUS, border_width=1, border_color=BORDER)
        row = ctk.CTkFrame(self.adv, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=10)

        def opt_label(text):
            ctk.CTkLabel(row, text=rtl(text), text_color=MUTED, font=ui).pack(side="right", padx=(0, 8))

        def seg(values, default):
            w = ctk.CTkSegmentedButton(row, values=values, corner_radius=RADIUS, font=ui,
                                       fg_color=GREY, unselected_color=GREY, unselected_hover_color=GREY_H,
                                       selected_color=ACCENT_DIM, selected_hover_color=ACCENT_DIM,
                                       text_color=FG)
            w.set(default)
            return w
        opt_label("פעולה")
        self.mode = seg(["העברה", "העתקה"], "העתקה")
        self.mode.pack(side="right", padx=(0, 22))
        opt_label("כפולים זהים")
        self.dups = seg(["שמור", "דלג"], "דלג")
        self.dups.pack(side="right", padx=(0, 22))
        opt_label("שנים")
        self.y_from = ctk.CTkEntry(row, width=70, corner_radius=RADIUS, fg_color=FIELD, border_color=BORDER,
                                   font=mono, placeholder_text=rtl("מ-"))
        self.y_from.pack(side="right", padx=(0, 4))
        self.y_to = ctk.CTkEntry(row, width=70, corner_radius=RADIUS, fg_color=FIELD, border_color=BORDER,
                                 font=mono, placeholder_text=rtl("עד"))
        self.y_to.pack(side="right", padx=(0, 22))
        chk = dict(corner_radius=2, border_width=1, border_color=BORDER_HI, fg_color=ACCENT,
                   hover_color=ACCENT_H, checkmark_color=ON_ACCENT, font=ui, text_color=FG)
        self.recursive = ctk.CTkCheckBox(row, text=rtl("כולל תתי-תיקיות"), **chk)
        self.recursive.select()
        self.recursive.pack(side="right", padx=(28, 0))
        self.fname = ctk.CTkCheckBox(row, text=rtl("תאריך משם הקובץ"), **chk)
        self.fname.select()
        self.fname.pack(side="right", padx=(28, 0))

        # ---- results: table on the right, summary on the left
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=22, pady=6)
        self.body.grid_columnconfigure(1, weight=3)
        self.body.grid_columnconfigure(0, weight=1)
        self.body.grid_rowconfigure(0, weight=1)

        tree_card = ctk.CTkFrame(self.body, fg_color=FIELD, corner_radius=RADIUS,
                                 border_width=1, border_color=BORDER)
        tree_card.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self._style_tree()
        cols = ("status", "original", "date", "source", "dest")
        self.tree = ttk.Treeview(tree_card, columns=cols, show="headings", style="Dark.Treeview",
                                 displaycolumns=tuple(reversed(cols)))
        for c, t, w in (("status", "סטטוס", 120), ("original", "קובץ מקורי", 190),
                        ("date", "תאריך", 140), ("source", "מקור התאריך / התאמה", 220),
                        ("dest", "יעד", 165)):
            self.tree.heading(c, text=rtl(t), anchor="e")
            self.tree.column(c, width=int(w * self._scale), anchor="e",
                             stretch=c in ("original", "source", "dest"))
        for st, color in STATUS_COLORS.items():
            self.tree.tag_configure(st, foreground=color)
        vs = ctk.CTkScrollbar(tree_card, command=self.tree.yview, button_color=BORDER_HI,
                              button_hover_color=MUTED, corner_radius=RADIUS)
        self.tree.configure(yscrollcommand=vs.set)
        vs.pack(side="left", fill="y", pady=4, padx=(2, 0))
        self.tree.pack(side="right", fill="both", expand=True, padx=(0, 2), pady=2)
        self.empty_lbl = ctk.CTkLabel(
            tree_card, text_color=MUTED, font=ui, fg_color=FIELD, justify="right",
            text=rtl("אין נתונים עדיין.\n"
                     "\"תצוגה מקדימה\" מראה מה יקרה לכל קובץ בלי לשנות דבר."))
        self.empty_lbl.place(relx=0.5, rely=0.5, anchor="center")

        side = ctk.CTkFrame(self.body, fg_color=PANEL, corner_radius=RADIUS, border_width=1, border_color=BORDER)
        side.grid(row=0, column=0, sticky="nsew")
        ctk.CTkLabel(side, text=rtl("סיכום"), font=ui_b, text_color=MUTED).pack(anchor="e", padx=14, pady=(10, 2))
        self.summary_font = ctk.CTkFont(family="Segoe UI", size=13)
        self.summary = ctk.CTkTextbox(side, fg_color=PANEL, text_color=FG, wrap="none", corner_radius=0,
                                      font=self.summary_font, border_width=0)
        self.summary.pack(fill="both", expand=True, padx=6, pady=(0, 8))
        self.summary._textbox.tag_configure("rtl", justify="right", spacing1=2, spacing3=2)
        self.summary.bind("<Configure>", lambda e: self._render_summary())
        self._summary_raw = ""
        self._set_summary(
            "שלבים\n"
            "01  מדביקים נתיב של תיקייה (אפשר כמה תיקיות).\n"
            "02  בודקים את תיקיית היעד.\n"
            "03  אופציונלי: תמונת ייחוס לחיפוש פנים.\n"
            "04  תצוגה מקדימה, ואז \"סדר הכל\".\n\n"
            "הקבצים המקוריים לא נמחקים ולא משתנים.\n"
            "כפולים זהים מדולגים ונרשמים בדוח.")

        # ---- footer: thin progress line + monospace status
        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.pack(fill="x", padx=22, pady=(2, 12))
        self.progress = ctk.CTkProgressBar(foot, height=4, corner_radius=0, progress_color=ACCENT,
                                           fg_color=BORDER)
        self.progress.set(0)
        self.status = ctk.CTkLabel(foot, text=rtl("מוכן."), text_color=MUTED, anchor="e", font=mono_s)
        self.status.pack(fill="x", pady=(4, 0))
        self._set_busy(False)

    def _all_label(self, count: int | None = None) -> str:
        return rtl(f"סדר {count} קבצים" if count else "סדר הכל")

    def _adv_label(self) -> str:
        return rtl(f"{'−' if self.adv_open else '+'}  אפשרויות מתקדמות")

    def _toggle_adv(self):
        self.adv_open = not self.adv_open
        self.adv_btn.configure(text=self._adv_label())
        if self.adv_open:
            self.adv.pack(fill="x", padx=22, pady=(4, 2), before=self.body)
        else:
            self.adv.pack_forget()

    def _style_tree(self):
        try:
            self._scale = ctk.ScalingTracker.get_window_scaling(self)
        except Exception:
            self._scale = self.winfo_fpixels("1i") / 96
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("Dark.Treeview", background=FIELD, fieldbackground=FIELD,
                    foreground=FG, rowheight=int(24 * self._scale), borderwidth=0,
                    font=(MONO, 9))
        s.configure("Dark.Treeview.Heading", background=PANEL, foreground=MUTED,
                    relief="flat", borderwidth=0, font=("Segoe UI Semibold", 9))
        s.layout("Dark.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        s.map("Dark.Treeview", background=[("selected", ACCENT_DIM)])
        s.map("Dark.Treeview.Heading", background=[("active", GREY_H)])

    # ------------------------------------------------------------------ helpers
    def _set_sources_text(self, text: str):
        self.src_box.delete("1.0", "end")
        self.src_box.insert("1.0", text)
        self._on_src_change()

    def _set_out(self, path: str, manual: bool):
        self.out_var.set(path)
        self._out_manual = manual

    def _on_src_change(self):
        srcs = parse_sources(self.src_box.get("1.0", "end"))
        if srcs and not self._out_manual:
            self.out_var.set(srcs[0].rstrip("\\/") + OUTPUT_SUFFIX)
        self.plan = None
        self.btn_all.configure(text=self._all_label())

    def _paste(self):
        try:
            text = self.clipboard_get()
        except Exception:
            text = ""
        paths = parse_sources(text)
        if not paths:
            mbox.showinfo(APP_NAME, "הלוח ריק - העתק קודם נתיב של תיקייה.")
            return
        self._set_sources_text("\n".join(paths))

    def _pick_src(self):
        d = filedialog.askdirectory(title="בחר תיקיית תמונות להוספה")
        if d:
            existing = parse_sources(self.src_box.get("1.0", "end"))
            p = os.path.normpath(d)
            if p not in existing:
                existing.append(p)
            self._set_sources_text("\n".join(existing))

    def _paste_out(self):
        try:
            text = self.clipboard_get()
        except Exception:
            text = ""
        p = clean_path(text)
        if p:
            self._set_out(p, manual=True)
        else:
            mbox.showinfo(APP_NAME, "הלוח ריק - העתק קודם נתיב של תיקיית יעד.")

    def _pick_out(self):
        d = filedialog.askdirectory(title="בחר תיקיית יעד")
        if d:
            self._set_out(os.path.normpath(d), manual=True)

    def _pick_person(self):
        f = filedialog.askopenfilename(
            title="בחר תמונת ייחוס של אדם",
            filetypes=[("תמונות", "*.jpg *.jpeg *.png *.webp *.heic *.heif *.bmp"), ("הכל", "*.*")],
        )
        if f:
            self._set_person(os.path.normpath(f))

    def _set_person(self, path: str):
        self.person_var.set(path)
        try:
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.thumbnail((38, 38))
                self._thumb_ref = ctk.CTkImage(light_image=im, dark_image=im, size=(36, 36))
                self.thumb_lbl.configure(image=self._thumb_ref, text="")
        except Exception:
            self._thumb_ref = None
            self.thumb_lbl.configure(image=None, text="—")

    def _clear_person(self):
        self.person_var.set("")
        self._thumb_ref = None
        self.thumb_lbl.configure(image=None, text="—")

    def _set_summary(self, text: str):
        self._summary_raw = text
        self._render_summary()

    def _render_summary(self):
        """Wrap in logical order first, then re-order each line for RTL display."""
        tb = self.summary._textbox
        width = tb.winfo_width() - 24
        if width < 100:          # not laid out yet; <Configure> will call us again
            return
        real_font = tkfont.Font(root=self, font=tb.cget("font"))   # DPI-scaled, unlike CTkFont
        text = rtl(wrap(self._summary_raw, real_font.measure, width))
        self.summary.configure(state="normal")
        self.summary.delete("1.0", "end")
        self.summary.insert("end", text, "rtl")
        self.summary.configure(state="disabled")

    def _status(self, text: str):
        self.status.configure(text=rtl(text))

    def _read_options(self) -> Options | None:
        srcs = parse_sources(self.src_box.get("1.0", "end"))
        if not srcs:
            if not self.quiet:
                mbox.showerror(APP_NAME, "אנא הדבק או בחר לפחות תיקיית מקור אחת.")
            return None
        missing = [s for s in srcs if not Path(s).is_dir()]
        if missing:
            if not self.quiet:
                mbox.showerror(APP_NAME, f"התיקייה לא נמצאה:\n{missing[0]}")
            return None

        out = clean_path(self.out_var.get()) or (srcs[0].rstrip("\\/") + OUTPUT_SUFFIX)
        out_res = Path(out).resolve()
        if any(Path(s).resolve() == out_res for s in srcs):
            if not self.quiet:
                mbox.showerror(APP_NAME, "תיקיית היעד חייבת להיות שונה מתיקיות המקור.")
            return None
        self.out_var.set(out)

        person_raw = clean_path(self.person_var.get())
        person: Path | None = None
        if person_raw:
            if not Path(person_raw).is_file():
                if not self.quiet:
                    mbox.showerror(APP_NAME, f"תמונת הייחוס לא נמצאה:\n{person_raw}")
                return None
            person = Path(person_raw)

        def year(e):
            v = e.get().strip()
            return int(v) if v.isdigit() else None

        return Options(
            sources=[Path(s) for s in srcs],
            output=Path(out),
            mode="move" if self.mode.get() == "העברה" else "copy",
            recursive=bool(self.recursive.get()),
            year_from=year(self.y_from),
            year_to=year(self.y_to),
            duplicates="keep" if self.dups.get() == "שמור" else "skip",
            use_filename_dates=bool(self.fname.get()),
            person_image=person,
            face_threshold=FACE_MODES.get(self.face_sens.get(), 0.363),
        )

    def _set_busy(self, busy: bool):
        self.busy = busy
        st = "disabled" if busy else "normal"
        self.btn_all.configure(state=st, fg_color=ACCENT_DIM if busy else ACCENT)
        self.btn_preview.configure(state=st)
        self.btn_undo.configure(state=st)
        # Stop is only meaningful (and only red) while something is running.
        self.btn_cancel.configure(state="normal" if busy else "disabled",
                                  border_color=RED if busy else BORDER_HI,
                                  text_color=RED if busy else FG)
        if busy:
            self.progress.set(0)
            self.progress.pack(fill="x", before=self.status)
        else:
            self.progress.pack_forget()

    def _progress(self, i, n, msg):
        self.q.put(("progress", i, n, msg))

    def _pump(self):
        try:
            while True:
                ev = self.q.get_nowait()
                if ev[0] == "progress":
                    _, i, n, msg = ev
                    if n > 0:
                        self.progress.set(i / n)
                        self._status(f"{i}/{n}   {msg}")
                    else:
                        self._status(msg)
                elif ev[0] == "row":       # live face mode: a match was just copied
                    self._add_row(ev[1], ev[2], index=0)
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
                self.q.put(("done", lambda e=e: finish(None, e)))
        threading.Thread(target=run, daemon=True).start()

    def _show(self, plan: Plan, executed: bool, extra: str = ""):
        self.tree.delete(*self.tree.get_children())
        out = plan.options.output

        def order(x):
            return (0 if x.dest else 1, str(x.dest.parent) if x.dest else "",
                    x.date.dt if x.date else 0, x.suffix, x.src.name)
        for it in sorted(plan.items, key=order):
            self._add_row(it, out)
        for p in plan.ignored:
            self.tree.insert("", "end", tags=(SKIPPED,),
                             values=(STATUS_HE[SKIPPED], p.name, "", rtl("לא קובץ תמונה/וידאו"), "-"))
        if self.tree.get_children():
            self.empty_lbl.place_forget()
        self._set_summary(hebrew_summary(plan, executed) + (f"\n\n{extra}" if extra else ""))

    def _add_row(self, it, out: Path, index="end"):
        dest = str(it.dest.relative_to(out)) if it.dest else (it.note or "-")
        src_col = it.date.label() if it.date else it.note
        if it.note and "face match" in it.note:
            src_col = f"{src_col} • {it.note}"
        self.tree.insert("", index, tags=(it.status,), values=(
            STATUS_HE.get(it.status, it.status), it.src.name,
            it.date.dt.strftime("%Y-%m-%d %H:%M") if it.date else "",
            rtl(src_col or ""), dest))
        self.empty_lbl.place_forget()

    # ------------------------------------------------------------------ actions
    def on_organize_all(self):
        """Paste-and-go: scan, optionally filter by face, plan and organize in one click."""
        opts = self._read_options()
        if not opts:
            return
        if opts.mode == "move" and not self.quiet:
            if not mbox.askyesno(APP_NAME, "מצב העברה: הקבצים יועברו מהתיקייה המקורית.\nלהמשיך?"):
                return

        def work():
            if opts.person_image:
                # Face search: each match is copied the moment it is found, and shows up
                # at the top of the table. Stop keeps (and logs, for undo) what was done.
                plan, manifest = organize_live(
                    opts, self._progress, self.cancel,
                    on_item=lambda it: self.q.put(("row", it, opts.output)))
                return plan, write_reports(plan, manifest, executed=True) if manifest else None
            plan = build_plan(opts, self._progress, self.cancel)
            if self.cancel.is_set():
                return plan, None
            manifest = execute(plan, self._progress, self.cancel)
            return plan, write_reports(plan, manifest, executed=True)

        def finish(res, err):
            self._set_busy(False)
            if err:
                mbox.showerror(APP_NAME, f"שגיאה: {err}")
                return
            plan, report = res
            self.plan = None
            done = sum(1 for it in plan.items if it.status == DONE)
            self._show(plan, executed=True, extra=f"דוח מלא:\n{report}" if report else "")
            self.progress.set(1)
            self._status(f"הסתיים! {done} קבצים סודרו לתוך {opts.output}")
            if not self.quiet and mbox.askyesno(
                    APP_NAME, f"הסתיים.\n\n{done} קבצים סודרו.\n\nלפתוח את התיקייה המסודרת?"):
                self.on_open()

        if opts.person_image:
            self.tree.delete(*self.tree.get_children())
            self._set_summary("חיפוש פנים פעיל\n"
                              "כל תמונה שמזוהה מועתקת מיד לתיקיית היעד ומופיעה בראש הטבלה.\n"
                              "אפשר לעצור בכל רגע. מה שכבר הועתק נשמר.")
        self._status("סורק ומסדר…")
        self._thread(work, finish)

    def on_preview(self):
        opts = self._read_options()
        if not opts:
            return

        def finish(plan, err):
            self._set_busy(False)
            if err:
                mbox.showerror(APP_NAME, str(err))
                return
            self.plan = plan
            n = sum(1 for it in plan.items if it.status == PLANNED)
            self._show(plan, executed=False,
                       extra="זו רק תצוגה מקדימה - שום דבר לא שונה.\nלחץ על הכפתור הירוק לביצוע.")
            self._status(f"תצוגה מקדימה מוכנה: {n} קבצים יסודרו.")
            if n:
                self.btn_all.configure(text=self._all_label(n))

        self._thread(lambda: build_plan(opts, self._progress, self.cancel), finish)

    def on_open(self):
        out = clean_path(self.out_var.get())
        if out and Path(out).exists():
            if sys.platform == "win32":
                os.startfile(out)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", out])
            else:
                subprocess.Popen(["xdg-open", out])
        else:
            mbox.showinfo(APP_NAME, "התיקייה המסודרת עדיין לא קיימת.")

    def on_check_updates(self):
        self.btn_update.configure(state="disabled", text=rtl("בודק עדכונים…"))
        self._status("בודק עדכונים מול GitHub Releases…")

        def finish(info, err):
            self._set_busy(False)
            self.btn_update.configure(state="normal", text=rtl("בדוק עדכונים"))
            if err:
                self._status("בדיקת עדכונים נכשלה.")
                mbox.showerror(APP_NAME, f"לא ניתן לבדוק עדכונים:\n{err}")
                return
            if not info.is_newer:
                self._status(f"התוכנה מעודכנת (v{__version__}).")
                mbox.showinfo(APP_NAME, f"התוכנה מעודכנת.\n\nגרסה מותקנת: v{__version__}")
                return
            notes = (info.notes[:400] + "…") if len(info.notes) > 400 else info.notes
            msg = f"גרסה חדשה זמינה: {info.tag} (מותקן: v{__version__})\n"
            if notes:
                msg += f"\n{notes}\n"
            if info.asset_url and getattr(sys, "frozen", False):
                msg += f"\nלהוריד ולהתקין את {info.asset_name} עכשיו?"
                if mbox.askyesno(APP_NAME, msg):
                    self._apply_update(info)
            else:
                msg += "\nלפתוח את עמוד ההורדה ב-GitHub?"
                if mbox.askyesno(APP_NAME, msg):
                    webbrowser.open(info.html_url)

        self._thread(check_for_updates, finish)

    def _apply_update(self, info):
        def finish(res, err):
            self._set_busy(False)
            if err:
                mbox.showerror(APP_NAME, f"הורדת העדכון נכשלה:\n{err}")
                return
            if res == "restarting":
                self.destroy()
            else:
                mbox.showinfo(APP_NAME, f"העדכון הורד אל:\n{res}")

        self._thread(lambda: download_and_apply(info, self._progress), finish)

    def on_undo(self):
        out = clean_path(self.out_var.get())
        m = latest_undo(Path(out)) if out else None
        if not m:
            mbox.showinfo(APP_NAME, "לא נמצאה ריצה קודמת בתיקיית היעד.")
            return
        if not mbox.askyesno(APP_NAME, f"לבטל את הריצה {m.name}?\n"
                                             "קבצים שהועתקו יימחקו מהיעד (המקור לא נפגע)."):
            return

        def finish(res, err):
            self._set_busy(False)
            mbox.showinfo(APP_NAME, f"בוטל: {res or err}")
            self._status("הריצה האחרונה בוטלה.")
        self._thread(lambda: undo(m, self._progress), finish)

    # ------------------------------------------------------------------ docs helper
    def _screenshot_when_idle(self, path: str):
        if self.busy or self.tree.get_children() == ():
            self.after(300, lambda: self._screenshot_when_idle(path))
            return
        self.after(900, lambda: self._grab(path))

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
    """GUI entry.  Optional:  <folder>  pre-fills the source path.
    Docs helper:  --screenshot OUT.png SRC DEST [--person IMG] [--auto-run]"""
    argv = list(sys.argv[1:] if argv is None else argv)
    shot = preset = person = None
    auto = "--auto-run" in argv
    if "--person" in argv:
        person = argv[argv.index("--person") + 1]
    if "--screenshot" in argv:
        i = argv.index("--screenshot")
        shot, preset = argv[i + 1], (argv[i + 2], argv[i + 3])
    elif argv and not argv[0].startswith("-"):
        preset = (clean_path(argv[0]), "")   # e.g. folder dropped onto the .exe icon
    _enable_dpi_awareness()
    App(screenshot=shot, preset=preset, person=person, auto_run=auto).mainloop()


if __name__ == "__main__":
    main()
