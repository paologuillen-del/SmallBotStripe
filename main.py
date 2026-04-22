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
    get_subscription_details,
    LIST_EXPAND_LIGHT,
    serialize_subscription,
    search_subscriptions_by_customer_email,
    validate_api_key,
)


SEARCH_MODAL_CALLBACK = "stripe_search_modal"
RESULTS_MODAL_CALLBACK = "stripe_results_modal"
CONFIRM_MODAL_CALLBACK = "stripe_confirm_modal"
SELECT_ALL_ACTION_ID = "select_all_matches"
SUBSCRIPTION_GROUP_PREFIX = "subscription_group_"
SUBSCRIPTION_GROUP_ACTION_ID = "subscription_group_select"
RESULTS_PREVIOUS_ACTION_ID = "results_previous_page"
RESULTS_NEXT_ACTION_ID = "results_next_page"
RESULTS_PAGE_SIZE_ACTION_ID = "results_page_size"
MAX_RESULTS = 500
DEFAULT_RESULTS_PAGE_SIZE = 10
RESULTS_PAGE_SIZE_OPTIONS = [10, 20, 50]
SESSION_TTL_SECONDS = 900
REQUIRED_EMAIL_TEXT = "openloophealth"
STATUS_OPTIONS = [
    ("all", "All"),
    ("active", "Active"),
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

    with SESSIONS_LOCK:
        SESSIONS[session_id] = {
            "user_id": user_id,
            "api_key": api_key,
            "search_text": search_text,
            "status_filter": status_filter,
            "subscriptions": subscriptions,
            "subscription_details_by_id": {},
            "selected_subscription_ids": set(),
            "results_page": 0,
            "results_page_size": DEFAULT_RESULTS_PAGE_SIZE,
            "select_all_enabled": False,
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


def filter_retrievable_subscription_summaries(subscriptions):
    return [
        subscription
        for subscription in subscriptions
        if subscription.get("status") != "canceled"
    ]


def load_subscription_summaries(api_key, search_text, status_filter, logger):
    try:
        subscriptions = search_subscriptions_by_customer_email(
            api_key,
            search_text,
            status_filter,
            REQUIRED_EMAIL_TEXT,
            MAX_RESULTS + 1,
            subscription_expand=LIST_EXPAND_LIGHT,
        )
        if subscriptions:
            return filter_retrievable_subscription_summaries(
                [serialize_for_slack(subscription) for subscription in subscriptions]
            )

        logger.info(
            "Fast customer-email search returned no matches; falling back to full subscription scan."
        )
    except stripe.error.StripeError as error:
        logger.warning(
            "Fast customer-email search failed; falling back to full subscription scan: %s",
            error,
        )

    try:
        subscriptions = get_all_subscriptions(
            api_key,
            status_filter=status_filter,
            expand=LIST_EXPAND_LIGHT,
        )
    except stripe.error.StripeError:
        if status_filter == "all":
            raise

        logger.warning(
            "Subscription list rejected status filter %s; retrying with status=all",
            status_filter,
        )
        subscriptions = get_all_subscriptions(
            api_key,
            status_filter="all",
            expand=LIST_EXPAND_LIGHT,
        )

    filtered = filter_subscriptions(
        subscriptions,
        search_text,
        status_filter,
        REQUIRED_EMAIL_TEXT,
    )
    return filter_retrievable_subscription_summaries(
        [serialize_for_slack(subscription) for subscription in filtered]
    )


def load_detailed_subscriptions(api_key, subscription_summaries):
    detailed_subscriptions = []

    for subscription in subscription_summaries:
        detailed_subscription = get_subscription_details(
            api_key,
            subscription["subscription_id"],
        )
        detailed_subscriptions.append(serialize_subscription(detailed_subscription))

    return detailed_subscriptions


def get_results_pagination(session):
    total_subscriptions = len(session["subscriptions"])
    page_size = session.get("results_page_size", DEFAULT_RESULTS_PAGE_SIZE)
    if page_size not in RESULTS_PAGE_SIZE_OPTIONS:
        page_size = DEFAULT_RESULTS_PAGE_SIZE
        session["results_page_size"] = page_size

    total_pages = max(1, (total_subscriptions + page_size - 1) // page_size)
    page = min(max(session.get("results_page", 0), 0), total_pages - 1)
    session["results_page"] = page

    start_index = page * page_size
    end_index = min(start_index + page_size, total_subscriptions)

    return {
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "start_index": start_index,
        "end_index": end_index,
    }


def get_current_page_subscription_summaries(session):
    pagination = get_results_pagination(session)
    return session["subscriptions"][
        pagination["start_index"] : pagination["end_index"]
    ]


def hydrate_results_page_subscriptions(session):
    current_page_subscriptions = get_current_page_subscription_summaries(session)
    details_by_id = session["subscription_details_by_id"]

    for subscription in current_page_subscriptions:
        subscription_id = subscription["subscription_id"]
        if subscription_id in details_by_id:
            continue

        detailed_subscription = get_subscription_details(
            session["api_key"],
            subscription_id,
        )
        details_by_id[subscription_id] = serialize_subscription(detailed_subscription)

    return [details_by_id[item["subscription_id"]] for item in current_page_subscriptions]


def sync_selected_ids_from_state(session, state_values):
    if session.get("select_all_enabled"):
        return

    current_page_ids = {
        subscription["subscription_id"]
        for subscription in get_current_page_subscription_summaries(session)
    }
    selected_ids = session["selected_subscription_ids"]
    selected_ids.difference_update(current_page_ids)

    for block_id, actions in state_values.items():
        if not block_id.startswith(SUBSCRIPTION_GROUP_PREFIX):
            continue
        selected_options = actions.get(SUBSCRIPTION_GROUP_ACTION_ID, {}).get(
            "selected_options",
            [],
        )
        selected_ids.update(option["value"] for option in selected_options)


def get_selected_subscriptions(session):
    if session.get("select_all_enabled"):
        return load_detailed_subscriptions(session["api_key"], session["subscriptions"])

    selected_ids = session["selected_subscription_ids"]
    selected_summaries = [
        subscription
        for subscription in session["subscriptions"]
        if subscription["subscription_id"] in selected_ids
    ]
    return load_detailed_subscriptions(session["api_key"], selected_summaries)


def is_select_all_selected(state_values):
    return bool(
        state_values.get("select_all_block", {})
        .get(SELECT_ALL_ACTION_ID, {})
        .get("selected_options", [])
    )


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
    current_page_subscriptions,
    page,
    page_size,
    total_pages,
    selected_subscription_ids,
    select_all_enabled=False,
    external_id=None,
):
    filter_text = search_text or "(no filter)"
    status_text = status_filter or "all"
    subscription_blocks = []
    page_start = page * page_size + 1 if subscriptions else 0
    page_end = min((page + 1) * page_size, len(subscriptions))

    if not select_all_enabled:
        for group_index, start in enumerate(
            range(0, len(current_page_subscriptions), 10),
            start=1,
        ):
            group_subscriptions = current_page_subscriptions[start : start + 10]
            options = []

            for subscription in group_subscriptions:
                email = subscription["email"]
                price_usd_cents = subscription.get("price_usd_cents")
                price_text = (
                    f"${price_usd_cents / 100:.2f}"
                    if isinstance(price_usd_cents, int)
                    else "n/a"
                )
                action_text = (
                    "notify Slack"
                    if subscription.get("notification_required")
                    else "can cancel"
                )
                label = shorten(
                    f"{email} | {subscription['status']} | {subscription['subscription_id']}",
                    75,
                )
                options.append(
                    {
                        "text": {"type": "plain_text", "text": label},
                        "value": subscription["subscription_id"],
                        "description": {
                            "type": "plain_text",
                            "text": shorten(f"{price_text} | {action_text}", 75),
                        },
                    }
                )

            initial_options = [
                option
                for option in options
                if option["value"] in selected_subscription_ids
            ]

            subscription_blocks.append(
                {
                    "type": "input",
                    "optional": True,
                    "dispatch_action": True,
                    "block_id": f"{SUBSCRIPTION_GROUP_PREFIX}{group_index}",
                    "label": {
                        "type": "plain_text",
                        "text": (
                            f"Subscriptions {page_start + start}-"
                            f"{page_start + start + len(group_subscriptions) - 1}"
                        ),
                    },
                    "element": {
                        "type": "checkboxes",
                        "action_id": SUBSCRIPTION_GROUP_ACTION_ID,
                        "options": options,
                        "initial_options": initial_options,
                    },
                }
            )

    hidden_notice_block = []
    if select_all_enabled:
        hidden_notice_block.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "Individual subscription checkboxes are hidden while *Select all matches* is enabled.",
                    }
                ],
            }
        )

    page_size_options = [
        {
            "text": {"type": "plain_text", "text": str(option)},
            "value": str(option),
        }
        for option in RESULTS_PAGE_SIZE_OPTIONS
    ]
    pagination_actions = []
    if page > 0:
        pagination_actions.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Previous"},
                "action_id": RESULTS_PREVIOUS_ACTION_ID,
                "value": "previous",
            }
        )
    if page < total_pages - 1:
        pagination_actions.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Next"},
                "action_id": RESULTS_NEXT_ACTION_ID,
                "value": "next",
            }
        )

    pagination_blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Page:* {page + 1}/{total_pages}\n"
                    f"*Showing:* {page_start}-{page_end} of {len(subscriptions)}\n"
                    f"*Selected:* {len(selected_subscription_ids)}"
                    if not select_all_enabled
                    else (
                        f"*Page:* {page + 1}/{total_pages}\n"
                        f"*Showing:* {page_start}-{page_end} of {len(subscriptions)}\n"
                        "*Selected:* all matches"
                    )
                ),
            },
            "accessory": {
                "type": "static_select",
                "action_id": RESULTS_PAGE_SIZE_ACTION_ID,
                "placeholder": {"type": "plain_text", "text": "Page size"},
                "initial_option": {
                    "text": {"type": "plain_text", "text": str(page_size)},
                    "value": str(page_size),
                },
                "options": page_size_options,
            },
        }
    ]
    if pagination_actions:
        pagination_blocks.append(
            {
                "type": "actions",
                "elements": pagination_actions,
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
                        "*Over $5 final invoice total:* sent to Slack instead of canceled"
                    ),
                },
            },
            {
                "type": "input",
                "optional": True,
                "dispatch_action": True,
                "block_id": "select_all_block",
                "label": {"type": "plain_text", "text": "Bulk action"},
                "element": {
                    "type": "checkboxes",
                    "action_id": SELECT_ALL_ACTION_ID,
                    "initial_options": [
                        {
                            "text": {
                                "type": "plain_text",
                                "text": "Select all matches",
                            },
                            "value": "all",
                        }
                    ] if select_all_enabled else [],
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
            *pagination_blocks,
            *hidden_notice_block,
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
        action_text = (
            "notify Slack"
            if subscription.get("notification_required")
            else "cancel"
        )
        lines.append(
            f"`{subscription['subscription_id']}` | {subscription['status']} | {price_text} | {action_text} | {subscription['email']}"
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
        "title": {"type": "plain_text", "text": "Confirm Processing"},
        "submit": {"type": "plain_text", "text": "Process selected"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*You are about to process {len(subscriptions)} subscription(s):*\n"
                        + "\n".join(lines)
                    ),
                },
            },
        ],
    }, external_id)


def get_default_search_text():
    return "openloophealth"


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
        notification = result.get("notification", {})
        mode = verification.get("mode", "canceled")
        status_line = "completed" if verification["verified"] else "failed"
        if mode == "notified":
            if notification.get("sent"):
                action_line = "sent to Slack"
            else:
                action_line = "Slack notification failed"
        else:
            action_line = "canceled"
        summary_lines.append(
            f"`{response['subscription_id']}` | {response['status']} | {status_line} | {action_line}"
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
                        f"*Completed:* {success_count}/{len(results)}\n"
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
def open_stripe_modal(ack, body, client):
    ack()
    default_search_text = get_default_search_text()
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
        filtered = load_subscription_summaries(
            api_key,
            search_text,
            status_filter,
            logger,
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
        current_page_subscriptions = hydrate_results_page_subscriptions(session)
        pagination = get_results_pagination(session)
        client.views_update(
            external_id=loading_external_id,
            view=build_results_modal(
                session_id,
                search_text,
                session["status_filter"],
                session["subscriptions"],
                current_page_subscriptions,
                pagination["page"],
                pagination["page_size"],
                pagination["total_pages"],
                session["selected_subscription_ids"],
                select_all_enabled=False,
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


@app.action(SELECT_ALL_ACTION_ID)
def handle_select_all_toggle(ack, body, client):
    ack()

    session_id = body["view"]["private_metadata"]
    user_id = body["user"]["id"]
    session = get_session(session_id, user_id)
    if not session:
        return

    selected_options = body["actions"][0].get("selected_options", [])
    select_all_enabled = bool(selected_options)
    view = body["view"]
    if select_all_enabled:
        sync_selected_ids_from_state(session, view["state"]["values"])
    session["select_all_enabled"] = select_all_enabled
    current_page_subscriptions = hydrate_results_page_subscriptions(session)
    pagination = get_results_pagination(session)

    client.views_update(
        view_id=view["id"],
        hash=view["hash"],
        view=build_results_modal(
            session_id,
            session["search_text"],
            session["status_filter"],
            session["subscriptions"],
            current_page_subscriptions,
            pagination["page"],
            pagination["page_size"],
            pagination["total_pages"],
            session["selected_subscription_ids"],
            select_all_enabled=select_all_enabled,
            external_id=view.get("external_id"),
        ),
    )


@app.action(SUBSCRIPTION_GROUP_ACTION_ID)
def handle_subscription_selection_change(ack, body):
    ack()

    session_id = body["view"]["private_metadata"]
    user_id = body["user"]["id"]
    session = get_session(session_id, user_id)
    if not session:
        return

    sync_selected_ids_from_state(session, body["view"]["state"]["values"])


def update_results_page_view(ack, body, client, page_delta=0, page_size=None):
    ack()

    session_id = body["view"]["private_metadata"]
    user_id = body["user"]["id"]
    session = get_session(session_id, user_id)
    if not session:
        return

    view = body["view"]
    sync_selected_ids_from_state(session, view["state"]["values"])

    if page_size is not None:
        old_page_size = session["results_page_size"]
        first_item_index = session["results_page"] * old_page_size
        session["results_page_size"] = page_size
        session["results_page"] = first_item_index // page_size
    else:
        session["results_page"] += page_delta

    pagination = get_results_pagination(session)
    current_page_subscriptions = hydrate_results_page_subscriptions(session)

    client.views_update(
        view_id=view["id"],
        hash=view["hash"],
        view=build_results_modal(
            session_id,
            session["search_text"],
            session["status_filter"],
            session["subscriptions"],
            current_page_subscriptions,
            pagination["page"],
            pagination["page_size"],
            pagination["total_pages"],
            session["selected_subscription_ids"],
            select_all_enabled=session["select_all_enabled"],
            external_id=view.get("external_id"),
        ),
    )


@app.action(RESULTS_PREVIOUS_ACTION_ID)
def handle_previous_results_page(ack, body, client):
    update_results_page_view(ack, body, client, page_delta=-1)


@app.action(RESULTS_NEXT_ACTION_ID)
def handle_next_results_page(ack, body, client):
    update_results_page_view(ack, body, client, page_delta=1)


@app.action(RESULTS_PAGE_SIZE_ACTION_ID)
def handle_results_page_size_change(ack, body, client):
    selected_option = body["actions"][0].get("selected_option", {})
    page_size = int(selected_option.get("value", DEFAULT_RESULTS_PAGE_SIZE))
    update_results_page_view(ack, body, client, page_size=page_size)


def prepare_results_confirmation(
    client,
    session_id,
    user_id,
    state_values,
    results_external_id,
    logger,
):
    session = get_session(session_id, user_id)

    if not session:
        client.views_update(
            external_id=results_external_id,
            view=build_error_modal(
                "That session expired. Run the command again.",
                external_id=results_external_id,
            ),
        )
        return

    try:
        if is_select_all_selected(state_values):
            session["select_all_enabled"] = True
            selected = get_selected_subscriptions(session)
        else:
            session["select_all_enabled"] = False
            sync_selected_ids_from_state(session, state_values)
            selected = get_selected_subscriptions(session)

            if not selected or len(selected) != len(session["selected_subscription_ids"]):
                client.views_update(
                    external_id=results_external_id,
                    view=build_error_modal(
                        "One or more selected subscriptions are no longer available. Run the command again.",
                        external_id=results_external_id,
                    ),
                )
                delete_session(session_id)
                return

        client.views_update(
            external_id=results_external_id,
            view=build_confirmation_modal(
                session_id,
                selected,
                external_id=f"stripe-confirm-{session_id}",
            ),
        )
    except stripe.error.StripeError as error:
        message = getattr(error, "user_message", None) or str(error)
        client.views_update(
            external_id=results_external_id,
            view=build_error_modal(
                f"Stripe rejected the request: {message}",
                external_id=results_external_id,
            ),
        )
    except Exception as error:
        logger.exception("Unhandled Stripe results submission error")
        client.views_update(
            external_id=results_external_id,
            view=build_error_modal(
                f"Unexpected error: {error}",
                external_id=results_external_id,
            ),
        )


@app.view(RESULTS_MODAL_CALLBACK)
def handle_results_submission(ack, body, client, logger):
    session_id = body["view"]["private_metadata"]
    user_id = body["user"]["id"]
    results_external_id = body["view"].get("external_id") or f"stripe-results-{session_id}"
    state_values = body["view"]["state"]["values"]

    ack(
        response_action="update",
        view=build_loading_modal(
            "Preparing Review",
            "Loading the selected subscriptions. This can take a few seconds.",
            external_id=results_external_id,
        ),
    )

    threading.Thread(
        target=prepare_results_confirmation,
        kwargs={
            "client": client,
            "session_id": session_id,
            "user_id": user_id,
            "state_values": state_values,
            "results_external_id": results_external_id,
            "logger": logger,
        },
        daemon=True,
    ).start()


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
            "Processing",
            f"Processing {len(subscription_ids)} subscription(s) and verifying the result.",
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
