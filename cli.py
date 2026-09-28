"""Command line:
   python cli.py demo                                   # make sample sheets + run everything
   python cli.py scan -t tpl.json -i sheets_folder -m out.mdb [--csv out.csv] [--debug dir]
"""
import argparse, os, sys, time
from omr import Template, OMRProcessor, process_paths, export_csv


def run_scan(tpl, inputs, mdb=None, csv_path=None, debug=None):
    t = Template.load(tpl)
    proc = OMRProcessor(t)

    def cb(n, total, r):
        print(f"[{n}/{total}] {os.path.basename(r.source)} p{r.page}: {r.status} {r.ids} {'; '.join(r.warnings)}")

    res = process_paths(proc, inputs, cb, debug_dir=debug)
    if csv_path:
        export_csv(res, csv_path); print("CSV ->", csv_path)
    if mdb:
        from omr.mdb import MdbStore, MdbError
        try:
            s = MdbStore(mdb, t); n = s.save(res); s.close()
            print(f"MDB -> {mdb}  ({n} sheets)")
        except MdbError as e:
            print("MDB not written:", e, file=sys.stderr)
    return res


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("-t", "--template", required=True)
    s.add_argument("-i", "--input", nargs="+", required=True, help="files and/or folders")
    s.add_argument("-m", "--mdb"); s.add_argument("--csv"); s.add_argument("--debug")
    d = sub.add_parser("demo"); d.add_argument("-n", type=int, default=5)
    a = ap.parse_args()
    if a.cmd == "scan":
        run_scan(a.template, a.input, a.mdb, a.csv, a.debug)
    else:
        from sample_data import make_demo
        scans = make_demo("demo", a.n)
        t0 = time.time()
        res = run_scan("demo/sample_exam.json", [p for p, *_ in scans], "demo/results.mdb", "demo/results.csv", "demo/debug")
        wrong = sum(1 for r, (_, ids, ans) in zip(res, scans) for part, d in ans.items() for q, v in d.items()
                    if r.answers[part][q] != v) + sum(1 for r, (_, ids, _) in zip(res, scans) for k, v in ids.items() if r.ids.get(k) != v)
        print(f"\ndemo finished in {time.time() - t0:.1f}s - wrong values vs ground truth: {wrong}")


if __name__ == "__main__":
    main()
