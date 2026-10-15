from src.checks import HEADER, as_magnitudes, assemble, check_derived, check_grounding, review
from src.solve import ROOT
from src.masters import Masters

M = Masters(ROOT / "master_data")


def line(qty, price, total="", taxes=()):
    return {"description": "", "item_type": "SERVICE", "uom": "", "quantity": qty, "unit_price": price,
            "total": total, "discount": "", "discount_percentage": "", "taxes": list(taxes)}


def draft(lines, invoice_type="INVOICE", **amounts):
    return {"invoice_number": "", "invoice_date": "", "due_date": "", "invoice_type": invoice_type, "currency": "EUR",
            "supplier": dict.fromkeys(["name", "address", "vat_id", "iban", "email", "country"], ""),
            "buyer": {"name": "", "address": ""}, "payment_terms": "", "po_number": "",
            **dict.fromkeys(HEADER, ""), **amounts, "taxes": [], "line_items": lines}


def submit(d, printed, derived=(), flags=()):
    return {"pages": [], "payables": [d], "declined": [], "printed": printed, "derived": list(derived), "flags": list(flags)}


def test_credit_flips_to_positive_but_invoice_with_negative_lines_does_not():
    credit = assemble(draft([line("1", "-327.87", "-327.87", [{"tax_type": "VAT", "tax_name": "KM", "tax_rate": "22", "tax_amount": "-72.13"}])],
                            "CREDIT_MEMO", gross_total="-400.00"), M)
    assert as_magnitudes(credit) and credit["gross_total"] == "400.00"
    invoice = assemble(draft([line("1", "500.00"), line("1", "-50.00")], gross_total="450.00"), M)
    assert not as_magnitudes(invoice)


def test_values_must_be_printed_or_derived_from_printed_figures():
    p = assemble(draft([line("400", "0.3252", "160.00")], gross_total="160.00"), M)
    assert check_grounding([p], ["400", "160.00", "0.400", "23"], [])[0]["kind"] == "ungrounded"
    derived = [{"payable": 0, "path": "line_items[0].unit_price", "expr": "0.400 / (1 + 23 / 100)"}]
    assert check_grounding([p], ["400", "160.00", "0.400", "23"], derived) == []
    assert check_derived(derived, [p]) == []
    assert check_grounding([p], ["400", "160.00"], derived)[0]["message"].endswith("not among the printed figures listed")


def test_derivation_from_the_target_gross_is_circular():
    p = assemble(draft([line("2", "21.73", "24.99")], gross_total="1683.98"), M)
    ratio = [{"payable": 0, "path": "line_items[0].unit_price", "expr": "24.99 * (1683.98 - 169.83) / 1683.98"}]
    assert "circular" in [s["kind"] for s in check_grounding([p], ["2", "24.99", "1683.98", "169.83"], ratio)]
    rebuilt = [{"payable": 0, "path": "line_items[0].unit_price", "expr": "24.99 * (1633.98 + 50.00 - 169.83) / (1633.98 + 50.00)"}]
    assert "circular" in [s["kind"] for s in check_grounding([p], ["2", "24.99", "1633.98", "50.00", "169.83"], rebuilt)]


def test_flag_stops_blocking_only_after_the_erp_view_was_seen():
    sub = submit(draft([line("1", "100.00")], gross_total="110.00"), ["100.00", "110.00"],
                 flags=[{"payable": 0, "message": "excise embedded in the lines"}])
    assert not review(sub, M, 0, False)[2] and review(sub, M, 1, False)[2]


def test_masters_leave_ambiguity_blank():
    assert M.supplier("", "DE 209 177 122") == "2845695"
    assert M.supplier("", "DE209177122", "EE101234567") == ""
    assert M.tax("GH", "2.5", "NHIL", "") == "TAX028" and M.tax("GH", "2.5", "VAT", "") == ""
    assert M.buyer("Northwind Operations OÜ", "VANA-LÕUNA TN 15, 10134 TALLINN")["business_unit_code"] == ""
    assert M.payment_term("", "2026-01-01", "2026-01-01") == ""
    assert M.po("4500012345") == ""
