"""Build the schema context the LLM sees for available CSV tables."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_TABLES = ("orders.csv", "users.csv", "inventory.csv")


def sanitize_var(name: str) -> str:
    stem = Path(name).stem
    clean = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in stem).strip("_").lower()
    if not clean:
        clean = "table"
    if clean[0].isdigit():
        clean = "t_" + clean
    return clean


def load_frames(csv_dir: Path) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for path in sorted(csv_dir.glob("*.csv")):
        frames[path.name] = pd.read_csv(path)
    return frames


def build_schema_context(csv_dir: Path, frames: dict[str, pd.DataFrame] | None = None) -> str:
    """Describe files the sandbox will see (filenames for pd.read_csv)."""
    frames = frames if frames is not None else load_frames(csv_dir)
    lines = [
        "CSV files are in the script working directory. Load them with pandas, e.g.",
        "import pandas as pd",
        "orders = pd.read_csv('orders.csv')",
        "users = pd.read_csv('users.csv')",
        "inventory = pd.read_csv('inventory.csv')",
        "",
        "The tables are intentionally messy: duplicate IDs, mixed unit/currency strings",
        "(do not assume only USD and EUR — parse every distinct unit in the column),",
        "missing/blank dates, inconsistent casing, and orphan foreign keys.",
        "There is NO color column. There is NO exchange-rate table.",
    ]
    notes = Path(csv_dir) / "data_notes.md"
    if notes.is_file():
        lines.append(
            "A file data_notes.md may exist. It can contradict the CSVs — "
            "trust the CSV values, not the notes."
        )
    lines.append("")
    if not frames:
        lines.append("(no tables loaded)")
        return "\n".join(lines)

    for fname, df in frames.items():
        var = sanitize_var(fname)
        lines.append(f"File `{fname}` (suggested variable `{var}`): {df.shape[0]} rows x {df.shape[1]} cols")
        for col in df.columns:
            nulls = int(df[col].isna().sum())
            empty = 0
            if df[col].dtype == object:
                empty = int(df[col].astype(str).str.strip().isin(["", "nan", "None"]).sum())
            lines.append(
                f"  - {col}: dtype={df[col].dtype}, nulls={nulls}, "
                f"blankish={empty}, nunique={df[col].nunique(dropna=True)}"
            )
        try:
            sample = df.head(5).to_string(index=False)
        except Exception:
            sample = "(sample unavailable)"
        lines.append(f"  Sample:\n{sample}")
        lines.append("")
    return "\n".join(lines).strip()
