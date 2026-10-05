"""
Prova locale del giro completo: CV qualsiasi -> estrazione profilo mcflash ->
CV nel modello MCFlash (.docx, e .pdf se c'e' Word).

Non passa da Blob ne' dalla cache: chiama direttamente l'LLM configurato in
local.settings.json.

Uso:
    python scripts/render_mcflash_cv.py "<cv.pdf|cv.docx>" --out-dir out/ [--pdf]
    python scripts/render_mcflash_cv.py --from-json estratto.json --out-dir out/ [--pdf]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_local_settings() -> None:
    path = ROOT / "local.settings.json"
    if path.exists():
        for key, value in json.loads(path.read_text(encoding="utf-8")).get("Values", {}).items():
            os.environ.setdefault(key, str(value))


def _extract(cv_path: Path) -> dict:
    from core.llm_chain import CVExtractionChain
    from db_data.mapper import to_domain
    from db_data.postprocess import enrich
    from services.document_processor import DocumentProcessor

    text = DocumentProcessor().process(cv_path.read_bytes())["extracted_text"]
    raw = asyncio.run(CVExtractionChain.for_mcflash().extract(text))
    return enrich(to_domain(raw)).model_dump(mode="json")


def _docx_to_pdf(docx_path: Path, pdf_path: Path) -> None:
    """Conversione con Word via PowerShell/COM (solo Windows con Word installato).

    Si preferisce pwsh (PowerShell 7): con Windows PowerShell 5 l'interop di Word
    puo' fallire con TYPE_E_ELEMENTNOTFOUND se la registrazione di Office e' sporca.
    """
    import shutil
    import subprocess

    def _ps(value: Path) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    script = (
        "$w = New-Object -ComObject Word.Application; $w.Visible = $false; "
        f"try {{ $d = $w.Documents.Open({_ps(docx_path)}, $false, $true); "
        f"$d.SaveAs2({_ps(pdf_path)}, 17); $d.Close($false) }} finally {{ $w.Quit() }}"
    )
    shell = shutil.which("pwsh") or "powershell"
    subprocess.run([shell, "-NoProfile", "-Command", script], check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cv", nargs="?", help="CV da estrarre (PDF o DOCX)")
    parser.add_argument("--from-json", help="usa un'estrazione gia' fatta (anche corretta a mano)")
    parser.add_argument("--out-dir", default=".", help="cartella di uscita")
    parser.add_argument("--pdf", action="store_true", help="converte anche in PDF con Word")
    args = parser.parse_args()
    if not args.cv and not args.from_json:
        parser.error("serve un CV o --from-json")

    _load_local_settings()
    from domain.candidate_identity import propose_cv_filename
    from services.cv_mcflash_renderer import build_context, render_docx

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.from_json:
        data = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
    else:
        data = _extract(Path(args.cv))
        json_path = out_dir / (Path(args.cv).stem + ".mcflash.json")
        json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Estrazione: {json_path}")

    tri = data.get("trittico") or "XXX"
    filename = propose_cv_filename(tri, [p.name for p in out_dir.iterdir()], extension=".docx")
    docx_path = out_dir / filename
    docx_path.write_bytes(render_docx(build_context(data)))
    print(f"CV MCFlash: {docx_path}")

    if args.pdf:
        pdf_path = docx_path.with_suffix(".pdf")
        _docx_to_pdf(docx_path.resolve(), pdf_path.resolve())
        print(f"PDF: {pdf_path}")


if __name__ == "__main__":
    main()
