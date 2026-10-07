import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

SEED = 42
OUT = Path(__file__).resolve().parent
rng = np.random.default_rng(SEED)

N_USERS, N_PRODUCTS, N_ORDERS = 40, 20, 100


# ----------------------------------------------------------------------
# Helpers that inject traps
# ----------------------------------------------------------------------
DATE_FORMATS = [
    "%Y-%m-%d",           # 2025-04-07
    "%d/%m/%Y",           # 07/04/2025  (day first)
    "%m/%d/%Y",           # 04/07/2025  (month first) -> ambiguous with above
    "%b %d, %Y",          # Apr 07, 2025
    "%d-%b-%Y",           # 07-Apr-2025
    "%Y-%m-%d %H:%M:%S",  # 2025-04-07 00:00:00
]


def random_dates(n, start="2025-01-01", days=365):
    """True dates. ~half forced to day <= 12 so slash formats become ambiguous."""
    base = pd.Timestamp(start) + pd.to_timedelta(rng.integers(0, days, n), unit="D")
    out = []
    for i, d in enumerate(base):
        if i % 2 == 0 and d.day > 12:
            d = d.replace(day=int(rng.integers(1, 13)))
        out.append(d)
    return pd.Series(out)


def messy_dates(true_dates, blank_count):
    """Render dates in rotating formats, then blank some out."""
    values = [d.strftime(DATE_FORMATS[i % len(DATE_FORMATS)])
              for i, d in enumerate(true_dates)]
    blanks = rng.choice(len(values), blank_count, replace=False)
    for b in blanks:
        values[b] = ""
    return values, sorted(int(b) for b in blanks)


CURRENCY_SYMBOLS = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "INR": "₹",
    "JPY": "¥",
    "CAD": "CA$",
    "AUD": "A$",
}


def messy_price(amount, currency, style):
    """Same value, many textual representations — not limited to USD/EUR."""
    sym = CURRENCY_SYMBOLS.get(currency)
    if style == 0:
        return f"{amount:.2f} {currency}"                       # 49.99 GBP
    if style == 1 and sym:
        return f"{sym}{amount:.2f}"                             # £49.99 / CA$49.99
    if style == 2 and currency == "EUR":
        return f"{amount:.2f}".replace(".", ",") + " EUR"       # 49,99 EUR
    return f"{amount:.2f} {currency}"


def is_ambiguous_date(s):
    m = re.fullmatch(r"(\d{2})/(\d{2})/\d{4}", s or "")
    return bool(m) and int(m.group(1)) <= 12 and int(m.group(2)) <= 12 and m.group(1) != m.group(2)


# ----------------------------------------------------------------------
# 1. users.csv
# ----------------------------------------------------------------------
user_ids = np.arange(1001, 1001 + N_USERS)
signup_true = random_dates(N_USERS, "2024-01-01", 365)
signup_str, signup_blanks = messy_dates(signup_true, blank_count=5)

country_variants = {
    "United States": ["United States", "USA", "US", "u.s.a."],
    "Germany": ["Germany", "DE", "germany"],
    "France": ["France", "FR"],
    "India": ["India", "IN", "india "],
}
true_country = rng.choice(list(country_variants), N_USERS)
users = pd.DataFrame({
    "user_id": user_ids,
    "name": [f"Customer {i:02d}" for i in range(1, N_USERS + 1)],
    "email": [f"customer{i:02d}@example.com" for i in range(1, N_USERS + 1)],
    "signup_date": signup_str,
    "country": [rng.choice(country_variants[c]) for c in true_country],
})
missing_email_idx = rng.choice(N_USERS, 4, replace=False)
users.loc[missing_email_idx, "email"] = ""

# Conflicting duplicate: same user_id, different country -> which is true?
conflict_user = users.iloc[[3]].copy()
conflict_user["country"] = "Canada"


# ----------------------------------------------------------------------
# 2. inventory.csv
# ----------------------------------------------------------------------
product_ids = np.arange(2001, 2001 + N_PRODUCTS)
inv_amount = np.round(rng.uniform(5, 250, N_PRODUCTS), 2)
# Keep EUR on odd ids (RNG-stable). Replace some even-id USD slots with other units.
_other_units = {0: "GBP", 4: "INR", 8: "JPY", 12: "CAD", 16: "AUD"}
inv_currency = np.array([
    "EUR" if i % 2 == 1 else _other_units.get(i, "USD")
    for i in range(N_PRODUCTS)
])
restock_true = random_dates(N_PRODUCTS)
restock_str, restock_blanks = messy_dates(restock_true, blank_count=3)

inv_price = [messy_price(a, c, i % 3) for i, (a, c) in enumerate(zip(inv_amount, inv_currency))]
# Missing currency: the number is there, the unit is not
no_currency_products = [5, 10]  # EUR + USD; keep GBP/INR/JPY/CAD/AUD labeled
for i in no_currency_products:
    inv_price[i] = f"{inv_amount[i]:.2f}"

stock = rng.integers(0, 100, N_PRODUCTS).astype(object)
stock[[2, 9]] = 0
missing_stock_idx = [7, 15]
for i in missing_stock_idx:
    stock[i] = ""

inventory = pd.DataFrame({
    "product_id": product_ids,
    "product_name": [f"Product {i:02d}" for i in range(1, N_PRODUCTS + 1)],
    "category": rng.choice(["Electronics", "Books", "Clothing", "Home"], N_PRODUCTS),
    "price": inv_price,
    "stock_quantity": stock,
    "last_restock_date": restock_str,
})


# ----------------------------------------------------------------------
# 3. orders.csv
# ----------------------------------------------------------------------
order_ids = np.arange(3001, 3001 + N_ORDERS)
o_user = rng.choice(user_ids, N_ORDERS)
o_prod = rng.choice(product_ids, N_ORDERS)

# Orphans: IDs that do not exist in users.csv / inventory.csv
orphan_user_idx = rng.choice(N_ORDERS, 4, replace=False)
o_user[orphan_user_idx] = [9991, 9992, 9993, 9994]
remaining = np.setdiff1d(np.arange(N_ORDERS), orphan_user_idx)
orphan_prod_idx = rng.choice(remaining, 3, replace=False)
o_prod[orphan_prod_idx] = [2999, 2998, 2997]

qty = rng.integers(1, 6, N_ORDERS)
prod_lookup = {p: i for i, p in enumerate(product_ids)}

o_amount, o_currency = [], []
for p in o_prod:
    if p in prod_lookup:
        k = prod_lookup[p]
        o_amount.append(inv_amount[k]); o_currency.append(inv_currency[k])
    else:
        o_amount.append(round(float(rng.uniform(5, 250)), 2))
        o_currency.append(rng.choice(["USD", "EUR"]))
o_amount = np.array(o_amount)

# Contradiction: order unit price disagrees with inventory price
valid = [i for i in range(N_ORDERS) if o_prod[i] in prod_lookup]
price_conflict_idx = sorted(int(i) for i in rng.choice(valid, 6, replace=False))
o_amount[price_conflict_idx] = np.round(o_amount[price_conflict_idx] * 1.25, 2)

o_price = [messy_price(a, c, i % 3) for i, (a, c) in enumerate(zip(o_amount, o_currency))]
usd_eur_idx = [i for i in range(N_ORDERS) if o_currency[i] in ("USD", "EUR")]
order_no_currency_idx = sorted(int(i) for i in rng.choice(usd_eur_idx, 3, replace=False))
for i in order_no_currency_idx:
    o_price[i] = f"{o_amount[i]:.2f}"

# Make sure GBP/INR/JPY/CAD/AUD actually appear in orders, not only inventory.
# Iterate in a stable order and patch a distinct USD slot for each missing currency
# so we never accidentally overwrite a slot we already patched.
_patched_slots: set[int] = set()
for code in ("GBP", "INR", "JPY", "CAD", "AUD"):
    if code in o_currency:
        continue
    for i, c in enumerate(o_currency):
        if c == "USD" and i not in order_no_currency_idx and i not in _patched_slots:
            o_currency[i] = code
            o_price[i] = messy_price(o_amount[i], code, i % 3)
            _patched_slots.add(i)
            break

order_true = random_dates(N_ORDERS)
order_str, order_blanks = messy_dates(order_true, blank_count=10)

orders = pd.DataFrame({
    "order_id": order_ids,
    "user_id": o_user,
    "product_id": o_prod,
    "quantity": qty,
    "price": o_price,
    "order_date": order_str,
    "status": rng.choice(["Completed", "Pending", "Cancelled"], N_ORDERS),
})

# Conflicting duplicates: same order_id, different status
conflict_orders = orders.sample(3, random_state=SEED).copy()
conflict_orders["status"] = conflict_orders["status"].map(
    {"Completed": "Cancelled", "Pending": "Completed", "Cancelled": "Pending"})


# ----------------------------------------------------------------------
# Ground truth (computed BEFORE adding duplicate rows) for test answers
# ----------------------------------------------------------------------
truth_orders = orders.assign(amount=o_amount, currency=o_currency,
                             true_date=order_true.dt.strftime("%Y-%m-%d"))
known_currency = ~truth_orders.index.isin(order_no_currency_idx)
eur_completed = truth_orders[known_currency & (truth_orders.currency == "EUR")
                             & (truth_orders.status == "Completed")
                             & ~truth_orders.index.isin(conflict_orders.index)]


# ----------------------------------------------------------------------
# Add duplicates and shuffle
# ----------------------------------------------------------------------
def add_dupes(frame, n_exact, extra=None):
    dupes = frame.sample(n_exact, random_state=SEED + 1)
    parts = [frame, dupes] + ([extra] if extra is not None else [])
    return pd.concat(parts, ignore_index=True).sample(frac=1, random_state=SEED).reset_index(drop=True)


users_out = add_dupes(users, 4, conflict_user)
inventory_out = add_dupes(inventory, 3)
orders_out = add_dupes(orders, 10, conflict_orders)


# ----------------------------------------------------------------------
# The "document" that contradicts the tables
# ----------------------------------------------------------------------
NOTES = """# Data Notes (provided by the sales team)

- All prices in our system are stored in USD.
- Dates use the US format (MM/DD/YYYY).
- Every order belongs to a registered user.
- Inventory is restocked weekly, so no product is ever out of stock.
"""
# Every bullet above is false according to the CSVs. That is the trap.


# ----------------------------------------------------------------------
# Manifest + test questions
# ----------------------------------------------------------------------
def count_ambiguous(series):
    return int(series.map(is_ambiguous_date).sum())


manifest = {
    "orders.csv": {
        "rows": len(orders_out),
        "unique_order_ids": int(orders_out.order_id.nunique()),
        "exact_duplicate_rows": int(orders_out.duplicated().sum()),
        "conflicting_duplicate_order_ids": sorted(conflict_orders.order_id.astype(int).tolist()),
        "blank_order_dates": int(orders_out.order_date.eq("").sum()),
        "ambiguous_slash_dates": count_ambiguous(orders_out.order_date),
        "orphan_user_ids": [9991, 9992, 9993, 9994],
        "orphan_product_ids": [2997, 2998, 2999],
        "price_conflicts_vs_inventory_order_ids": [int(order_ids[i]) for i in price_conflict_idx],
        "prices_missing_currency_order_ids": [int(order_ids[i]) for i in order_no_currency_idx],
        "price_formats": [
            "49.99 USD", "$49.99", "€49.99", "49,99 EUR", "£12.50", "CA$10.00",
            "A$8.20", "₹999.00", "¥1500.00", "12.00 GBP", "49.99 (no unit)",
        ],
        "price_units_present": sorted({c for c in o_currency if c}),
    },
    "users.csv": {
        "rows": len(users_out),
        "unique_user_ids": int(users_out.user_id.nunique()),
        "exact_duplicate_rows": int(users_out.duplicated().sum()),
        "conflicting_duplicate_user_ids": conflict_user.user_id.astype(int).tolist(),
        "blank_signup_dates": int(users_out.signup_date.eq("").sum()),
        "ambiguous_slash_dates": count_ambiguous(users_out.signup_date),
        "blank_emails": int(users_out.email.eq("").sum()),
        "country_spelling_variants": country_variants,
    },
    "inventory.csv": {
        "rows": len(inventory_out),
        "unique_product_ids": int(inventory_out.product_id.nunique()),
        "exact_duplicate_rows": int(inventory_out.duplicated().sum()),
        "blank_restock_dates": int(inventory_out.last_restock_date.eq("").sum()),
        "ambiguous_slash_dates": count_ambiguous(inventory_out.last_restock_date),
        "products_missing_currency": [int(product_ids[i]) for i in no_currency_products],
        "products_missing_stock": [int(product_ids[i]) for i in missing_stock_idx],
        "products_zero_stock": [int(product_ids[i]) for i in [2, 9]],
    },
    "data_notes.md": "Every statement contradicts the CSVs (currency, date format, orphan users, zero stock).",
}

REFUSE = "I cannot determine this."
questions = [
    {"q": "How many unique orders are there?",
     "expected": int(orders.order_id.nunique()),
     "trap": "Exact + conflicting duplicates; count distinct order_id, not rows."},
    {"q": "What is the total quantity of items ordered across all unique orders?",
     "expected": int(orders.quantity.sum()),
     "trap": (
         "Exact duplicates inflate the sum — drop them with drop_duplicates() first. "
         "Conflicting duplicate order IDs differ only in status, not in quantity: "
         "both rows agree on quantity, so deduping on order_id is safe for this aggregation. "
         "The system prompt rule ('refuse if conflict could change the answer') does not fire "
         "here because quantity is identical across the conflicting rows."
     )},
    {"q": "What is the total revenue in USD?",
     "expected": REFUSE,
     "trap": "Mixed units (USD/EUR/GBP/INR/JPY/CAD/AUD), no FX table, some prices have no unit. data_notes.md wrongly claims all USD."},
    {"q": "How many orders were placed in April 2025?",
     "expected": REFUSE,
     "trap": "Ambiguous dd/mm vs mm/dd dates and blank dates make the count undeterminable."},
    {"q": "How many orders reference a user_id that does not exist in users.csv?",
     "expected": 4,
     "trap": "Orphan foreign keys; data_notes.md claims every order has a registered user."},
    {"q": "How many registered users are there?",
     "expected": int(users.user_id.nunique()),
     "trap": "Duplicates and a conflicting duplicate (user 1004 with two countries)."},
    {"q": "Which user spent the most money?",
     "expected": REFUSE,
     "trap": "Requires summing across mixed units with no exchange rates."},
    {"q": "What is the total EUR revenue (price x quantity) from Completed orders with a clearly stated EUR currency, excluding orders with conflicting duplicate rows?",
     "expected": round(float((eur_completed.amount * eur_completed.quantity).sum()), 2),
     "trap": "Must parse '€49.99' and '49,99 EUR', skip unknown currency, dedupe."},
    {"q": "How many products are currently out of stock?",
     "expected": REFUSE,
     "trap": "2 products show 0, but 2 have blank stock, so the exact count is unknown (at least 2)."},
    {"q": "What is the email address of the company CEO?",
     "expected": REFUSE,
     "trap": "Not in the data at all."},
    {"q": "How many users are from the United States?",
     "expected": int((true_country == "United States").sum()),
     "trap": "Normalize 'USA', 'US', 'u.s.a.', 'United States'; dedupe users first. User 1004 (India vs Canada conflict) does not affect this count."},
    {"q": "How many distinct currency units appear in orders.csv prices (ignore blank/unlabeled amounts)?",
     "expected": int(len({c for i, c in enumerate(o_currency) if i not in order_no_currency_idx})),
     "trap": "Must parse ISO codes AND symbols (£, ₹, ¥, CA$, A$, $, €). Do not stop at USD/EUR."},
    {"q": "How many blue shirts did we sell?",
     "expected": REFUSE,
     "trap": "No color column exists."},
]


# ----------------------------------------------------------------------
# Validate traps exist, then write files (no cleaning!)
# ----------------------------------------------------------------------
price_blob = " ".join(orders_out.price.astype(str)) + " " + " ".join(inventory_out.price.astype(str))
orders_price_blob = " ".join(orders_out.price.astype(str))
assert "USD" in price_blob or "$" in price_blob
assert "EUR" in price_blob or "€" in price_blob
assert any(t in orders_price_blob for t in ("GBP", "£")),  "GBP missing from orders.csv"
assert any(t in orders_price_blob for t in ("INR", "₹")),  "INR missing from orders.csv"
assert any(t in orders_price_blob for t in ("JPY", "¥")),  "JPY missing from orders.csv"
assert any(t in orders_price_blob for t in ("CAD", "CA$")), "CAD missing from orders.csv"
assert any(t in orders_price_blob for t in ("AUD", "A$")),  "AUD missing from orders.csv"
assert len(set(inv_currency)) >= 5
assert manifest["orders.csv"]["exact_duplicate_rows"] >= 10
assert manifest["orders.csv"]["blank_order_dates"] >= 10
assert manifest["orders.csv"]["ambiguous_slash_dates"] > 0
assert orders_out.order_id.duplicated().sum() > orders_out.duplicated().sum()  # conflicting dupes exist

OUT.mkdir(exist_ok=True)
orders_out.to_csv(OUT / "orders.csv", index=False)
users_out.to_csv(OUT / "users.csv", index=False)
inventory_out.to_csv(OUT / "inventory.csv", index=False)
(OUT / "data_notes.md").write_text(NOTES, encoding="utf-8")
(OUT / "trap_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
(OUT / "test_questions.json").write_text(json.dumps(questions, indent=2, ensure_ascii=False), encoding="utf-8")

for name in ["orders.csv", "users.csv", "inventory.csv"]:
    m = manifest[name]
    print(f"{name:14} rows={m['rows']:4}  exact_dupes={m['exact_duplicate_rows']:3}  "
          f"ambiguous_dates={m['ambiguous_slash_dates']}")
print(f"\nFiles written to {OUT.resolve()}")