"""Fail CI on new Python advisories; report the one mitigated, unfixed upstream issue."""
import json
import subprocess
import sys
from pathlib import Path

# Exact package, version and advisory; never blanket-ignore a package or severity.
# DSPy requires diskcache. core.ai_cache gives DSPy an empty private import-time
# cache then disables both caches and enables restricted pickle deserialization.
MITIGATED = {("diskcache", "5.6.3", "PYSEC-2026-2447")}
report = Path("pip-audit.json")
result = subprocess.run([sys.executable, "-m", "pip_audit", "--disable-pip", "--no-deps",
                         "-r", "production_requirements.lock", "--format", "json", "-o", str(report)])
if not report.exists():
    raise SystemExit(result.returncode or 1)
data = json.loads(report.read_text())
remaining = []
for dep in data["dependencies"]:
    for vuln in dep.get("vulns", []):
        item = (dep["name"], dep["version"], vuln["id"])
        if item in MITIGATED:
            print("MITIGATED (not patched):", *item)
        else:
            remaining.append(item)
            print("UNRESOLVED:", *item)
if result.returncode not in (0, 1):
    raise SystemExit(result.returncode)
raise SystemExit(bool(remaining))
