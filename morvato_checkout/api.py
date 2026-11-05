import re
from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, now_datetime, nowdate

from webshop.webshop.shopping_cart import cart as webshop_cart


def enabled():
	return cint(frappe.conf.get("morvato_pay_first"))


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
	with as_admin():
		wc = _get_or_create_checkout(quotation, settings, amount, user)
		url = _payment_url(wc, settings)
	return {"mode": "pay_first", "payment_url": url, "checkout": wc.name}


def _get_or_create_checkout(quotation, settings, amount, user):
	for name in frappe.get_all(
		"Web Checkout", filters={"quotation": quotation.name, "status": "Pending Payment"}, pluck="name"
	):
		wc = frappe.get_doc("Web Checkout", name)
		if abs(flt(wc.amount) - amount) < 0.01:
			return wc
		wc.db_set("status", "Superseded")  # cart changed since that attempt

	wc = frappe.get_doc(
		{
			"doctype": "Web Checkout",
			"quotation": quotation.name,
			"customer": quotation.party_name,
			"company": settings.company,
			"currency": quotation.currency,
			"amount": amount,
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
	if hasattr(controller, "validate_minimum_transaction_amount"):
		controller.validate_minimum_transaction_amount(wc.currency, wc.amount)

	return controller.get_payment_url(
		**{
			"amount": flt(wc.amount, 2),
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
	if not pe_name:
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		account = frappe.db.get_value(
			"Payment Gateway Account", settings.payment_gateway_account, "payment_account"
		)
		amount = flt(wc.amount)
		pe = get_payment_entry(
			"Sales Order", so_name, party_amount=amount, bank_account=account, bank_amount=amount
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
