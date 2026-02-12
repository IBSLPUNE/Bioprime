# Copyright (c) 2026, Your Name/Company
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _, _dict
from frappe.utils import getdate

TRANSLATIONS = frappe._dict()


def execute(filters=None):
    if not filters:
        return [], []

    if filters.get("party"):
        filters.party = frappe.parse_json(filters.get("party"))

    if filters.get("account"):
        filters.account = frappe.parse_json(filters.get("account"))

    if filters.get("cost_center"):
        filters.cost_center = frappe.parse_json(filters.get("cost_center"))

    if filters.get("project"):
        filters.project = frappe.parse_json(filters.get("project"))

    validate_filters(filters)
    set_account_currency(filters)

    aging_data = {}
    if filters.get("to_date"):
        aging_data = get_aged_ar_data(filters.get("to_date"))

    columns = get_columns(filters)
    update_translations()

    data = get_result(filters, aging_data)
    return columns, data


def validate_filters(filters):
    if not filters.get("company"):
        frappe.throw(_("Company is mandatory"))
    if not filters.get("from_date") or not filters.get("to_date"):
        frappe.throw(_("From Date and To Date are mandatory"))
    if getdate(filters.from_date) > getdate(filters.to_date):
        frappe.throw(_("From Date must be before To Date"))


def update_translations():
    TRANSLATIONS.update(
        dict(
            OPENING=_("Opening"),
            TOTAL=_("Total"),
            CLOSING_TOTAL=_("Closing (Opening + Total)")
        )
    )


def set_account_currency(filters):
    filters["company_currency"] = frappe.get_cached_value(
        "Company", filters.company, "default_currency"
    )


def get_result(filters, aging_data):
    gl_entries = get_gl_entries(filters)
    data = get_data_with_opening_closing(filters, gl_entries, aging_data)
    return get_result_as_list(data)


def get_gl_entries(filters):
    conditions = get_conditions(filters)

    return frappe.db.sql(
        f"""
        SELECT
            name,
            posting_date,
            account,
            party_type,
            party,
            voucher_type,
            voucher_no,
            against_voucher,
            cost_center,
            project,
            account_currency,
            is_opening,
            debit,
            credit
        FROM `tabGL Entry`
        WHERE company = %(company)s
        {conditions}
        ORDER BY posting_date, account, creation
        """,
        filters,
        as_dict=True,
    )


def get_conditions(filters):
    conditions = []

    # Date condition
    conditions.append("(posting_date <= %(to_date)s OR is_opening = 'Yes')")

    if filters.get("account"):
        conditions.append("account IN %(account)s")

    if filters.get("party"):
        conditions.append("party IN %(party)s")

    if filters.get("voucher_no"):
        conditions.append("voucher_no = %(voucher_no)s")

    if filters.get("against_voucher_no"):
        conditions.append("against_voucher = %(against_voucher_no)s")

    if filters.get("cost_center"):
        conditions.append("cost_center IN %(cost_center)s")

    if filters.get("project"):
        conditions.append("project IN %(project)s")

    if not filters.get("show_cancelled_entries"):
        conditions.append("is_cancelled = 0")

    # 🔹 Ignore Exchange Rate Revaluation Journals (GL SAME)
    if filters.get("ignore_err"):
        err_journals = frappe.db.get_all(
            "Journal Entry",
            filters={
                "company": filters.company,
                "docstatus": 1,
                "voucher_type": ("in", ["Exchange Rate Revaluation", "Exchange Gain Or Loss"]),
            },
            pluck="name",
        )
        if err_journals:
            filters["voucher_no_not_in"] = err_journals

    # 🔹 Ignore System Generated Credit / Debit Notes (GL SAME)
    if filters.get("ignore_cr_dr_notes"):
        system_generated_jv = frappe.db.get_all(
            "Journal Entry",
            filters={
                "company": filters.company,
                "docstatus": 1,
                "voucher_type": ("in", ["Credit Note", "Debit Note"]),
                "is_system_generated": 1,
            },
            pluck="name",
        )

        if system_generated_jv:
            filters.setdefault("voucher_no_not_in", [])
            filters["voucher_no_not_in"] += system_generated_jv

    if filters.get("voucher_no_not_in"):
        conditions.append("voucher_no NOT IN %(voucher_no_not_in)s")

    return " AND " + " AND ".join(conditions)


def get_data_with_opening_closing(filters, gl_entries, aging_data):
    data = []
    from_date = getdate(filters.from_date)

    totals = _dict(
        opening={"debit": 0, "credit": 0, "age_0_90": 0, "age_91_120": 0, "age_121_180": 0, "age_over_180": 0},
        period={"debit": 0, "credit": 0, "age_0_90": 0, "age_91_120": 0, "age_121_180": 0, "age_over_180": 0},
        closing={"debit": 0, "credit": 0, "age_0_90": 0, "age_91_120": 0, "age_121_180": 0, "age_over_180": 0},
    )

    entries = []

    for gle in gl_entries:
        gle.update(
            dict(age_0_90=0, age_91_120=0, age_121_180=0, age_over_180=0)
        )

        if gle.voucher_type == "Sales Invoice":
            age = aging_data.get(gle.voucher_no, {})
            gle.update(
                dict(
                    age_0_90=age.get("b1", 0),
                    age_91_120=age.get("b2", 0),
                    age_121_180=age.get("b3", 0),
                    age_over_180=age.get("b4", 0),
                )
            )

        if gle.posting_date < from_date or gle.is_opening == "Yes":
            update_totals(totals.opening, gle)
        else:
            update_totals(totals.period, gle)
            entries.append(gle)

        update_totals(totals.closing, gle)

    data.append(create_total_row(TRANSLATIONS.OPENING, totals.opening))
    data.extend(entries)
    data.append(create_total_row(TRANSLATIONS.TOTAL, totals.period))
    data.append(create_total_row(TRANSLATIONS.CLOSING_TOTAL, totals.closing))

    return data

def update_totals(target, gle):
    target["debit"] += float(gle.debit or 0)
    target["credit"] += float(gle.credit or 0)
    
    # IMPORTANT: Only add to aging totals if this is a row that has aging data
    # This prevents the totals from inflating if there are non-invoice entries
    if gle.get("age_over_180") or gle.get("age_0_90") or gle.get("age_91_120") or gle.get("age_121_180"):
        target["age_0_90"] += float(gle.age_0_90 or 0)
        target["age_91_120"] += float(gle.age_91_120 or 0)
        target["age_121_180"] += float(gle.age_121_180 or 0)
        target["age_over_180"] += float(gle.age_over_180 or 0)


def create_total_row(label, totals):
    return _dict(
        account=label,
        debit=totals["debit"],
        credit=totals["credit"],
        balance=totals["debit"] - totals["credit"],
        age_0_90=totals["age_0_90"],
        age_91_120=totals["age_91_120"],
        age_121_180=totals["age_121_180"],
        age_over_180=totals["age_over_180"],
    )


def get_result_as_list(data):
    running_balance = 0.0
    
    for i, d in enumerate(data):
        debit = float(d.get("debit") or 0)
        credit = float(d.get("credit") or 0)

        # 1. For the very first row (Opening), set the initial balance
        if i == 0:
            running_balance = debit - credit
            d["balance"] = running_balance
        
        # 2. For Total rows (Row 7 in your screenshot), we show the Period Net Change
        elif d.get("account") == TRANSLATIONS.TOTAL:
            d["balance"] = debit - credit
            # We do NOT update running_balance here to avoid double-counting
            
        # 3. For the Closing row, we show the final accumulated balance
        elif d.get("account") == TRANSLATIONS.CLOSING_TOTAL:
            d["balance"] = running_balance
            
        # 4. For standard transaction rows
        else:
            running_balance += (debit - credit)
            d["balance"] = running_balance
            
    return data
    
    

def get_columns(filters):
    return [
        {"label": _("Date"), "fieldname": "posting_date", "fieldtype": "Date", "width": 100},
        {"label": _("Voucher No"), "fieldname": "voucher_no", "fieldtype": "Dynamic Link", "options": "voucher_type", "width": 160},
        {"label": _("Debit"), "fieldname": "debit", "fieldtype": "Currency", "width": 120},
        {"label": _("Credit"), "fieldname": "credit", "fieldtype": "Currency", "width": 120},
        {"label": _("Balance"), "fieldname": "balance", "fieldtype": "Currency", "width": 120},
        {"label": _("0-90 Days"), "fieldname": "age_0_90", "fieldtype": "Currency", "width": 110},
        {"label": _("91-120 Days"), "fieldname": "age_91_120", "fieldtype": "Currency", "width": 110},
        {"label": _("121-180 Days"), "fieldname": "age_121_180", "fieldtype": "Currency", "width": 110},
        {"label": _("Over 180 Days"), "fieldname": "age_over_180", "fieldtype": "Currency", "width": 110},
    ]


@frappe.whitelist()
def get_aged_ar_data(report_date):
    ref_date = getdate(report_date)

    invoices = frappe.db.sql(
        """
        SELECT name, posting_date, outstanding_amount
        FROM `tabSales Invoice`
        WHERE docstatus = 1
        AND outstanding_amount > 0
        AND posting_date <= %(date)s
        """,
        {"date": report_date},
        as_dict=True,
    )

    aging = {}
    for inv in invoices:
        diff = (ref_date - inv.posting_date).days
        amt = inv.outstanding_amount
        aging[inv.name] = {
            "b1": amt if diff <= 90 else 0,
            "b2": amt if 91 <= diff <= 120 else 0,
            "b3": amt if 121 <= diff <= 180 else 0,
            "b4": amt if diff > 180 else 0,
        }

    return aging
