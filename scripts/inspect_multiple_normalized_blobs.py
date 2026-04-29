import sys
from pathlib import Path
import re

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from azure.storage.blob import BlobServiceClient
from core.config import settings


def analyze_text(text: str) -> dict:
    body = re.sub(r"^---[\s\S]*?---\s*", "", text, count=1)
    char_count = len(body)
    words = re.findall(r"\w+", body)
    word_count = len(words)
    headings = re.findall(r"^#{1,6}\s+", body, flags=re.MULTILINE)
    para_count = len([p for p in body.split('\n\n') if p.strip()])
    return {
        "chars": char_count,
        "words": word_count,
        "headings": len(headings),
        "paragraphs": para_count,
    }


def main(limit: int = 5):
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

    blobs_sorted = sorted(blobs, key=lambda b: getattr(b, 'last_modified', None) or 0, reverse=True)
    to_check = blobs_sorted[:limit]

    totals = {"count": 0, "words": 0, "chars": 0}
    print(f"Checking {len(to_check)} most recent blobs in {container}\n")
    for b in to_check:
        name = b.name
        blob_client = container_client.get_blob_client(name)
        data = blob_client.download_blob().readall()
        try:
            text = data.decode('utf-8')
        except Exception:
            text = data.decode('latin-1', errors='ignore')

        stats = analyze_text(text)
        totals["count"] += 1
        totals["words"] += stats["words"]
        totals["chars"] += stats["chars"]

        print(f"Blob: {name}")
        print(f"  words: {stats['words']} chars: {stats['chars']} headings: {stats['headings']} paragraphs: {stats['paragraphs']}")

    avg_words = totals["words"] / totals["count"] if totals["count"] else 0
    print(f"\nAverage words across {totals['count']} blobs: {avg_words:.1f}")
    if avg_words > 800:
        print("Recommendation: enable chunking (e.g., 500-800 words/chunk).")
    else:
        print("Recommendation: chunking not generally necessary for these CVs.")

    return 0


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--limit', type=int, default=5)
    args = p.parse_args()
    raise SystemExit(main(limit=args.limit))
