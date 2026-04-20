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
    validate_api_key,
)


SEARCH_MODAL_CALLBACK = "stripe_search_modal"
RESULTS_MODAL_CALLBACK = "stripe_results_modal"
CONFIRM_MODAL_CALLBACK = "stripe_confirm_modal"
MAX_RESULTS = 100
SESSION_TTL_SECONDS = 900
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


def store_session(user_id, api_key, search_text, status_filter, subscriptions):
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
    }


def shorten(text, limit):
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def build_search_modal():
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
                        "The app keeps it only in memory until the flow finishes."
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


def build_loading_modal(title, message):
    return {
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
    }


def build_error_modal(message):
    return {
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
    }


def build_results_modal(session_id, search_text, status_filter, subscriptions):
    option_list = []
    for subscription in subscriptions:
        email = subscription["email"]
        label = shorten(
            f"{email} | {subscription['status']} | {subscription['subscription_id']}",
            75,
        )
        option_list.append(
            {
                "text": {"type": "plain_text", "text": label},
                "value": subscription["subscription_id"],
            }
        )

    filter_text = search_text or "(no filter)"
    status_text = status_filter or "all"
    return {
        "type": "modal",
        "callback_id": RESULTS_MODAL_CALLBACK,
        "private_metadata": session_id,
        "notify_on_close": True,
        "title": {"type": "plain_text", "text": "Pick Subscription"},
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
                        f"*Status filter:* `{status_text}`"
                    ),
                },
            },
            {
                "type": "input",
                "block_id": "subscription_block",
                "label": {"type": "plain_text", "text": "Subscription"},
                "element": {
                    "type": "static_select",
                    "action_id": "subscription_select",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Choose one subscription",
                    },
                    "options": option_list,
                },
            },
        ],
    }


def build_confirmation_modal(session_id, subscription):
    return {
        "type": "modal",
        "callback_id": CONFIRM_MODAL_CALLBACK,
        "private_metadata": f"{session_id}:{subscription['subscription_id']}",
        "notify_on_close": True,
        "title": {"type": "plain_text", "text": "Confirm Cancel"},
        "submit": {"type": "plain_text", "text": "Cancel it"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*You are about to cancel this subscription:*\n"
                        f"`{subscription['subscription_id']}`"
                    ),
                },
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Email*\n{subscription['email']}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Status*\n{subscription['status']}",
                    },
                ],
            },
        ],
    }


def build_status_modal(result):
    verification = result["verification"]
    response = result["response"]
    status_line = "verified" if verification["verified"] else "not verified"
    return {
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
                        f"*Cancellation completed:* `{response['subscription_id']}`\n"
                        f"*Verification:* {status_line}"
                    ),
                },
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Response status*\n{response['status']}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Verified status*\n{verification['status']}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Email*\n{verification['email']}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": (
                            "*Canceled at*\n"
                            f"{verification['canceled_at'] or 'not returned'}"
                        ),
                    },
                ],
            },
        ],
    }


def build_too_many_results_modal(count):
    return {
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
    }


@app.command("/stripe-subscriptions")
def open_stripe_modal(ack, body, client):
    ack()
    client.views_open(trigger_id=body["trigger_id"], view=build_search_modal())


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
    view_id = body["view"]["id"]
    user_id = body["user"]["id"]

    ack(
        response_action="update",
        view=build_loading_modal(
            "Stripe Search",
            "Searching subscriptions in Stripe. This can take a few seconds.",
        ),
    )

    try:
        validate_api_key(api_key)
        subscriptions = get_all_subscriptions(api_key)
        filtered = filter_subscriptions(subscriptions, search_text, status_filter)

        if not filtered:
            client.views_update(
                view_id=view_id,
                view=build_error_modal(
                    "No subscriptions matched that search. Run the command again."
                ),
            )
            return

        if len(filtered) > MAX_RESULTS:
            client.views_update(
                view_id=view_id,
                view=build_too_many_results_modal(len(filtered)),
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
            view_id=view_id,
            view=build_results_modal(
                session_id,
                search_text,
                session["status_filter"],
                session["subscriptions"],
            ),
        )
    except stripe.error.StripeError as error:
        message = getattr(error, "user_message", None) or str(error)
        client.views_update(
            view_id=view_id,
            view=build_error_modal(f"Stripe rejected the request: {message}"),
        )
    except Exception as error:
        logger.exception("Unhandled Stripe search error")
        client.views_update(
            view_id=view_id,
            view=build_error_modal(f"Unexpected error: {error}"),
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

    selected_id = (
        body["view"]["state"]["values"]["subscription_block"]["subscription_select"]
        ["selected_option"]["value"]
    )

    selected = None
    for subscription in session["subscriptions"]:
        if subscription["subscription_id"] == selected_id:
            selected = subscription
            break

    if not selected:
        ack(
            response_action="update",
            view=build_error_modal("That subscription is no longer available. Run the command again."),
        )
        delete_session(session_id)
        return

    ack(response_action="update", view=build_confirmation_modal(session_id, selected))


@app.view(CONFIRM_MODAL_CALLBACK)
def handle_confirmation_submission(ack, body, client, logger):
    session_id, subscription_id = body["view"]["private_metadata"].split(":", 1)
    user_id = body["user"]["id"]
    view_id = body["view"]["id"]
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
            f"Cancelling `{subscription_id}` and verifying the Stripe response.",
        ),
    )

    try:
        result = cancel_subscription(session["api_key"], subscription_id)
        delete_session(session_id)
        client.views_update(view_id=view_id, view=build_status_modal(result))
    except stripe.error.StripeError as error:
        message = getattr(error, "user_message", None) or str(error)
        delete_session(session_id)
        client.views_update(
            view_id=view_id,
            view=build_error_modal(f"Stripe could not cancel the subscription: {message}"),
        )
    except Exception as error:
        logger.exception("Unhandled Stripe cancellation error")
        delete_session(session_id)
        client.views_update(
            view_id=view_id,
            view=build_error_modal(f"Unexpected error: {error}"),
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
    session_id = metadata.split(":", 1)[0]
    delete_session(session_id)


def main():
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()


if __name__ == "__main__":
    main()
