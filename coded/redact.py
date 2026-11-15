"""Hide secrets in text coded writes for people or other systems to read.

Applied to what leaves the conversation: the event log, telemetry spans,
`/export`, mission reports and their notifications, and the error text that
ends a turn. Never to the user's files, and not to the saved session itself
(which must stay an exact record and is stored 0600, see securefs).

What counts as a secret: well-known token formats (OpenAI/Anthropic, GitHub,
GitLab, Slack, AWS, Google), bearer/basic Authorization values, passwords in
URLs and connection strings, `password=`-style assignments, PEM private keys,
JWTs, and the values of this process's sensitive environment variables
(`*_KEY`, `*_TOKEN`, `*SECRET*`, `*PASSWORD*`, …).
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterable, Optional

MASK = "[REDACTED]"

_PATTERNS = [
    # PEM private keys (whole block)
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
               re.S),
    # provider tokens
    re.compile(r"\bsk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"\bhf_[A-Za-z0-9]{30,}\b"),
    # JWT
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
]
# Authorization: Bearer x / Basic x / token x (header or prose)
_AUTH = re.compile(r"(?i)\b((?:authorization|proxy-authorization)\s*[:=]\s*"
                   r"(?:bearer\s+|basic\s+|token\s+)?|bearer\s+|basic\s+(?=[A-Za-z0-9+/=]{12}))"
                   r"([^\s'\",;]{8,})")
# scheme://user:password@host  (database URLs, git remotes, proxies)
_URL_CRED = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^:/\s@'\"]+:)([^@/\s'\"]+)(@)")
# password=..., "api_key": "...", SECRET: ...
_ASSIGN = re.compile(
    r"(?i)((?:\"|')?\b[\w.\-]*(?:password|passwd|pwd|secret|token|api[_\-]?key|apikey|"
    r"access[_\-]?key|private[_\-]?key|client[_\-]?secret|auth[_\-]?key)(?:\"|')?"
    r"\s*[:=]\s*(?:\"|')?)(?!(?:true|false|none|null)\b)([^\s\"',;&]*[^\s\"',;&\d][^\s\"',;&]*)")
_SENSITIVE_ENV = re.compile(r"(?i)(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|"
                            r"PRIVATE|SESSION|COOKIE)")
_ENV_SAFE = {"PATH", "HOME", "TERM", "SHELL", "USER", "LANG", "PWD", "OLDPWD",
             "CODED_TRUST_PROJECT", "SSH_AUTH_SOCK", "XDG_SESSION_ID", "XDG_SESSION_TYPE",
             "XDG_SESSION_CLASS", "XDG_SESSION_DESKTOP", "DBUS_SESSION_BUS_ADDRESS"}


def env_secrets(env: Optional[dict] = None) -> list:
    """Values of sensitive-looking environment variables, longest first."""
    env = os.environ if env is None else env
    vals = {v for k, v in env.items()
            if k not in _ENV_SAFE and _SENSITIVE_ENV.search(k) and isinstance(v, str)
            and len(v) >= 8 and not v.startswith(("/", "~", "http://localhost"))}
    return sorted(vals, key=len, reverse=True)


def redact(text: Any, extra: Iterable[str] = (), env: Optional[dict] = None) -> Any:
    """`text` with secrets replaced by [REDACTED]. Non-strings pass through."""
    if not isinstance(text, str) or not text:
        return text
    out = text
    for value in [*env_secrets(env), *[e for e in extra if e and len(e) >= 6]]:
        if value in out:
            out = out.replace(value, MASK)
    for pat in _PATTERNS:
        out = pat.sub(MASK, out)
    out = _URL_CRED.sub(lambda m: m.group(1) + MASK + m.group(3), out)
    out = _AUTH.sub(lambda m: m.group(1) + MASK if m.group(2) != MASK else m.group(0), out)
    out = _ASSIGN.sub(lambda m: m.group(1) + MASK if MASK not in m.group(2) else m.group(0), out)
    return out


def redact_data(value: Any, env: Optional[dict] = None) -> Any:
    """Redact every string inside dicts/lists/tuples (a copy)."""
    if isinstance(value, str):
        return redact(value, env=env)
    if isinstance(value, dict):
        return {k: redact_data(v, env) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(redact_data(v, env) for v in value)
    return value
