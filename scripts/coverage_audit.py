import os
import re
import json

ROOT = os.path.dirname(os.path.dirname(__file__))
TESTS_DIR = os.path.join(ROOT, "tests")

def gather_source_files(root):
    srcs = []
    for dirpath, dirnames, filenames in os.walk(root):
        # skip virtualenv and tests
        if ".venv" in dirpath or "/.venv" in dirpath:
            continue
        if dirpath.startswith(TESTS_DIR):
            continue
        # skip .git, __pycache__
        if any(p in dirpath for p in ("__pycache__", ".git")):
            continue
        for f in filenames:
            if f.endswith(".py"):
                srcs.append(os.path.relpath(os.path.join(dirpath, f), ROOT))
    return sorted(srcs)

def gather_test_texts(tdir):
    texts = []
    for dirpath, _, filenames in os.walk(tdir):
        for f in filenames:
            if f.endswith('.py'):
                p = os.path.join(dirpath, f)
                try:
                    with open(p, 'r', encoding='utf-8') as fh:
                        texts.append(fh.read())
                except Exception:
                    pass
    return texts

def is_covered(basename, test_texts):
    # check occurrences of module name or import path
    name = os.path.splitext(os.path.basename(basename))[0]
    pat = re.compile(r"\b(" + re.escape(name) + r")\b")
    for t in test_texts:
        if pat.search(t):
            return True
    return False

def main():
    srcs = gather_source_files(ROOT)
    tests = gather_test_texts(TESTS_DIR)

    uncovered = []
    covered = []
    for s in srcs:
        # ignore top-level tests and scripts
        if s.startswith('tests/') or s.startswith('scripts/coverage'):
            continue
        if is_covered(s, tests):
            covered.append(s)
        else:
            uncovered.append(s)

    out = {"covered_count": len(covered), "uncovered_count": len(uncovered), "uncovered": uncovered}
    print(json.dumps(out, indent=2))

if __name__ == '__main__':
    main()
