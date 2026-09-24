"""Shared diagnostic redaction; never changes task inputs or execution results."""
import json
import os
import re
from urllib.parse import urlsplit, urlunsplit


def secret_name(name):
    return any(part in str(name).upper() for part in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"))


def redact_text(value, *, secrets=()):
    text = str(value)
    values = set(secrets) | {value for key, value in os.environ.items() if secret_name(key)}
    for secret in sorted((s for s in values if isinstance(s, str) and s), key=len, reverse=True):
        text = text.replace(secret, "[redacted]")

    def url(match):
        try:
            parts = urlsplit(match.group())
            host = parts.netloc.rsplit("@", 1)[-1]
            return urlunsplit((parts.scheme, host, parts.path,
                               "[redacted]" if parts.query else "",
                               "[redacted]" if parts.fragment else ""))
        except ValueError:
            return "[malformed URL]"

    return re.sub(r'https?://[^\s<>"\']+', url, text)


def redact_data(value, *, secrets=()):
    if isinstance(value, dict):
        return {key: "[redacted]" if secret_name(key) else redact_data(item, secrets=secrets)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_data(item, secrets=secrets) for item in value]
    return redact_text(value, secrets=secrets) if isinstance(value, str) else value


def audit_message(message):
    # Redact before serialization so quotes/backslashes in secrets cannot evade it
    # and a redacted URL cannot consume JSON delimiters.
    try:
        return json.dumps(redact_data(json.loads(message)), ensure_ascii=False)
    except (ValueError, TypeError):
        return redact_text(message)


def error_text(error):
    return redact_text(error)[:4096]
