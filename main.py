import json
import os
import threading
import time
import uuid

import stripe
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from stripe_service import (
    cancel_subscription,
    filter_subscriptions,
    get_all_subscriptions,
    get_latest_invoice_final_usd_cents,
    is_refund_eligible,
    search_subscriptions_by_customer_email,
    validate_api_key,
)


SEARCH_MODAL_CALLBACK = "stripe_search_modal"
RESULTS_MODAL_CALLBACK = "stripe_results_modal"
CONFIRM_MODAL_CALLBACK = "stripe_confirm_modal"
SELECT_ALL_ACTION_ID = "select_all_matches"
SUBSCRIPTION_GROUP_PREFIX = "subscription_group_"
SUBSCRIPTION_GROUP_ACTION_ID = "subscription_group_select"
MAX_RESULTS = 100
SESSION_TTL_SECONDS = 900
REQUIRED_EMAIL_TEXT = "openloophealth"
STATUS_OPTIONS = [
    ("all", "All"),
    ("active", "Active"),
    ("canceled", "Canceled"),
    ("trialing", "Trialing"),
    ("past_due", "Past due"),
    ("unpaid", "Unpaid"),
    ("paused", "Paused"),
    ("incomplete", "Incomplete"),
    ("incomplete_expired", "Incomplete expired"),
]

SESSIONS = {}
SESSIONS_LOCK = threading.Lock()


app = App(
    token=os.environ["SLACK_BOT_TOKEN"],
    token_verification_enabled=False,
)


def cleanup_expired_sessions():
    now = time.time()
    expired_session_ids = []

    with SESSIONS_LOCK:
        for session_id, session in SESSIONS.items():
            if now - session["updated_at"] > SESSION_TTL_SECONDS:
                expired_session_ids.append(session_id)

        for session_id in expired_session_ids:
            del SESSIONS[session_id]


def store_session(
    user_id,
    api_key,
    search_text,
    status_filter,
    subscriptions,
):
    cleanup_expired_sessions()
    session_id = uuid.uuid4().hex
    summaries = [serialize_for_slack(subscription) for subscription in subscriptions]

    with SESSIONS_LOCK:
        SESSIONS[session_id] = {
            "user_id": user_id,
            "api_key": api_key,
            "search_text": search_text,
            "status_filter": status_filter,
            "subscriptions": summaries,
            "updated_at": time.time(),
        }

    return session_id


def get_session(session_id, user_id):
    cleanup_expired_sessions()

    with SESSIONS_LOCK:
        session = SESSIONS.get(session_id)
        if not session:
            return None
        if session["user_id"] != user_id:
            return None
        session["updated_at"] = time.time()
        return session


def delete_session(session_id):
    with SESSIONS_LOCK:
        if session_id in SESSIONS:
            del SESSIONS[session_id]


def serialize_for_slack(subscription):
    customer = getattr(subscription, "customer", None)
    email = getattr(customer, "email", None) if customer else None

    return {
        "subscription_id": subscription.id,
        "status": subscription.status,
        "customer_id": getattr(customer, "id", None),
        "email": email or "(no email)",
        "current_period_end": getattr(subscription, "current_period_end", None),
        "price_usd_cents": get_latest_invoice_final_usd_cents(subscription),
        "refund_eligible": is_refund_eligible(subscription),
    }


def shorten(text, limit):
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def add_external_id(view, external_id=None):
    if external_id:
        view["external_id"] = external_id
    return view


def build_search_modal(default_search_text=""):
    status_options = []
    for value, label in STATUS_OPTIONS:
        status_options.append(
            {
                "text": {"type": "plain_text", "text": label},
                "value": value,
            }
        )

    return {
        "type": "modal",
        "callback_id": SEARCH_MODAL_CALLBACK,
        "title": {"type": "plain_text", "text": "Stripe Search"},
        "submit": {"type": "plain_text", "text": "Search"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "Paste a Stripe restricted key for this one workflow. "
                        "The app keeps it only in memory until the flow finishes.\n"
                        f"Only subscriptions whose email contains `{REQUIRED_EMAIL_TEXT}` "
                        "will be shown."
                    ),
                },
            },
            {
                "type": "input",
                "block_id": "api_key_block",
                "label": {"type": "plain_text", "text": "Stripe restricted key"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "api_key_input",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "rk_live_... or rk_test_...",
                    },
                    "min_length": 10,
                },
            },
            {
                "type": "input",
                "block_id": "search_text_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "Email contains text"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "search_text_input",
                    "initial_value": default_search_text,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "openloophealth",
                    },
                },
            },
            {
                "type": "input",
                "block_id": "status_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "Subscription status"},
                "element": {
                    "type": "static_select",
                    "action_id": "status_select",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Choose a status",
                    },
                    "initial_option": {
                        "text": {"type": "plain_text", "text": "All"},
                        "value": "all",
                    },
                    "options": status_options,
                },
            },
        ],
    }


def build_loading_modal(title, message, external_id=None):
    return add_external_id({
        "type": "modal",
        "callback_id": "stripe_loading_modal",
        "title": {"type": "plain_text", "text": title},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": message},
            }
        ],
    }, external_id)


def build_error_modal(message, external_id=None):
    return add_external_id({
        "type": "modal",
        "callback_id": "stripe_error_modal",
        "title": {"type": "plain_text", "text": "Stripe Error"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": message},
            }
        ],
    }, external_id)


def build_results_modal(
    session_id,
    search_text,
    status_filter,
    subscriptions,
    external_id=None,
):
    filter_text = search_text or "(no filter)"
    status_text = status_filter or "all"
    subscription_blocks = []

    for group_index, start in enumerate(range(0, len(subscriptions), 10), start=1):
        group_subscriptions = subscriptions[start : start + 10]
        options = []

        for subscription in group_subscriptions:
            email = subscription["email"]
            price_usd_cents = subscription.get("price_usd_cents")
            price_text = (
                f"${price_usd_cents / 100:.2f}"
                if isinstance(price_usd_cents, int)
                else "n/a"
            )
            refund_text = "refund" if subscription.get("refund_eligible") else "no refund"
            label = shorten(
                f"{email} | {subscription['status']} | {subscription['subscription_id']}",
                75,
            )
            description = shorten(
                f"{price_text} | {refund_text}",
                75,
            )
            options.append(
                {
                    "text": {"type": "plain_text", "text": label},
                    "value": subscription["subscription_id"],
                    "description": {"type": "plain_text", "text": description},
                }
            )

        subscription_blocks.append(
            {
                "type": "input",
                "optional": True,
                "block_id": f"{SUBSCRIPTION_GROUP_PREFIX}{group_index}",
                "label": {
                    "type": "plain_text",
                    "text": f"Subscriptions {start + 1}-{start + len(group_subscriptions)}",
                },
                "element": {
                    "type": "checkboxes",
                    "action_id": SUBSCRIPTION_GROUP_ACTION_ID,
                    "options": options,
                },
            }
        )

    return add_external_id({
        "type": "modal",
        "callback_id": RESULTS_MODAL_CALLBACK,
        "private_metadata": session_id,
        "notify_on_close": True,
        "title": {"type": "plain_text", "text": "Pick Subscriptions"},
        "submit": {"type": "plain_text", "text": "Review"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Matches:* {len(subscriptions)}\n"
                        f"*Email filter:* `{filter_text}`\n"
                        f"*Required email text:* `{REQUIRED_EMAIL_TEXT}`\n"
                        f"*Status filter:* `{status_text}`\n"
                        "*Auto-refund on cancel:* `latest invoice total > $5 USD`"
                    ),
                },
            },
            {
                "type": "input",
                "optional": True,
                "block_id": "select_all_block",
                "label": {"type": "plain_text", "text": "Bulk action"},
                "element": {
                    "type": "checkboxes",
                    "action_id": SELECT_ALL_ACTION_ID,
                    "options": [
                        {
                            "text": {
                                "type": "plain_text",
                                "text": "Select all matches",
                            },
                            "value": "all",
                        }
                    ],
                },
            },
            *subscription_blocks,
        ],
    }, external_id)


def build_confirmation_modal(session_id, subscriptions, external_id=None):
    lines = []
    for subscription in subscriptions[:10]:
        price_usd_cents = subscription.get("price_usd_cents")
        price_text = (
            f"${price_usd_cents / 100:.2f}"
            if isinstance(price_usd_cents, int)
            else "n/a"
        )
        refund_text = "refund" if subscription.get("refund_eligible") else "no refund"
        lines.append(
            f"`{subscription['subscription_id']}` | {subscription['status']} | {price_text} | {refund_text} | {subscription['email']}"
        )

    if len(subscriptions) > 10:
        lines.append(f"...and {len(subscriptions) - 10} more")

    selected_ids = [subscription["subscription_id"] for subscription in subscriptions]
    return add_external_id({
        "type": "modal",
        "callback_id": CONFIRM_MODAL_CALLBACK,
        "private_metadata": json.dumps(
            {
                "session_id": session_id,
                "subscription_ids": selected_ids,
            }
        ),
        "notify_on_close": True,
        "title": {"type": "plain_text", "text": "Confirm Cancel"},
        "submit": {"type": "plain_text", "text": "Cancel selected"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*You are about to cancel {len(subscriptions)} subscription(s):*\n"
                        "*Automatic refund mode:* `latest invoice total > $5 USD`\n"
                        + "\n".join(lines)
                    ),
                },
            },
        ],
    }, external_id)


def get_default_search_text(client, user_id, fallback_username, logger):
    try:
        user = client.users_info(user=user_id)["user"]
    except Exception as error:
        logger.info("Using Slack username for search prefill: %s", error)
        return (fallback_username or "").strip()

    email = (
        user.get("profile", {}).get("email")
        if isinstance(user, dict)
        else getattr(getattr(user, "profile", None), "email", None)
    )
    if not email or "@" not in email:
        return ""

    return email.split("@", 1)[0]


def build_status_modal(results, external_id=None):
    if not isinstance(results, list):
        results = [results]

    success_count = sum(1 for result in results if result["verification"]["verified"])
    error_count = sum(1 for result in results if result.get("error"))
    summary_lines = []
    for result in results[:10]:
        if result.get("error"):
            summary_lines.append(
                f"`{result['subscription_id']}` | error | {result['error']}"
            )
            continue

        verification = result["verification"]
        response = result["response"]
        refund = result.get("refund", {})
        status_line = "verified" if verification["verified"] else "not verified"
        if refund.get("attempted"):
            refund_line = "refund created" if refund.get("refunded") else "refund failed"
        else:
            refund_line = "no refund"
        summary_lines.append(
            f"`{response['subscription_id']}` | {response['status']} | {status_line} | {refund_line}"
        )

    if len(results) > 10:
        summary_lines.append(f"...and {len(results) - 10} more")

    return add_external_id({
        "type": "modal",
        "callback_id": "stripe_status_modal",
        "title": {"type": "plain_text", "text": "Stripe Result"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Cancellation completed:* {success_count}/{len(results)} verified\n"
                        f"*Errors:* {error_count}\n"
                        + "\n".join(summary_lines)
                    ),
                },
            },
        ],
    }, external_id)


def build_too_many_results_modal(count, external_id=None):
    return add_external_id({
        "type": "modal",
        "callback_id": "stripe_too_many_results_modal",
        "title": {"type": "plain_text", "text": "Too Many Matches"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"The search returned *{count}* subscriptions. "
                        "Slack selection menus are limited, so narrow the email text "
                        "and run the command again."
                    ),
                },
            }
        ],
    }, external_id)


@app.command("/stripe-subscriptions")
def open_stripe_modal(ack, body, client, logger):
    ack()
    default_search_text = get_default_search_text(
        client,
        body["user_id"],
        body.get("user_name", ""),
        logger,
    )
    client.views_open(
        trigger_id=body["trigger_id"],
        view=build_search_modal(default_search_text),
    )


@app.view(SEARCH_MODAL_CALLBACK)
def handle_search_submission(ack, body, client, logger):
    values = body["view"]["state"]["values"]
    api_key = values["api_key_block"]["api_key_input"]["value"].strip()
    raw_search_text = values["search_text_block"]["search_text_input"].get("value")
    search_text = (raw_search_text or "").strip()
    status_filter = (
        values["status_block"]["status_select"]
        .get("selected_option", {})
        .get("value", "all")
    )
    user_id = body["user"]["id"]
    loading_external_id = f"stripe-search-{uuid.uuid4().hex}"

    ack(
        response_action="update",
        view=build_loading_modal(
            "Stripe Search",
            "Searching subscriptions in Stripe. This can take a few seconds.",
            external_id=loading_external_id,
        ),
    )

    try:
        validate_api_key(api_key)
        filtered = search_subscriptions_by_customer_email(
            api_key,
            search_text,
            status_filter,
            REQUIRED_EMAIL_TEXT,
            MAX_RESULTS + 1,
        )
        if not filtered:
            logger.info(
                "Fast customer-email search returned no matches; falling back to full subscription scan."
            )
            subscriptions = get_all_subscriptions(api_key)
            filtered = filter_subscriptions(
                subscriptions,
                search_text,
                status_filter,
                REQUIRED_EMAIL_TEXT,
            )

        if not filtered:
            client.views_update(
                external_id=loading_external_id,
                view=build_error_modal(
                    "No subscriptions matched that search. Run the command again.",
                    external_id=loading_external_id,
                ),
            )
            return

        if len(filtered) > MAX_RESULTS:
            client.views_update(
                external_id=loading_external_id,
                view=build_too_many_results_modal(
                    len(filtered),
                    external_id=loading_external_id,
                ),
            )
            return

        session_id = store_session(
            user_id,
            api_key,
            search_text,
            status_filter,
            filtered,
        )
        session = get_session(session_id, user_id)
        client.views_update(
            external_id=loading_external_id,
            view=build_results_modal(
                session_id,
                search_text,
                session["status_filter"],
                session["subscriptions"],
                external_id=loading_external_id,
            ),
        )
    except stripe.error.StripeError as error:
        message = getattr(error, "user_message", None) or str(error)
        client.views_update(
            external_id=loading_external_id,
            view=build_error_modal(
                f"Stripe rejected the request: {message}",
                external_id=loading_external_id,
            ),
        )
    except Exception as error:
        logger.exception("Unhandled Stripe search error")
        client.views_update(
            external_id=loading_external_id,
            view=build_error_modal(
                f"Unexpected error: {error}",
                external_id=loading_external_id,
            ),
        )


@app.view(RESULTS_MODAL_CALLBACK)
def handle_results_submission(ack, body):
    session_id = body["view"]["private_metadata"]
    user_id = body["user"]["id"]
    session = get_session(session_id, user_id)

    if not session:
        ack(
            response_action="update",
            view=build_error_modal("That session expired. Run the command again."),
        )
        return

    selected_all = (
        body["view"]["state"]["values"]["select_all_block"][SELECT_ALL_ACTION_ID]
        .get("selected_options", [])
    )
    if selected_all:
        ack(
            response_action="update",
            view=build_confirmation_modal(
                session_id,
                session["subscriptions"],
                external_id=f"stripe-confirm-{session_id}",
            ),
        )
        return

    selected_ids = []
    state_values = body["view"]["state"]["values"]
    for block_id, actions in state_values.items():
        if not block_id.startswith(SUBSCRIPTION_GROUP_PREFIX):
            continue
        selected_options = actions[SUBSCRIPTION_GROUP_ACTION_ID].get(
            "selected_options",
            [],
        )
        selected_ids.extend(option["value"] for option in selected_options)

    selected = []
    for subscription in session["subscriptions"]:
        if subscription["subscription_id"] in selected_ids:
            selected.append(subscription)

    if not selected or len(selected) != len(selected_ids):
        ack(
            response_action="update",
            view=build_error_modal(
                "One or more selected subscriptions are no longer available. Run the command again."
            ),
        )
        delete_session(session_id)
        return

    ack(
        response_action="update",
        view=build_confirmation_modal(
            session_id,
            selected,
            external_id=f"stripe-confirm-{session_id}",
        ),
    )


@app.view(CONFIRM_MODAL_CALLBACK)
def handle_confirmation_submission(ack, body, client, logger):
    metadata = json.loads(body["view"]["private_metadata"])
    session_id = metadata["session_id"]
    subscription_ids = metadata["subscription_ids"]
    user_id = body["user"]["id"]
    view_external_id = body["view"].get("external_id") or f"stripe-confirm-{session_id}"
    session = get_session(session_id, user_id)

    if not session:
        ack(
            response_action="update",
            view=build_error_modal("That session expired. Run the command again."),
        )
        return

    ack(
        response_action="update",
        view=build_loading_modal(
            "Cancelling",
            f"Cancelling {len(subscription_ids)} subscription(s) and verifying the Stripe response.",
            external_id=view_external_id,
        ),
    )

    try:
        results = []
        for subscription_id in subscription_ids:
            try:
                results.append(
                    cancel_subscription(
                        session["api_key"],
                        subscription_id,
                    )
                )
            except stripe.error.StripeError as error:
                results.append(
                    {
                        "subscription_id": subscription_id,
                        "error": getattr(error, "user_message", None) or str(error),
                        "verification": {"verified": False},
                    }
                )
        delete_session(session_id)
        client.views_update(
            external_id=view_external_id,
            view=build_status_modal(results, external_id=view_external_id),
        )
    except Exception as error:
        logger.exception("Unhandled Stripe cancellation error")
        delete_session(session_id)
        client.views_update(
            external_id=view_external_id,
            view=build_error_modal(
                f"Unexpected error: {error}",
                external_id=view_external_id,
            ),
        )


@app.view_closed(RESULTS_MODAL_CALLBACK)
def handle_results_closed(ack, body):
    ack()
    session_id = body["view"].get("private_metadata")
    if session_id:
        delete_session(session_id)


@app.view_closed(CONFIRM_MODAL_CALLBACK)
def handle_confirm_closed(ack, body):
    ack()
    metadata = body["view"].get("private_metadata", "")
    if not metadata:
        return
    session_id = json.loads(metadata)["session_id"]
    delete_session(session_id)


def main():
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()


if __name__ == "__main__":
    main()
