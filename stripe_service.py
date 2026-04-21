import json
import os
from urllib import error, request
from collections import Counter

import stripe


PAGE_SIZE = 100
OVER_FIVE_USD_CENTS = 500
REQUIRED_EMAIL_TEXT = "openloophealth"
HIGH_VALUE_NOTIFICATION_WEBHOOK_ENV = "HIGH_VALUE_SLACK_WEBHOOK_URL"
LIST_EXPAND_FULL = [
    "data.customer",
    "data.items.data.price",
    "data.latest_invoice.charge",
    "data.latest_invoice.payment_intent",
]
LIST_EXPAND_LIGHT = ["data.customer"]
SUBSCRIPTION_EXPAND_FULL = [
    "customer",
    "items.data.price",
    "latest_invoice.charge",
    "latest_invoice.payment_intent",
]


def validate_api_key(api_key):
    stripe.Subscription.list(limit=1, status="all", api_key=api_key)


def get_all_subscriptions(api_key, status_filter="all", expand=None):
    all_subscriptions = []
    starting_after = None
    expand = LIST_EXPAND_FULL if expand is None else expand

    while True:
        params = {
            "limit": PAGE_SIZE,
            "expand": expand,
            "status": status_filter or "all",
            "api_key": api_key,
        }

        if starting_after:
            params["starting_after"] = starting_after

        response = stripe.Subscription.list(**params)
        all_subscriptions.extend(response.data)

        if not response.has_more:
            break

        starting_after = response.data[-1].id

    return all_subscriptions


def build_customer_email_search_query(search_text="", required_text=REQUIRED_EMAIL_TEXT):
    terms = []
    for raw_term in (required_text, search_text):
        term = (raw_term or "").strip()
        if not term:
            continue
        escaped = term.replace("\\", "\\\\").replace('"', '\\"')
        terms.append(f'email:"{escaped}"')

    return " AND ".join(terms)


def search_customers_by_email(api_key, search_text="", required_text=REQUIRED_EMAIL_TEXT):
    query = build_customer_email_search_query(search_text, required_text)
    if not query:
        return []

    customers = []
    page = None

    while True:
        params = {
            "query": query,
            "limit": PAGE_SIZE,
            "api_key": api_key,
        }
        if page:
            params["page"] = page

        response = stripe.Customer.search(**params)
        customers.extend(response.data)

        page = getattr(response, "next_page", None)
        if not page:
            break

    return customers


def get_subscriptions_for_customer(
    api_key,
    customer_id,
    status_filter="all",
    max_results=None,
    expand=None,
):
    subscriptions = []
    starting_after = None
    expand = LIST_EXPAND_FULL if expand is None else expand

    while True:
        params = {
            "customer": customer_id,
            "limit": PAGE_SIZE,
            "expand": expand,
            "status": status_filter or "all",
            "api_key": api_key,
        }

        if starting_after:
            params["starting_after"] = starting_after

        response = stripe.Subscription.list(**params)
        subscriptions.extend(response.data)

        if max_results is not None and len(subscriptions) >= max_results:
            return subscriptions[:max_results]

        if not response.has_more:
            break

        starting_after = response.data[-1].id

    return subscriptions


def search_subscriptions_by_customer_email(
    api_key,
    search_text="",
    status_filter="all",
    required_email_text=REQUIRED_EMAIL_TEXT,
    max_results=None,
    subscription_expand=None,
):
    query = build_customer_email_search_query(search_text, required_email_text)
    if not query:
        return []

    subscriptions = []
    seen_subscription_ids = set()
    customer_page = None

    while True:
        customer_search_params = {
            "query": query,
            "limit": PAGE_SIZE,
            "api_key": api_key,
        }
        if customer_page:
            customer_search_params["page"] = customer_page

        customer_response = stripe.Customer.search(**customer_search_params)

        for customer in customer_response.data:
            customer_id = getattr(customer, "id", None)
            if not customer_id:
                continue

            remaining_capacity = max_results - len(subscriptions) if max_results is not None else None
            customer_subscriptions = get_subscriptions_for_customer(
                api_key,
                customer_id,
                status_filter=status_filter,
                max_results=remaining_capacity,
                expand=subscription_expand,
            )

            for subscription in customer_subscriptions:
                if subscription.id in seen_subscription_ids:
                    continue
                seen_subscription_ids.add(subscription.id)
                subscriptions.append(subscription)

                if max_results is not None and len(subscriptions) >= max_results:
                    return subscriptions

        customer_page = getattr(customer_response, "next_page", None)
        if not customer_page:
            break

    return subscriptions


def get_subscription_details(api_key, subscription_id):
    return stripe.Subscription.retrieve(
        subscription_id,
        api_key=api_key,
        expand=SUBSCRIPTION_EXPAND_FULL,
    )


def get_customer_email(subscription):
    customer = getattr(subscription, "customer", None)
    return getattr(customer, "email", None) if customer else None


def serialize_subscription(subscription):
    customer = getattr(subscription, "customer", None)
    return {
        "subscription_id": subscription.id,
        "status": subscription.status,
        "customer_id": getattr(customer, "id", None),
        "email": get_customer_email(subscription) or "(no email)",
        "current_period_end": getattr(subscription, "current_period_end", None),
        "price_usd_cents": get_latest_invoice_final_usd_cents(subscription),
        "notification_required": requires_high_value_notification(subscription),
    }


def filter_subscriptions_by_email(
    subscriptions,
    search_text,
    required_text=REQUIRED_EMAIL_TEXT,
):
    search_text = search_text.lower().strip()
    required_text = (required_text or "").lower().strip()
    if not search_text:
        return [
            subscription
            for subscription in subscriptions
            if required_text in (get_customer_email(subscription) or "").lower()
        ] if required_text else subscriptions

    filtered = []
    for subscription in subscriptions:
        email = (get_customer_email(subscription) or "").lower()
        if required_text and required_text not in email:
            continue
        if search_text in email:
            filtered.append(subscription)
    return filtered


def get_latest_invoice_final_usd_cents(subscription):
    latest_invoice = getattr(subscription, "latest_invoice", None)
    if not latest_invoice or isinstance(latest_invoice, str):
        return None

    currency = getattr(latest_invoice, "currency", None)
    total = getattr(latest_invoice, "total", None)

    if currency != "usd" or total is None:
        return None

    return total


def requires_high_value_notification(subscription):
    price_usd_cents = get_latest_invoice_final_usd_cents(subscription)
    return price_usd_cents is not None and price_usd_cents > OVER_FIVE_USD_CENTS


def send_high_value_subscription_notification(webhook_url, subscription):
    details = serialize_subscription(subscription)
    price_usd_cents = details["price_usd_cents"]
    price_text = (
        f"${price_usd_cents / 100:.2f}"
        if isinstance(price_usd_cents, int)
        else "n/a"
    )
    payload = {
        "text": (
            "High-value Stripe subscription requires manual review instead of cancellation.\n"
            f"Subscription: {details['subscription_id']}\n"
            f"Status: {details['status']}\n"
            f"Email: {details['email']}\n"
            f"Customer ID: {details['customer_id'] or 'n/a'}\n"
            f"Final invoice total: {price_text}"
        )
    }
    request_data = json.dumps(payload).encode("utf-8")
    slack_request = request.Request(
        webhook_url,
        data=request_data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(slack_request, timeout=15) as response:
        response.read()


def filter_subscriptions(
    subscriptions,
    search_text="",
    status_filter="all",
    required_email_text=REQUIRED_EMAIL_TEXT,
):
    filtered = filter_subscriptions_by_email(
        subscriptions,
        search_text,
        required_text=required_email_text,
    )

    if status_filter and status_filter != "all":
        filtered = [
            subscription
            for subscription in filtered
            if getattr(subscription, "status", None) == status_filter
        ]

    return filtered


def print_subscription_summary(subscriptions):
    print(f"\nTotal subscriptions retrieved: {len(subscriptions)}")
    status_counts = Counter(sub.status for sub in subscriptions)
    print(f"Status counts: {dict(status_counts)}")


def print_subscription_list(subscriptions):
    if not subscriptions:
        print("\nNo subscriptions found.")
        return

    print_subscription_summary(subscriptions)
    print("\nMatching subscriptions:")
    for index, subscription in enumerate(subscriptions, start=1):
        details = serialize_subscription(subscription)
        print(
            f"{index}. {details['subscription_id']} | "
            f"{details['status']} | "
            f"{details['email']}"
        )


def cancel_subscription(api_key, subscription_id):
    subscription = get_subscription_details(api_key, subscription_id)
    notification_details = {
        "attempted": False,
        "required": requires_high_value_notification(subscription),
        "sent": False,
        "reason": None,
        "price_usd_cents": get_latest_invoice_final_usd_cents(subscription),
    }
    if notification_details["required"]:
        notification_details["attempted"] = True
        webhook_url = os.environ.get(HIGH_VALUE_NOTIFICATION_WEBHOOK_ENV, "").strip()
        if not webhook_url:
            notification_details["reason"] = (
                f"Missing {HIGH_VALUE_NOTIFICATION_WEBHOOK_ENV} environment variable."
            )
        else:
            try:
                send_high_value_subscription_notification(webhook_url, subscription)
                notification_details["sent"] = True
            except (OSError, error.URLError) as notify_error:
                notification_details["reason"] = str(notify_error)

        verified = stripe.Subscription.retrieve(
            subscription_id,
            api_key=api_key,
            expand=["customer"],
        )
        response_details = {
            "subscription_id": verified.id,
            "status": verified.status,
            "canceled_at": getattr(verified, "canceled_at", None),
            "customer_id": getattr(verified, "customer", None),
            "action": "notified" if notification_details["sent"] else "notification_failed",
        }
        verification_details = {
            "subscription_id": verified.id,
            "status": verified.status,
            "email": get_customer_email(verified) or "(no email)",
            "canceled_at": getattr(verified, "canceled_at", None),
            "verified": notification_details["sent"],
            "mode": "notified",
        }
    else:
        response = stripe.Subscription.delete(subscription_id, api_key=api_key)
        verified = stripe.Subscription.retrieve(
            subscription_id,
            api_key=api_key,
            expand=["customer"],
        )
        response_details = {
            "subscription_id": response.id,
            "status": response.status,
            "canceled_at": getattr(response, "canceled_at", None),
            "customer_id": getattr(response, "customer", None),
            "action": "canceled",
        }
        verification_details = {
            "subscription_id": verified.id,
            "status": verified.status,
            "email": get_customer_email(verified) or "(no email)",
            "canceled_at": getattr(verified, "canceled_at", None),
            "verified": verified.status == "canceled",
            "mode": "canceled",
        }

    return {
        "response": response_details,
        "verification": verification_details,
        "notification": notification_details,
    }
