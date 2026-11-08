import re
from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, now_datetime, nowdate

from webshop.webshop.shopping_cart import cart as webshop_cart

MIN_ONLINE = 1  # Razorpay will not take less than 1 rupee


def enabled():
	if not cint(frappe.conf.get("morvato_pay_first")):
		return False
	users = frappe.conf.get("morvato_pay_first_users")
	return (not users) or frappe.session.user in users


@contextmanager
def as_admin():
	"""Run as Administrator without wiping the request's session or form data."""
	s = frappe.local.session
	user, sid, data = s.user, s.sid, s.data
	form_dict = frappe.local.form_dict
	frappe.set_user("Administrator")
	try:
		yield
	finally:
		frappe.set_user(user)
		frappe.local.session.sid = sid
		frappe.local.session.data = data
		frappe.local.form_dict = form_dict


def _lock(name):
	frappe.db.sql("select name from `tabWeb Checkout` where name=%s for update", name)


def _amount(q):
	return flt(flt(q.rounded_total) or flt(q.grand_total), q.precision("grand_total"))


def _payable(wc):
	"""Amount to collect online. Records from before credit existed pay the full amount."""
	if flt(wc.payable) > 0 or flt(wc.credit_applied) > 0:
		return flt(wc.payable)
	return flt(wc.amount)


# --------------------------------------------------------------------- credit
def _credit_notes(customer):
	return frappe.get_all(
		"Sales Invoice",
		filters={"customer": customer, "docstatus": 1, "is_return": 1, "outstanding_amount": ["<", 0]},
		fields=["name", "outstanding_amount"],
		order_by="posting_date asc, creation asc",
	)


def _credit_balance(customer):
	balance = sum(-flt(n.outstanding_amount) for n in _credit_notes(customer))
	held = frappe.get_all(
		"Web Checkout",
		filters={
			"customer": customer,
			"credit_applied": [">", 0],
			"status": ["in", ["Needs Attention", "Order Created"]],
			"credit_status": ["!=", "Applied"],
		},
		fields=["credit_applied"],
	)
	balance -= sum(flt(h.credit_applied) for h in held)
	return max(flt(balance, 2), 0)


def _plan_credit(customer, amount):
	use = min(_credit_balance(customer), amount)
	payable = flt(amount - use, 2)
	if 0 < payable < MIN_ONLINE:
		use = flt(amount - MIN_ONLINE, 2)
		payable = flt(amount - use, 2)
		if use < 0:
			use, payable = 0, amount
	return flt(use, 2), flt(payable, 2)


@frappe.whitelist()
def credit_preview():
	if not enabled():
		return {"balance": 0}
	cust = frappe.db.get_value("Portal User", {"user": frappe.session.user, "parenttype": "Customer"}, "parent")
	if not cust:
		return {"balance": 0}
	return {"balance": _credit_balance(cust), "customer": cust}


# ---------------------------------------------------------------- place order
@frappe.whitelist()
def place_order():
	if not enabled():
		return webshop_cart.place_order()  # original behaviour (kill switch off)

	settings = frappe.get_cached_doc("Webshop Settings")
	if not settings.payment_gateway_account:
		frappe.throw(_("Payment gateway is not configured."))

	quotation = webshop_cart._get_cart_quotation()
	if quotation.quotation_to != "Customer" or not quotation.party_name:
		frappe.throw(_("Please log in with your outlet account to place an order."))
	if not (quotation.shipping_address_name or quotation.customer_address):
		frappe.throw(_("Set Shipping Address or Billing Address"))

	if frappe.db.exists("Web Checkout", {"quotation": quotation.name, "status": "Needs Attention"}):
		frappe.throw(
			_("Your last payment is still being processed. Please check My Orders in a few minutes before ordering again.")
		)

	if not cint(settings.allow_items_not_in_stock):
		for item in quotation.get("items"):
			if frappe.db.get_value("Item", item.item_code, "is_stock_item"):
				stock = webshop_cart.get_web_item_qty_in_stock(item.item_code, "website_warehouse")
				if not cint(stock.in_stock):
					frappe.throw(_("{0} Not in Stock").format(item.item_code))
				if item.qty > stock.stock_qty:
					frappe.throw(_("Only {0} in Stock for item {1}").format(stock.stock_qty, item.item_code))

	amount = _amount(quotation)
	if amount <= 0:
		frappe.throw(_("Your cart total is zero."))

	user = frappe.session.user
	url = None
	with as_admin():
		frappe.db.sql("select name from `tabCustomer` where name=%s for update", quotation.party_name)
		credit = _plan_credit(quotation.party_name, amount)
		wc = _get_or_create_checkout(quotation, settings, amount, user, credit)
		wc_name = wc.name
		if _payable(wc) > 0:
			url = _payment_url(wc, settings)
		else:
			wc.db_set("payment_reference", "CREDIT-ONLY")

	if url:
		return {"mode": "pay_first", "payment_url": url, "checkout": wc_name,
			"credit_applied": credit[0], "payable": credit[1]}

	# the credit covers the whole order: no online payment needed
	process_payment(wc_name, "Completed")
	res = frappe.db.get_value("Web Checkout", wc_name, ["status", "sales_order"], as_dict=True)
	if res.status != "Order Created" or not res.sales_order:
		frappe.throw(_("Your order is being processed. Please check My Orders in a few minutes. Do not order again."))
	return {"mode": "credit_only", "redirect": "/orders/" + res.sales_order, "checkout": wc_name}


def _get_or_create_checkout(quotation, settings, amount, user, credit):
	use, payable = credit
	keep = None
	for name in frappe.get_all(
		"Web Checkout", filters={"customer": quotation.party_name, "status": "Pending Payment"}, pluck="name"
	):
		wc = frappe.get_doc("Web Checkout", name)
		if not keep and wc.quotation == quotation.name and abs(flt(wc.amount) - amount) < 0.01:
			wc.db_set({"credit_applied": use, "payable": payable, "credit_status": "Pending" if use else ""})
			keep = wc
		else:
			wc.db_set("status", "Superseded")  # the cart changed, or an older attempt
	if keep:
		return keep

	wc = frappe.get_doc(
		{
			"doctype": "Web Checkout",
			"quotation": quotation.name,
			"customer": quotation.party_name,
			"company": settings.company,
			"currency": quotation.currency,
			"amount": amount,
			"credit_applied": use,
			"payable": payable,
			"credit_status": "Pending" if use else "",
			"status": "Pending Payment",
			"user": user,
		}
	)
	wc.insert(ignore_permissions=True)
	return wc


def _payment_url(wc, settings):
	from erpnext.accounts.doctype.payment_request.payment_request import _get_payment_gateway_controller

	gateway = frappe.db.get_value("Payment Gateway Account", settings.payment_gateway_account, "payment_gateway")
	controller = _get_payment_gateway_controller(gateway)
	controller.validate_transaction_currency(wc.currency)
	pay = _payable(wc)
	if hasattr(controller, "validate_minimum_transaction_amount"):
		controller.validate_minimum_transaction_amount(wc.currency, pay)

	return controller.get_payment_url(
		**{
			"amount": flt(pay, 2),
			"title": wc.company,
			"description": _("Payment for order {0}").format(wc.quotation),
			"reference_doctype": "Web Checkout",
			"reference_docname": wc.name,
			"payer_email": frappe.db.get_value("User", wc.user, "email") or wc.user,
			"payer_name": frappe.db.get_value("Customer", wc.customer, "customer_name"),
			"order_id": wc.name,
			"currency": wc.currency,
			"payment_gateway": gateway,
		}
	)


# ------------------------------------------------------------ payment callback
def process_payment(name, status):
	"""Called by Razorpay's callback via Web Checkout.on_payment_authorized. Never raises."""
	redirect = "/orders"
	try:
		ref = _find_payment_id(name)
		with as_admin():
			_lock(name)
			wc = frappe.get_doc("Web Checkout", name)
			if wc.status == "Order Created":
				_note_duplicate(wc, ref)
			else:
				wc.db_set(
					{
						"status": "Needs Attention",
						"payment_reference": ref or wc.payment_reference,
						"paid_on": now_datetime(),
						"error": "Payment received. Order not created yet.",
					}
				)
				_convert(wc)
			wc.reload()
			if wc.sales_order:
				redirect = "/orders/" + wc.sales_order
		if hasattr(frappe.local, "cookie_manager"):
			frappe.local.cookie_manager.delete_cookie("cart_count")
	except Exception:
		frappe.log_error(title=f"Web Checkout {name}: callback error")
	return redirect


def _find_payment_id(name):
	try:
		for row in frappe.get_all(
			"Integration Request",
			filters={"reference_doctype": "Web Checkout", "reference_docname": name},
			fields=["output"],
			order_by="modified desc",
		):
			m = re.search(r"pay_[A-Za-z0-9]+", row.output or "")
			if m:
				return m.group(0)
	except Exception:
		pass
	return None


def _note_duplicate(wc, ref):
	if ref and ref == wc.payment_reference:
		return  # same payment reported twice, nothing to do
	note = f"DUPLICATE PAYMENT {ref or ''} at {now_datetime()}. Refund needed."
	wc.db_set({"needs_refund": 1, "error": ((wc.error or "") + "\n" + note).strip()})
	frappe.log_error(title=f"Web Checkout {wc.name}: duplicate payment", message=note)


def _convert(wc):
	frappe.db.savepoint("wc_order")
	try:
		so, pe = _create_order_and_payment(wc)
	except Exception:
		frappe.db.rollback(save_point="wc_order")
		frappe.log_error(title=f"Web Checkout {wc.name}: order creation failed")
		wc.db_set(
			{"status": "Needs Attention", "retry_count": cint(wc.retry_count) + 1,
			 "error": frappe.get_traceback()[-1500:]}
		)
		return False

	wc.db_set({"status": "Order Created", "sales_order": so, "payment_entry": pe, "error": ""})
	_try_invoice(wc, so)
	return True


def _create_order_and_payment(wc):
	settings = frappe.get_cached_doc("Webshop Settings")
	q = frappe.get_doc("Quotation", wc.quotation)
	if q.docstatus == 2:
		frappe.throw("Quotation is cancelled")

	so_name = frappe.db.get_value(
		"Sales Order Item", {"prevdoc_docname": q.name, "docstatus": ["<", 2]}, "parent"
	)
	if not so_name:
		if q.docstatus == 0 and abs(_amount(q) - flt(wc.amount)) > 0.01:
			frappe.throw("Cart changed after payment started. Amount paid differs from the cart.")
		if q.valid_till and getdate(q.valid_till) < getdate(nowdate()):
			frappe.db.set_value("Quotation", q.name, "valid_till", nowdate(), update_modified=False)
			q.reload()
		if q.docstatus == 0:
			q.company = settings.company
			q.flags.ignore_permissions = True
			q.submit()

		so = frappe.get_doc(webshop_cart._make_sales_order(q.name, ignore_permissions=True))
		so.payment_schedule = []
		if not cint(settings.allow_items_not_in_stock):
			for item in so.get("items"):
				item.warehouse = frappe.db.get_value(
					"Website Item", {"item_code": item.item_code}, "website_warehouse"
				)
		so.flags.ignore_permissions = True
		so.insert()
		so.submit()
		so_name = so.name

	pe_name = frappe.db.get_value(
		"Payment Entry Reference", {"reference_doctype": "Sales Order", "reference_name": so_name}, "parent"
	)
	pay = _payable(wc)
	if not pe_name and pay > 0:
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		account = frappe.db.get_value(
			"Payment Gateway Account", settings.payment_gateway_account, "payment_account"
		)
		pe = get_payment_entry(
			"Sales Order", so_name, party_amount=pay, bank_account=account, bank_amount=pay
		)
		pe.set_missing_ref_details(force=True)
		pe.update(
			{
				"reference_no": wc.payment_reference or wc.name,
				"reference_date": nowdate(),
				"remarks": f"Razorpay payment for {so_name} ({wc.name})",
			}
		)
		pe.insert(ignore_permissions=True)
		pe.submit()
		pe_name = pe.name
	return so_name, pe_name


def _try_invoice(wc, so):
	existing = frappe.db.get_value("Sales Invoice Item", {"sales_order": so, "docstatus": 1}, "parent")
	if existing:
		wc.db_set("sales_invoice", existing)
		_apply_credit(wc, existing)
		return existing

	frappe.db.savepoint("wc_invoice")
	try:
		from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

		si = make_sales_invoice(so, ignore_permissions=True)
		si.allocate_advances_automatically = True
		si = si.insert(ignore_permissions=True)
		si.submit()
		wc.db_set("sales_invoice", si.name)
	except Exception:
		frappe.db.rollback(save_point="wc_invoice")
		frappe.log_error(title=f"Web Checkout {wc.name}: invoice failed")
		wc.db_set("error", "Order and payment recorded. Invoice failed:\n" + frappe.get_traceback()[-800:])
		return None

	_apply_credit(wc, si.name)
	return si.name


def _apply_credit(wc, si_name):
	"""Use the outlet's credit notes against the new invoice. Never undoes the order or payment."""
	need = flt(wc.credit_applied)
	if need <= 0 or wc.credit_status == "Applied":
		return
	frappe.db.savepoint("wc_credit")
	try:
		si = frappe.get_doc("Sales Invoice", si_name)
		remaining = min(need, flt(si.outstanding_amount))
		for n in _credit_notes(wc.customer):
			if remaining <= 0.005:
				break
			use = min(remaining, -flt(n.outstanding_amount))
			_reconcile(si, n.name, use)
			remaining = flt(remaining - use, 2)
		if remaining > 0.005:
			frappe.throw("Not enough credit left to apply " + str(remaining))
		wc.db_set("credit_status", "Applied")
	except Exception:
		frappe.db.rollback(save_point="wc_credit")
		frappe.log_error(title=f"Web Checkout {wc.name}: credit not applied")
		wc.db_set({"credit_status": "Needs Attention",
			   "error": "Credit not applied:\n" + frappe.get_traceback()[-800:]})


def _reconcile(si, credit_note, amount):
	pr = frappe.get_doc("Payment Reconciliation")
	pr.company = si.company
	pr.party_type = "Customer"
	pr.party = si.customer
	pr.receivable_payable_account = si.debit_to
	pr.invoice_name = si.name
	pr.payment_name = credit_note
	pr.invoice_limit = 10
	pr.payment_limit = 10
	pr.get_unreconciled_entries()

	inv = [d.as_dict() for d in pr.get("invoices") if d.invoice_number == si.name]
	pay = [d.as_dict() for d in pr.get("payments") if d.reference_name == credit_note]
	if not inv or not pay:
		frappe.throw("Payment Reconciliation could not find " + si.name + " or " + credit_note)
	pr.allocate_entries({"invoices": inv, "payments": pay})
	rows = pr.get("allocation")
	if len(rows) != 1:
		frappe.throw("Unexpected allocation rows: " + str(len(rows)))
	rows[0].allocated_amount = flt(min(flt(rows[0].allocated_amount), amount), 2)
	pr.reconcile()


# ------------------------------------------------------------------ scheduler
def retry_failed():
	for name in frappe.get_all(
		"Web Checkout",
		filters={"status": "Needs Attention", "retry_count": ["<", 5], "paid_on": ["is", "set"]},
		pluck="name",
	):
		try:
			_lock(name)
			wc = frappe.get_doc("Web Checkout", name)
			if wc.status == "Needs Attention":
				_convert(wc)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Web Checkout {name}: retry failed")

	for name in frappe.get_all(
		"Web Checkout",
		filters={"status": "Order Created", "retry_count": ["<", 5], "sales_order": ["is", "set"]},
		or_filters=[["sales_invoice", "is", "not set"], ["credit_status", "=", "Needs Attention"]],
		pluck="name",
	):
		try:
			_lock(name)
			wc = frappe.get_doc("Web Checkout", name)
			wc.db_set("retry_count", cint(wc.retry_count) + 1)
			if not wc.sales_invoice:
				_try_invoice(wc, wc.sales_order)
			elif wc.credit_status == "Needs Attention":
				_apply_credit(wc, wc.sales_invoice)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Web Checkout {name}: credit/invoice retry failed")
