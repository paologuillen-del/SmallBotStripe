# Stripe Subscription Slack App

This project now includes:

- `script.py`: terminal workflow
- `main.py`: Slack app workflow using a modal and Socket Mode
- `stripe_service.py`: shared Stripe operations

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run The Terminal Script

```bash
python3 script.py
```

## Run The Slack App

Set these environment variables:

- `SLACK_BOT_TOKEN`
- `SLACK_APP_TOKEN`
- `HIGH_VALUE_SLACK_WEBHOOK_URL`

Then run:

```bash
python3 main.py
```

## Slack App Setup

Create a Slack app and configure:

1. Enable Socket Mode.
2. Create an app-level token with `connections:write`.
3. Add a slash command named `/stripe-subscriptions`.
4. Install the app to your workspace.

Recommended bot scopes:

- `commands`

No `users:*` OAuth scopes are required.

## Slack Flow

1. Run `/stripe-subscriptions`.
2. Paste the Stripe restricted key into the modal.
3. Review or edit the prefilled email text filter.
4. Pick one matching subscription.
5. Confirm processing.
6. Review the Stripe result and verification status.

## Security Notes

- The Slack app keeps the pasted Stripe restricted key only in memory for the active workflow session.
- The app deletes the in-memory session after cancellation or when the modal is closed.
- Slack modal inputs are not masked like password fields, so use restricted keys only.
- If the latest subscription invoice total after discounts is more than `$5 USD`, the app sends a Slack notification to `HIGH_VALUE_SLACK_WEBHOOK_URL` instead of canceling that subscription.
