"""Prompt template rendering.

Templates live in mangacopy/prompts/{name}.md. Placeholders use the syntax
<<FIELD>> (chosen over {FIELD} to avoid clashing with JSON braces in prompt
bodies). Missing fields are left as-is with a warning on stderr.
"""

from __future__ import annotations

import re
import sys

from . import config

_PLACEHOLDER = re.compile(r"<<([A-Za-z_][A-Za-z0-9_]*)>>")


def render(name: str, **fields) -> str:
    """Read prompts/{name}.md and substitute <<KEY>> placeholders with
    str(fields[KEY]). Unknown placeholders are kept verbatim (warning to
    stderr). Raises OSError if the template file does not exist."""
    path = config.PROMPTS_DIR / f"{name}.md"
    text = path.read_text(encoding="utf-8")

    def _sub(match: re.Match) -> str:
        key = match.group(1)
        if key in fields:
            return str(fields[key])
        print(
            f"[templates] warning: field {key!r} not provided for template {name!r}",
            file=sys.stderr,
        )
        return match.group(0)

    return _PLACEHOLDER.sub(_sub, text)
