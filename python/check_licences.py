"""Checks the licence of every distribution installed in this Python environment against
the SPEC.md licence rule (the same allowlist as deny.toml) and prints a table.

    python check_licences.py      # inside the SDK's venv; exits 1 on any failure

A distribution passes when its licence, read from its metadata (License-Expression,
then the License field, then the trove classifiers), is MIT, MIT-0, Apache-2.0,
BSD-2-Clause, BSD-3-Clause, ISC, Unicode or CC0-1.0. An unrecognised or ambiguous
licence fails: it needs a person to look, not a guess. The SDK itself is skipped.
"""

from __future__ import annotations

import re
import sys
from importlib.metadata import Distribution, distributions
from typing import Optional

ALLOWED = {
    "MIT",
    "MIT-0",
    "Apache-2.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "ISC",
    "Unicode-3.0",
    "Unicode-DFS-2016",
    "CC0-1.0",
}
OURS = {"clovelly-watch", "clovelly-watch-runner"}

# Free-text License fields and classifiers seen in the wild, to SPDX ids.
_TEXT = {
    "mit": "MIT",
    "mit license": "MIT",
    "mit-0": "MIT-0",
    "apache 2.0": "Apache-2.0",
    "apache-2.0": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "apache software license 2.0": "Apache-2.0",
    "bsd-2-clause": "BSD-2-Clause",
    "2-clause bsd license": "BSD-2-Clause",
    "simplified bsd": "BSD-2-Clause",
    "bsd-3-clause": "BSD-3-Clause",
    "3-clause bsd license": "BSD-3-Clause",
    "new bsd license": "BSD-3-Clause",
    "isc": "ISC",
    "isc license": "ISC",
    "isc license (iscl)": "ISC",
    "cc0-1.0": "CC0-1.0",
    "cc0 1.0 universal (cc0 1.0) public domain dedication": "CC0-1.0",
    # cryptography before 46 states its SPDX expression in License, not License-Expression.
    "apache-2.0 or bsd-3-clause": "Apache-2.0 OR BSD-3-Clause",
}
_CLASSIFIER = "License :: OSI Approved :: "


def _spdx_allowed(expression: str) -> bool:
    """A flat SPDX expression: every AND term must pass, any OR alternative suffices.
    Parentheses are not supported and fail."""
    if "(" in expression or ")" in expression:
        return False
    return any(
        all(term.strip() in ALLOWED for term in re.split(r"\s+AND\s+", alternative))
        for alternative in re.split(r"\s+OR\s+", expression)
    )


def licence(dist: Distribution) -> tuple[str, Optional[str]]:
    """(what the metadata says, the SPDX expression it maps to or None)."""
    meta = dist.metadata
    expression = meta.get("License-Expression")
    if expression:
        return expression, expression
    text = (meta.get("License") or "").strip()
    if text and "\n" not in text:
        mapped = _TEXT.get(text.lower())
        if mapped:
            return text, mapped
    classifiers = [c[len(_CLASSIFIER):] for c in meta.get_all("Classifier") or [] if c.startswith(_CLASSIFIER)]
    mapped_classifiers = {_TEXT.get(c.lower()) for c in classifiers}
    if classifiers and None not in mapped_classifiers:
        return "; ".join(classifiers), " OR ".join(sorted(m for m in mapped_classifiers if m))
    shown = text.splitlines()[0][:60] if text else "; ".join(classifiers) or "(none)"
    return shown, None


def main() -> int:
    rows = []
    for dist in sorted(distributions(), key=lambda d: d.metadata["Name"].lower()):
        name = dist.metadata["Name"]
        if name in OURS:
            continue
        stated, spdx = licence(dist)
        if spdx is None:
            verdict = "UNKNOWN"
        else:
            verdict = "ok" if _spdx_allowed(spdx) else "FORBIDDEN"
        rows.append((name, dist.version, stated, spdx or "-", verdict))

    header = ("distribution", "version", "metadata", "spdx", "result")
    widths = [max(len(str(row[i])) for row in [header, *rows]) for i in range(len(header))]
    for row in [header, *rows]:
        print("  ".join(str(cell).ljust(width) for cell, width in zip(row, widths)))
    failed = [row for row in rows if row[-1] != "ok"]
    if failed:
        print(f"\n{len(failed)} distribution(s) outside the licence rule: {', '.join(r[0] for r in failed)}")
        return 1
    print(f"\nall {len(rows)} distributions are under the licence rule")
    return 0


if __name__ == "__main__":
    sys.exit(main())
