from collections import Counter

import stripe


PAGE_SIZE = 100


def validate_api_key(api_key):
    stripe.Subscription.list(limit=1, status="all", api_key=api_key)


def get_all_subscriptions(api_key):
    all_subscriptions = []
    starting_after = None

    while True:
        params = {
            "limit": PAGE_SIZE,
            "expand": ["data.customer"],
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
    }
