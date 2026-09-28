---
title: OMR Reader
emoji: 📋
colorFrom: blue
colorTo: indigo
sdk: gradio
app_file: hf_space/app.py
python_version: "3.12"
models:
  - microsoft/trocr-base-handwritten
---

# OMR Reader — any layout → Microsoft Access (.mdb)

```
omr_project/
├── main.py            ← start the desktop app (2 tabs)
├── cli.py             ← same engine without GUI:  python cli.py scan / demo
├── sample_data.py     ← makes a fake sheet + simulated scans for testing
├── omr/
│   ├── template.py    Field / Template (layout description, saved as .json)
│   ├── align.py       straightens every scan onto the blank reference sheet
│   ├── reader.py      measures bubble darkness, decides which are marked
│   ├── pipeline.py    template + scan → result, CSV export, annotated images
│   ├── imageio.py     JPG/JPEG image loader
│   └── mdb.py         writes the .mdb (creates it if missing)
└── gui/               designer.py · scanner.py · common.py · app.py
```

## Install & run
```
pip install -r requirements.txt
python main.py            # GUI
python cli.py demo        # self-test with generated sheets (no real sheets needed)
```

## Workflow
**Tab 1 – Template (once per sheet design)**
1. Open a *blank* JPG sheet (best) or a filled JPG sheet as reference.
2. Tick which bubble-coded header fields exist: Name, Hall Ticket No, Booklet Code, Set, Subject — unticked = not on the sheet.
   Use **+ Other field** for anything else (Center code, Medium, ...).
3. For each bubble field, set the layout, number of positions/boxes, and option labels, then drag around the bubbles. Edit the count and pixel geometry in the field dialog. Select a field and use arrow keys to move it, Shift+arrows to resize it, or Ctrl+arrows for larger steps. **Redraw region** lets you drag a replacement box.
4. Use **+ Handwritten field (OCR)** for handwritten fields, or **+ Written booklet code (OCR)** for the separate `Booklet Code Written` column. Set the box count and allowed characters, then drag around the written line. OCR reads the full line.
5. Use **+ Subject bubbles for a part** to link an optional subject-choice bubble group to an answer part. Choose its labels and draw its bubble region. The recognized subject is stored in its own column, such as Part A Subject; this is optional for every part.
6. **+ Answers part**: name of the part, first question number, number of questions, options.
   Add as many parts/blocks as the sheet has; blocks with the same part name are merged
   (e.g. Part A Q1-25 and Part A Q26-50 sitting in two columns).
7. **Test on a scanned sheet** → annotated result. Tune the reading settings if needed. **Save template**.

Custom bubble fields can override **Bubble probe size** in their Edit dialog; a value of 0 inherits the template-wide setting. The blank reference is available in the review window alongside the annotated scanned sheet.

Handwritten fields use Microsoft's TrOCR handwriting model locally. Install the Python packages with pip install -r requirements.txt. Before the first handwritten scan, the app asks before downloading the model from Hugging Face; its weights are about 1.33 GB and are cached on this computer. OMR images are processed locally. Handwritten results always require review before saving, and low-confidence readings are highlighted. Bubble recognition does not require the handwriting model.

**Tab 2 – Scan**: load a template, click **Insert one JPG** for a single sheet, **Insert many JPGs** to multi-select a batch, or **Insert root folder** to add every JPG/JPEG from that folder and all nested subfolders. Choose the .mdb path, then click *START SCAN*. PDF and other image formats are not accepted. Re-selecting an already-added file does not add a duplicate.
Review is on by default. After scanning, select a row and click **Edit selected results** (or double-click the row) to correct identification values and any question's answer before saving. **CHECK** means a configured required hall-ticket, class, admission-number, part-subject, or center-code field is blank. SCS number is optional. **BLANK** means no answers were marked in any answer part; **OK** means at least one answer was marked and no required field is missing. Other recognition warnings remain visible in the Warnings column but do not independently change the status. CSV exports include the full source JPG path for each row. The app plays a bell and shows a completion popup when the full batch has finished scanning.
Un-ticked "Review" = results go into the MDB automatically when the scan finishes.

## Browser-hosted preview (free tier)

The `hf_space/app.py` web front end runs the recognition engine in a browser-hosted Gradio Space. It accepts a saved JSON template plus its reference image, then multiple JPGs or a root folder upload. Results appear in an editable table and can be downloaded as CSV. Online scanning uses temporary uploads; it does not write scans to the desktop MDB.

The hosted login uses usernames and passwords without email addresses. The initial admin account is `admin` unless `OMR_ADMIN_USERNAME` is set. Set `OMR_ADMIN_PASSWORD` as a private host secret before first launch; use a unique password with at least 12 characters. Admins can create/disable accounts and set the maximum active account count. Passwords are stored as salted scrypt hashes.

For hosted use, configure a persistent database by setting the private `DATABASE_URL` secret to a PostgreSQL connection URL. Without it, the app uses temporary local SQLite; accounts and the user limit can be lost when a free Space restarts. Keep scan JPGs out of the public source repository.

To host free on Hugging Face, create a public Gradio Space from this repository, select ZeroGPU hardware in the Space settings, then set the secrets above. The host account must be in good standing, verified, and at least 30 days old to host free ZeroGPU; this does not require app users to provide emails. Free ZeroGPU has daily inference quotas (5 minutes/day for logged-in Hugging Face users; lower for visitors not signed into Hugging Face), and free Spaces sleep when idle. A separate free PostgreSQL account is needed for persistent user settings. The code in this public Space is visible to everyone, even though the app itself requires a username and password.

Local preview (after `pip install -r requirements.txt`):

```powershell
$env:OMR_ADMIN_PASSWORD = "replace-with-a-unique-12-character-secret"
python hf_space/app.py
```

## What lands in the MDB
| Table | Content |
|---|---|
| `Sheets` | 1 row per sheet: SheetID, SourceFile, PageNo, ScannedAt, Status, Warnings, TemplateName, **one column per identification field**, **one memo column per answer part** (`Part_A_Answers` = `A,C,,D,AB,…`; blank = unanswered) |
| `Answers` | 1 row per question: SheetID, Part, QNo, Answer (long format → no Access 255-column limit, any number of parts/questions) |

Columns are added automatically when a new template has extra fields, so several exams can share one .mdb.

## Requirements for the .mdb part
Windows + *Microsoft Access ODBC driver* (installed with Office, or the free "Access Database Engine"
redistributable). Python and driver must have the **same bitness** (64-bit ↔ 64-bit).
The blank file itself is created by `msaccessdb` (any OS). Without the driver, use **Export CSV** — same data.

## Tuning / troubleshooting
* Light pencil marks missed → lower *Mark sensitivity* (0.22 → 0.15) and *Min. darkness* (0.30 → 0.20).
* Smudges counted as marks → raise them.
* "sheet could not be aligned" → use a cleaner blank reference, or set Alignment to `page` (photos of a sheet on a table) / `none` (perfectly registered flatbed scans).
* Circles not centred on bubbles in tab 1 → redraw the rectangle tighter, or nudge with arrow keys. Spacing is assumed uniform inside one rectangle; if the sheet has gaps every 5 rows, draw one block per group.
* Each sheet ≈ 0.5 s on a normal PC.

## Ideas for next steps
Answer-key scoring (add a `Key` table + a query), editing answers in the review grid, multiprocessing for 10k+ sheets.
