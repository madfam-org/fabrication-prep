#!/usr/bin/env bash
# Licence gate for the RUNTIME dependency set (run it in an environment that has only
# `pip install .` plus pip-licenses). fabrication-prep is AGPL-3.0-only:
#   - permissive and weak-copyleft licences (MIT, BSD, Apache, PSF, MPL, LGPL, ...) pass;
#   - GPL/AGPL dependencies are compatible but are REPORTED, so adding one is a conscious choice;
#   - non-free / source-available licences (SSPL, BUSL, Commons Clause, non-commercial) FAIL;
#   - packages without recognisable metadata are listed for a human and FAIL until resolved in KNOWN.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python}"
OUT="$(mktemp)"
"$PYTHON" -m piplicenses --format=json --from=mixed > "$OUT"
"$PYTHON" - "$OUT" <<'PY'
import json, re, sys
rows = json.load(open(sys.argv[1]))
ALLOW = re.compile(r"MIT|BSD|Apache|ISC|PSF|Python Software Foundation|MPL|Mozilla|Unlicense|Zlib|HPND|CC0|LGPL|Lesser General", re.I)
REVIEW = re.compile(r"AGPL|Affero|(?<![L])GPL(?!.*Lesser)", re.I)
DENY = re.compile(r"SSPL|Server Side Public|BUSL|Business Source|Commons Clause|Non-?Commercial|\bNC\b|Proprietary", re.I)
# Known licences for packages whose metadata pip-licenses cannot classify.
KNOWN = {"fabrication-prep": "AGPL-3.0-only (this repository)"}
SKIP = {"pip", "setuptools", "wheel", "pip-licenses", "prettytable", "wcwidth", "tomli"}
bad, unknown, review, ok = [], [], [], []
for r in rows:
    name, lic = r["Name"], r.get("License") or ""
    if name.lower() in SKIP:
        continue
    lic = KNOWN.get(name, lic)
    if name == "fabrication-prep":
        continue
    if DENY.search(lic):
        bad.append((name, lic))
    elif ALLOW.search(lic):
        ok.append((name, r.get("Version", ""), lic))
    elif REVIEW.search(lic):
        review.append((name, lic))
    else:
        unknown.append((name, lic))
for n, v, l in sorted(ok, key=lambda x: x[0].lower()):
    print(f"OK       {n} {v}: {l}")
for n, l in review:
    print(f"REVIEW   {n}: {l} (copyleft; compatible with AGPL-3.0, keep it deliberate)")
for n, l in unknown:
    print(f"UNKNOWN  {n}: {l or '(no metadata)'}")
for n, l in bad:
    print(f"DENIED   {n}: {l}")
print(f"licences: {len(ok)} ok, {len(review)} copyleft (review), {len(unknown)} unknown, {len(bad)} denied")
sys.exit(1 if bad or unknown else 0)
PY
rm -f "$OUT"
