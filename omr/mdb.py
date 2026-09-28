"""Store results in a Microsoft Access .mdb file.

Tables
  Sheets  - one row per OMR sheet: SheetID, SourceFile, PageNo, ScannedAt, Status, Warnings, TemplateName,
            one text column per identification field (Name, Hall_Ticket_No, Set ...),
            one memo column per answer part  (Part_A_Answers = "A,C,,D,AB,...")
  Answers - one row per question: SheetID, Part, QNo, Answer   (no 255-column Access limit, any number of questions)

Needs: Windows + Microsoft Access ODBC driver (comes with Office / "Access Database Engine" redistributable).
Bitness of Python and driver must match (64-bit Python -> 64-bit driver).
"""
from __future__ import annotations
import datetime as dt
import os
from typing import List

from .pipeline import SheetResult, safe_name as _safe
from .template import Template

_BASE = {"sheetid", "sourcefile", "pageno", "scannedat", "status", "warnings", "templatename"}


def safe_name(s: str) -> str:
    """column name that cannot collide with the fixed columns"""
    n = _safe(s)
    return n + "_" if n.lower() in _BASE else n


class MdbError(RuntimeError):
    pass


def create_blank_mdb(path: str):
    try:
        import msaccessdb
        msaccessdb.create(path)
        return
    except ImportError:
        pass
    try:                                              # Windows-only fallback through ADOX
        import win32com.client
        win32com.client.Dispatch("ADOX.Catalog").Create(f"Provider=Microsoft.Jet.OLEDB.4.0;Data Source={path}")
    except Exception as e:
        raise MdbError("Cannot create a blank .mdb. Run:  pip install msaccessdb   (" + str(e) + ")")


class MdbStore:
    SHEETS, ANSWERS = "Sheets", "Answers"

    def __init__(self, path: str, template: Template):
        try:
            import pyodbc
        except ImportError:
            raise MdbError("pip install pyodbc")
        self.pyodbc = pyodbc
        self.path = os.path.abspath(path)
        self.t = template
        drivers = [d for d in pyodbc.drivers() if "Access Driver" in d]
        if not drivers:
            raise MdbError("Microsoft Access ODBC driver not found.\nInstall the 'Microsoft Access Database Engine' "
                           "(same bitness as Python) or use the CSV export.")
        if not os.path.exists(self.path):
            create_blank_mdb(self.path)
        self.conn = pyodbc.connect(f"Driver={{{drivers[0]}}};DBQ={self.path};", autocommit=False)
        self._ensure_schema()

    # ------------------------------------------------------------------
    def _tables(self):
        cur = self.conn.cursor()
        return {r.table_name.lower() for r in cur.tables(tableType="TABLE")}

    def _columns(self, table):
        cur = self.conn.cursor()
        return {r.column_name.lower() for r in cur.columns(table=table)}

    def _ensure_schema(self):
        cur = self.conn.cursor()
        tables = self._tables()
        if self.SHEETS.lower() not in tables:
            cur.execute(f"CREATE TABLE [{self.SHEETS}] ([SheetID] COUNTER PRIMARY KEY, [SourceFile] TEXT(255), "
                        "[PageNo] LONG, [ScannedAt] DATETIME, [Status] TEXT(20), [Warnings] TEXT(255), "
                        "[TemplateName] TEXT(100))")
        if self.ANSWERS.lower() not in tables:
            cur.execute(f"CREATE TABLE [{self.ANSWERS}] ([AnswerID] COUNTER PRIMARY KEY, [SheetID] LONG, "
                        "[Part] TEXT(100), [QNo] LONG, [Answer] TEXT(50))")
            try:
                cur.execute(f"CREATE INDEX [ix_ans_sheet] ON [{self.ANSWERS}] ([SheetID])")
            except Exception:
                pass
        have = self._columns(self.SHEETS)
        wanted = [(safe_name(f.name), "TEXT(255)") for f in self.t.id_fields()]
        wanted += [(self.part_col(f.part or f.name), "LONGTEXT") for f in self.t.answer_fields()]
        for col, typ in dict.fromkeys(wanted):
            if col.lower() not in have:
                cur.execute(f"ALTER TABLE [{self.SHEETS}] ADD COLUMN [{col}] {typ}")
                have.add(col.lower())
        self.conn.commit()

    @staticmethod
    def part_col(part: str) -> str:
        return safe_name(part) + "_Answers"

    # ------------------------------------------------------------------
    def save(self, results: List[SheetResult]) -> int:
        """insert all not-yet-saved results; returns number of sheets written"""
        cur = self.conn.cursor()
        # Results may have been reviewed with different saved templates. Add
        # the union of their columns before inserting any row.
        have = self._columns(self.SHEETS)
        extra_columns = {}
        for result in results:
            if result.saved:
                continue
            extra_columns.update({safe_name(name): "TEXT(255)" for name in result.ids})
            extra_columns.update({self.part_col(part): "LONGTEXT" for part in result.answers})
        for column, col_type in extra_columns.items():
            if column.lower() not in have:
                cur.execute(f"ALTER TABLE [{self.SHEETS}] ADD COLUMN [{column}] {col_type}")
                have.add(column.lower())
        n = 0
        for r in results:
            if r.saved:
                continue
            cols = ["SourceFile", "PageNo", "ScannedAt", "Status", "Warnings", "TemplateName"]
            vals = [os.path.basename(r.source), r.page, dt.datetime.now().replace(microsecond=0), r.status,
                    ("; ".join(r.warnings))[:255] or None, r.template_name or self.t.name]
            for k, v in r.ids.items():
                cols.append(safe_name(k)); vals.append(v or None)
            for part in r.answers:
                cols.append(self.part_col(part)); vals.append(r.answer_string(part) or None)
            cur.execute(f"INSERT INTO [{self.SHEETS}] ({','.join(f'[{c}]' for c in cols)}) "
                        f"VALUES ({','.join('?' * len(cols))})", vals)
            sid = int(cur.execute("SELECT @@IDENTITY").fetchone()[0])
            rows = [(sid, part, q, a or None) for part, d in r.answers.items() for q, a in sorted(d.items())]
            if rows:
                cur.executemany(f"INSERT INTO [{self.ANSWERS}] ([SheetID],[Part],[QNo],[Answer]) VALUES (?,?,?,?)", rows)
            r.saved = True
            n += 1
        self.conn.commit()
        return n

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass
