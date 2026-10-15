# Design

## What I understand now that I didn't on day one

At first this looked like extraction. The failures that followed looked unrelated to each other:
- prices with the VAT already in them;
- VAT printed on every line and again in the summary;
- a surcharge that the VAT was charged on;
- a credit note printed in negatives;
- an "amount credited" after the total;
- VAT on the lines plus an agency fee;
- a withholding tax that reduces what's paid.

They're the same problem. A document is the supplier's presentation of a calculation. `erp.py` is a second calculation with its own, very literal idea of a bill:
- unit prices are net;
- a header rate applies to the lines minus the header discount, and to nothing else;
- charges are added after tax and are never taxed;
- line and header taxes are simply added together.

Wherever the two calculations disagree, a faithful copy of the page books the wrong number. The job is translation: say what the page says in the ERP's terms, using only the page's figures and keeping its structure.

Three things followed from that.

**The printed gross is a witness.** The ERP never reads `gross_total`, so the gap between its rebuild and the printed total measures how wrong the translation is. The model is shown how the ERP rebuilt its draft: the components, the tax it derived from each rate, and any line whose base differs from the printed line total. That turns a reader into a solver. The feedback says what is, never which field to change.

**That feedback creates pressure to plug.** So grounding is enforced in code rather than left to the prompt:
- Every number is either listed as printed or declared as a formula over printed figures.
- The arithmetic of every formula is checked.
- A formula with any part that works out to the gross is rejected as circular.

INV-25's trace shows it working: the first draft scaled the printed net by the sum of the tax-inclusive lines, which equals the gross. The circular signal fired, and the second draft used the printed 23% rate instead.

**Only some decisions are judgment.** These have one right answer, so they're code: the sign of a credit, whether a string is a number, whether `16.00 / 1.23` is `13.01`, whether a VAT id is in the master. These need the model: which pages are a payable, whether prices include tax, where a tax sits. The line is drawn at ambiguity.

## What it does with a document it hasn't seen

It runs the same loop it runs on every document. No rule names a supplier, country, language, layout or file.

1. **Read.** Pages are rendered at 150 dpi and sent once, along with the PDF's text layer when it has a real one.
2. **One conversation per document.** The instructions (the role, `erp.py` verbatim, a few principles) and the tool schema are the same for every document, so most documents start with that prefix already cached. The pages come next, and the task is restated at the end, so the goal sits at both ends of the context.
3. **Submit.** The model calls `submit` with:
   - a page map;
   - the payables;
   - the declined documents;
   - the printed figures it used;
   - its derivations;
   - its flags.
4. **Check.** `checks.py` builds the autodrafts and resolves codes through indexed lookups in `masters.py`; an ambiguous match is left blank. It applies the one certain fix: credits become positive magnitudes, kept only if the ERP then books a positive amount. Then it returns signals as `{path, kind, message}`. These block the loop: an ERP gross that differs from the printed one, a bad number format, an ungrounded value, a wrong derivation, a circular derivation.
5. **Repeat** until nothing blocks.
   - A flagged payable stops blocking, but only after the model has seen the ERP's view of it.
   - Reasoning effort goes from medium to high from the third turn.
   - The loop ends on an identical resubmission or after six turns. Anything unresolved is written to `flags` rather than forced to foot.

**Why it generalises:** the code knows only two things, and neither changes between documents: the ERP's arithmetic (finite, and shown to the model verbatim) and the grounding rules. Everything specific to a document is the model's world knowledge applied to the page in front of it. Signals carry facts, not remedies, so a new kind of document needs no new rule.

**Across the 42 documents in `output/`:**
- **Turns:** 52 in total. 34 documents finished in one turn and 8 needed the loop. Most extra turns were for grounding (values the model had computed but not declared); a few were for an ERP mismatch or a circular derivation.
- **Results:** 38 payables, each footing to its printed gross; 5 files declined outright.
- **Tokens:** about 620k input, a quarter of it cached, and 166k output.

**Where the cost goes.** Classification gets no feedback signal, so reading needs a strong model; everything certain is plain code and costs nothing. A first turn can think for longer than the default prompt cache lives, so long retention is on to keep the pages cached for the follow-up turns.

## Documents that can't be solved like the others

**INV-06, a retail slip.** Item prices include VAT. One VAT amount is printed, with no rate and no net prices. The ERP needs net prices, and the page doesn't contain them. The record:
- keeps the VAT at the header, exactly as printed;
- nets the taxable lines by spreading that printed VAT across them pro rata, with every formula listed in `derived`.

That is an allocation, not a printed split. A clerk would want to know, and the next step would be to flag allocation-style derivations automatically.

**HLD-05** prints no single "total payable". The system books the printed subtotal including IVA plus the deposit line, and says so in `flags`.

**Classification has no arithmetic witness.**
- **INV-26** is headed "SALES" but numbered "Quotation No.". The system declines it as a quote; it's the call most likely to go either way.
- **DU-03** hides a second supplier's invoice behind customs and air-waybill pages. It's booked, but it's the same kind of call.

The fix for these isn't another rule but a second witness, such as a PO or a goods receipt.

**Known gaps:**
- **Buyer codes rarely resolve.** Documents address the tenant under another name, so only matching addresses resolve.
- **Tax codes need a known supplier country.**
- **An inferred date got through once.** On DU-03 the model inferred an invoice date from the due date and terms and flagged it, where it should have left the field blank.
