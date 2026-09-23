"""Parse junit xml files and print a compact verdict + failure list."""
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

for path in sys.argv[1:]:
    p = Path(path)
    if not p.exists():
        print(f"{path}: MISSING")
        continue
    try:
        root = ET.parse(p).getroot()
    except Exception as exc:  # noqa: BLE001
        print(f"{path}: PARSE ERROR {exc}")
        continue
    suites = [root] if root.tag == "testsuite" else list(root)
    tests = failures = errors = skipped = 0
    failed_names = []
    for s in suites:
        tests += int(s.get("tests", 0))
        failures += int(s.get("failures", 0))
        errors += int(s.get("errors", 0))
        skipped += int(s.get("skipped", 0))
        for tc in s.iter("testcase"):
            for bad in list(tc.findall("failure")) + list(tc.findall("error")):
                failed_names.append(f"{tc.get('classname')}::{tc.get('name')}#{bad.tag}")
    print(f"--- {path}")
    print(f"    tests={tests} failures={failures} errors={errors} skipped={skipped}")
    for n in failed_names:
        print(f"    FAIL {n}")
