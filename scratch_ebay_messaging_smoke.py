"""Read-only smoke test against the eBay Trading API.

Calls `get_messages_for_item` with a synthetic item_id to verify:
  - EBAY_USER_TOKEN works for Trading API auth
  - Headers + XML envelope are accepted (returns Ack=Success, just empty results)
  - XML parsing doesn't crash on the real response shape

Does NOT send any message. The send path (`send_member_message`) should be
exercised the first time you do an actual negotiation, via the dashboard —
not from a scratch script, because every send goes to a real seller's inbox.
"""

import logging

from integrations import ebay_trading


logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")


def main() -> None:
    # Synthetic item_id — guaranteed not to match any real listing in your inbox.
    print("Calling GetMyMessages (filtering to synthetic item_id)...")
    try:
        msgs = ebay_trading.get_messages_for_item("0000000000")
    except ebay_trading.EbayTradingError as exc:
        print(f"\nFAILED: {exc}")
        return
    print(f"\nOK — got {len(msgs)} matching messages (expected 0).")
    print("Trading API auth + XML parsing works.")


if __name__ == "__main__":
    main()
