#!/usr/bin/env python3
"""Create deploy.zip for Azure deployment, excluding virtualenv and local logs."""
import os
import zipfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
EXCLUDE_DIRS = {'.venv', '.git', '__pycache__'}
EXCLUDE_FILES = {'deploy.zip', 'func_verbose.log'}

def should_exclude(path):
    # exclude if any path component matches an exclude dir
    rel = os.path.relpath(path, ROOT)
    parts = rel.split(os.sep)
    for p in parts:
        if p in EXCLUDE_DIRS:
            return True
    if os.path.basename(path) in EXCLUDE_FILES:
        return True
    return False

def make_zip():
    zip_path = os.path.join(ROOT, 'deploy.zip')
    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(ROOT):
            # skip excluded directories by modifying dirnames in-place
            dirnames[:] = [d for d in dirnames if not should_exclude(os.path.join(dirpath, d))]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                if should_exclude(full):
                    continue
                arcname = os.path.relpath(full, ROOT)
                zf.write(full, arcname)
    print('Created', zip_path)

if __name__ == '__main__':
    make_zip()
