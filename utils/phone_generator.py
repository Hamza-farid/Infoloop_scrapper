"""
Phone number generator.
Builds a list of 10-digit US phone numbers based on:
  - A fixed 3-digit area code prefix
  - A range of middle 3 digits (exchange)
  - A range of last 4 digits (subscriber)
"""

from typing import Generator


def generate_phone_numbers(
    area_code: str,
    exchange_start: int,
    exchange_end: int,
    subscriber_start: int,
    subscriber_end: int,
) -> Generator[str, None, None]:
    """
    Yields phone number strings (digits only, no formatting) e.g. '9107852362'.

    Args:
        area_code:        Exactly 3 digits, e.g. '910'
        exchange_start:   Start of middle-3 range  (0–999)
        exchange_end:     End   of middle-3 range  (0–999), inclusive
        subscriber_start: Start of last-4 range    (0–9999)
        subscriber_end:   End   of last-4 range    (0–9999), inclusive
    """
    area_code = area_code.strip()
    if len(area_code) != 3 or not area_code.isdigit():
        raise ValueError(f"❌ Area code must be exactly 3 digits, got: '{area_code}'")

    exchange_start   = max(0, min(999,  exchange_start))
    exchange_end     = max(0, min(999,  exchange_end))
    subscriber_start = max(0, min(9999, subscriber_start))
    subscriber_end   = max(0, min(9999, subscriber_end))

    if exchange_start > exchange_end:
        raise ValueError("❌ Exchange start must be ≤ exchange end.")
    if subscriber_start > subscriber_end:
        raise ValueError("❌ Subscriber start must be ≤ subscriber end.")

    for ex in range(exchange_start, exchange_end + 1):
        for sub in range(subscriber_start, subscriber_end + 1):
            yield f"{area_code}{ex:03d}{sub:04d}"


def count_numbers(
    exchange_start: int,
    exchange_end: int,
    subscriber_start: int,
    subscriber_end: int,
) -> int:
    """Return total count without generating."""
    ex_count  = max(0, exchange_end  - exchange_start  + 1)
    sub_count = max(0, subscriber_end - subscriber_start + 1)
    return ex_count * sub_count
