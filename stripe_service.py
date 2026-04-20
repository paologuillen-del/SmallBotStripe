from collections import Counter

import stripe


PAGE_SIZE = 100
OVER_FIVE_USD_CENTS = 500


def validate_api_key(api_key):
    stripe.Subscription.list(limit=1, status="all", api_key=api_key)


def get_all_subscriptions(api_key):
    all_subscriptions = []
    starting_after = None

    while True:
        params = {
            "limit": PAGE_SIZE,
            "expand": [
                "data.customer",
                "data.items.data.price",
                "data.latest_invoice.payment_intent",
            ],
            "status": "all",
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
        "price_usd_cents": get_subscription_price_usd_cents(subscription),
        "refund_eligible": is_refund_eligible(subscription),
    }


def filter_subscriptions_by_email(subscriptions, search_text):
    search_text = search_text.lower().strip()
    if not search_text:
        return subscriptions

    filtered = []
    for subscription in subscriptions:
        email = (get_customer_email(subscription) or "").lower()
        if search_text in email:
            filtered.append(subscription)
    return filtered


def get_subscription_price_usd_cents(subscription):
    items = getattr(getattr(subscription, "items", None), "data", None) or []
    total_cents = 0

    for item in items:
        price = getattr(item, "price", None)
        if not price:
            return None

        currency = getattr(price, "currency", None)
        unit_amount = getattr(price, "unit_amount", None)
        quantity = getattr(item, "quantity", None) or 1

        if currency != "usd" or unit_amount is None:
            return None

        total_cents += unit_amount * quantity

    return total_cents


def is_refund_eligible(subscription):
    price_usd_cents = get_subscription_price_usd_cents(subscription)
    return price_usd_cents is not None and price_usd_cents > OVER_FIVE_USD_CENTS


def filter_subscriptions_by_price(subscriptions, price_filter="any"):
    if not price_filter or price_filter == "any":
        return subscriptions

    if price_filter == "over_5_usd":
        return [
            subscription
            for subscription in subscriptions
            if is_refund_eligible(subscription)
        ]

    return subscriptions


def filter_subscriptions(
    subscriptions,
    search_text="",
    status_filter="all",
    price_filter="any",
):
    filtered = filter_subscriptions_by_email(subscriptions, search_text)

    if status_filter and status_filter != "all":
        filtered = [
            subscription
            for subscription in filtered
            if getattr(subscription, "status", None) == status_filter
        ]

    return filter_subscriptions_by_price(filtered, price_filter)


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


def cancel_subscription(api_key, subscription_id, refund_on_cancel=False):
    subscription = stripe.Subscription.retrieve(
        subscription_id,
        api_key=api_key,
        expand=["customer", "items.data.price", "latest_invoice.payment_intent"],
    )
    response = stripe.Subscription.delete(subscription_id, api_key=api_key)
    verified = stripe.Subscription.retrieve(
        subscription_id,
        api_key=api_key,
        expand=["customer"],
    )
    refund_details = {
        "attempted": False,
        "eligible": is_refund_eligible(subscription),
        "refunded": False,
        "reason": None,
        "refund_id": None,
        "price_usd_cents": get_subscription_price_usd_cents(subscription),
    }

    if refund_on_cancel and refund_details["eligible"]:
        refund_details["attempted"] = True
        latest_invoice = getattr(subscription, "latest_invoice", None)
        payment_intent = getattr(latest_invoice, "payment_intent", None) if latest_invoice else None
        payment_intent_id = getattr(payment_intent, "id", payment_intent)

        if payment_intent_id:
            try:
                refund = stripe.Refund.create(
                    api_key=api_key,
                    payment_intent=payment_intent_id,
                    reason="requested_by_customer",
                )
                refund_details["refunded"] = True
                refund_details["refund_id"] = refund.id
            except stripe.error.StripeError as error:
                refund_details["reason"] = (
                    getattr(error, "user_message", None) or str(error)
                )
        else:
            refund_details["reason"] = (
                "No latest invoice payment intent available for refund."
            )

    response_details = {
        "subscription_id": response.id,
        "status": response.status,
        "canceled_at": getattr(response, "canceled_at", None),
        "customer_id": getattr(response, "customer", None),
    }

    verification_details = {
        "subscription_id": verified.id,
        "status": verified.status,
        "email": get_customer_email(verified) or "(no email)",
        "canceled_at": getattr(verified, "canceled_at", None),
        "verified": verified.status == "canceled",
    }

    return {
        "response": response_details,
        "verification": verification_details,
        "refund": refund_details,
    }
