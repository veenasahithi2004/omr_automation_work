"""Tab 2 - pick template + sheets + MDB file, scan, review, store."""
import os
import re
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import cv2
import numpy as np

from omr import Template, OMRProcessor, process_paths, export_csv
from omr.imageio import read_image, collect_files
from omr.mdb import MdbStore, MdbError
from .common import review_result_window


class Scanner(ttk.Frame):
    def __init__(self, master, designer_source=None):
        super().__init__(master, padding=8)
        self.designer_source = designer_source
        self.template = None
        self.template_path = None
        self.ref = None
        self.files = []
        self.results = []
        self.result_templates = {}
        self.q = queue.Queue()
        self.v_tpl = tk.StringVar(value="(no template loaded)")
        self.v_files = tk.StringVar(value="0 JPG sheets")
        self.v_mdb = tk.StringVar()
        self.v_review = tk.BooleanVar(value=True)
        self.v_debug = tk.BooleanVar(value=False)
        self.v_status = tk.StringVar(value="Ready.")
        self.v_filter = tk.StringVar(value="All sheets")
        self._handwriting_model_notice = False
        self._build()

    def _build(self):
        top = ttk.LabelFrame(self, text="Inputs", padding=8); top.pack(fill="x")
        ttk.Label(top, text="Template").grid(row=0, column=0, sticky="w")
        ttk.Label(top, textvariable=self.v_tpl, foreground="#0645ad").grid(row=0, column=1, sticky="w", padx=8)
        ttk.Button(top, text="Load template...", command=self.load_template).grid(row=0, column=2)
        ttk.Button(top, text="Use template from tab 1", command=self.use_designer).grid(row=0, column=3, padx=4)

        ttk.Label(top, text="OMR sheets").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Label(top, textvariable=self.v_files).grid(row=1, column=1, sticky="w", padx=8)
        ttk.Button(top, text="Insert one JPG...", command=self.add_one).grid(row=1, column=2)
        ttk.Button(top, text="Insert many JPGs...", command=self.add_many).grid(row=1, column=3, padx=4)
        ttk.Button(top, text="Insert root folder...", command=self.add_folder).grid(row=1, column=4, padx=4)
        ttk.Button(top, text="Clear", command=self.clear_files).grid(row=1, column=5)

        ttk.Label(top, text="Save to MDB").grid(row=2, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.v_mdb, width=70).grid(row=2, column=1, sticky="w", padx=8)
        ttk.Button(top, text="Browse...", command=self.pick_mdb).grid(row=2, column=2)

        opt = ttk.Frame(top); opt.grid(row=3, column=1, columnspan=4, sticky="w", pady=4)
        ttk.Checkbutton(opt, text="Review results before saving (required for handwriting OCR)", variable=self.v_review).pack(side="left")
        ttk.Checkbutton(opt, text="Also write annotated images (folder 'omr_debug' beside the MDB)", variable=self.v_debug).pack(side="left", padx=12)

        act = ttk.Frame(self); act.pack(fill="x", pady=6)
        self.btn_run = ttk.Button(act, text="  START SCAN  ", command=self.start); self.btn_run.pack(side="left")
        self.pb = ttk.Progressbar(act, length=320); self.pb.pack(side="left", padx=10)
        ttk.Label(act, textvariable=self.v_status).pack(side="left")

        mid = ttk.Frame(self); mid.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(mid, show="headings", selectmode="browse")
        sy = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        sx = ttk.Scrollbar(mid, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        sy.pack(side="right", fill="y"); sx.pack(side="bottom", fill="x"); self.tree.pack(fill="both", expand=True)
        self.tree.tag_configure("CHECK", background="#fff0c8")
        self.tree.tag_configure("BLANK", background="#eeeeee")
        self.tree.tag_configure("DUPLICATE", background="#ffdce0")
        self.tree.tag_configure("SAVED", foreground="#666")
        self.tree.bind("<Double-1>", self._dbl)
        self.tree.bind("<Return>", lambda e: self.view_selected())

        bt = ttk.Frame(self); bt.pack(fill="x", pady=4)
        ttk.Button(bt, text="View selected sheet", command=self.view_selected).pack(side="left")
        ttk.Button(bt, text="Edit selected results...", command=self.edit_selected).pack(side="left", padx=6)
        ttk.Button(bt, text="Save to MDB", command=self.save_mdb).pack(side="left", padx=6)
        ttk.Button(bt, text="Export CSV...", command=self.export).pack(side="left")
        ttk.Label(bt, text="Show:").pack(side="left", padx=(14, 4))
        self.filter_box = ttk.Combobox(bt, textvariable=self.v_filter,
                                       values=("All sheets", "OK", "CHECK", "BLANK", "DUPLICATE", "CHECK or DUPLICATE"),
                                       state="readonly", width=21)
        self.filter_box.pack(side="left")
        self.filter_box.bind("<<ComboboxSelected>>", lambda _e: self._refresh_rows())
        ttk.Label(bt, text="Review is on by default; edit IDs and each answer before saving.", foreground="#666").pack(side="left", padx=12)

    # ------------------------------------------------------------ inputs
    def _set_template(self, t, ref, label, path=None):
        self.template, self.ref = t, ref
        inferred = os.path.join(t.base_dir, f"{t.name}.json") if t.base_dir and t.name else None
        self.template_path = os.path.abspath(path) if path else (inferred if inferred and os.path.isfile(inferred) else None)
        self.v_tpl.set(label)
        self._configure_result_columns()

    def _configure_result_columns(self):
        names = list(f.name for f in self.template.id_fields()) if self.template else []
        for result in self.results:
            for name in result.ids:
                if name not in names:
                    names.append(name)
        self.cols = ["File", "Page", "Status"] + names + ["Answered"]
        self.tree.configure(columns=self.cols)
        for c in self.cols:
            self.tree.heading(c, text=c)
            self.tree.column(c, width=90 if c in ("Page", "Status") else 150, anchor="w")

    def load_template(self):
        p = filedialog.askopenfilename(filetypes=[("OMR template", "*.json")])
        if not p:
            return
        try:
            t = Template.load(p)
            ref = read_image(t.reference_path)
            if ref is None:
                raise FileNotFoundError(t.reference_path)
        except Exception as e:
            return messagebox.showerror("Template", str(e))
        self._set_template(t, ref, os.path.basename(p), p)

    def use_designer(self):
        t, ref = self.designer_source()
        if ref is None or not (t.id_fields() or t.answer_fields()):
            return messagebox.showinfo("Template", "Design a template in tab 1 first.")
        self._set_template(t, ref, f"{t.name} (from tab 1, unsaved)")

    def add_one(self):
        path = filedialog.askopenfilename(
            title="Insert one OMR sheet (JPG)",
            filetypes=[("JPG images", "*.jpg *.jpeg")],
        )
        if path:
            self._add_jpgs([path])

    def add_many(self):
        paths = filedialog.askopenfilenames(
            title="Insert multiple OMR sheets (JPG)",
            filetypes=[("JPG images", "*.jpg *.jpeg")],
        )
        if paths:
            self._add_jpgs(paths)

    def add_folder(self):
        """Add JPGs from the selected root and every nested subfolder."""
        folder = filedialog.askdirectory(title="Choose root folder; JPGs in all subfolders will be added")
        if folder:
            self._add_jpgs([folder])

    def _add_jpgs(self, paths):
        existing = {os.path.normcase(os.path.abspath(p)) for p in self.files}
        added = []
        for path in collect_files(paths):
            if os.path.splitext(path)[1].lower() not in (".jpg", ".jpeg"):
                continue
            key = os.path.normcase(os.path.abspath(path))
            if key not in existing:
                added.append(path)
                existing.add(key)
        self.files.extend(added)
        self.v_files.set(f"{len(self.files)} JPG sheet(s) from selected files/folders")

    def clear_files(self):
        self.files = []
        self.v_files.set("0 JPG sheets")

    def pick_mdb(self):
        p = filedialog.asksaveasfilename(defaultextension=".mdb", filetypes=[("Access MDB", "*.mdb")], confirmoverwrite=False)
        if p:
            self.v_mdb.set(p)

    # ------------------------------------------------------------ scan
    def start(self):
        if self.template is None:
            return messagebox.showinfo("Scan", "Load a template first.")
        if not self.files:
            return messagebox.showinfo("Scan", "Add some OMR sheets first.")
        has_handwriting = any(f.enabled and f.kind == "handwriting" for f in self.template.fields)
        if has_handwriting and not self._handwriting_model_notice:
            if not messagebox.askyesno(
                "Handwriting model",
                "The first handwritten scan downloads Microsoft's TrOCR handwriting model (about 1.33 GB) "
                "and caches it on this computer. OMR images are processed locally. Continue?",
                parent=self,
            ):
                return
            self._handwriting_model_notice = True
        if has_handwriting:
            self.v_review.set(True)
            self.v_status.set("Handwritten OCR enabled: review values before saving.")
        if not self.v_mdb.get().strip() and not self.v_review.get():
            return messagebox.showinfo("Scan", "Choose the .mdb file to save into (or tick 'Review before saving').")
        # The review panel can edit field layout settings in memory. Rebuild the
        # result columns from that current template before a rescan.
        self._set_template(self.template, self.ref, self.v_tpl.get())
        self.results = []
        self.result_templates = {}
        self._configure_result_columns()
        self.tree.delete(*self.tree.get_children())
        self.btn_run.configure(state="disabled")
        self.pb.configure(maximum=len(self.files), value=0)
        dbg = None
        if self.v_debug.get() and self.v_mdb.get():
            dbg = os.path.join(os.path.dirname(os.path.abspath(self.v_mdb.get())), "omr_debug")
        threading.Thread(target=self._worker, args=(list(self.files), dbg), daemon=True).start()
        self.after(120, self._poll)

    def _worker(self, files, dbg):
        try:
            proc = OMRProcessor(self.template, self.ref)

            def cb(n, total, r):
                if r.debug is not None:                       # keep RAM small: store as jpeg bytes
                    r.debug = cv2.imencode(".jpg", r.debug, [cv2.IMWRITE_JPEG_QUALITY, 80])[1]
                if r.aligned_image is not None:                # preserve a clean image for review-region remapping
                    r.aligned_image = cv2.imencode(".jpg", r.aligned_image, [cv2.IMWRITE_JPEG_QUALITY, 92])[1]
                self.q.put(("res", n, total, r))

            process_paths(proc, files, cb, debug_dir=dbg)
            self.q.put(("done",))
        except Exception as e:
            self.q.put(("error", str(e)))

    def _poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == "res":
                    _, n, total, r = m
                    self.pb.configure(value=n)
                    if not r.template_name:
                        r.template_name = self.template.name
                    self.results.append(r)
                    self.result_templates[id(r)] = (self.template, self.ref, self.template_path)
                    self.v_status.set(f"{n}/{total}  {os.path.basename(r.source)}")
                    self._add_row(r, len(self.results) - 1)
                elif m[0] == "error":
                    self.btn_run.configure(state="normal")
                    return messagebox.showerror("Scan", m[1])
                else:
                    self.btn_run.configure(state="normal")
                    self._reconcile_duplicates()
                    self._refresh_rows()
                    check_count = sum(r.status == "CHECK" for r in self.results)
                    blank_count = sum(r.status == "BLANK" for r in self.results)
                    duplicate_count = sum(r.duplicate for r in self.results)
                    self.v_status.set(f"Done: {len(self.results)} sheets, {check_count} CHECK, {blank_count} BLANK, {duplicate_count} duplicate hall-ticket number(s).")
                    if not self.v_review.get():
                        self.save_mdb(show_message=False)
                    self._notify_scan_complete()
                    return
        except queue.Empty:
            pass
        self.after(120, self._poll)

    def _notify_scan_complete(self):
        """Play the system notification bell and confirm the whole batch finished."""
        try:
            import winsound
            winsound.MessageBeep()
        except Exception:
            try:
                self.bell()
            except Exception:
                pass
        count = len(self.files)
        messagebox.showinfo(
            "Scan complete",
            f"All {count} JPG file(s) have been recognised.\n\n"
            f"{len(self.results)} sheet result(s) are ready to review or save.",
            parent=self,
        )

    def _row_values(self, r):
        tot = sum(len(d) for d in r.answers.values())
        ans = sum(1 for d in r.answers.values() for v in d.values() if v)
        return [os.path.basename(r.source), r.page, r.status] + [r.ids.get(c, "") for c in self.cols[3:-1]] + [f"{ans}/{tot}"]

    @staticmethod
    def _normalized_id(value):
        return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())

    def _hall_ticket_field_name(self, template=None):
        template = template or self.template
        if template is None:
            return None
        fields = [f for f in template.fields if f.kind in ("id", "handwriting")]
        exact_names = {"hallticketno", "hallticketnumber", "hallticket", "halltktno", "halltktnumber"}
        for field in fields:
            name = re.sub(r"[^a-z0-9]", "", field.name.lower())
            if name in exact_names:
                return field.name
        for field in fields:
            name = re.sub(r"[^a-z0-9]", "", field.name.lower())
            if "hallticket" in name or "halltkt" in name:
                return field.name
        return None

    def _reconcile_duplicates(self):
        groups = {}
        field_names = {}
        for index, result in enumerate(self.results):
            selected_template = self.result_templates.get(id(result), (self.template, self.ref, None))[0]
            field_name = self._hall_ticket_field_name(selected_template)
            field_names[index] = field_name
            if field_name:
                value = self._normalized_id(result.ids.get(field_name, ""))
                if value:
                    groups.setdefault(value, []).append(index)
        duplicates = {index for group in groups.values() if len(group) > 1 for index in group}
        for index, result in enumerate(self.results):
            result.warnings = [warning for warning in result.warnings
                               if not warning.startswith("Duplicate hall ticket number:")]
            result.duplicate = index in duplicates
            if result.duplicate:
                field_name = field_names.get(index)
                raw = result.ids.get(field_name, "").strip()
                result.warnings.append(f"Duplicate hall ticket number: {raw}")

    def _matches_filter(self, result):
        selected = self.v_filter.get()
        if selected == "All sheets":
            return True
        if selected == "OK":
            return result.status == "OK"
        if selected == "BLANK":
            return result.status == "BLANK"
        if selected == "CHECK":
            return result.status == "CHECK"
        if selected == "DUPLICATE":
            return result.duplicate
        if selected == "CHECK or DUPLICATE":
            return result.status in ("CHECK", "DUPLICATE")
        return True

    def _insert_row(self, index, result):
        tags = []
        if result.duplicate:
            tags.append("DUPLICATE")
        elif result.status == "CHECK":
            tags.append("CHECK")
        elif result.status == "BLANK":
            tags.append("BLANK")
        if result.saved:
            tags.append("SAVED")
        self.tree.insert("", "end", iid=str(index), values=self._row_values(result), tags=tuple(tags))

    def _add_row(self, result, index):
        if self._matches_filter(result):
            self._insert_row(index, result)

    def _refresh_rows(self):
        if not hasattr(self, "tree") or self.template is None:
            return
        self._configure_result_columns()
        self.tree.delete(*self.tree.get_children())
        for index, result in enumerate(self.results):
            if self._matches_filter(result):
                self._insert_row(index, result)

    # ------------------------------------------------------------ review / save
    def _dbl(self, e):
        row, col = self.tree.identify_row(e.y), self.tree.identify_column(e.x)
        if not row or not col:
            return
        self.edit_selected(int(row))

    def edit_selected(self, index=None):
        if index is None:
            selected = self.tree.selection()
            if not selected:
                return messagebox.showinfo("Edit results", "Select a scanned sheet first.", parent=self)
            index = int(selected[0])
        if index < 0 or index >= len(self.results):
            return
        self._review_result(index)

    def _review_result(self, index):
        result = self.results[index]
        template, ref, path = self.result_templates.get(
            id(result), (self.template, self.ref, self.template_path))
        if review_result_window(self, result, template, ref,
                                template_path=path,
                                on_template_change=lambda sheet, new_path, progress=None:
                                    self._switch_review_template(sheet, new_path, progress)):
            self._reconcile_duplicates()
            self._refresh_rows()

    def _switch_review_template(self, result, path, progress=None):
        """Re-recognize only the selected sheet using another saved JSON layout."""
        new_template = Template.load(path)
        new_ref = read_image(new_template.reference_path)
        if new_ref is None:
            raise FileNotFoundError(new_template.reference_path)
        if any(field.enabled and field.kind == "handwriting" for field in new_template.fields) and not self._handwriting_model_notice:
            if not messagebox.askyesno(
                "Handwriting model",
                "This template uses handwriting OCR. Its first run downloads Microsoft's TrOCR model (about 1.33 GB). Continue?",
                parent=self,
            ):
                raise RuntimeError("Template change cancelled.")
            self._handwriting_model_notice = True

        processor = OMRProcessor(new_template, new_ref)
        image = read_image(result.source)
        if image is None:
            raise FileNotFoundError(f"Could not reopen scanned JPG: {result.source}")
        fresh = processor.process(image, result.source, result.page)
        fresh.saved = result.saved
        fresh.template_name = new_template.name or os.path.splitext(os.path.basename(path))[0]
        if fresh.debug is not None:
            fresh.debug = cv2.imencode(".jpg", fresh.debug, [cv2.IMWRITE_JPEG_QUALITY, 80])[1]
        if fresh.aligned_image is not None:
            fresh.aligned_image = cv2.imencode(".jpg", fresh.aligned_image, [cv2.IMWRITE_JPEG_QUALITY, 92])[1]
        result.__dict__.update(fresh.__dict__)
        self.result_templates[id(result)] = (new_template, new_ref, os.path.abspath(path))
        if progress:
            progress(1, 1, os.path.basename(result.source))
        self._reconcile_duplicates()
        self._refresh_rows()
        self.v_status.set(f"Loaded {os.path.basename(path)} for {os.path.basename(result.source)} only.")
        return new_template, new_ref

    def view_selected(self):
        s = self.tree.selection()
        if s:
            self._review_result(int(s[0]))

    def save_mdb(self, show_message=True):
        path = self.v_mdb.get().strip()
        if not self.results:
            return messagebox.showinfo("Save", "Nothing to save yet.")
        if not path:
            return messagebox.showinfo("Save", "Choose the .mdb file first.")
        try:
            store = MdbStore(path, self.template)
            n = store.save(self.results)
            store.close()
        except MdbError as e:
            return messagebox.showerror("MDB", str(e))
        except Exception as e:
            return messagebox.showerror("MDB", f"{type(e).__name__}: {e}\n\nTip: use Export CSV to keep the data.")
        self._refresh_rows()
        self.v_status.set(f"Saved {n} sheet(s) to {os.path.basename(path)}")
        if show_message:
            messagebox.showinfo("MDB", f"Stored {n} sheet(s) in\n{path}")

    def export(self):
        if not self.results:
            return
        p = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if p:
            export_csv(self.results, p)
            self.v_status.set("CSV written: " + p)
