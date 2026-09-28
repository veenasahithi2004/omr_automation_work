"""Glue: template + scan -> SheetResult. Also CSV export and debug overlays."""
from __future__ import annotations
import csv, os, re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import cv2
import numpy as np

from .align import Aligner
from .imageio import collect_files, iter_pages, read_image
from .reader import darkness_map, read_field, read_handwriting
from .template import Template


def safe_name(s: str) -> str:
    """column-safe name for MDB / CSV"""
    return re.sub(r"\W+", "_", s.strip()).strip("_") or "Field"


@dataclass
class SheetResult:
    source: str
    page: int = 1
    ids: Dict[str, str] = field(default_factory=dict)                 # field name -> text
    answers: Dict[str, Dict[int, str]] = field(default_factory=dict)  # part -> {question no: answer}
    warnings: List[str] = field(default_factory=list)
    required_fields: List[str] = field(default_factory=list)
    align_method: str = ""
    debug: Optional[np.ndarray] = None                                # annotated aligned image
    aligned_image: Optional[np.ndarray] = None                        # clean aligned JPG for review/editing
    duplicate: bool = False                                            # duplicate nonblank hall-ticket number
    template_name: str = ""
    saved: bool = False

    @property
    def status(self) -> str:
        if self.duplicate:
            return "DUPLICATE"
        if any(not str(self.ids.get(name, "")).strip() for name in self.required_fields):
            return "CHECK"
        if any(self.answers.values()) and not any(str(value).strip() for part in self.answers.values() for value in part.values()):
            return "BLANK"
        return "OK"

    def answer_string(self, part: str) -> str:
        qs = self.answers.get(part, {})
        return ",".join(qs[q] for q in sorted(qs))


class OMRProcessor:
    def __init__(self, template: Template, ref_bgr: np.ndarray | None = None, draw_debug: bool = True):
        self.t = template
        self.ref = ref_bgr if ref_bgr is not None else read_image(template.reference_path)
        if self.ref is None:
            raise FileNotFoundError(f"reference image not found: {template.reference_path}")
        self.aligner = Aligner(self.ref, template.align)
        self.draw_debug = draw_debug

    def process(self, bgr: np.ndarray, source: str = "", page: int = 1) -> SheetResult:
        t = self.t
        self.aligner.mode = t.align
        aligned, method = self.aligner.align(bgr)
        dark = darkness_map(aligned)
        res = SheetResult(source=source, page=page, align_method=method, template_name=t.name)
        required_names = {
            "hallticketno", "hallticketnumber", "hallticket", "halltktno", "halltktnumber",
            "class", "admissionnum", "admissionnumber", "admissionno", "admissionnumberno",
            "partsubject", "centercode",
        }
        for f in t.fields:
            if not f.enabled or not f.has_region() or f.kind not in ("id", "subject", "handwriting"):
                continue
            normalized = re.sub(r"[^a-z0-9]", "", f.name.lower())
            if normalized in required_names or ("hallticket" in normalized or "halltkt" in normalized
                    or ("subject" in normalized and normalized.startswith("part"))):
                res.required_fields.append(f.name)
        res.aligned_image = aligned.copy()
        if method.startswith("none") and t.align != "none":
            res.warnings.append("sheet could not be aligned - results may be wrong")
        readings = []
        answer_marked = {}
        for f in t.fields:
            if not f.enabled or not f.has_region():
                continue
            if f.kind == "handwriting":
                try:
                    rd = read_handwriting(aligned, f)
                except Exception as e:
                    from .reader import HandwritingReading
                    rd = HandwritingReading([""] * f.count)
                    res.warnings.append(f"{f.name}: handwriting OCR failed ({e})")
                readings.append((f, rd))
                text = "".join(rd.values).strip()
                res.ids[f.name] = text
                if not text and not any(f"{f.name}: handwriting OCR failed" in w for w in res.warnings):
                    res.warnings.append(f"{f.name}: handwriting OCR returned no characters")
                elif rd.confidence < 0.70:
                    res.warnings.append(f"{f.name}: handwriting OCR confidence is low; verify before saving")
                continue
            rd = read_field(dark, f, t)
            readings.append((f, rd))
            if f.kind in ("id", "subject"):
                res.ids[f.name] = "".join(rd.values).strip()
                if rd.problems:
                    res.warnings.append(f"{f.name}: {rd.problems} unreadable/ambiguous position(s)")
            else:
                part = f.part or f.name
                d = res.answers.setdefault(part, {})
                answer_marked[part] = answer_marked.get(part, 0) + int(np.count_nonzero(np.any(rd.marked, axis=1)))
                for i, v in enumerate(rd.values):
                    d[f.start + i] = v
                if rd.problems:
                    res.warnings.append(f"{part}: {rd.problems} question(s) with multiple marks")
        if self.draw_debug:
            res.debug = draw_debug(aligned, readings)
        return res


def draw_debug(aligned_bgr, readings):
    img = aligned_bgr.copy()
    overlay = img.copy()
    for f, rd in readings:
        col = (150, 0, 180) if f.kind == "handwriting" else ((0, 160, 0) if f.kind in ("id", "subject") else (200, 90, 0))
        cv2.rectangle(img, (int(f.x), int(f.y)), (int(f.x + f.w), int(f.y + f.h)), col, 2)
        cv2.putText(img, f.part or f.name, (int(f.x), max(12, int(f.y) - 6)), cv2.FONT_HERSHEY_SIMPLEX, .55, col, 2)
        if f.kind == "handwriting":
            txt = "".join(rd.values).strip()
            if txt:
                cv2.putText(img, txt, (int(f.x), int(f.y + f.h + 20)), cv2.FONT_HERSHEY_SIMPLEX, .6, (0, 0, 220), 2)
            continue
        flat = rd.marked.ravel()
        for (cx, cy), m in zip(rd.centers, flat):
            c = (int(cx), int(cy))
            if m:
                cv2.circle(overlay, c, int(rd.radius) + 2, (0, 200, 0), -1)
            else:
                cv2.circle(img, c, int(rd.radius), (190, 190, 190), 1)
        # recognised value next to the block
        txt = "".join(rd.values) if f.kind in ("id", "subject") else ""
        if txt:
            cv2.putText(img, txt.strip() or "-", (int(f.x), int(f.y + f.h + 20)), cv2.FONT_HERSHEY_SIMPLEX, .6, (0, 0, 220), 2)
    return cv2.addWeighted(overlay, .45, img, .55, 0)


def process_paths(processor: OMRProcessor, paths, progress: Callable[[int, int, SheetResult], None] | None = None,
                  debug_dir: str | None = None):
    """Process files / folders. Returns list[SheetResult]."""
    files = collect_files(paths)
    results: List[SheetResult] = []
    total = len(files)
    for n, path in enumerate(files, 1):
        try:
            for page, img in iter_pages(path):
                r = processor.process(img, path, page)
                results.append(r)
                if debug_dir and r.debug is not None:
                    os.makedirs(debug_dir, exist_ok=True)
                    stem = f"{os.path.splitext(os.path.basename(path))[0]}_p{page}.jpg"
                    cv2.imencode(".jpg", r.debug)[1].tofile(os.path.join(debug_dir, stem))
                if progress:
                    progress(n, total, r)
        except Exception as e:                          # keep the batch going
            r = SheetResult(source=path, warnings=[f"failed: {e}"])
            results.append(r)
            if progress:
                progress(n, total, r)
    return results


def export_csv(results: List[SheetResult], path: str):
    """wide CSV: one row per sheet, one column per question (PartA_Q1 ...)"""
    id_cols = list(dict.fromkeys(k for r in results for k in r.ids))
    q_cols = list(dict.fromkeys((p, q) for r in results for p, d in r.answers.items() for q in sorted(d)))
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["SourceFile", "Page", "Status", "Warnings", "TemplateName"] + [safe_name(c) for c in id_cols]
                   + [f"{safe_name(p)}_Q{q}" for p, q in q_cols])
        for r in results:
            w.writerow([os.path.abspath(r.source), r.page, r.status, "; ".join(r.warnings), r.template_name]
                       + [r.ids.get(c, "") for c in id_cols]
                       + [r.answers.get(p, {}).get(q, "") for p, q in q_cols])
