from frappe.model.document import Document


class WebCheckout(Document):
	def on_payment_authorized(self, status=None):
		if status not in ("Authorized", "Completed"):
			return
		from morvato_checkout.api import process_payment

		return process_payment(self.name, status)
