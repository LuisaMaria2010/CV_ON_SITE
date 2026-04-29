import sys
from pathlib import Path
import re

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from azure.storage.blob import BlobServiceClient
from core.config import settings


def main():
    conn = settings.storage_account_connection_string
    if not conn:
        print("No storage connection string available")
        return 2

    container = settings.storage_container_normalized_markdown
    client = BlobServiceClient.from_connection_string(conn)
    container_client = client.get_container_client(container)

    blobs = list(container_client.list_blobs())
    if not blobs:
        print(f"No blobs found in container: {container}")
        return 1

    # pick the most recent blob by last_modified
    blobs_sorted = sorted(blobs, key=lambda b: getattr(b, 'last_modified', None) or 0, reverse=True)
    blob = blobs_sorted[0]
    name = blob.name
    print(f"Analyzing blob: {name}")

    blob_client = container_client.get_blob_client(name)
    data = blob_client.download_blob().readall()
    try:
        text = data.decode('utf-8')
    except Exception:
        text = data.decode('latin-1', errors='ignore')

    # strip YAML front matter
    body = re.sub(r"^---[\s\S]*?---\s*", "", text, count=1)

    char_count = len(body)
    words = re.findall(r"\w+", body)
    word_count = len(words)
    headings = re.findall(r"^#{1,6}\s+", body, flags=re.MULTILINE)
    para_count = len([p for p in body.split('\n\n') if p.strip()])

    print(f"characters: {char_count}")
    print(f"words: {word_count}")
    print(f"headings: {len(headings)} paragraphs: {para_count}")

    # Simple heuristic for chunking
    # if more than 800 words -> recommend chunking; else no
    threshold = 800
    if word_count > threshold:
        print("Recommendation: document is long — perform chunking (e.g., ~500-800 words per chunk).")
    else:
        print("Recommendation: document is short — chunking not required for this document.")

    # print short preview
    preview = body.strip()[:1000]
    print("--- preview ---")
    print(preview)
    print("--- end preview ---")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
