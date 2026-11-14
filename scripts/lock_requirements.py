"""Regenerate the Linux CPU lock. Requires uv on PATH."""
import subprocess
from pathlib import Path
subprocess.run(["uv", "pip", "compile", "production_requirements.txt", "--python-version", "3.12",
    "--python-platform", "x86_64-manylinux_2_28", "--torch-backend", "cpu", "--emit-index-url",
    "--generate-hashes", "-o", "production_requirements.lock"], check=True)
p = Path("production_requirements.lock")
s = p.read_text()
if "--extra-index-url https://download.pytorch.org/whl/cpu" not in s:
    s = s.replace("--index-url https://pypi.org/simple", "--index-url https://pypi.org/simple\n--extra-index-url https://download.pytorch.org/whl/cpu")
p.write_text(s)
