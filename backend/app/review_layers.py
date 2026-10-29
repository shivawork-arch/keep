"""Code-side triggers for PR Review layers 2-4. Fixed rules live here; Claude only judges."""

import re

LINKED_ISSUE = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)")

SECURITY_TRIGGERS = [
    ("hardcoded secret", re.compile(r"(?i)\b(password|passwd|secret|api_?key|token|private_key)\b\s*[:=]\s*['\"][^'\"]{4,}['\"]")),
    ("eval/exec", re.compile(r"\b(eval|exec)\s*\(")),
    ("shell command", re.compile(r"shell\s*=\s*True|os\.system\s*\(|child_process\.exec\s*\(")),
    ("TLS check off", re.compile(r"verify\s*=\s*False|rejectUnauthorized\s*:\s*false|InsecureSkipVerify\s*:\s*true")),
    ("SQL from strings", re.compile(r"(?i)(execute|query)\s*\(\s*f?['\"].*\b(select|insert|update|delete)\b.*(\{|%s?|\+)")),
    ("weak hash", re.compile(r"(?i)\b(md5|sha1)\s*\(|hashlib\.(md5|sha1)\b")),
    ("unsafe deserialise", re.compile(r"pickle\.loads?\s*\(|yaml\.load\s*\((?!.*Loader=yaml\.SafeLoader)")),
    ("plaintext password", re.compile(r"(?i)password\s*=\s*(request|req|body|form|data)\b")),
]

DEPENDENCY_FILES = re.compile(
    r"(^|/)(requirements[^/]*\.txt|pyproject\.toml|Pipfile|package\.json|go\.mod|Cargo\.toml|Gemfile|pom\.xml|build\.gradle)$")


def linked_issues(text: str) -> list[int]:
    seen = []
    for n in LINKED_ISSUE.findall(text or ""):
        if int(n) not in seen:
            seen.append(int(n))
    return seen


def security_hits(files) -> list[dict]:
    hits = []
    for f in files:
        for line, text in f.added:
            for label, pattern in SECURITY_TRIGGERS:
                if pattern.search(text):
                    hits.append({"file": f.path, "line": line, "trigger": label, "code": text.strip()[:200]})
    return hits


def dependency_changes(files) -> list[dict]:
    return [{"file": f.path, "added": [t.strip() for _, t in f.added if t.strip()][:30]}
            for f in files if DEPENDENCY_FILES.search(f.path)]
