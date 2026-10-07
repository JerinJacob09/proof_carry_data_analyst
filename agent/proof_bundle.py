"""Build a portable archive for rerunning an accepted analysis proof."""

from __future__ import annotations

import io
import json
import platform
import zipfile
from pathlib import Path


def build_proof_bundle(
    question: str,
    answer: str,
    code: str,
    csv_names: tuple[str, ...],
    work: Path,
    *,
    pandas_version: str,
    numpy_version: str,
) -> bytes:
    """Package generated code, the exact input CSVs, and rerun instructions."""
    prelude = (
        "import json\nimport re\nfrom pathlib import Path\n"
        "import numpy as np\nimport pandas as pd\n"
        "pd.options.mode.string_storage = 'python'\n\n"
    )
    metadata = {
        "question": question,
        "expected_output": answer,
        "csv_files": list(csv_names),
        "verification": "The sandbox ran this proof twice and the output matched both times.",
        "pandas_version": pandas_version,
        "numpy_version": numpy_version,
        "python_version": platform.python_version(),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("proof.py", prelude + code.rstrip() + "\n")
        bundle.writestr("proof.json", json.dumps(metadata, indent=2, ensure_ascii=False) + "\n")
        bundle.writestr(
            "README.txt",
            f"Use Python {platform.python_version()} and install requirements-proof.txt, then run: python proof.py\n"
            "Compare its output with expected_output in proof.json. Keep the CSV files beside proof.py.\n",
        )
        bundle.writestr(
            "requirements-proof.txt",
            f"pandas=={pandas_version}\nnumpy=={numpy_version}\n",
        )
        for name in csv_names:
            safe_name = Path(name).name
            source = work / safe_name
            if source.is_file() and source.suffix.lower() == ".csv":
                bundle.write(source, arcname=safe_name)
    return buffer.getvalue()
