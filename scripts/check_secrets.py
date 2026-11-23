"""Fail if files git would publish contain credentials, personal paths or files that must stay local.

usage: python scripts/check_secrets.py     (run from anywhere inside the repository; CI runs it on every push)
Findings show the file, line and rule, never the matched value.
"""
import re, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULES = {
    "Discord webhook": r"discord(?:app)?\.com/api/webhooks/\d+/[\w-]{20,}",
    "GitHub token": r"\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_\w{20,}",
    "Google API key": r"\bAIza[\w-]{30,}",
    "Google OAuth secret or token": r"\bGOCSPX-[\w-]{10,}|\bya29\.[\w.-]{20,}|\b1//[\w-]{30,}",
    "AWS key": r"\bAKIA[0-9A-Z]{16}\b",
    "Private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "Assigned credential": r"(?i)\b(?:api[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token|password)\b[\"']?\s*[:=]\s*[\"'](?!your_)[A-Za-z0-9_\-./+]{24,}[\"']",
    "Personal email": r"[\w.+-]+@(?:gmail|googlemail|outlook|hotmail|live|yahoo|icloud|proton|protonmail)\.[a-z.]+",
    "Personal Windows path": r"(?i)\b[A-Z]:[\\/]+Users[\\/]+(?!<|you\b|your)[A-Za-z0-9._-]+",
    "Personal home path": r"/(?:home|Users)/(?!runner\b|you\b|your)[a-z][\w.-]+/",
}
RX = {name: re.compile(p) for name, p in RULES.items()}
BLOCKED = re.compile(r"(^|/)(\.env(\.(?!example$).+)?|config\.toml|client_secret[^/]*\.json|credentials[^/]*\.json|token[^/]*\.json|posted_log\.csv)$"
                     r"|\.(mp4|mkv|mov|ts|mp3|wav|log|db|sqlite3?|pem|key)$", re.IGNORECASE)
# Test data that deliberately looks like a credential.
ALLOWED = {("pipeline/tests/test_integrations.py", "Assigned credential")}


def tracked():
    out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def main():
    findings = []
    files = tracked()
    for rel in files:
        if BLOCKED.search(rel):
            findings.append(f"{rel}: this kind of file must not be committed")
            continue
        path = ROOT / rel
        if rel == "scripts/check_secrets.py" or not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data[:4096]:
            continue
        for number, line in enumerate(data.decode("utf-8", "ignore").splitlines(), 1):
            findings += [f"{rel}:{number}: {name}" for name, rx in RX.items() if (rel, name) not in ALLOWED and rx.search(line)]
    for finding in findings:
        print(finding)
    print(f"Checked {len(files)} files: {len(findings)} problem(s).")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
