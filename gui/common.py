import tkinter as tk
import os
from tkinter import ttk, simpledialog, messagebox, filedialog
import cv2
from PIL import Image, ImageTk
from omr.template import Field, Template, parse_options
from omr.reader import darkness_map, read_field, read_handwriting


def to_photo(bgr, max_w=None, max_h=None, zoom=None):
    """BGR ndarray (or jpeg-encoded 1-D array) -> ImageTk.PhotoImage"""
    if bgr.ndim == 1:
        bgr = cv2.imdecode(bgr, cv2.IMREAD_COLOR)
    h, w = bgr.shape[:2]
    if zoom is None:
        zoom = min((max_w or w) / w, (max_h or h) / h, 1.0)
    im = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
    if zoom != 1.0:
        im = im.resize((max(1, int(w * zoom)), max(1, int(h * zoom))), Image.LANCZOS)
    return ImageTk.PhotoImage(im)


def show_result_window(parent, result, blank_reference=None):
    """Review an annotated scan and switch to the blank reference when useful."""
    win = tk.Toplevel(parent)
    win.title(f"{result.source}  (page {result.page})  -  {result.status}")
    win.geometry("1250x850")
    left = ttk.Frame(win); left.pack(side="left", fill="both", expand=True)
    right = ttk.Frame(win, width=340); right.pack(side="right", fill="y")

    bar = ttk.Frame(left); bar.pack(fill="x")
    cv = tk.Canvas(left, bg="#444")
    sx = ttk.Scrollbar(left, orient="horizontal", command=cv.xview)
    sy = ttk.Scrollbar(left, orient="vertical", command=cv.yview)
    cv.configure(xscrollcommand=sx.set, yscrollcommand=sy.set)
    sy.pack(side="right", fill="y"); sx.pack(side="bottom", fill="x"); cv.pack(fill="both", expand=True)
    def show_image(image):
        if image is None:
            return
        win._photo = to_photo(image, max_w=880, max_h=None)
        cv.delete("all")
        cv.create_image(0, 0, anchor="nw", image=win._photo)
        cv.configure(scrollregion=(0, 0, win._photo.width(), win._photo.height()))

    ttk.Button(bar, text="Show scanned sheet / results", command=lambda: show_image(result.debug)).pack(side="left", padx=3, pady=3)
    if blank_reference is not None:
        ttk.Button(bar, text="Show blank reference", command=lambda: show_image(blank_reference)).pack(side="left", padx=3, pady=3)
    # Start on the blank reference so a reviewer can compare the template
    # layout before switching to the annotated scanned sheet.
    show_image(blank_reference if blank_reference is not None else result.debug)
    cv.bind("<MouseWheel>", lambda e: cv.yview_scroll(-1 * (e.delta // 120), "units"))

    txt = tk.Text(right, width=42, wrap="word", font=("Consolas", 10))
    txt.pack(fill="both", expand=True, padx=6, pady=6)
    txt.insert("end", f"Alignment: {result.align_method}\n\n")
    for k, v in result.ids.items():
        txt.insert("end", f"{k}: {v}\n")
    for part in result.answers:
        txt.insert("end", f"\n{part}\n")
        d = result.answers[part]
        line = ""
        for q in sorted(d):
            line += f"{q}:{d[q] or '-'}  "
            if len(line) > 34:
                txt.insert("end", line + "\n"); line = ""
        txt.insert("end", line + "\n")
    if result.warnings:
        txt.insert("end", "\nWARNINGS\n" + "\n".join("! " + w for w in result.warnings))
    txt.configure(state="disabled")
    return win


def edit_result_window(parent, result):
    """Edit recognized ID and per-question values before the result is saved."""
    if result.saved:
        messagebox.showinfo("Edit results", "This sheet is already saved. Edit results before saving to the MDB.", parent=parent)
        return False

    win = tk.Toplevel(parent)
    win.title(f"Edit scan results — {result.source}")
    win.geometry("720x620")
    ttk.Label(win, text="Double-click a value to correct it. Use Done to apply changes before saving.",
              padding=8).pack(fill="x")
    tree = ttk.Treeview(win, columns=("component", "value"), show="headings", selectmode="browse")
    tree.heading("component", text="Field / question")
    tree.heading("value", text="Recognized value")
    tree.column("component", width=300, anchor="w")
    tree.column("value", width=360, anchor="w")
    tree.pack(fill="both", expand=True, padx=8, pady=4)
    sy = ttk.Scrollbar(tree, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sy.set)
    sy.pack(side="right", fill="y")

    draft_ids = dict(result.ids)
    draft_answers = {part: dict(values) for part, values in result.answers.items()}
    row_data = {}
    committed = False
    for name, value in draft_ids.items():
        iid = f"id:{len(row_data)}"
        row_data[iid] = ("id", name, None)
        tree.insert("", "end", iid=iid, values=(name, value))
    for part, questions in draft_answers.items():
        parent_iid = f"part:{len(row_data)}"
        tree.insert("", "end", iid=parent_iid, values=(part, ""), open=True)
        for q, value in sorted(questions.items()):
            iid = f"answer:{len(row_data)}"
            row_data[iid] = ("answer", part, q)
            tree.insert(parent_iid, "end", iid=iid, values=(f"{part} — Q{q}", value))

    def edit_value(event):
        row, column = tree.identify_row(event.y), tree.identify_column(event.x)
        if not row or row not in row_data or column != "#2":
            return
        kind, key, qno = row_data[row]
        current = draft_ids[key] if kind == "id" else draft_answers[key][qno]
        value = simpledialog.askstring("Correct recognized value", f"{key if kind == 'id' else f'{key} — Q{qno}'}:",
                                       initialvalue=current, parent=win)
        if value is None:
            return
        if kind == "id":
            draft_ids[key] = value
        else:
            draft_answers[key][qno] = value
        tree.set(row, "value", value)

    tree.bind("<Double-1>", edit_value)
    buttons = ttk.Frame(win); buttons.pack(fill="x", padx=8, pady=8)

    def apply_changes():
        nonlocal committed
        result.ids.clear(); result.ids.update(draft_ids)
        result.answers.clear(); result.answers.update(draft_answers)
        result.warnings = [w for w in result.warnings if not w.endswith(": no answers marked")]
        for part, questions in result.answers.items():
            if not any(str(value).strip() for value in questions.values()):
                result.warnings.append(f"{part}: no answers marked")
        committed = True
        win.destroy()

    ttk.Button(buttons, text="Done", command=apply_changes).pack(side="right", padx=4)
    ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side="right")
    win.transient(parent); win.grab_set()
    win.wait_window()
    return committed


def review_result_window(parent, result, template, blank_reference=None, template_path=None, on_template_change=None):
    """Review an OMR scan by selecting a result attribute and mapping its image region."""
    if result.saved:
        messagebox.showinfo("Review", "This sheet is already saved. Review and correct sheets before saving to MDB.", parent=parent)
        return False

    win = tk.Toplevel(parent)
    win.title(f"OMR review — {result.source} — page {result.page}")
    win.geometry("1420x850")
    win.minsize(1050, 650)
    win.transient(parent)

    ttk.Label(win, text=f"Review scan  ·  {result.source}  ·  Page {result.page}",
              font=("Segoe UI", 12, "bold"), padding=9).pack(fill="x")
    panes = ttk.Panedwindow(win, orient="horizontal")
    panes.pack(fill="both", expand=True, padx=8, pady=4)
    image_pane = ttk.Frame(panes, padding=5)
    values_pane = ttk.Frame(panes, padding=8, width=430)
    panes.add(image_pane, weight=3)
    panes.add(values_pane, weight=2)

    image_header = ttk.Frame(image_pane)
    image_header.pack(fill="x", pady=(0, 5))
    ttk.Label(image_header, text="Sheet image · drag to assign a region · zoom: Shift +/− or keypad +/-",
              font=("Segoe UI", 10, "bold")).pack(side="left", anchor="w")
    zoom_value = tk.StringVar(value="100%")
    zoom_controls = ttk.Frame(image_header)
    zoom_controls.pack(side="right")
    zoom_factor = {"value": 1.0}
    canvas = tk.Canvas(image_pane, background="#3e4248", highlightthickness=0)
    sx = ttk.Scrollbar(image_pane, orient="horizontal", command=canvas.xview)
    sy = ttk.Scrollbar(image_pane, orient="vertical", command=canvas.yview)
    canvas.configure(xscrollcommand=sx.set, yscrollcommand=sy.set)
    sy.pack(side="right", fill="y"); sx.pack(side="bottom", fill="x"); canvas.pack(fill="both", expand=True)

    # Use the clean aligned JPG. The annotated debug overlay can obscure marks in a remapped crop.
    scan_image = result.aligned_image
    if scan_image is not None and getattr(scan_image, "ndim", 0) == 1:
        scan_image = cv2.imdecode(scan_image, cv2.IMREAD_COLOR)
    if scan_image is None:
        scan_image = result.debug
        if scan_image is not None and getattr(scan_image, "ndim", 0) == 1:
            scan_image = cv2.imdecode(scan_image, cv2.IMREAD_COLOR)
    if scan_image is None:
        ttk.Label(image_pane, text="The scanned sheet image is unavailable.").pack()

    shown = {"image": scan_image, "photo": None, "scale": 1.0, "fit_scale": 1.0}
    selected = {"field": None, "item": None}
    mapping = {"active": False, "start": None, "rect": None}
    committed = {"value": False}

    def find_answer_field(part, q):
        return next((f for f in template.answer_fields()
                     if (f.part or f.name) == part and f.start <= q < f.start + f.count), None)

    template_path = template_path or getattr(parent, "template_path", None)
    template_folder = template.base_dir or (os.path.dirname(template_path) if template_path else "")
    template_paths = {}
    if template_folder and os.path.isdir(template_folder):
        for filename in sorted(os.listdir(template_folder), key=str.casefold):
            if filename.lower().endswith(".json"):
                template_paths[filename] = os.path.join(template_folder, filename)
    if template_path and os.path.isfile(template_path):
        template_paths.setdefault(os.path.basename(template_path), template_path)
    current_template_label = os.path.basename(template_path) if template_path else "Current designer template (unsaved)"
    template_var = tk.StringVar(value=current_template_label)
    ttk.Label(values_pane, text="Saved template (.json)", font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(5, 3))
    template_controls = ttk.Frame(values_pane); template_controls.pack(fill="x", pady=(0, 7))
    template_combo = ttk.Combobox(template_controls, textvariable=template_var,
                                  values=tuple(dict.fromkeys([current_template_label, *template_paths.keys()])),
                                  state="readonly")
    template_combo.pack(side="left", fill="x", expand=True)
    ttk.Button(template_controls, text="Browse .json…", command=lambda: browse_template()).pack(side="left", padx=(5, 0))
    ttk.Label(values_pane, text="Selecting a saved template re-reads this sheet with its saved field positions.",
              wraplength=390, foreground="#666").pack(anchor="w", pady=(0, 5))

    # Each editable result row also carries the template field that produced it.
    row_meta = {}
    tree = ttk.Treeview(values_pane, columns=("attribute", "value"), show="headings", selectmode="browse")
    tree.heading("attribute", text="Attribute")
    tree.heading("value", text="Recognized value · editable")
    tree.column("attribute", width=190, anchor="w")
    tree.column("value", width=210, anchor="w")
    tree.pack(fill="both", expand=True)
    tree_scroll = ttk.Scrollbar(tree, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=tree_scroll.set)
    tree_scroll.pack(side="right", fill="y")

    def populate_result_rows():
        row_meta.clear()
        tree.delete(*tree.get_children())
        for name, value in result.ids.items():
            field = next((f for f in template.fields if f.name == name and f.enabled), None)
            iid = f"id:{len(row_meta)}"
            row_meta[iid] = ("id", name, None, field)
            tree.insert("", "end", iid=iid, values=(name, value))
        for part, questions in result.answers.items():
            for q, value in sorted(questions.items()):
                field = find_answer_field(part, q)
                iid = f"answer:{len(row_meta)}"
                label = f"{part} · Q{q}"
                row_meta[iid] = ("answer", part, q, field)
                tree.insert("", "end", iid=iid, values=(label, value))

    populate_result_rows()

    status = tk.StringVar(value="Select an attribute row, then choose Map region to drag its box on the sheet.")
    ttk.Label(values_pane, textvariable=status, wraplength=390, foreground="#555").pack(fill="x", pady=(7, 5))

    def use_template_path(path):
        nonlocal template, blank_reference, template_path, template_folder, current_template_label, scan_image
        if not path or not on_template_change:
            return
        path = os.path.abspath(path)
        if template_path and os.path.normcase(path) == os.path.normcase(os.path.abspath(template_path)):
            return
        if not messagebox.askyesno(
            "Re-read this sheet",
            f"Load {os.path.basename(path)} and re-recognize {os.path.basename(result.source)} with its saved field positions?\n\n"
            "Current recognition edits on this sheet will be replaced. Other sheets will not change.", parent=win,
        ):
            template_var.set(current_template_label)
            return
        try:
            def progress(done, total, filename):
                status.set(f"Loading template and re-reading this sheet… {done}/{total} · {filename}")
                win.update_idletasks()
            template, blank_reference = on_template_change(result, path, progress)
            template_path = path
            template_folder = os.path.dirname(path)
            current_template_label = os.path.basename(path)
            if os.path.isdir(template_folder):
                for filename in sorted(os.listdir(template_folder), key=str.casefold):
                    if filename.lower().endswith(".json"):
                        template_paths.setdefault(filename, os.path.join(template_folder, filename))
            template_paths[os.path.basename(path)] = path
            choices = tuple(dict.fromkeys([current_template_label, *template_paths.keys()]))
            template_combo.configure(values=choices)
            template_var.set(os.path.basename(path))
            scan_image = result.aligned_image
            if scan_image is not None and getattr(scan_image, "ndim", 0) == 1:
                scan_image = cv2.imdecode(scan_image, cv2.IMREAD_COLOR)
            shown.update(image=scan_image, photo=None, scale=1.0)
            selected.update(field=None, item=None)
            populate_result_rows()
            render_sheet()
            status.set(f"Loaded {os.path.basename(path)} for this sheet only; other sheets are unchanged.")
        except RuntimeError as exc:
            template_var.set(current_template_label)
            status.set(str(exc))
        except Exception as exc:
            template_var.set(current_template_label)
            status.set(f"Could not load template: {exc}")
            messagebox.showerror("Template", str(exc), parent=win)

    def on_template_selected(_event=None):
        use_template_path(template_paths.get(template_var.get()))

    def browse_template():
        path = filedialog.askopenfilename(
            parent=win, title="Choose a saved OMR template", initialdir=template_folder or None,
            filetypes=[("Saved OMR templates", "*.json"), ("JSON files", "*.json")])
        if path:
            use_template_path(path)

    template_combo.bind("<<ComboboxSelected>>", on_template_selected)
    controls = ttk.Frame(values_pane); controls.pack(fill="x")
    map_button = ttk.Button(controls, text="Map region to selected attribute")
    map_button.pack(fill="x", pady=2)
    ttk.Label(values_pane, text="Double-click a recognized value to edit it. Region changes apply to the selected result now; save the template in the designer to keep them for future scans.",
              wraplength=390, foreground="#666").pack(anchor="w", pady=(5, 0))

    def selected_row():
        items = tree.selection()
        if not items:
            return None
        return row_meta.get(items[0])

    def draw_overlay():
        if shown["image"] is None:
            return
        canvas.delete("region")
        scale = shown["scale"]
        for field in template.fields:
            if not field.enabled or not field.has_region():
                continue
            color = "#ff9e32" if field is selected["field"] else "#3bc584"
            canvas.create_rectangle(field.x*scale, field.y*scale,
                                    (field.x+field.w)*scale, (field.y+field.h)*scale,
                                    outline=color, width=2 if field is selected["field"] else 1,
                                    tags="region")
            canvas.create_text(field.x*scale+3, max(12, field.y*scale-2), text=field.name,
                               anchor="sw", fill=color, font=("Segoe UI", 8, "bold"), tags="region")

    def render_sheet():
        if shown["image"] is None:
            return
        win.update_idletasks()
        image_h, image_w = shown["image"].shape[:2]
        fit_w = max(350, image_pane.winfo_width()-40)
        fit_h = max(350, image_pane.winfo_height()-70)
        shown["fit_scale"] = min(fit_w/image_w, fit_h/image_h, 1.0)
        scale = shown["fit_scale"] * zoom_factor["value"]
        photo = to_photo(shown["image"], zoom=scale)
        shown["photo"] = photo
        shown["scale"] = photo.width()/shown["image"].shape[1]
        zoom_value.set(f"{round(zoom_factor['value'] * 100)}%")
        canvas.delete("all")
        canvas.create_image(0, 0, anchor="nw", image=photo, tags="sheet")
        canvas.configure(scrollregion=(0, 0, photo.width(), photo.height()))
        draw_overlay()

    def set_zoom(value):
        if shown["image"] is None:
            return
        old_x = canvas.xview()[0] if canvas.xview() else 0.0
        old_y = canvas.yview()[0] if canvas.yview() else 0.0
        zoom_factor["value"] = max(0.25, min(4.0, value))
        render_sheet()
        canvas.xview_moveto(old_x)
        canvas.yview_moveto(old_y)

    def zoom_in(_event=None):
        set_zoom(zoom_factor["value"] * 1.25)
        return "break"

    def zoom_out(_event=None):
        set_zoom(zoom_factor["value"] / 1.25)
        return "break"

    # Support the shifted symbol keys, the Shift+= form of '+', and keypad keys.
    for sequence in ("<Shift-plus>", "<Shift-equal>", "<KP_Add>", "<Shift-KP_Add>"):
        win.bind(sequence, zoom_in)
    for sequence in ("<Shift-minus>", "<KP_Subtract>", "<Shift-KP_Subtract>"):
        win.bind(sequence, zoom_out)

    ttk.Button(zoom_controls, text="−", width=3,
               command=lambda: set_zoom(zoom_factor["value"] / 1.25)).pack(side="left", padx=2)
    ttk.Button(zoom_controls, text="Fit", command=lambda: set_zoom(1.0)).pack(side="left", padx=2)
    ttk.Label(zoom_controls, textvariable=zoom_value, width=5, anchor="center").pack(side="left", padx=2)
    ttk.Button(zoom_controls, text="+", width=3,
               command=lambda: set_zoom(zoom_factor["value"] * 1.25)).pack(side="left", padx=2)
    save_corrections_button = ttk.Button(zoom_controls, text="Save corrections")
    save_corrections_button.pack(side="left", padx=(6, 2))

    def select_row(_event=None):
        meta = selected_row()
        selected["item"] = tree.selection()[0] if tree.selection() else None
        selected["field"] = meta[3] if meta else None
        if meta and meta[3]:
            status.set(f"Selected {tree.item(selected['item'], 'values')[0]}. Choose Map region, then drag a box on the sheet.")
        elif meta:
            status.set("No template region is linked to this row. Add/configure its field in the template designer first.")
        draw_overlay()

    tree.bind("<<TreeviewSelect>>", select_row)

    edit_box = {"widget": None}
    def finish_edit(_event=None, cancel=False):
        widget = edit_box["widget"]
        if widget is None:
            return
        iid = edit_box["iid"]
        value = widget.get()
        widget.destroy(); edit_box["widget"] = None
        if cancel or iid not in row_meta:
            return
        kind, key, q, _field = row_meta[iid]
        if kind == "id":
            result.ids[key] = value
        else:
            result.answers.setdefault(key, {})[q] = value
        tree.set(iid, "value", value)
        refresh_no_answer_warnings()

    def edit_cell(event):
        iid, column = tree.identify_row(event.y), tree.identify_column(event.x)
        if not iid or column != "#2" or iid not in row_meta:
            return
        if edit_box["widget"] is not None:
            finish_edit()
        box = tree.bbox(iid, column)
        if not box:
            return
        x, y, w, h = box
        editor = ttk.Entry(tree)
        editor.insert(0, tree.set(iid, "value"))
        editor.place(x=x, y=y, width=w, height=h)
        edit_box.update(widget=editor, iid=iid)
        editor.bind("<Return>", finish_edit)
        editor.bind("<Escape>", lambda _e: finish_edit(cancel=True))
        editor.bind("<FocusOut>", finish_edit)
        editor.focus_set(); editor.selection_range(0, "end")

    tree.bind("<Double-1>", edit_cell)

    def refresh_no_answer_warnings():
        result.warnings = [w for w in result.warnings if not w.endswith(": no answers marked")]
        for part, questions in result.answers.items():
            if not any(str(v).strip() for v in questions.values()):
                result.warnings.append(f"{part}: no answers marked")

    def remap_and_read(field, x, y, w, h):
        field.x, field.y, field.w, field.h = x, y, w, h
        try:
            if field.kind == "handwriting":
                reading = read_handwriting(shown["image"], field)
                value = "".join(reading.values).strip()
                result.ids[field.name] = value
                iid = selected["item"]
                tree.set(iid, "value", value)
                result.warnings = [warning for warning in result.warnings
                                   if not warning.startswith(field.name + ": handwriting OCR")]
                if not value:
                    result.warnings.append(f"{field.name}: handwriting OCR returned no characters")
                elif reading.confidence < .70:
                    result.warnings.append(f"{field.name}: handwriting OCR confidence is low; verify before saving")
            else:
                reading = read_field(darkness_map(shown["image"]), field, template)
                kind, key, q, _ = row_meta[selected["item"]]
                result.warnings = [warning for warning in result.warnings
                                   if not warning.startswith(field.name + ":")]
                if reading.problems:
                    result.warnings.append(f"{field.name}: {reading.problems} unreadable/ambiguous position(s)")
                if kind == "id":
                    value = "".join(reading.values).strip()
                    result.ids[key] = value
                    tree.set(selected["item"], "value", value)
                else:
                    part = field.part or field.name
                    answers = result.answers.setdefault(part, {})
                    for offset, value in enumerate(reading.values):
                        answers[field.start + offset] = value
                    for iid, (row_kind, row_part, row_q, row_field) in row_meta.items():
                        if row_kind == "answer" and row_part == part and row_field is field:
                            tree.set(iid, "value", result.answers[part].get(row_q, ""))
                refresh_no_answer_warnings()
            status.set(f"Mapped {field.name} and re-read its value. Choose Save corrections to keep this scan's edits.")
        except Exception as exc:
            status.set(f"Region saved, but recognition failed: {exc}")
        draw_overlay()

    def start_mapping():
        meta = selected_row()
        if not meta:
            return messagebox.showinfo("Map region", "Select an attribute row first.", parent=win)
        if not meta[3]:
            return messagebox.showinfo("Map region", "This attribute has no linked template field. Configure it in the template designer first.", parent=win)
        if shown["image"] is None:
            return messagebox.showerror("Map region", "The sheet image is unavailable.", parent=win)
        selected["field"] = meta[3]
        mapping["active"] = True
        status.set("Drag a rectangle around the selected attribute on the sheet image.")
        draw_overlay()

    map_button.configure(command=start_mapping)

    def drag_start(event):
        if not mapping["active"]:
            return
        x, y = canvas.canvasx(event.x), canvas.canvasy(event.y)
        mapping["start"] = (x, y)
        if mapping["rect"]:
            canvas.delete(mapping["rect"])
        mapping["rect"] = canvas.create_rectangle(x, y, x, y, outline="#ff922b", dash=(5, 3), width=2)

    def drag_move(event):
        if mapping["active"] and mapping["rect"]:
            canvas.coords(mapping["rect"], *mapping["start"], canvas.canvasx(event.x), canvas.canvasy(event.y))

    def drag_end(event):
        if not mapping["active"] or not mapping["start"]:
            return
        x0, y0 = mapping["start"]
        x1, y1 = canvas.canvasx(event.x), canvas.canvasy(event.y)
        scale = shown["scale"]
        image_h, image_w = shown["image"].shape[:2]
        x0, x1 = (max(0, min(v, image_w*scale))/scale for v in (x0, x1))
        y0, y1 = (max(0, min(v, image_h*scale))/scale for v in (y0, y1))
        x, y, w, h = min(x0, x1), min(y0, y1), abs(x1-x0), abs(y1-y0)
        if w >= 5 and h >= 5:
            remap_and_read(selected["field"], x, y, w, h)
        else:
            status.set("Region was too small. Choose Map region and drag a larger box.")
        mapping.update(active=False, start=None, rect=None)
        draw_overlay()

    canvas.bind("<ButtonPress-1>", drag_start)
    canvas.bind("<B1-Motion>", drag_move)
    canvas.bind("<ButtonRelease-1>", drag_end)
    canvas.bind("<MouseWheel>", lambda e: set_zoom(zoom_factor["value"] * (1.15 if e.delta > 0 else 1/1.15))
                if e.state & 0x0004 else canvas.yview_scroll(-1*(e.delta//120), "units"))

    footer = ttk.Frame(win, padding=8); footer.pack(fill="x")
    ttk.Label(footer, text="Edits apply to this scan and can be saved/exported from the Scan tab.",
              foreground="#555").pack(side="left", fill="x", expand=True)
    def apply_corrections():
        if edit_box["widget"] is not None:
            finish_edit()
        refresh_no_answer_warnings()
        committed["value"] = True
        win.destroy()

    save_corrections_button.configure(command=apply_corrections)

    def save_to_mdb():
        apply_corrections()
        save_callback = getattr(parent, "save_mdb", None)
        if callable(save_callback):
            parent.after_idle(save_callback)

    ttk.Button(footer, text="Cancel", command=win.destroy).pack(side="right", padx=4)
    if callable(getattr(parent, "save_mdb", None)):
        ttk.Button(footer, text="Save to MDB", command=save_to_mdb).pack(side="right", padx=4)
    render_sheet()
    win.grab_set(); win.wait_window()
    return committed["value"]
