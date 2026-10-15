"""python run.py [documents] [--out output] [--only INV-01 ...]"""
import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import find_dotenv, load_dotenv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("documents", nargs="?", default="documents")
    ap.add_argument("--out", default="output")
    ap.add_argument("--traces", default="traces")
    ap.add_argument("--only", nargs="*", help="file names without .pdf")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    load_dotenv(find_dotenv(usecwd=True))
    from src.solve import Solver

    solver = Solver()
    pdfs = sorted(p for p in Path(args.documents).iterdir()
                  if p.suffix.lower() == ".pdf" and (not args.only or p.stem in args.only))
    out, traces = Path(args.out), Path(args.traces)
    out.mkdir(parents=True, exist_ok=True)
    traces.mkdir(parents=True, exist_ok=True)

    def book(pdf):
        try:
            record, trace = solver.solve(pdf)
        except Exception as e:
            print(f"{pdf.stem:8} FAILED {type(e).__name__}: {e}", file=sys.stderr)
            return None
        (out / f"{pdf.stem}.json").write_text(json.dumps({"file": pdf.name, **record}, indent=2, ensure_ascii=False))
        (traces / f"{pdf.stem}.json").write_text(json.dumps({"turns": trace}, indent=2, ensure_ascii=False))
        state = "booked" if trace[-1]["done"] else "flagged"
        print(f"{pdf.stem:8} {state:7} turns={len(trace)} payables={len(record['payables'])} declined={len(record['declined'])}")
        return trace

    with ThreadPoolExecutor(args.workers) as pool:
        runs = [t for t in pool.map(book, pdfs) if t]
    turns = [t for run in runs for t in run]
    total = lambda k: sum(t[k] for t in turns)
    print(f"\n{len(runs)}/{len(pdfs)} documents, {len(turns)} turns; tokens in {total('input_tokens')} "
          f"(cached {total('cached_tokens')}), out {total('output_tokens')} (reasoning {total('reasoning_tokens')})")


if __name__ == "__main__":
    main()
