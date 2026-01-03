# Copyright (c) 2026, Your Name/Company
# License: GNU General Public License v3. See license.txt

from collections import OrderedDict
import frappe
from frappe import _, _dict
from frappe.utils import cstr, getdate

from erpnext import get_company_currency, get_default_company
from erpnext.accounts.report.utils import convert_to_presentation_currency, get_currency
from erpnext.accounts.utils import get_account_currency

# Cache translations for headers
TRANSLATIONS = frappe._dict()

def execute(filters=None):
    if not filters:
        return [], []

    account_details = {}
    for acc in frappe.db.sql("""select name, is_group from tabAccount""", as_dict=1):
        account_details.setdefault(acc.name, acc)

    if filters.get("party"):
        filters.party = frappe.parse_json(filters.get("party"))

    validate_filters(filters, account_details)
    filters = set_account_currency(filters)

    # 1. Aging Data based on Posting Date
    aging_data = {}
    if filters.get("to_date"):
        aging_data = get_aged_ar_data(filters.get("to_date"))

    columns = get_columns(filters)
    update_translations()

    # 2. Get GL Data and build result
    res = get_result(filters, account_details, aging_data)
    return columns, res

def validate_filters(filters, account_details):
    if not filters.get("company"):
        frappe.throw(_("Company is mandatory"))
    if not filters.get("from_date") or not filters.get("to_date"):
        frappe.throw(_("From Date and To Date are mandatory"))
    if getdate(filters.from_date) > getdate(filters.to_date):
        frappe.throw(_("From Date must be before To Date"))

def update_translations():
    TRANSLATIONS.update(dict(
        OPENING=_("Opening"), 
        TOTAL=_("Total"), 
        CLOSING_TOTAL=_("Closing (Opening + Total)")
    ))

def set_account_currency(filters):
    filters["company_currency"] = frappe.get_cached_value("Company", filters.company, "default_currency")
    return filters

def get_result(filters, account_details, aging_data):
    gl_entries = get_gl_entries(filters)
    data = get_data_with_opening_closing(filters, account_details, gl_entries, aging_data)
    return get_result_as_list(data)

def get_gl_entries(filters):
    conditions = get_conditions(filters)
    return frappe.db.sql(f"""
        select
            name as gl_entry, posting_date, account, party_type, party,
            voucher_type, voucher_no, cost_center, project,
            against_voucher, account_currency, is_opening, debit, credit
        from `tabGL Entry`
        where company=%(company)s {conditions}
        order by posting_date, account, creation
    """, filters, as_dict=1)

def get_conditions(filters):
    conditions = [] 
    if filters.get("account"): conditions.append("account in %(account)s")
    if filters.get("party"): conditions.append("party in %(party)s")
    conditions.append("(posting_date <= %(to_date)s or is_opening = 'Yes')")
    if not filters.get("show_cancelled_entries"): conditions.append("is_cancelled = 0")
    return "and {}".format(" and ".join(conditions)) if conditions else ""

def get_data_with_opening_closing(filters, account_details, gl_entries, aging_data):
    data = []
    from_date = getdate(filters.from_date)
    
    # Initialize separate dictionaries for Opening and Transaction Totals
    # This ensures the bottom rows match image_690f4f.png exactly
    totals = _dict({
        "opening": {"debit": 0, "credit": 0},
        "period": {"debit": 0, "credit": 0},
        "closing": {"debit": 0, "credit": 0}
    })

    entries = []

    for gle in gl_entries:
        # Reset aging fields for each entry
        gle.update({"age_0_90": 0.0, "age_91_120": 0.0, "age_121_180": 0.0, "age_over_180": 0.0})

        # Inject Aging only for Sales Invoices
        if gle.voucher_type == "Sales Invoice":
            age_info = aging_data.get(gle.voucher_no, {})
            gle.update({
                "age_0_90": age_info.get("b1", 0.0),
                "age_91_120": age_info.get("b2", 0.0),
                "age_121_180": age_info.get("b3", 0.0),
                "age_over_180": age_info.get("b4", 0.0)
            })

        # Calculate Running Totals based on date
        if gle.posting_date < from_date or gle.is_opening == "Yes":
            totals.opening["debit"] += gle.debit
            totals.opening["credit"] += gle.credit
        else:
            totals.period["debit"] += gle.debit
            totals.period["credit"] += gle.credit
            entries.append(gle)

        # Closing is always the sum of everything
        totals.closing["debit"] += gle.debit
        totals.closing["credit"] += gle.credit

    # 1. Build the 'Opening' Row (₹3,81,483.00 Balance)
    data.append(_dict({
        "account": TRANSLATIONS.OPENING,
        "debit": totals.opening["debit"],
        "credit": totals.opening["credit"],
        "balance": totals.opening["debit"] - totals.opening["credit"]
    }))
    
    # 2. Add Transaction Entries
    data += entries
    
    # 3. Add 'Total' Row (Period totals: ₹1,28,904.00 / ₹4,50,834.00)
    data.append(_dict({
        "account": TRANSLATIONS.TOTAL,
        "debit": totals.period["debit"],
        "credit": totals.period["credit"],
        "balance": totals.period["debit"] - totals.period["credit"]
    }))

    # 4. Add 'Closing' Row (Final Balance: ₹59,553.00)
    data.append(_dict({
        "account": TRANSLATIONS.CLOSING_TOTAL,
        "debit": totals.closing["debit"],
        "credit": totals.closing["credit"],
        "balance": totals.closing["debit"] - totals.closing["credit"]
    }))

    return data

def get_result_as_list(data):
    """Refines the running balance calculation to match image_690c28.png."""
    running_balance = 0
    for i, d in enumerate(data):
        # The Opening row defines the starting balance
        if i == 0:
            running_balance = d.get("debit", 0) - d.get("credit", 0)
            d["balance"] = running_balance
            continue
            
        # Update running balance for entries
        if d.get("voucher_no") or d.get("posting_date"):
            running_balance += d.get("debit", 0) - d.get("credit", 0)
            d["balance"] = running_balance
        
        # Summary rows already have calculated balances from the logic above
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
    invoices = frappe.db.sql("""
        SELECT name, posting_date, outstanding_amount
        FROM `tabSales Invoice`
        WHERE docstatus = 1 AND outstanding_amount > 0
        AND posting_date <= %(report_date)s
    """, {"report_date": report_date}, as_dict=True)

    aging_buckets = {}
    for inv in invoices:
        diff = (ref_date - getdate(inv['posting_date'])).days
        amt = inv['outstanding_amount']
        res = {"b1": 0.0, "b2": 0.0, "b3": 0.0, "b4": 0.0}
        
        if diff <= 90: res["b1"] = amt
        elif 91 <= diff <= 120: res["b2"] = amt
        elif 121 <= diff <= 180: res["b3"] = amt
        else: res["b4"] = amt
        aging_buckets[inv['name']] = res
    return aging_buckets
