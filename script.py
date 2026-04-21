import stripe

from stripe_service import (
    cancel_subscription,
    filter_subscriptions_by_email,
    get_all_subscriptions,
    print_subscription_list,
    validate_api_key,
)


def prompt_api_key():
    while True:
        api_key = input("Enter your Stripe API key: ").strip()
        if not api_key:
            print("API key is required.")
            continue

        try:
            validate_api_key(api_key)
            print("API key accepted.")
            return api_key
        except stripe.error.StripeError as error:
            message = getattr(error, "user_message", None) or str(error)
            print(f"Stripe rejected that API key: {message}")


def clear_api_key(api_key=None):
    api_key = None
    print("\nSession cleanup complete: API key cleared from the running script.")


def choose_subscription(subscriptions):
    while True:
        selection = input(
            "\nEnter the number of the subscription to cancel "
            "(or press Enter to go back): "
        ).strip()

        if not selection:
            return None

        if not selection.isdigit():
            print("Please enter a valid number.")
            continue

        index = int(selection)
        if 1 <= index <= len(subscriptions):
            return subscriptions[index - 1]

        print("Selection out of range.")


def confirm_cancellation(subscription):
    print("\nSelected subscription:")
    print(
        {
            "subscription_id": subscription.id,
            "status": subscription.status,
            "email": getattr(getattr(subscription, "customer", None), "email", None),
        }
    )

    confirmation = input(
        "Type CANCEL to confirm cancellation, or press Enter to abort: "
    ).strip()
    return confirmation == "CANCEL"


def handle_results(api_key, subscriptions):
    print_subscription_list(subscriptions)
    if not subscriptions:
        return

    selected = choose_subscription(subscriptions)
    if not selected:
        return

    if not confirm_cancellation(selected):
        print("Cancellation aborted.")
        return

    result = cancel_subscription(api_key, selected.id)
    print("\nStripe processing response:")
    print(result["response"])
    print("\nVerification result:")
    print(result["verification"])

    if not result["verification"]["verified"]:
        print("Warning: the requested action did not complete successfully.")


def main():
    session_api_key = prompt_api_key()

    while True:
        try:
            print(
                "\nChoose an option:\n"
                "1. See all subscriptions\n"
                "2. See subscriptions whose email contains text\n"
                "3. Exit"
            )
            choice = input("Option: ").strip()

            if choice == "1":
                subscriptions = get_all_subscriptions(session_api_key)
                handle_results(session_api_key, subscriptions)
            elif choice == "2":
                search_text = input("Email contains: ").strip()
                subscriptions = get_all_subscriptions(session_api_key)
                filtered = filter_subscriptions_by_email(subscriptions, search_text)
                handle_results(session_api_key, filtered)
            elif choice == "3":
                print("Exiting.")
                break
            else:
                print("Invalid option.")
        except stripe.error.StripeError as error:
            message = getattr(error, "user_message", None) or str(error)
            print(f"\nStripe error: {message}")
        except KeyboardInterrupt:
            print("\nOperation cancelled by user.")
            break

    clear_api_key(session_api_key)


if __name__ == "__main__":
    main()
