"""Tab 1 - draw the sheet layout once on a blank reference sheet."""
import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import cv2
import numpy as np
from PIL import Image, ImageTk

from omr import Template, Field, OMRProcessor
from omr.imageio import iter_pages
from omr.template import parse_options
from .common import show_result_window

PRESETS = {     # header fields the user can tick; defaults are only a starting point, everything is editable
    "Name":           dict(kind="id", orient="columns", count=20, options="A-Z", blank=" "),
    "Hall Ticket No": dict(kind="id", orient="columns", count=10, options="0-9", blank="?"),
    "Booklet Code":   dict(kind="id", orient="columns", count=7, options="0-9", blank="?"),
    "Set":            dict(kind="id", orient="rows", count=1, options="A-D", blank="?"),
    "Subject":        dict(kind="id", orient="columns", count=2, options="0-9", blank="?"),
}
BLANK_CHOICES = {"<empty>": "", "<space>": " ", "?": "?", "-": "-", "_": "_", "0": "0"}


def blank_label(v):
    return next((k for k, x in BLANK_CHOICES.items() if x == v), v)


class FieldDialog(tk.Toplevel):
    def __init__(self, parent, field: Field, fixed_kind=None, taken=(), parts=()):
        super().__init__(parent)
        self.title("Field settings")
        self.transient(parent); self.resizable(False, False)
        self.result = None
        self.f, self.taken = field, set(taken)
        self.parts = list(dict.fromkeys([*parts, field.part] if field.part else parts))
        f = field
        self.v_name = tk.StringVar(value=f.name)
        self.v_kind = tk.StringVar(value=f.kind)
        self.v_orient = tk.StringVar(value=f.orient)
        self.v_count = tk.IntVar(value=f.count)
        self.v_opts = tk.StringVar(value=f.options)
        self.v_multi = tk.BooleanVar(value=f.multi)
        self.v_blank = tk.StringVar(value=blank_label(f.blank))
        self.v_part = tk.StringVar(value=f.part or f.name)
        self.v_start = tk.IntVar(value=f.start)
        self.had_region = f.has_region()
        self._probe_default = float(getattr(getattr(parent, "template", None), "probe", 0.28))
        self.v_probe = tk.StringVar(value=str(f.probe if f.probe > 0 else self._probe_default))
        self.v_x = tk.StringVar(value=f"{f.x:g}"); self.v_y = tk.StringVar(value=f"{f.y:g}")
        self.v_w = tk.StringVar(value=f"{f.w:g}"); self.v_h = tk.StringVar(value=f"{f.h:g}")
        self.v_info = tk.StringVar()

        body = ttk.Frame(self, padding=10); body.pack()
        r = 0
        ttk.Label(body, text="Field name").grid(row=r, column=0, sticky="w")
        self.e_name = ttk.Entry(body, textvariable=self.v_name, width=28); self.e_name.grid(row=r, column=1, columnspan=3, sticky="w"); r += 1
        if fixed_kind is None:
            ttk.Label(body, text="Type").grid(row=r, column=0, sticky="w")
            ttk.Radiobutton(body, text="Identification value (name, roll no...)", variable=self.v_kind, value="id",
                            command=self._sync).grid(row=r, column=1, columnspan=3, sticky="w"); r += 1
            ttk.Radiobutton(body, text="Answers (list of questions)", variable=self.v_kind, value="answers",
                            command=self._sync).grid(row=r, column=1, columnspan=3, sticky="w"); r += 1
            ttk.Radiobutton(body, text="Subject bubbles for an answer part", variable=self.v_kind, value="subject",
                            command=self._sync).grid(row=r, column=1, columnspan=3, sticky="w"); r += 1
            ttk.Radiobutton(body, text="Handwritten text (TrOCR handwriting model)", variable=self.v_kind,
                            value="handwriting", command=self._sync).grid(row=r, column=1, columnspan=3, sticky="w"); r += 1
        self.l_layout = ttk.Label(body, text="Bubble layout")
        self.l_layout.grid(row=r, column=0, sticky="w")
        ttk.Radiobutton(body, text="Each COLUMN = one position (options run top to bottom)\n   e.g. hall ticket digits, name letters",
                        variable=self.v_orient, value="columns", command=self._sync).grid(row=r, column=1, columnspan=3, sticky="w"); r += 1
        ttk.Radiobutton(body, text="Each ROW = one position (options run left to right)\n   e.g. questions A B C D, a single 'Set' row",
                        variable=self.v_orient, value="rows", command=self._sync).grid(row=r, column=1, columnspan=3, sticky="w"); r += 1
        self.l_count = ttk.Label(body, text="Positions"); self.l_count.grid(row=r, column=0, sticky="w")
        ttk.Spinbox(body, from_=1, to=500, textvariable=self.v_count, width=6, command=self._sync).grid(row=r, column=1, sticky="w"); r += 1
        self.l_opts = ttk.Label(body, text="Options")
        self.l_opts.grid(row=r, column=0, sticky="w")
        self.e_opts = ttk.Entry(body, textvariable=self.v_opts, width=28)
        self.e_opts.grid(row=r, column=1, columnspan=3, sticky="w"); r += 1
        quick = ttk.Frame(body); quick.grid(row=r, column=1, columnspan=3, sticky="w")
        for q in ("0-9", "1-9,0", "A-Z", "A-D", "A-E", "1-4", "1-5", "T,F"):
            ttk.Button(quick, text=q, width=5, command=lambda q=q: (self.v_opts.set(q), self._sync())).pack(side="left")
        r += 1
        ttk.Label(body, text="(ranges like A-D, 0-9, 9-0 or a list like 1,2,3,4 / Yes,No, in the order printed on the sheet)",
                  foreground="#666").grid(row=r, column=0, columnspan=4, sticky="w"); r += 1
        self.cb_multi = ttk.Checkbutton(body, text="Allow several marks in one position (answer like 'AC')", variable=self.v_multi)
        self.cb_multi.grid(row=r, column=0, columnspan=4, sticky="w"); r += 1
        ttk.Label(body, text="Unmarked position shows as").grid(row=r, column=0, sticky="w")
        ttk.Combobox(body, textvariable=self.v_blank, values=list(BLANK_CHOICES), width=10).grid(row=r, column=1, sticky="w"); r += 1
        geo = ttk.LabelFrame(body, text="Region geometry (pixels)", padding=4)
        geo.grid(row=r, column=0, columnspan=4, sticky="ew", pady=3)
        for col, (label, var) in enumerate((("Left X", self.v_x), ("Top Y", self.v_y), ("Width", self.v_w), ("Height", self.v_h))):
            ttk.Label(geo, text=label).grid(row=0, column=col, padx=2)
            ttk.Entry(geo, textvariable=var, width=8).grid(row=1, column=col, padx=2)
        r += 1
        ttk.Label(body, text="Bubble probe size (0 uses global)").grid(row=r, column=0, sticky="w")
        self.probe_spin = ttk.Spinbox(body, from_=0.0, to=0.6, increment=0.01, textvariable=self.v_probe, width=7)
        self.probe_spin.grid(row=r, column=1, sticky="w"); r += 1
        self.fr_ans = ttk.LabelFrame(body, text="Answers / part subject", padding=6)
        self.fr_ans.grid(row=r, column=0, columnspan=4, sticky="ew", pady=6); r += 1
        self.l_part = ttk.Label(self.fr_ans, text="Part / section name")
        self.l_part.grid(row=0, column=0, sticky="w")
        self.e_part = ttk.Entry(self.fr_ans, textvariable=self.v_part, width=22)
        self.e_part.grid(row=0, column=1, sticky="w")
        self.cb_part = ttk.Combobox(self.fr_ans, textvariable=self.v_part, values=self.parts, width=20, state="readonly")
        self.cb_part.grid(row=0, column=1, sticky="w")
        self.l_start = ttk.Label(self.fr_ans, text="First question no.")
        self.l_start.grid(row=1, column=0, sticky="w")
        self.w_start = ttk.Spinbox(self.fr_ans, from_=1, to=9999, textvariable=self.v_start, width=6)
        self.w_start.grid(row=1, column=1, sticky="w")
        self.l_tip = ttk.Label(self.fr_ans, text="Tip: same-name answer blocks merge into one part.", foreground="#666")
        self.l_tip.grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Label(body, textvariable=self.v_info, foreground="#0a5").grid(row=r, column=0, columnspan=4, sticky="w"); r += 1
        bt = ttk.Frame(body); bt.grid(row=r, column=0, columnspan=4, sticky="e", pady=(8, 0))
        ttk.Button(bt, text="OK - now drag the region", command=self._ok).pack(side="right", padx=4)
        ttk.Button(bt, text="Cancel", command=self.destroy).pack(side="right")
        self.v_opts.trace_add("write", lambda *_: self._sync())
        self.v_count.trace_add("write", lambda *_: self._sync())
        self._sync()
        self.grab_set(); self.wait_visibility(); self.focus_set()
        self.wait_window(self)

    def _sync(self):
        answers = self.v_kind.get() == "answers"
        subject = self.v_kind.get() == "subject"
        handwriting = self.v_kind.get() == "handwriting"
        self.e_name.configure(state="disabled" if answers or subject else "normal")
        if answers or subject:
            self.fr_ans.grid()
        else:
            self.fr_ans.grid_remove()
        self.l_part.configure(text="Part / section name" if answers else "Answer part")
        self.e_part.grid() if answers else self.e_part.grid_remove()
        self.cb_part.grid() if subject else self.cb_part.grid_remove()
        self.fr_ans.configure(text="Answers" if answers else "Subject bubbles")
        if answers:
            self.l_start.grid(); self.w_start.grid(); self.l_tip.grid()
        else:
            self.l_start.grid_remove(); self.w_start.grid_remove(); self.l_tip.grid_remove()
        if subject:
            self.fr_ans.configure(text="Subject bubbles for this part")
        self.l_opts.configure(text="Allowed characters" if handwriting else "Options")
        self.l_layout.configure(text="Character-box layout" if handwriting else "Bubble layout")
        self.probe_spin.configure(state="disabled" if handwriting else "normal")
        self.cb_multi.configure(state="disabled" if handwriting or subject else "normal")
        unit = "questions" if answers else ("character boxes" if handwriting else "characters")
        self.l_count.configure(text=("Number of columns" if self.v_orient.get() == "columns" else "Number of rows") + f" (= {unit})")
        try:
            n_opt = len(parse_options(self.v_opts.get())); c = self.v_count.get()
            rows, cols = (n_opt, c) if self.v_orient.get() == "columns" else (c, n_opt)
            self.v_info.set((f"OCR boxes = {c} character(s); box size follows the drawn region" if handwriting else
                             f"Bubble grid = {rows} rows x {cols} columns  ({n_opt} options)"))
        except Exception:
            self.v_info.set("")

    def _ok(self):
        try:
            count, start = int(self.v_count.get()), int(self.v_start.get())
            geometry = tuple(float(v.get()) for v in (self.v_x, self.v_y, self.v_w, self.v_h))
            probe = float(self.v_probe.get())
        except Exception:
            return messagebox.showerror("Field", "Use numbers for count, geometry, probe size, and first question.", parent=self)
        opts = parse_options(self.v_opts.get())
        if len(opts) < 2 or count < 1:
            return messagebox.showerror("Field", "Need at least 2 options and 1 position.", parent=self)
        f, kind = self.f, self.v_kind.get()
        f.kind, f.orient, f.count, f.options = kind, self.v_orient.get(), count, self.v_opts.get()
        f.multi = bool(self.v_multi.get())
        b = self.v_blank.get()
        f.blank = BLANK_CHOICES.get(b, b)
        if kind == "answers":
            f.part = self.v_part.get().strip() or "Answers"
            f.start = start
            f.name = f"{f.part} Q{start}-{start + count - 1}"
        elif kind == "subject":
            f.part = self.v_part.get().strip()
            if not f.part:
                return messagebox.showerror("Field", "Choose the answer part for this subject field.", parent=self)
            f.start = 1
            f.name = f"{f.part} Subject"
        else:
            f.name = self.v_name.get().strip()
            f.part, f.start = "", 1
            if not f.name:
                return messagebox.showerror("Field", "Give the field a name.", parent=self)
        if f.name in self.taken:
            return messagebox.showerror("Field", f"'{f.name}' already exists.", parent=self)
        if not 0 <= probe <= 0.6:
            return messagebox.showerror("Field", "Bubble probe size must be between 0 and 0.60.", parent=self)
        if self.had_region:
            if geometry[0] < 0 or geometry[1] < 0 or geometry[2] < 5 or geometry[3] < 5:
                return messagebox.showerror("Field", "Region needs non-negative X/Y and width/height of at least 5 pixels.", parent=self)
            f.x, f.y, f.w, f.h = geometry
        f.probe = 0.0 if abs(probe - self._probe_default) < 1e-9 and f.probe == 0 else probe
        self.result = f
        self.destroy()


class Designer(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.template = Template()
        self.ref_bgr = None
        self.pil = None
        self.zoom = 0.5
        self.pending = None
        self._rb = None
        self.template_path = None
        self._tk_img = None
        self._build()

    # ------------------------------------------------------------ UI
    def _build(self):
        left = ttk.Frame(self, padding=6, width=330); left.pack(side="left", fill="y")
        ttk.Button(left, text="1. Open BLANK reference sheet (JPG)...", command=self.open_reference).pack(fill="x")
        ttk.Button(left, text="Open saved template...", command=self.open_template).pack(fill="x", pady=2)

        box = ttk.LabelFrame(left, text="2. Tick what this sheet contains", padding=6); box.pack(fill="x", pady=6)
        self.vars = {}
        for name in PRESETS:
            v = tk.BooleanVar(value=False); self.vars[name] = v
            ttk.Checkbutton(box, text=name, variable=v, command=lambda n=name: self._toggle_preset(n)).pack(anchor="w")
        ttk.Button(box, text="+ Answers part / block", command=self.add_answers).pack(fill="x", pady=(6, 1))
        ttk.Button(box, text="+ Subject bubbles for a part", command=self.add_subject).pack(fill="x")
        ttk.Button(box, text="+ Handwritten field (OCR)", command=self.add_handwritten).pack(fill="x", pady=(2, 0))
        ttk.Button(box, text="+ Written booklet code (OCR)", command=self.add_written_booklet_code).pack(fill="x", pady=(2, 0))
        ttk.Button(box, text="+ Other field (custom)", command=self.add_custom).pack(fill="x", pady=(2, 0))

        ttk.Label(left, text="Fields: select, then Edit or Redraw. Arrow keys move; Shift+arrows resize; Ctrl+arrows move/resize faster.", wraplength=315).pack(anchor="w")
        self.lb = tk.Listbox(left, height=11, exportselection=False, width=44)
        self.lb.pack(fill="x"); self.lb.bind("<<ListboxSelect>>", self._field_selected)
        self.lb.bind("<KeyPress-Left>", self._keyboard_nudge); self.lb.bind("<KeyPress-Right>", self._keyboard_nudge)
        self.lb.bind("<KeyPress-Up>", self._keyboard_nudge); self.lb.bind("<KeyPress-Down>", self._keyboard_nudge)
        row = ttk.Frame(left); row.pack(fill="x", pady=2)
        ttk.Button(row, text="Edit", width=8, command=self.edit_selected).pack(side="left")
        ttk.Button(row, text="Redraw region", command=self.redraw_region).pack(side="left", padx=2)
        ttk.Button(row, text="Delete", width=8, command=self.delete_selected).pack(side="left")

        st = ttk.LabelFrame(left, text="Reading settings", padding=6); st.pack(fill="x", pady=6)
        self.v_align = tk.StringVar(value="auto"); self.v_delta = tk.StringVar(value="0.22")
        self.v_min = tk.StringVar(value="0.30"); self.v_probe = tk.StringVar(value="0.28")
        for i, (lab, var, w) in enumerate([("Alignment", self.v_align, None), ("Mark sensitivity (lower = more sensitive)", self.v_delta, 1),
                                           ("Min. darkness of a mark", self.v_min, 1), ("Bubble probe size", self.v_probe, 1)]):
            ttk.Label(st, text=lab).grid(row=i, column=0, sticky="w")
            if w is None:
                ttk.Combobox(st, textvariable=var, values=["auto", "features", "page", "none"], width=9, state="readonly").grid(row=i, column=1)
            else:
                ttk.Spinbox(st, from_=0.05, to=0.6, increment=0.01, textvariable=var, width=7).grid(row=i, column=1)
            var.trace_add("write", lambda *_: self._settings())
        ttk.Button(left, text="3. Test on a scanned sheet...", command=self.test_sheet).pack(fill="x")
        ttk.Button(left, text="4. Save template...", command=self.save_template).pack(fill="x", pady=2)
        self.status = tk.StringVar(value="Open a blank reference sheet to begin.")
        ttk.Label(left, textvariable=self.status, wraplength=310, foreground="#0645ad").pack(fill="x", pady=6)

        right = ttk.Frame(self); right.pack(side="left", fill="both", expand=True)
        bar = ttk.Frame(right); bar.pack(fill="x")
        ttk.Button(bar, text="Zoom +", command=lambda: self.set_zoom(self.zoom * 1.25)).pack(side="left")
        ttk.Button(bar, text="Zoom -", command=lambda: self.set_zoom(self.zoom / 1.25)).pack(side="left")
        ttk.Button(bar, text="Fit width", command=self.fit).pack(side="left")
        self.canvas = tk.Canvas(right, bg="#555", highlightthickness=0, takefocus=1)
        sx = ttk.Scrollbar(right, orient="horizontal", command=self.canvas.xview)
        sy = ttk.Scrollbar(right, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=sx.set, yscrollcommand=sy.set)
        sy.pack(side="right", fill="y"); sx.pack(side="bottom", fill="x"); self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-1 * (e.delta // 120), "units"))
        for key in ("Left", "Right", "Up", "Down"):
            self.canvas.bind(f"<{key}>", self._keyboard_nudge)
            self.canvas.bind(f"<Shift-{key}>", self._keyboard_nudge)
            self.canvas.bind(f"<Control-{key}>", self._keyboard_nudge)
            self.canvas.bind(f"<Control-Shift-{key}>", self._keyboard_nudge)

    # ------------------------------------------------------------ helpers
    def current(self):
        """(template, reference image) for the scanner tab"""
        return self.template, self.ref_bgr

    def _settings(self):
        t = self.template
        t.align = self.v_align.get()
        for attr, var in (("mark_delta", self.v_delta), ("mark_min", self.v_min), ("probe", self.v_probe)):
            try:
                setattr(t, attr, float(var.get()))
            except ValueError:
                pass

    def _load_settings_to_ui(self):
        t = self.template
        self.v_align.set(t.align); self.v_delta.set(str(t.mark_delta)); self.v_min.set(str(t.mark_min)); self.v_probe.set(str(t.probe))

    def _refresh_list(self, keep=None):
        self.lb.delete(0, "end")
        for f in self.template.fields:
            mark = "OK " if f.has_region() else "-- "
            self.lb.insert("end", f"{mark}{f.name}   [{f.options} x{f.count}, {'cols' if f.orient == 'columns' else 'rows'}]")
        if keep is not None and 0 <= keep < len(self.template.fields):
            self.lb.selection_set(keep)
        present = {f.name for f in self.template.fields}
        for n, v in self.vars.items():
            v.set(n in present)

    def selected(self):
        s = self.lb.curselection()
        return self.template.fields[s[0]] if s else None

    def _need_ref(self):
        if self.ref_bgr is None:
            messagebox.showinfo("Reference", "Open a blank reference sheet first.")
            return False
        return True

    # ------------------------------------------------------------ image handling
    def open_reference(self):
        p = filedialog.askopenfilename(title="Blank reference sheet (JPG)",
                                       filetypes=[("JPG images", "*.jpg *.jpeg")])
        if not p:
            return
        try:
            _, img = next(iter_pages(p))
        except Exception as e:
            return messagebox.showerror("Open", str(e))
        self.ref_bgr = img
        self.pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        self.template.ref_h, self.template.ref_w = img.shape[:2]
        self.fit()
        self.status.set("Tick the fields present on the sheet. For each one: fill the dialog, then DRAG a rectangle "
                        "tightly around ALL its bubbles (outer edge of first to outer edge of last).")

    def fit(self):
        if self.pil:
            self.set_zoom(max(0.1, (self.canvas.winfo_width() - 20) / self.pil.width) if self.canvas.winfo_width() > 50 else 0.5)

    def set_zoom(self, z):
        if not self.pil:
            return
        self.zoom = min(4.0, max(0.1, z))
        w, h = int(self.pil.width * self.zoom), int(self.pil.height * self.zoom)
        self._tk_img = ImageTk.PhotoImage(self.pil.resize((w, h), Image.BILINEAR))
        self.canvas.delete("bg")
        self.canvas.create_image(0, 0, anchor="nw", image=self._tk_img, tags="bg")
        self.canvas.tag_lower("bg")
        self.canvas.configure(scrollregion=(0, 0, w, h))
        self.redraw()

    def redraw(self):
        c, z = self.canvas, self.zoom
        c.delete("ov")
        sel = self.selected()
        for f in self.template.fields:
            if not f.has_region():
                continue
            col = "#e00000" if f is sel else ("#00a000" if f.kind == "id" else "#0060d0")
            c.create_rectangle(f.x * z, f.y * z, (f.x + f.w) * z, (f.y + f.h) * z, outline=col, width=2, tags="ov")
            c.create_text(f.x * z, f.y * z - 8, text=f.part or f.name, anchor="sw", fill=col, font=("Arial", 9, "bold"), tags="ov")
            if f.kind == "handwriting":
                for i in range(1, f.count):
                    if f.orient == "columns":
                        x = (f.x + f.w * i / f.count) * z
                        c.create_line(x, f.y * z, x, (f.y + f.h) * z, fill=col, dash=(2, 2), tags="ov")
                    else:
                        y = (f.y + f.h * i / f.count) * z
                        c.create_line(f.x * z, y, (f.x + f.w) * z, y, fill=col, dash=(2, 2), tags="ov")
                continue
            cw, ch = f.cell_size()
            probe = f.probe if f.probe > 0 else self.template.probe
            r = max(1.5, probe * min(cw, ch) * z)
            for _, _, cx, cy in f.centers():
                c.create_oval(cx * z - r, cy * z - r, cx * z + r, cy * z + r, outline=col, tags="ov")

    # ------------------------------------------------------------ region drawing
    def _press(self, e):
        self.canvas.focus_set()
        if self.pending is None:
            return
        self._x0, self._y0 = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        self._rb = self.canvas.create_rectangle(self._x0, self._y0, self._x0, self._y0, outline="red", dash=(4, 2), width=2)

    def _drag(self, e):
        if self._rb:
            self.canvas.coords(self._rb, self._x0, self._y0, self.canvas.canvasx(e.x), self.canvas.canvasy(e.y))

    def _release(self, e):
        if not self._rb:
            return
        x1, y1 = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        self.canvas.delete(self._rb); self._rb = None
        z = self.zoom
        x, y = min(self._x0, x1) / z, min(self._y0, y1) / z
        w, h = abs(x1 - self._x0) / z, abs(y1 - self._y0) / z
        if w < 5 or h < 5:
            return
        f = self.pending
        f.x, f.y, f.w, f.h = x, y, w, h
        if f not in self.template.fields:
            self.template.fields.append(f)
        self.pending = None
        self._refresh_list(self.template.fields.index(f))
        self.redraw()
        hint = "Check that each OCR box covers one handwritten character." if f.kind == "handwriting" else "Check that every circle sits on a bubble."
        self.status.set(f"'{f.name}' placed. {hint} Adjust position with arrow keys (Shift+arrows resize).")

    def _nudge(self, dx, dy, resize):
        f = self.selected()
        if not f:
            return
        if resize:
            f.w = max(4, f.w + dx); f.h = max(4, f.h + dy)
        else:
            f.x += dx; f.y += dy
        self.redraw()

    def _field_selected(self, _event=None):
        self.redraw()
        self.lb.focus_set()

    def _keyboard_nudge(self, event):
        delta = 10 if event.state & 0x4 else 1
        dx, dy = {"Left": (-delta, 0), "Right": (delta, 0), "Up": (0, -delta), "Down": (0, delta)}.get(event.keysym, (0, 0))
        if dx or dy:
            self._nudge(dx, dy, bool(event.state & 0x1))
            return "break"

    # ------------------------------------------------------------ field actions
    def _new_field(self, name, preset=None, kind=None):
        if not self._need_ref():
            return False
        f = Field(name=name, **(preset or {}))
        if kind:
            f.kind = kind
            if kind == "answers":
                f.orient, f.count, f.options = "rows", 25, "A-D"
                f.part = name
        d = FieldDialog(self, f, fixed_kind=None, taken={x.name for x in self.template.fields})
        if d.result is None:
            return False
        self.pending = d.result
        self.status.set(f"Now DRAG a rectangle around all bubbles of '{d.result.name}' on the sheet.")
        return True

    def add_subject(self):
        parts = list(dict.fromkeys(f.part for f in self.template.answer_fields() if f.part))
        if not parts:
            return messagebox.showinfo("Part subject", "Add an answers part first.", parent=self)
        f = Field(name=f"{parts[0]} Subject", kind="subject", orient="rows", count=1,
                  options="Mathematics & Science, Social Science", blank="?", part=parts[0])
        d = FieldDialog(self, f, taken={x.name for x in self.template.fields}, parts=parts)
        if d.result is None:
            return
        self.pending = d.result
        self.status.set(f"Drag a rectangle around the subject bubbles for {d.result.part}.")

    def add_handwritten(self):
        f = Field(name="Name Written", kind="handwriting", orient="columns", count=20,
                  options="A-Z", blank="")
        d = FieldDialog(self, f, taken={x.name for x in self.template.fields})
        if d.result is None:
            return
        self.pending = d.result
        self.status.set(f"Drag a rectangle around the {d.result.count} handwritten character boxes.")

    def add_written_booklet_code(self):
        f = Field(name="Booklet Code Written", kind="handwriting", orient="columns", count=7,
                  options="0-9", blank="")
        d = FieldDialog(self, f, taken={x.name for x in self.template.fields})
        if d.result is None:
            return
        self.pending = d.result
        self.status.set(f"Drag a rectangle around the {d.result.count} handwritten booklet-code boxes.")

    def _toggle_preset(self, name):
        if self.vars[name].get():
            if not self._new_field(name, PRESETS[name]):
                self.vars[name].set(False)
        else:
            f = self.template.get(name)
            if f and messagebox.askyesno("Remove", f"Remove field '{name}'?"):
                self.template.fields.remove(f)
                self._refresh_list(); self.redraw()
            elif f:
                self.vars[name].set(True)

    def add_answers(self):
        nxt = 1
        for f in self.template.answer_fields():
            nxt = max(nxt, f.start + f.count)
        p = Field(name="Part A", kind="answers", orient="rows", count=25, options="A-D", part="Part A", start=1)
        # continue numbering after existing blocks
        if self.template.answer_fields():
            p.part = self.template.answer_fields()[-1].part; p.start = nxt
            p.count, p.options = self.template.answer_fields()[-1].count, self.template.answer_fields()[-1].options
        self._new_field(p.part, dict(kind="answers", orient="rows", count=p.count, options=p.options, part=p.part, start=p.start))

    def add_custom(self):
        self._new_field("Custom", dict(kind="id", orient="columns", count=5, options="0-9", blank="?"))

    def edit_selected(self):
        f = self.selected()
        if not f:
            return
        parts = list(dict.fromkeys(x.part for x in self.template.answer_fields() if x.part))
        d = FieldDialog(self, f, taken={x.name for x in self.template.fields if x is not f}, parts=parts)
        if d.result:
            self._refresh_list(self.template.fields.index(f)); self.redraw()

    def redraw_region(self):
        f = self.selected()
        if f:
            self.pending = f
            self.status.set(f"Drag a new rectangle for '{f.name}'.")

    def delete_selected(self):
        f = self.selected()
        if f and messagebox.askyesno("Delete", f"Delete '{f.name}'?"):
            self.template.fields.remove(f)
            self._refresh_list(); self.redraw()

    # ------------------------------------------------------------ template file
    def save_template(self):
        if not self._need_ref():
            return
        missing = [f.name for f in self.template.fields if not f.has_region()]
        if missing:
            return messagebox.showwarning("Save", "No region drawn yet for: " + ", ".join(missing))
        p = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("OMR template", "*.json")],
                                         initialfile=(self.template.name or "template") + ".json")
        if not p:
            return
        folder = os.path.dirname(os.path.abspath(p))
        stem = os.path.splitext(os.path.basename(p))[0]
        self.template.name = stem
        ref_png = os.path.join(folder, stem + "_reference.png")
        cv2.imencode(".png", self.ref_bgr)[1].tofile(ref_png)
        self.template.reference = os.path.basename(ref_png)
        self.template.save(p)
        self.template_path = p
        self.status.set(f"Saved {p}. Go to tab 2 to scan.")

    def open_template(self):
        p = filedialog.askopenfilename(filetypes=[("OMR template", "*.json")])
        if not p:
            return
        try:
            t = Template.load(p)
            ref = cv2.imdecode(np.fromfile(t.reference_path, dtype=np.uint8), cv2.IMREAD_COLOR)
            if ref is None:
                raise FileNotFoundError(t.reference_path)
        except Exception as e:
            return messagebox.showerror("Open template", str(e))
        self.template, self.ref_bgr, self.template_path = t, ref, p
        self.pil = Image.fromarray(cv2.cvtColor(ref, cv2.COLOR_BGR2RGB))
        self._load_settings_to_ui(); self._refresh_list(); self.fit()
        self.status.set(f"Loaded {os.path.basename(p)}")

    def test_sheet(self):
        if not self._need_ref():
            return
        p = filedialog.askopenfilename(title="A scanned / filled sheet (JPG)",
                                       filetypes=[("JPG images", "*.jpg *.jpeg")])
        if not p:
            return
        try:
            self._settings()
            page, img = next(iter_pages(p))
            res = OMRProcessor(self.template, self.ref_bgr).process(img, p, page)
            show_result_window(self, res, self.ref_bgr)
        except Exception as e:
            messagebox.showerror("Test", str(e))
