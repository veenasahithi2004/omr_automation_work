"""Template = description of ONE sheet layout (which fields exist, where, how many bubbles)."""
from __future__ import annotations
import json, os, re, shutil
from dataclasses import dataclass, field, asdict
from typing import List


def parse_options(spec: str) -> List[str]:
    """'0-9' -> 0..9 | 'A-D' -> A..D | '1-9,0' | 'A,B,C,D' | '9-0' (descending) | 'Yes,No'"""
    out: List[str] = []
    for tok in (t.strip() for t in spec.split(",")):
        if not tok:
            continue
        m = re.fullmatch(r"([A-Za-z])\s*-\s*([A-Za-z])", tok)
        if m:
            a, b = ord(m.group(1)), ord(m.group(2))
            out += [chr(c) for c in range(a, b + (1 if b >= a else -1), 1 if b >= a else -1)]
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", tok)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            out += [str(n) for n in range(a, b + (1 if b >= a else -1), 1 if b >= a else -1)]
            continue
        out.append(tok)
    return out


@dataclass
class Field:
    name: str
    kind: str = "id"            # "id" -> value string (name, hall ticket...) | "answers" -> list of questions
    x: float = 0.0              # region = tight box around ALL bubbles of the field,
    y: float = 0.0              # in pixels of the reference image
    w: float = 0.0
    h: float = 0.0
    orient: str = "columns"     # "columns": every column is one position, options run top->bottom
                                # "rows"   : every row is one position, options run left->right
    count: int = 1              # number of positions (columns or rows, depending on orient)
    options: str = "0-9"        # option labels, see parse_options()
    multi: bool = False         # allow several marks in one position (e.g. answer "AC")
    blank: str = ""             # text stored for an unmarked position
    multi_char: str = "*"       # text stored when a position is ambiguous
    part: str = ""              # answers: part / section name (several fields may share one part)
    start: int = 1              # answers: number of the first question in this block
    enabled: bool = True
    probe: float = 0.0           # optional per-field bubble probe fraction; 0 uses Template.probe

    def labels(self) -> List[str]:
        return parse_options(self.options)

    def shape(self):
        """(rows, cols) of the bubble grid"""
        n = len(self.labels())
        return (n, self.count) if self.orient == "columns" else (self.count, n)

    def has_region(self) -> bool:
        return self.w > 2 and self.h > 2

    def centers(self):
        """yield (position_index, option_index, cx, cy)"""
        rows, cols = self.shape()
        cw, ch = self.w / cols, self.h / rows
        for p in range(self.count):
            for o in range(len(self.labels())):
                r, c = (o, p) if self.orient == "columns" else (p, o)
                yield p, o, self.x + (c + .5) * cw, self.y + (r + .5) * ch

    def cell_size(self):
        rows, cols = self.shape()
        return self.w / cols, self.h / rows


@dataclass
class Template:
    name: str = "template"
    reference: str = ""          # reference (blank) sheet image, relative to the .json file
    ref_w: int = 0
    ref_h: int = 0
    align: str = "auto"          # auto | features | page | none
    probe: float = 0.28          # bubble sampling radius as fraction of the smaller cell side
    mark_delta: float = 0.22     # how much darker than an empty bubble a mark must be
    mark_min: float = 0.30       # absolute darkness a mark must reach (0..1)
    dominance: float = 1.5       # 2 marks in a single-choice position: keep the darker if it is >= x times the other
    fields: List[Field] = field(default_factory=list)
    base_dir: str = field(default="", repr=False)

    # ---- helpers -------------------------------------------------------
    @property
    def reference_path(self) -> str:
        return os.path.join(self.base_dir, self.reference)

    def get(self, name: str):
        return next((f for f in self.fields if f.name == name), None)

    def id_fields(self):
        return [f for f in self.fields if f.enabled and f.kind in ("id", "subject", "handwriting") and f.has_region()]

    def answer_fields(self):
        return [f for f in self.fields if f.enabled and f.kind == "answers" and f.has_region()]

    # ---- persistence ---------------------------------------------------
    def save(self, json_path: str, reference_src: str | None = None):
        folder = os.path.dirname(os.path.abspath(json_path))
        os.makedirs(folder, exist_ok=True)
        if reference_src:
            ext = os.path.splitext(reference_src)[1] or ".png"
            self.reference = os.path.splitext(os.path.basename(json_path))[0] + "_reference" + ext
            dst = os.path.join(folder, self.reference)
            if os.path.abspath(reference_src) != os.path.abspath(dst):
                shutil.copyfile(reference_src, dst)
        self.base_dir = folder
        data = asdict(self)
        data.pop("base_dir")
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)

    @staticmethod
    def load(json_path: str) -> "Template":
        with open(json_path, encoding="utf-8") as fh:
            d = json.load(fh)
        fields = [Field(**f) for f in d.pop("fields", [])]
        t = Template(**{k: v for k, v in d.items() if k in Template.__dataclass_fields__ and k != "base_dir"})
        t.fields = fields
        t.base_dir = os.path.dirname(os.path.abspath(json_path))
        return t
