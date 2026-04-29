import json
import os
import sys
from datetime import datetime, timezone
from uuid import uuid4
from pathlib import Path

# ensure repo root in path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from azure.storage.blob import BlobServiceClient
from azure.storage.queue import QueueClient

from core.config import settings


def main(sample_path: str):
    conn = settings.storage_account_connection_string
    if not conn:
        print("Missing storage connection string in settings/local.settings.json or env")
        return 2

    container = os.environ.get("STORAGE_CONTAINER_INCOMING") or settings.storage_container_incoming
    queue_name = os.environ.get("DOCUMENT_PROCESSING_QUEUE_NAME") or settings.document_processing_queue_name

    p = Path(sample_path)
    if not p.exists():
        print("Sample file not found:", sample_path)
        return 3

    client = BlobServiceClient.from_connection_string(conn)
    container_client = client.get_container_client(container)
    try:
        container_client.create_container()
    except Exception:
        pass

    blob_name = p.name
    print(f"Uploading {p} -> {container}/{blob_name}")
    blob_client = container_client.get_blob_client(blob_name)
    blob_client.upload_blob(p.read_bytes(), overwrite=True, content_type="application/octet-stream")

    # Build message payload
    payload = {
        "blob": f"{container}/{blob_name}",
        "filename": blob_name,
        "source_path": f"/{container}/{blob_name}",
        "last_modified": datetime.now(timezone.utc).isoformat(),
        "correlation_id": f"local-{uuid4().hex[:8]}",
    }

    print("Sending queue message to", queue_name)
    queue = QueueClient.from_connection_string(conn, queue_name)
    try:
        queue.create_queue()
    except Exception:
        pass

    queue.send_message(json.dumps(payload))

    print("Done. Watch your Functions host logs to see processing output.")
    return 0


if __name__ == "__main__":
    import sys

    sample = sys.argv[1] if len(sys.argv) > 1 else "tests/sample_cv.docx"
    raise SystemExit(main(sample))
