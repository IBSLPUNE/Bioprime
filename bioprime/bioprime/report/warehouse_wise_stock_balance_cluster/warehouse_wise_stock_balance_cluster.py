# Copyright (c) 2022, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from typing import Any, Dict, List, Optional, TypedDict

import frappe
from frappe import _
from frappe.query_builder.functions import Sum


class StockBalanceFilter(TypedDict):
    company: Optional[str]
    warehouse: Optional[str]
    show_disabled_warehouses: Optional[int]


SLEntry = Dict[str, Any]


def execute(filters=None):
    columns, data = [], []
    columns = get_columns(filters)
    data = get_data(filters)

    return columns, data


def get_warehouse_wise_balance(filters: StockBalanceFilter) -> List[SLEntry]:
    sle = frappe.qb.DocType("Stock Ledger Entry")

    query = (
        frappe.qb.from_(sle)
        .select(sle.warehouse, Sum(sle.stock_value_difference).as_("stock_balance"))
        .where((sle.docstatus < 2) & (sle.is_cancelled == 0))
        .groupby(sle.warehouse)
    )

    if filters.get("company"):
        query = query.where(sle.company == filters.get("company"))

    data = query.run(as_list=True)
    return frappe._dict(data) if data else frappe._dict()


def get_warehouses(report_filters: StockBalanceFilter):
    filters = {"company": report_filters.company, "disabled": 0}
    if report_filters.get("show_disabled_warehouses"):
        filters["disabled"] = ("in", [0, report_filters.show_disabled_warehouses])

    return frappe.get_all(
        "Warehouse",
        fields=["name", "parent_warehouse", "is_group", "disabled"],
        filters=filters,
        order_by="lft",
    )


def get_data(filters: StockBalanceFilter):
    warehouse_balance = get_warehouse_wise_balance(filters)
    warehouses = get_warehouses(filters)

    territories = get_territories()  # Fetch territory information

    # Get the user's territory
    user_territory = frappe.db.get_value('Sales Person', {'custom_user': frappe.session.user}, 'custom_territory')
    
    # If user has a custom territory, filter by it, else show no data (or you can define a fallback)
    if user_territory:
        warehouses = [warehouse for warehouse in warehouses if territories.get(warehouse.name) == user_territory]
    else:
        warehouses = []

    for warehouse in warehouses:
        warehouse.stock_balance = warehouse_balance.get(warehouse.name, 0) or 0.0
        warehouse.territory = territories.get(warehouse.name, "")  # Set territory

    update_indent(warehouses)
    set_balance_in_parent(warehouses)

    return warehouses


def get_territories():
    """Fetch the territory associated with each warehouse via Sales Invoice and filter by user's territory."""
    # Fetch the logged-in user's territory
    user_territory = frappe.db.get_value('Sales Person', {'custom_user': frappe.session.user}, 'custom_territory')

    # If user has a custom territory, use it, otherwise default to 'default_territory'
    if user_territory:
        territory_condition = frappe.qb.DocType("Sales Invoice").territory == user_territory
    else:
        user_territory = 'default_territory'
        territory_condition = frappe.qb.DocType("Sales Invoice").territory == user_territory

    sales_invoice = frappe.qb.DocType("Sales Invoice")

    # Building the query with the condition
    warehouse_territories = (
        frappe.qb.from_(sales_invoice)
        .select(sales_invoice.set_warehouse, sales_invoice.territory)
        .where(sales_invoice.docstatus == 1)
        .where(territory_condition)  # Use the condition here
        .groupby(sales_invoice.set_warehouse)
    ).run(as_dict=True)

    return {entry["set_warehouse"]: entry["territory"] for entry in warehouse_territories}


def update_indent(warehouses):
    for warehouse in warehouses:

        def add_indent(warehouse, indent):
            warehouse.indent = indent
            for child in warehouses:
                if child.parent_warehouse == warehouse.name:
                    add_indent(child, indent + 1)

        if warehouse.is_group:
            add_indent(warehouse, warehouse.indent or 0)


def set_balance_in_parent(warehouses):
    # sort warehouses by indent in descending order
    warehouses = sorted(warehouses, key=lambda x: x.get("indent", 0), reverse=1)

    for warehouse in warehouses:

        def update_balance(warehouse, balance):
            for parent in warehouses:
                if warehouse.parent_warehouse == parent.name:
                    parent.stock_balance += balance

        update_balance(warehouse, warehouse.stock_balance)


def get_columns(filters: StockBalanceFilter) -> List[Dict]:
    columns = [
        {
            "label": _("Warehouse"),
            "fieldname": "name",
            "fieldtype": "Link",
            "options": "Warehouse",
            "width": 200,
        },
        {"label": _("Stock Balance"), "fieldname": "stock_balance", "fieldtype": "Float", "width": 150},
        {"label": _("Territory"), "fieldname": "territory", "fieldtype": "Link", "options": "Territory", "width": 150},
    ]

    if filters.get("show_disabled_warehouses"):
        columns.append(
            {
                "label": _("Warehouse Disabled?"),
                "fieldname": "disabled",
                "fieldtype": "Check",
                "width": 200,
            }
        )

    return columns

