"""Master-data matching. Codes only ever come from the masters; ambiguous or unmatched is "".
Lookups go through indexes rather than scans, so this holds up at millions of rows."""
import json
import re
import unicodedata
from collections import defaultdict
from datetime import date
from pathlib import Path

LEGAL_FORMS = {"gmbh", "ltd", "limited", "ou", "as", "ab", "sa", "lda", "pty", "plc", "inc", "llc",
               "bv", "nv", "sdn", "bhd", "srl", "spa", "oy", "aps", "co", "company", "the", "and"}


def fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", fold(s))


def tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", fold(s)) if len(t) > 1 and t not in LEGAL_FORMS}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


class TokenIndex:
    """Inverted index for fuzzy name/address lookups; candidates come from the rarest tokens."""

    def __init__(self):
        self.postings = defaultdict(set)
        self.docs = {}

    def add(self, ref, text: str):
        self.docs[ref] = tokens(text)
        for t in self.docs[ref]:
            self.postings[t].add(ref)

    def best(self, text: str, threshold: float, score=jaccard) -> set:
        query = tokens(text)
        rare = sorted((t for t in query if t in self.postings), key=lambda t: len(self.postings[t]))[:3]
        scored = {ref: score(query, self.docs[ref]) for t in rare for ref in self.postings[t]}
        top = max(scored.values(), default=0.0)
        return {ref for ref, s in scored.items() if s == top} if top >= threshold else set()


def covered(query: set, doc: set) -> float:
    return len(query & doc) / len(doc) if doc else 0.0


def only(found: set) -> str:
    return next(iter(found)) if len(found) == 1 else ""


class Masters:
    def __init__(self, root: str | Path):
        load = lambda name: json.loads((Path(root) / name).read_text(encoding="utf-8"))

        self.suppliers = {s["supplier_id"]: s for s in load("suppliers.json")["suppliers"]}
        self.supplier_ids, self.supplier_names = defaultdict(set), TokenIndex()
        for sid, s in self.suppliers.items():
            for ident in (s.get("vat_id"), s.get("bank_iban"), s.get("email")):
                if key(ident):
                    self.supplier_ids[key(ident)].add(sid)
            self.supplier_names.add(sid, s["name"])

        self.units, self.unit_names, self.addresses = {}, TokenIndex(), TokenIndex()
        for c in load("chart_of_books.json")["companies"]:
            for bu in c["business_units"]:
                self.units[bu["business_unit_code"]] = (c["company_code"], [l["location_code"] for l in bu["locations"]])
                self.unit_names.add(bu["business_unit_code"], bu["business_unit_name"])
                for loc in bu["locations"]:
                    self.addresses.add((c["company_code"], loc["location_code"]), loc["invoice_to_address"])

        self.taxes = defaultdict(list)
        for t in load("tax_master.json")["taxes"]:
            self.taxes[(t["country"], float(t["rate"]))].append(t)

        self.term_aliases, self.term_days = {}, defaultdict(set)
        for t in load("payment_terms.json")["payment_terms"]:
            self.term_days[t["days"]].add(t["payment_term_id"])
            for alias in t["text_aliases"]:
                self.term_aliases[fold(alias)] = t["payment_term_id"]

        self.pos = {key(p["po_number"]): p["po_id"] for p in load("po_master.json")["purchase_orders"]}

    def supplier(self, name: str, *identifiers: str) -> str:
        by_id = set().union(*(self.supplier_ids.get(key(i), set()) for i in identifiers if key(i)))
        return only(by_id) if by_id else only(self.supplier_names.best(name, 0.6))

    def country(self, supplier_id: str, stated: str, vat_id: str) -> str:
        if supplier_id:
            return self.suppliers[supplier_id]["country"]
        prefix = key(vat_id)[:2].upper()
        return (stated or "").upper() or (prefix if prefix.isalpha() else "")

    def buyer(self, name: str, address: str) -> dict:
        codes = {"company_code": "", "business_unit_code": "", "location_code": ""}
        unit = only(self.unit_names.best(name, 0.6))
        places = self.addresses.best(address, 0.75, score=covered)
        if unit:
            company, locations = self.units[unit]
            codes.update(company_code=company, business_unit_code=unit,
                         location_code=locations[0] if len(locations) == 1 else "")
        elif len({c for c, _ in places}) == 1 and len({l for _, l in places}) == 1:
            codes.update(company_code=next(iter(places))[0], location_code=next(iter(places))[1])
        return codes

    def tax(self, country: str, rate: str, *labels: str) -> str:
        try:
            candidates = self.taxes.get((country, abs(float(str(rate).strip().rstrip("%")))), [])
        except ValueError:
            return ""
        if len(candidates) > 1:
            said = set().union(*(tokens(l) for l in labels))
            candidates = [t for t in candidates if said & tokens(f"{t['tax_type']} {t['name']}")]
        return candidates[0]["code"] if len(candidates) == 1 else ""

    def payment_term(self, text: str, invoice_date: str, due_date: str) -> str:
        said = fold(text)
        named = {tid for alias, tid in self.term_aliases.items() if re.search(rf"\b{re.escape(alias)}\b", said)}
        if named:
            return only(named)
        try:
            days = (date.fromisoformat(due_date) - date.fromisoformat(invoice_date)).days
        except ValueError:
            return ""
        return only(self.term_days.get(days, set()))

    def po(self, number: str) -> str:
        return self.pos.get(key(number), "") if key(number) else ""
