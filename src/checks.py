"""Reviews a submitted draft. Certain fixes are applied here; everything that needs judgment goes
back to the model as signals: facts about the draft, never instructions."""
import ast
import copy
import operator
import re

import erp

HEADER = ("gross_total", "subtotal", "total_tax_amount", "discount_amount",
          "freight_charges", "insurance_charges", "extra_charges", "excise_duties")
CHARGES = HEADER[4:]
MAGNITUDES = {"tax_rate", "discount", "discount_percentage", "discount_amount", "gross_total", "subtotal", "total_tax_amount"}
NUMBER = re.compile(r"-?\d+(\.\d+)?")
OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def signal(path, kind, message):
    return {"path": path, "kind": kind, "message": message}


def assemble(draft, masters):
    s = draft["supplier"]
    supplier_id = masters.supplier(s["name"], s["vat_id"], s["iban"], s["email"])
    country = masters.country(supplier_id, s["country"], s["vat_id"])

    def tax(t):
        code = masters.tax(country, t["tax_rate"], t["tax_type"], t["tax_name"]) if t["tax_rate"] else ""
        return {**t, "tax_type_code": code}

    return {
        "invoice_number": draft["invoice_number"],
        "invoice_date": draft["invoice_date"],
        "due_date": draft["due_date"],
        "invoice_type": draft["invoice_type"],
        "currency": draft["currency"],
        "supplier": {"name": s["name"], "supplier_id": supplier_id, "address": s["address"], "vat_id": s["vat_id"]},
        "buyer": masters.buyer(draft["buyer"]["name"], draft["buyer"]["address"]),
        "payment_term_id": masters.payment_term(draft["payment_terms"], draft["invoice_date"], draft["due_date"]),
        "po_number": draft["po_number"],
        "po_id": masters.po(draft["po_number"]),
        **{f: draft[f] for f in HEADER},
        "taxes": [tax(t) for t in draft["taxes"]],
        "line_items": [{**li, "tax_rate": "", "tax_amount": "", "taxes": [tax(t) for t in li["taxes"]]}
                       for li in draft["line_items"]],
    }


def numbers(p):
    """(path, holder, field) for every number in a payable."""
    for f in HEADER:
        yield f, p, f
    for i, t in enumerate(p["taxes"]):
        yield from ((f"taxes[{i}].{f}", t, f) for f in ("tax_rate", "tax_amount"))
    for j, li in enumerate(p["line_items"]):
        yield from ((f"line_items[{j}].{f}", li, f) for f in ("quantity", "unit_price", "total", "discount", "discount_percentage"))
        for k, t in enumerate(li["taxes"]):
            yield from ((f"line_items[{j}].taxes[{k}].{f}", t, f) for f in ("tax_rate", "tax_amount"))


def book(p):
    return erp.erp_book(p)["will_book_gross"]


def magnitude(v):
    return round(abs(float(v)), 6)


def negate(v):
    if not NUMBER.fullmatch(v) or float(v) == 0:
        return v
    return v[1:] if v.startswith("-") else f"-{v}"


def check_format(p, at):
    found = []
    for path, holder, f in numbers(p):
        v = holder[f] = str(holder[f]).strip().removesuffix("%").strip()
        if v and not NUMBER.fullmatch(v):
            found.append(signal(f"{at}.{path}", "format", f"{v!r} is not a dot-decimal number; the ERP reads it as {erp.num(v)}"))
    return found


def as_magnitudes(p):
    """Credits go out as positive magnitudes. Flip only when the ERP books one negative, and keep
    the flip only if the ERP then books it positive."""
    if p["invoice_type"] != "CREDIT_MEMO" or book(p) >= 0:
        return False
    q = copy.deepcopy(p)
    for li in q["line_items"]:
        field = "quantity" if erp.num(li["quantity"]) < 0 else "unit_price"
        li[field] = negate(li[field])
        li["total"] = negate(li["total"])
    for path, holder, f in numbers(q):
        if f in MAGNITUDES:
            holder[f] = holder[f].lstrip("-")
        elif not path.startswith("line_items") or path.endswith("tax_amount"):
            holder[f] = negate(holder[f])
    if book(q) <= 0:
        return False
    p.update(q)
    return True


def value(n):
    if isinstance(n, ast.Constant) and type(n.value) in (int, float):
        return n.value
    if isinstance(n, ast.BinOp) and type(n.op) in OPS:
        return OPS[type(n.op)](value(n.left), value(n.right))
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
        return -value(n.operand)
    raise ValueError(ast.dump(n))


def evaluate(expr):
    return value(ast.parse(expr, mode="eval").body)


def parts(tree):
    """Magnitudes of every sub-expression of a formula."""
    found = set()
    for n in ast.walk(tree):
        try:
            found.add(magnitude(value(n)))
        except (ValueError, ZeroDivisionError, TypeError):
            pass
    return found


def relative(path):
    return re.sub(r"^payables\[\d+\]\.", "", path.replace(" ", ""))


def resolve(p, path):
    node = p
    for name, index in re.findall(r"(\w+)(?:\[(\d+)\])?", relative(path)):
        node = node[name] if not index else node[name][int(index)]
    return node


def check_derived(derived, payables):
    """The arithmetic of every declared derivation, compared as magnitudes (a credit may have been flipped)."""
    found = []
    for i, d in enumerate(derived):
        where = f"payables[{d['payable']}].{relative(d['path'])}"
        try:
            held, result = str(resolve(payables[d["payable"]], d["path"])), evaluate(d["expr"])
        except (LookupError, TypeError, ValueError, SyntaxError, ZeroDivisionError):
            found.append(signal(f"derived[{i}]", "derivation", f"cannot evaluate {d['expr']!r} against {where}"))
            continue
        places = len(held.partition(".")[2])
        if not NUMBER.fullmatch(held) or abs(abs(result) - abs(float(held))) > 0.5 * 10 ** -places + 1e-9:
            found.append(signal(f"derived[{i}]", "derivation", f"{d['expr']} = {result:.6g}, but {where} is {held!r}"))
    return found


def check_grounding(payables, printed, derived):
    """Every number is a printed figure or declared as computed from printed figures, and no
    computation leans on the gross it is meant to reproduce."""
    known = {magnitude(x) for x in printed if NUMBER.fullmatch(x.strip())} | {0.0, 1.0}
    declared = {(d["payable"], relative(d["path"])) for d in derived}
    found = []
    for i, p in enumerate(payables):
        for path, holder, f in numbers(p):
            v = holder[f]
            if NUMBER.fullmatch(v) and magnitude(v) not in known and (i, path) not in declared:
                found.append(signal(f"payables[{i}].{path}", "ungrounded",
                                    f"{v} is not among the printed figures listed, and no derivation is declared for it"))
    for i, d in enumerate(derived):
        try:
            tree = ast.parse(d["expr"], mode="eval")
            p = payables[d["payable"]]
        except (SyntaxError, LookupError):
            continue
        operands = {magnitude(n.value) for n in ast.walk(tree) if isinstance(n, ast.Constant) and type(n.value) in (int, float)}
        if extra := sorted(operands - known - {100.0}):
            found.append(signal(f"derived[{i}]", "ungrounded",
                                f"{d['expr']} uses {', '.join(f'{x:g}' for x in extra)}, not among the printed figures listed"))
        line_figures = {magnitude(li[f]) for li in p["line_items"] for f in ("unit_price", "total") if NUMBER.fullmatch(li[f])}
        target = magnitude(p["gross_total"]) if NUMBER.fullmatch(p["gross_total"]) else None
        if relative(d["path"]) != "gross_total" and target in parts(tree) and target not in line_figures:
            found.append(signal(f"derived[{i}]", "circular",
                                f"{d['expr']} works from {p['gross_total']}, the gross_total the ERP's rebuild is checked against"))
    return found


def erp_view(p, at):
    """How the ERP rebuilds this payable, next to what the document prints. Empty when it foots."""
    gross = book(p)
    if not p["gross_total"]:
        return [signal(f"{at}.gross_total", "erp_gross", f"the ERP books {gross:.2f}; no gross_total was recorded from the document")]
    printed = erp.num(p["gross_total"])
    if abs(gross - printed) < 0.005:
        return []

    lines = p["line_items"]
    bases = [erp._line_base(li) for li in lines]
    line_tax = sum(erp._line_taxes(li, b) for li, b in zip(lines, bases))
    discount = abs(erp.num(p["discount_amount"]))
    net = sum(bases) - discount
    header_tax = erp._header_taxes(p["taxes"], net)
    charges = sum(erp.num(p[f]) for f in CHARGES)

    found = [signal(at, "erp_gross",
                    f"the ERP books {gross:.2f} {p['currency']} = line bases {sum(bases):.2f} - header discount {discount:.2f}"
                    f" + line taxes {line_tax:.2f} + header taxes {header_tax:.2f} + charges {charges:.2f};"
                    f" the document's gross_total is {printed:.2f} (difference {gross - printed:+.2f})")]
    if p["total_tax_amount"] and abs(line_tax + header_tax - erp.num(p["total_tax_amount"])) >= 0.005:
        found.append(signal(f"{at}.total_tax_amount", "erp_tax",
                            f"the ERP's taxes total {line_tax + header_tax:.2f}; the document's total_tax_amount is {p['total_tax_amount']}"))
    for i, t in enumerate(p["taxes"]):
        rate = erp.num(t["tax_rate"])
        if not erp.num(t["tax_amount"]) and rate > 0:
            found.append(signal(f"{at}.taxes[{i}]", "erp_tax",
                                f"no amount given; the ERP derives {erp.round2(net * rate / 100):.2f} = {rate:g}% of {net:.2f}"))
    off = [(j, b) for j, (li, b) in enumerate(zip(lines, bases)) if li["total"] and abs(b - erp.num(li["total"])) >= 0.005]
    for j, b in off[:8]:
        found.append(signal(f"{at}.line_items[{j}]", "erp_line", f"the ERP's base is {b:.2f}; the printed line total is {lines[j]['total']}"))
    if len(off) > 8:
        found.append(signal(f"{at}.line_items", "erp_line", f"{len(off) - 8} more lines where the ERP's base differs from the printed total"))
    return found


def codes(p, at):
    taxes = p["taxes"] + [t for li in p["line_items"] for t in li["taxes"]]
    b = p["buyer"]
    return signal(at, "masters",
                  f"supplier_id {p['supplier']['supplier_id'] or '-'}; buyer {b['company_code'] or '-'}/"
                  f"{b['business_unit_code'] or '-'}/{b['location_code'] or '-'}; payment_term_id {p['payment_term_id'] or '-'};"
                  f" po_id {p['po_id'] or '-'}; tax codes {', '.join(t['tax_type_code'] or '-' for t in taxes) or '-'}")


def review(submission, masters, turn, final):
    """Returns (record, signals, done). Done when nothing blocks. A payable the model flags as
    unreconcilable stops blocking only once the model has seen the ERP's view of it."""
    payables = [assemble(d, masters) for d in submission["payables"]]
    flags = list(submission["flags"])
    flagged = {f["payable"] for f in flags}
    blocking, context = [], []

    for i, p in enumerate(payables):
        at = f"payables[{i}]"
        blocking += check_format(p, at)
        if as_magnitudes(p):
            context.append(signal(at, "normalized", "credit memo delivered as positive magnitudes (signs flipped)"))
        view = erp_view(p, at)
        if view:
            (context if i in flagged and turn > 0 else blocking).append(view[0])
            context += view[1:]
        context.append(codes(p, at))
    blocking += check_derived(submission["derived"], payables)
    blocking += check_grounding(payables, submission["printed"], submission["derived"])

    if blocking and final:
        for s in blocking:
            m = re.match(r"payables\[(\d+)\]", s["path"])
            flags.append({"payable": int(m[1]) if m else -1, "message": f"unresolved {s['kind']}: {s['message']}"})
    record = {"payables": payables, "declined": submission["declined"], "derived": submission["derived"], "flags": flags}
    return record, blocking + context, not blocking
