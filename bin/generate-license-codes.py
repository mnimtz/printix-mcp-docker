#!/usr/bin/env python3
"""
Generates the Pro-Feature activation codes for Printix MCP.

Codes are derived from the secret embedded in src/license.py — change
the secret there to invalidate all previously-issued codes (next image
build will then regenerate them with the new secret).

Usage:
    python3 bin/generate-license-codes.py
"""
import hashlib
import sys
import pathlib

# Load the secret + feature list from src/license.py without importing
# the full module (which has runtime db dependencies).
ROOT = pathlib.Path(__file__).resolve().parent.parent
LICENSE_PY = ROOT / "src" / "license.py"
src = LICENSE_PY.read_text()

# Extract _LICENSE_SECRET = "..."
import re
m = re.search(r'_LICENSE_SECRET\s*=\s*"([^"]+)"', src)
if not m:
    sys.exit("could not find _LICENSE_SECRET in src/license.py")
secret = m.group(1)

# Extract feature names from PRO_FEATURES dict keys
features = re.findall(r'^\s*"([a-z_]+)":\s*\{', src, re.M)
features = [f for f in features if f not in ("icon", "label_de", "label_en",
                                              "label_no", "description_de",
                                              "description_en", "description_no",
                                              "url_path")]

def code(feature: str) -> str:
    return hashlib.sha256(f"{secret}|{feature}".encode()).hexdigest()[:12].upper()

print(f"Printix MCP — Pro feature activation codes")
print(f"Secret: {secret}")
print(f"")
print(f"  {'Feature':<20} {'Code':<14}")
print(f"  {'-' * 20} {'-' * 14}")
for f in features:
    print(f"  {f:<20} {code(f)}")
print(f"  {'-' * 20} {'-' * 14}")
print(f"  {'*all* (master)':<20} {code('*all*')}")
print(f"")
print(f"Format these as e.g. ABCD-EFGH-1234 when sending to customers.")
