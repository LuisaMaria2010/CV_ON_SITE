import sys
from pathlib import Path
import re
import statistics

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from azure.storage.blob import BlobServiceClient
from core.config import settings


THRESHOLD_CHARS = int((__import__('os').environ.get('AZURE_SEARCH_CHUNK_SIZE') or '2000'))


def strip_front_matter(text: str) -> str:
    return re.sub(r"^---[\s\S]*?---\s*", "", text, count=1)


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

    stats = []
    over = []
    samples_over = []

    for b in blobs:
        name = b.name
        blob_client = container_client.get_blob_client(name)
        data = blob_client.download_blob().readall()
        try:
            text = data.decode('utf-8')
        except Exception:
            text = data.decode('latin-1', errors='ignore')

        body = strip_front_matter(text)
        chars = len(body)
        stats.append(chars)
        if chars > THRESHOLD_CHARS:
            over.append(chars)
            samples_over.append((name, chars))

    total = len(stats)
    avg = statistics.mean(stats) if stats else 0
    med = statistics.median(stats) if stats else 0
    pct_over = 100.0 * len(over) / total if total else 0

    print(f"Analyzed {total} blobs in {container}")
    print(f"Threshold (chars): {THRESHOLD_CHARS}")
    print(f"Avg chars: {avg:.1f}, Median chars: {med}, Above threshold: {len(over)} ({pct_over:.1f}%)")

    if len(over) == 0:
        print("Recommendation: chunking not required for stored CVs (none exceed threshold).")
    elif pct_over < 15:
        print("Recommendation: chunking optional — only a small fraction exceed threshold; consider conditional chunking.")
    else:
        print("Recommendation: enable chunking for documents exceeding threshold (conditional chunking).")

    if samples_over:
        print("\nSample documents above threshold:")
        for name, c in samples_over[:10]:
            print(f" - {name}: {c} chars")

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
