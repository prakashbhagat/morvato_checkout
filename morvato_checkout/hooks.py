app_name = "morvato_checkout"
app_title = "Morvato Checkout"
app_publisher = "Morvato"
app_description = "Pay first, then create the Sales Order"
app_email = "admin@morvato.com"
app_license = "MIT"
required_apps = ["frappe", "erpnext", "webshop", "payments"]

override_whitelisted_methods = {
    "webshop.webshop.shopping_cart.cart.place_order": "morvato_checkout.api.place_order"
}

web_include_js = ["/assets/morvato_checkout/js/pay_first.js"]

scheduler_events = {
    "cron": {"*/15 * * * *": ["morvato_checkout.api.retry_failed"]}
}
