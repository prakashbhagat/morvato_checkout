(function () {
	function showCredit() {
		try {
			var btn = document.querySelector(".btn-place-order");
			if (!btn || document.getElementById("mc-credit-note")) return;
			frappe.call({
				method: "morvato_checkout.api.credit_preview",
				callback: function (r) {
					var b = r.message && r.message.balance;
					if (!b || b <= 0) return;
					var d = document.createElement("div");
					d.id = "mc-credit-note";
					d.style.cssText = "margin-top:8px;font-size:13px";
					d.textContent = "Your credit of Rs. " + (Math.round(b * 100) / 100).toFixed(2) + " will be adjusted automatically at checkout.";
					btn.parentNode.appendChild(d);
				},
				error: function () {},
			});
		} catch (e) {}
	}
	if (window.frappe && frappe.ready) { frappe.ready(showCredit); }
	else { document.addEventListener("DOMContentLoaded", showCredit); }

	document.addEventListener("click", function (e) {
		var btn = e.target.closest && e.target.closest(".btn-place-order");
		if (!btn) return;
		e.preventDefault();
		e.stopImmediatePropagation();
		if (btn.dataset.busy) return;
		btn.dataset.busy = "1";

		var fail = function (msg) {
			delete btn.dataset.busy;
			if (window.shopping_cart && shopping_cart.unfreeze) shopping_cart.unfreeze();
			$("#cart-error").empty().html(msg || frappe._("Something went wrong!")).toggle(true);
		};

		if (window.shopping_cart && shopping_cart.freeze) shopping_cart.freeze();
		frappe.call({
			type: "POST",
			method: "webshop.webshop.shopping_cart.cart.place_order",
			callback: function (r) {
				if (r.exc) {
					var msg = "";
					if (r._server_messages) {
						msg = JSON.parse(r._server_messages || []).join("<br>");
					}
					return fail(msg);
				}
				var m = r.message;
				if (m && m.payment_url) {
					window.location.href = m.payment_url;
				} else if (m && m.redirect) {
					window.location.href = m.redirect;
				} else if (typeof m === "string") {
					window.location.href = "/orders/" + encodeURIComponent(m);
				} else {
					fail();
				}
			},
			error: function () { fail(); },
		});
	}, true);
})();
