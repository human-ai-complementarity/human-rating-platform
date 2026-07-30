"""Single parser for a question's raw `options` string.

Datasets store options as one free-form string, in any of several formats
(pipe-delimited, letter-labeled, newline-delimited, comma-delimited). Every
consumer must agree on how it splits: the rater UI renders the resulting list,
and assistance methods rank against it by position. Keeping one implementation
here, and sending the parsed list to the client, is what makes an option index
mean the same thing on both sides.
"""

from __future__ import annotations

import re

_OPTION_LABEL_PATTERN = re.compile(r"(?:^|[,\r\n])\s*(?:\(?[A-Z]\)?[.)]|[A-Z]:)\s+")


def parse_options(raw_options: str | None) -> list[str]:
    if not raw_options:
        return []

    if "|" in raw_options:
        return [option.strip() for option in raw_options.split("|") if option.strip()]

    labeled_option_starts = [match.start() for match in _OPTION_LABEL_PATTERN.finditer(raw_options)]
    if len(labeled_option_starts) > 1:
        options = []
        for index, start in enumerate(labeled_option_starts):
            end = (
                labeled_option_starts[index + 1] if index + 1 < len(labeled_option_starts) else None
            )
            option = raw_options[start:end].strip(" ,\r\n")
            if option:
                options.append(option)
        return options

    line_options = [option.strip() for option in re.split(r"\r?\n+", raw_options) if option.strip()]
    if len(line_options) > 1:
        return line_options

    return [option.strip() for option in raw_options.split(",") if option.strip()]
