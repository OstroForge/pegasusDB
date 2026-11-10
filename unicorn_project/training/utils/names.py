import re

# Keep hyphens and apostrophes so each part of a name can keep its capital.
_NAME_BREAKS = re.compile(r"([\-'’])")


def title_case_person_name(value: str) -> str:
    """
    Title-case a person's name without flattening the letter after a hyphen
    or apostrophe. "anne-marie o'brien" becomes "Anne-Marie O'Brien".
    """
    text = " ".join((value or "").split())
    if not text:
        return ""
    return " ".join(_title_case_token(word) for word in text.split(" "))


def _title_case_token(token: str) -> str:
    parts = _NAME_BREAKS.split(token)
    titled = []
    for part in parts:
        if not part or part in "-'’":
            titled.append(part)
            continue
        titled.append(part[:1].upper() + part[1:].lower())
    return "".join(titled)
