"""In-flight secret redaction. Runs on every string BEFORE anything is parsed, indexed or stored."""
import re

PATTERNS = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("stripe_key", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{10,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}")),
]
URL_CREDS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^/\s@]+)@")
GENERIC = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:api[_-]?key|secret|token|passwd|password|pwd)[A-Z0-9_]*)\s*[:=]\s*[\"']?([^\s\"',;]{6,})[\"']?"
)


def scrub(text: str):
    """Return (clean_text, [kinds]). Secrets are never returned or stored."""
    kinds = []
    out = text
    for kind, rx in PATTERNS:
        def _r(m, kind=kind):
            kinds.append(kind)
            return f"[REDACTED:{kind}]"
        out = rx.sub(_r, out)

    def _url(m):
        kinds.append("url_credentials")
        return f"{m.group(1)}[REDACTED:url_credentials]@"
    out = URL_CREDS.sub(_url, out)

    def _gen(m):
        if m.group(2).startswith("[REDACTED"):
            return m.group(0)
        kinds.append("generic_secret")
        return f"{m.group(1)}=[REDACTED:generic_secret]"
    out = GENERIC.sub(_gen, out)
    return out, kinds


def scrub_obj(obj, path, report):
    """Recursively scrub every string in a parsed JSON/CSV structure.
    `report` collects (kind, location) pairs; the secret value itself is dropped."""
    if isinstance(obj, str):
        clean, kinds = scrub(obj)
        for k in kinds:
            report.append((k, path))
        return clean
    if isinstance(obj, dict):
        return {k: scrub_obj(v, f"{path}.{k}", report) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_obj(v, f"{path}[{i}]", report) for i, v in enumerate(obj)]
    return obj
