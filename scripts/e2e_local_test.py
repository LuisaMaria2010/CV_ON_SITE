import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.document_processor import DocumentProcessor


def run(sample_path: str):
    p = Path(sample_path)
    if not p.exists():
        print("Sample file not found:", sample_path)
        return 2

    data = p.read_bytes()
    mime = None
    if p.suffix.lower() == ".pdf":
        mime = "application/pdf"
    elif p.suffix.lower() == ".pptx":
        mime = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    elif p.suffix.lower() == ".docx":
        mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    proc = DocumentProcessor()
    try:
        res = proc.process(data, mime_type=mime, filename=p.name, source_path=f"/incoming-cv/{p.name}")
    except Exception as e:
        print("Processing failed:", e)
        return 3

    print("content_hash:", res.get("content_hash"))
    md = res.get("markdown") or ""
    print("--- markdown preview (first 800 chars) ---")
    print(md[:800])
    print("--- end preview ---")

    elements = res.get("elements") or []
    images = [e for e in elements if getattr(e, "element_type", None) == "image"]
    print("image_count:", len(images))
    for i, img in enumerate(images, start=1):
        has_bytes = bool(getattr(img, "image_bytes", None))
        print(f"image {i}: has_bytes={has_bytes} image_src={getattr(img, 'image_src', None)} image_description={getattr(img,'image_description',None)})")

    return 0


if __name__ == "__main__":
    # default sample from other workspace (adjust if missing)
    default = r"c:\Users\User\Documents\Progetti_agenti\AgentIngestionPipeline\test_docs_md\pptx_tests\sample_test.pptx"
    path = sys.argv[1] if len(sys.argv) > 1 else default
    raise SystemExit(run(path))
