import sys
import xml.etree.ElementTree as ET

path = sys.argv[1]
tree = ET.parse(path)
root = tree.getroot()
suite = root.find("testsuite") if root.tag == "testsuites" else root
print("tests=%s failures=%s errors=%s skipped=%s" % (
    suite.get("tests"), suite.get("failures"), suite.get("errors"), suite.get("skipped")))
for tc in suite.iter("testcase"):
    fail = tc.find("failure")
    err = tc.find("error")
    skip = tc.find("skipped")
    if fail is not None or err is not None:
        node = fail if fail is not None else err
        msg = (node.get("message") or "").replace("\n", " ")[:240]
        print("FAIL:", tc.get("classname"), "::", tc.get("name"))
        print("      msg:", msg)
    elif skip is not None:
        print("SKIP:", tc.get("classname"), "::", tc.get("name"), "|", (skip.get("message") or "")[:120])
