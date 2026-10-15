import json
import os
import time
from pathlib import Path

from openai import OpenAI

from .checks import HEADER, review
from .masters import Masters
from .pdf import pages

ROOT = Path(__file__).resolve().parent.parent
PROMPT = (Path(__file__).with_name("prompt.md").read_text(encoding="utf-8")
          .replace("{erp}", (ROOT / "erp.py").read_text(encoding="utf-8").strip()))
EFFORT = ("medium", "medium", "high", "high", "high", "high")
TASK = ("Above are all {n} pages of one supplier document. Decide what it is and what, if anything, is owed, "
        "then submit the autodrafts: each one's ERP rebuild must arrive at what the document says is owed, "
        "through components that mirror the document.")


def obj(**props):
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def arr(item, about=""):
    return {"type": "array", "items": item, **({"description": about} if about else {})}


def text(about=""):
    return {"type": "string", **({"description": about} if about else {})}


def enum(*values):
    return {"type": "string", "enum": list(values)}


NUM = text('dot-decimal number, or ""')
TAX = obj(tax_type=text("e.g. VAT, IVA, GST, NHIL, WHT"), tax_name=text("as printed"),
          tax_rate=text('percent without %, or ""'), tax_amount=NUM)
LINE = obj(description=text(), item_type=enum("GOODS", "SERVICE", "FREIGHT", "TAX"), uom=text(),
           quantity=NUM, unit_price=text("NET unit price, dot-decimal"), total=text("line total as printed"),
           discount=text("amount discount (magnitude)"), discount_percentage=NUM, taxes=arr(TAX, "taxes charged on this line"))
PAYABLE = obj(
    invoice_number=text(), invoice_date=text("YYYY-MM-DD"), due_date=text("YYYY-MM-DD"),
    invoice_type=enum("INVOICE", "CREDIT_MEMO"), currency=text("ISO 4217"),
    supplier=obj(name=text(), address=text(), vat_id=text(), iban=text(), email=text(),
                 country=text("ISO 3166-1 alpha-2")),
    buyer=obj(name=text(), address=text()),
    payment_terms=text("payment-terms wording as printed"), po_number=text("purchase-order reference as printed"),
    **{f: NUM for f in HEADER},
    taxes=arr(TAX, "taxes stated at the header"), line_items=arr(LINE),
)
SUBMIT = {
    "type": "function", "name": "submit", "strict": True,
    "description": "Submit the autodrafts for this document. Returns signals about the draft.",
    "parameters": obj(
        pages=arr(text(), "one line per page, in order: what the page is"),
        payables=arr(PAYABLE),
        printed=arr(text(), "every number your autodrafts take from the page, dot-decimal, copied as printed"),
        declined=arr(obj(doc_type=text(), reason=text()), "documents in this file that are not payables"),
        derived=arr(obj(payable={"type": "integer"}, path=text("e.g. line_items[0].unit_price"),
                        expr=text("arithmetic over printed figures")), "values computed rather than copied"),
        flags=arr(obj(payable={"type": "integer"}, message=text()), "what the page cannot support, and why"),
    ),
}


class Solver:
    def __init__(self):
        self.masters = Masters(ROOT / "master_data")
        self.model = os.environ["OPENAI_MODEL"]
        self.client = OpenAI(max_retries=6, timeout=900)

    def solve(self, pdf: Path):
        """One conversation per document. The instructions and tool are identical for every document
        and the pages go in once, so later turns read them from the prompt cache and only the signals
        are new."""
        doc = pages(str(pdf))
        content = []
        for n, page in enumerate(doc, 1):
            content.append({"type": "input_text", "text": f"Page {n} of {len(doc)}"})
            content.append({"type": "input_image", "image_url": page["image"], "detail": "original"})
            if page["text"]:
                content.append({"type": "input_text", "text": f"Text layer of page {n}:\n{page['text']}"})
        content.append({"type": "input_text", "text": TASK.format(n=len(doc))})

        items, previous, last, trace = [{"role": "user", "content": content}], None, None, []
        for turn, effort in enumerate(EFFORT):
            started = time.time()
            response = self.client.responses.create(
                model=self.model, instructions=PROMPT, tools=[SUBMIT], tool_choice="required",
                parallel_tool_calls=False, input=items, previous_response_id=previous,
                reasoning={"effort": effort}, prompt_cache_key="bookable-payable")
            call = next(o for o in response.output if o.type == "function_call")
            submission = json.loads(call.arguments)
            unchanged = submission == last
            record, signals, done = review(submission, self.masters, turn, final=unchanged or turn == len(EFFORT) - 1)
            usage = response.usage
            trace.append({"turn": turn + 1, "effort": effort, "seconds": round(time.time() - started, 1),
                          "input_tokens": usage.input_tokens, "cached_tokens": usage.input_tokens_details.cached_tokens,
                          "output_tokens": usage.output_tokens, "reasoning_tokens": usage.output_tokens_details.reasoning_tokens,
                          "pages": submission["pages"], "printed": submission["printed"], "signals": signals, "done": done})
            if done or unchanged:
                break
            previous, last = response.id, submission
            items = [{"type": "function_call_output", "call_id": call.call_id, "output": json.dumps({"signals": signals})}]
        return record, trace
