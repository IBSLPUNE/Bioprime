# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

from collections import OrderedDict
import frappe
from frappe import _, _dict
from frappe.utils import cstr, getdate

from erpnext import get_company_currency, get_default_company
from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import (
	get_accounting_dimensions,
	get_dimension_with_children,
)
from erpnext.accounts.report.financial_statements import get_cost_centers_with_children
from erpnext.accounts.report.utils import convert_to_presentation_currency, get_currency
from erpnext.accounts.utils import get_account_currency

# to cache translations
TRANSLATIONS = frappe._dict()

def execute(filters=None):
	if not filters:
		return [], []

	account_details = {}

	if filters and filters.get("print_in_account_currency") and not filters.get("account"):
		frappe.throw(_("Select an account to print in account currency"))

	for acc in frappe.db.sql("""select name, is_group from tabAccount""", as_dict=1):
		account_details.setdefault(acc.name, acc)

	if filters.get("party"):
		filters.party = frappe.parse_json(filters.get("party"))

	validate_filters(filters, account_details)
	validate_party(filters)
	filters = set_account_currency(filters)

	# --- AGING LOGIC START ---
	aging_data = {}
	if filters.get("to_date"):
		account_list = filters.get("account")
		is_ar_report = filters.get("party_type") == "Customer" or (
			account_list and any(is_receivable_account(acc) for acc in account_list)
		)
		
		if is_ar_report:
			# Fetching aging data based on POSTING DATE
			aging_data = get_aged_ar_data(filters.get("to_date"))
	# --- AGING LOGIC END ---

	columns = get_columns(filters)
	update_translations()

	res = get_result(filters, account_details, aging_data)
	return columns, res

def update_translations():
	TRANSLATIONS.update(
		dict(OPENING=_("Opening"), TOTAL=_("Total"), CLOSING_TOTAL=_("Closing (Opening + Total)"))
	)

def validate_filters(filters, account_details):
	if not filters.get("company"):
		frappe.throw(_("{0} is mandatory").format(_("Company")))
	if not filters.get("from_date") or not filters.get("to_date"):
		frappe.throw(_("From Date and To Date are mandatory"))
	if filters.from_date > filters.to_date:
		frappe.throw(_("From Date must be before To Date"))

def is_receivable_account(account_name):
	return frappe.db.get_value("Account", account_name, "account_type") == "Receivable"

@frappe.whitelist()
def get_aged_ar_data(report_date):
	""" Calculates aging buckets based on POSTING DATE """
	invoices = frappe.db.sql("""
		SELECT name, posting_date, outstanding_amount
		FROM `tabSales Invoice`
		WHERE docstatus = 1 AND outstanding_amount > 0
		AND posting_date <= %(report_date)s
	""", {"report_date": report_date}, as_dict=True)

	aging_buckets = {}
	ref_date = getdate(report_date)

	for inv in invoices:
		days = (ref_date - getdate(inv['posting_date'])).days
		out = inv['outstanding_amount']
		
		bucket = {"b1": 0.0, "b2": 0.0, "b3": 0.0, "b4": 0.0}
		
		# Movement logic: Ensures transition at 91, 121, 181 days
		if days <= 90:
			bucket["b1"] = out
		elif days <= 120:
			bucket["b2"] = out
		elif days <= 180:
			bucket["b3"] = out
		else:
			bucket["b4"] = out

		aging_buckets[inv['name']] = bucket
		
	return aging_buckets

def get_result(filters, account_details, aging_data):
	gl_entries = get_gl_entries(filters)
	data = get_data_with_opening_closing(filters, account_details, gl_entries, aging_data)
	return get_result_as_list(data, filters)

def get_gl_entries(filters):
	currency_map = get_currency(filters)
	conditions = get_conditions(filters)
	
	# Exclude Journal Entry Modification
	conditions += " AND voucher_type != 'Journal Entry'"

	gl_entries = frappe.db.sql(f"""
		SELECT
			name as gl_entry, posting_date, account, party_type, party,
			voucher_type, voucher_no, debit, credit, is_opening, against_voucher
		FROM `tabGL Entry`
		WHERE company=%(company)s {conditions}
		ORDER BY posting_date, account, creation
	""", filters, as_dict=1)

	if filters.get("presentation_currency"):
		return convert_to_presentation_currency(gl_entries, currency_map, filters)
	return gl_entries

def get_conditions(filters):
	conditions = []
	if filters.get("account"):
		conditions.append("account in %(account)s")
	if filters.get("party"):
		conditions.append("party in %(party)s")
	# Filter for all entries up to to_date for correct opening/closing balance
	conditions.append("posting_date <= %(to_date)s")
	if not filters.get("show_cancelled_entries"):
		conditions.append("is_cancelled = 0")
	
	# Return with leading AND for safe concatenation
	return " AND " + " AND ".join(conditions) if conditions else ""

def get_data_with_opening_closing(filters, account_details, gl_entries, aging_data):
	data = []
	gle_map = OrderedDict()
	from_date = getdate(filters.from_date)

	# Initialize GL Entry map with aging buckets
	for gle in gl_entries:
		if gle.account not in gle_map:
			gle_map[gle.account] = _dict(totals=get_totals_dict(), entries=[])
		
		# Apply aging data to the row
		if gle.voucher_type == "Sales Invoice":
			age = aging_data.get(gle.voucher_no, {})
			gle.update({
				"age_0_90": age.get("b1", 0),
				"age_91_120": age.get("b2", 0),
				"age_121_180": age.get("b3", 0),
				"age_over_180": age.get("b4", 0)
			})
		else:
			gle.update({"age_0_90": 0, "age_91_120": 0, "age_121_180": 0, "age_over_180": 0})

	global_totals = get_totals_dict()

	for gle in gl_entries:
		acc_totals = gle_map[gle.account].totals
		# Logic for Opening Balance Rows
		if gle.posting_date < from_date:
			update_totals(global_totals.opening, gle)
			update_totals(global_totals.closing, gle)
			update_totals(acc_totals.opening, gle)
			update_totals(acc_totals.closing, gle)
		else:
			# Logic for Period Activity Rows
			update_totals(global_totals.total, gle)
			update_totals(global_totals.closing, gle)
			update_totals(acc_totals.total, gle)
			update_totals(acc_totals.closing, gle)
			gle_map[gle.account].entries.append(gle)

	# Construct Final Report Rows
	data.append(global_totals.opening)
	for acc in gle_map:
		acc_obj = gle_map[acc]
		if acc_obj.entries:
			data.append(acc_obj.totals.opening)
			data += acc_obj.entries
			data.append(acc_obj.totals.total)
			data.append(acc_obj.totals.closing)
			data.append({}) # Blank spacer row

	data.append(global_totals.total)
	data.append(global_totals.closing)
	return data

def get_totals_dict():
	def _get_row(label):
		return _dict({
			"account": label, "debit": 0.0, "credit": 0.0, 
			"age_0_90": 0.0, "age_91_120": 0.0, "age_121_180": 0.0, "age_over_180": 0.0
		})
	return _dict({
		"opening": _get_row(TRANSLATIONS.OPENING),
		"total": _get_row(TRANSLATIONS.TOTAL),
		"closing": _get_row(TRANSLATIONS.CLOSING_TOTAL)
	})

def update_totals(target, gle):
	target.debit += gle.get("debit", 0)
	target.credit += gle.get("credit", 0)
	target.age_0_90 += gle.get("age_0_90", 0)
	target.age_91_120 += gle.get("age_91_120", 0)
	target.age_121_180 += gle.get("age_121_180", 0)
	target.age_over_180 += gle.get("age_over_180", 0)

def get_result_as_list(data, filters):
	balance = 0
	for d in data:
		if d.get("account") == TRANSLATIONS.OPENING:
			balance = d.get("debit", 0) - d.get("credit", 0)
		else:
			balance += (d.get("debit", 0) - d.get("credit", 0))
		d["balance"] = balance
	return data

def get_columns(filters):
	return [
		{"label": _("Posting Date"), "fieldname": "posting_date", "fieldtype": "Date", "width": 100},
		{"label": _("Voucher Type"), "fieldname": "voucher_type", "width": 120},
		{"label": _("Voucher No"), "fieldname": "voucher_no", "fieldtype": "Dynamic Link", "options": "voucher_type", "width": 160},
		{"label": _("Debit"), "fieldname": "debit", "fieldtype": "Currency", "width": 110},
		{"label": _("Credit"), "fieldname": "credit", "fieldtype": "Currency", "width": 110},
		{"label": _("Balance"), "fieldname": "balance", "fieldtype": "Currency", "width": 110},
		{"label": _("0 - 90"), "fieldname": "age_0_90", "fieldtype": "Currency", "width": 100},
		{"label": _("91 - 120"), "fieldname": "age_91_120", "fieldtype": "Currency", "width": 100},
		{"label": _("121 - 180"), "fieldname": "age_121_180", "fieldtype": "Currency", "width": 100},
		{"label": _("Over 180"), "fieldname": "age_over_180", "fieldtype": "Currency", "width": 100},
	]

def set_account_currency(filters):
	filters.company_currency = frappe.get_cached_value("Company", filters.company, "default_currency")
	return filters

def validate_party(filters):
	pass # Standard party validation can be added here
