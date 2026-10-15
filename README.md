# Bookable Payable

Reads supplier PDFs and writes the autodrafts that `erp.py` books.

## Run

```
pip install -r requirements.txt
cp .env.example .env        # add OPENAI_API_KEY and OPENAI_MODEL
python run.py documents/
```

One JSON per PDF goes to `output/`, and a log of each document's turns goes to `traces/`. Use `--only INV-01 DU-03` to run a subset. Run the tests with `pytest`.

Any OpenAI model that reads images and supports reasoning works. A stronger model classifies mixed bundles more reliably.

## How it works

Each document gets one conversation with the model. The model reads the pages and submits a draft. The code rebuilds that draft with `erp.py`, checks every number against the figures the model says are printed, and sends back what it found. This repeats until the ERP books what the document says is owed, or until the model explains why the page can't support that. [DESIGN.md](DESIGN.md) has the details.

| | |
|---|---|
| `run.py` | entry point |
| `src/solve.py` | the conversation loop |
| `src/checks.py` | ERP rebuild, grounding and format checks |
| `src/masters.py` | master-data matching |
| `src/pdf.py` | page rendering |
| `src/prompt.md` | instructions for the model |

Besides `payables` and `declined`, each output file has two more fields:
- `derived`: values computed from printed figures, each with its formula;
- `flags`: what the page can't support.
