"""Display helpers used by templates and messages."""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def _indian_group(n: int) -> str:
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups) + "," + tail


def rupees(paise: int | None) -> str:
    """12345 -> "₹123.45"; 150000 -> "₹1,500"; 12345600 -> "₹1,23,456"."""
    if paise is None:
        return ""
    sign = "-" if paise < 0 else ""
    paise = abs(int(paise))
    whole, frac = divmod(paise, 100)
    text = _indian_group(whole)
    if frac:
        text += f".{frac:02d}"
    return f"{sign}₹{text}"


def rupees_plain(paise: int | None) -> str:
    """For form inputs: 12345 -> "123.45", 15000 -> "150"."""
    if paise is None:
        return ""
    whole, frac = divmod(int(paise), 100)
    return f"{whole}.{frac:02d}" if frac else str(whole)


def parse_rupees(value: str | None) -> int | None:
    """ "123.45" -> 12345. Empty -> None. Raises ValueError on bad or negative input."""
    if value is None or not str(value).strip():
        return None
    cleaned = str(value).strip().replace(",", "").replace("₹", "")
    try:
        amount = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError("Enter an amount like 120 or 120.50") from exc
    if amount < 0 or amount > Decimal("10000000"):
        raise ValueError("Enter an amount between 0 and 1,00,00,000")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def weight_label(grams: int | None) -> str:
    if not grams:
        return ""
    if grams >= 1000 and grams % 1000 == 0:
        return f"{grams // 1000} kg"
    if grams >= 1000:
        return f"{grams / 1000:g} kg"
    return f"{grams} g"


def percent_off(price: int, sale: int | None) -> int:
    if not sale or sale >= price or price <= 0:
        return 0
    return round((price - sale) * 100 / price)
