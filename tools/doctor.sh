#!/usr/bin/env bash
# Check that this computer can run a trial, and say what to do if not.
# `make doctor` runs this. It ends with a real trial, run twice, which has to
# give the same results file byte for byte.
#
# Exit 0 means trials work. Exit 1 means they don't, and the message says
# which part is missing.

cd "$(dirname "$0")/.." || exit 1
PYTHON="${PYTHON:-python3}"
# The Python version Gradescope runs. Results match it byte for byte only on
# the same minor version; on another one they can differ in rare cases.
REFERENCE_PY=3.12

ok=0
fail=0
warn=0

pass() { printf '  ok    %s\n' "$1"; ok=$((ok + 1)); }
bad()  { printf '  FAIL  %s\n' "$1"; fail=$((fail + 1)); }
note() { printf '  warn  %s\n' "$1"; warn=$((warn + 1)); }
hint() { printf '        %s\n' "$1"; }

echo "this computer"
printf '  os    %s %s\n' "$(uname -s)" "$(uname -m)"
if grep -qi microsoft /proc/version 2>/dev/null; then
    printf '  wsl   yes\n'
fi

echo
echo "python"
if ! command -v "$PYTHON" >/dev/null; then
    bad "no $PYTHON on PATH"
    hint "see README.md, Setup"
    exit 1
fi
version=$("$PYTHON" -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
if "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    pass "$PYTHON is $version ($(command -v "$PYTHON"))"
else
    bad "$PYTHON is $version, needs 3.10 or newer"
fi
if [ "$version" != "$REFERENCE_PY" ]; then
    note "Gradescope uses Python $REFERENCE_PY. your results will almost always"
    hint "match it exactly, but the reference is Gradescope's"
fi
if "$PYTHON" -c 'import yaml' 2>/dev/null; then
    pass "yaml imports"
else
    bad "yaml is missing"
    hint "uv pip install pyyaml   (inside your .venv, see README.md, Setup)"
fi
if "$PYTHON" -c 'import matplotlib' 2>/dev/null; then
    pass "matplotlib imports (plots.png)"
else
    note "matplotlib is missing, so runs skip plots.png. optional"
fi

echo
echo "simulator"
pinned=$(grep '^version=' tools/ns3-version.txt | cut -d= -f2)
if built=$("$PYTHON" -c 'from transportlab import _tlsim; print(_tlsim.ns3_version())' 2>/dev/null); then
    if [ "$built" = "$pinned" ]; then
        pass "ns-$built backend imports"
    else
        bad "backend is built on ns-$built, the course pins ns-$pinned"
        hint "run make setup"
    fi
else
    bad "the ns-3 backend is not built for this python"
    hint "run make setup. if you just switched python versions, run it again"
fi

if [ "$fail" -gt 0 ]; then
    echo
    echo "$fail problem(s). fix them and run make doctor again."
    exit 1
fi

echo
echo "a real trial, twice"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
for i in 1 2; do
    if ! "$PYTHON" -m transportlab run --scenario leo --workload basic_workload \
        --protocol example/baseline_stopwait --out "$tmp/run$i" --no-plots \
        > "$tmp/run$i.log" 2>&1; then
        bad "the trial failed"
        tail -15 "$tmp/run$i.log" | sed 's/^/        /'
        exit 1
    fi
done
pass "leo trial runs"
if cmp -s "$tmp/run1/results.json" "$tmp/run2/results.json"; then
    pass "two runs give identical results"
else
    bad "two runs with the same seed gave different results"
    hint "send this whole output to course staff"
fi

echo
if [ "$fail" -gt 0 ]; then
    echo "$fail problem(s)."
    exit 1
fi
echo "all good ($ok checks, $warn warnings). try: make run"
