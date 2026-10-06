"""CV nel modello MCFlash: dai dati estratti (eventualmente corretti) al .docx.

build_context() traduce un CVExtraction (o il suo dict, come lo rimanda la
dashboard dopo le modifiche) nei segnaposto di templates/cv_mcflash.docx;
render_docx() compila il modello. Il modello si rigenera con
scripts/build_mcflash_template.py.

Il CV e' anonimo: nome, cognome, email, telefono non ci finiscono mai, solo
il trittico.
"""
from __future__ import annotations

import io
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from core.errors import InvalidInputError
from core.schema import CVExtraction
from domain.role_catalog import display_role

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "cv_mcflash.docx"


def _s(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _month_year(d: date | None) -> str:
    return d.strftime("%m/%Y") if d else ""


def _period(start: date | None, end: date | None, ongoing: bool) -> str:
    """Formato dei CV MC: "Da 06/2023 ad Oggi", "Da 12/2021 a 06/2023"."""
    s, e = _month_year(start), _month_year(end)
    if s and (ongoing or not e):
        return f"Da {s} ad Oggi" if ongoing else f"Da {s}"
    if s and e:
        return f"Da {s} a {e}"
    if e:
        return f"Fino a {e}"
    return ""


def _lines(text: str | None) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


_ACTIVITIES_LABEL = "Attività: "


def _activity_lines(text: str | None) -> list[str]:
    """Righe di attivita' con l'etichetta dei CV MC sulla prima ("Attività: ...")."""
    lines = _lines(text)
    if lines and not lines[0].lower().startswith(("attività", "attivita")):
        lines[0] = _ACTIVITIES_LABEL + lines[0]
    return lines


def _technologies(items: list[str]) -> str:
    joined = ", ".join(i.strip() for i in items if i and i.strip())
    return f"{joined}." if joined and not joined.endswith(".") else joined


def build_context(cv: CVExtraction | dict) -> dict[str, Any]:
    if not isinstance(cv, CVExtraction):
        cv = CVExtraction.model_validate(cv)

    if cv.education:
        formazione = [
            {"anno": _s(e.year), "titolo": _s(e.title), "istituto": _s(e.institution)}
            for e in cv.education
        ]
    else:
        formazione = [{"anno": "", "titolo": t, "istituto": ""} for t in cv.education_titles]

    esperienze = [
        {
            "periodo": _period(x.start_date, x.end_date, x.ongoing),
            "azienda": _s(x.company),
            "ruolo": _s(x.role),
            "righe": _activity_lines(x.description),
            "tecnologie": _technologies(x.technologies),
            "progetti": [
                {
                    "periodo": _period(p.start_date, p.end_date, p.ongoing),
                    "nome": _s(p.name),
                    # Il ruolo del progetto si mostra solo se diverso da quello del lavoro.
                    "ruolo": "" if _s(p.role).casefold() == _s(x.role).casefold() else _s(p.role),
                    "righe": _activity_lines(p.activities),
                    "tecnologie": _technologies(p.technologies),
                }
                for p in x.projects
            ],
        }
        for x in cv.experiences
    ]

    if cv.trainings:
        training = [
            {"anno": _s(t.year), "titolo": _s(t.title), "ente": _s(t.provider)}
            for t in cv.trainings
            if _s(t.title)
        ]
    else:
        training = [{"anno": "", "titolo": _s(c), "ente": ""} for c in cv.certifications if _s(c)]

    skills_src = cv.cv_skills or cv.skills
    skills = [{"nome": _s(s.name), "livello": _s(s.level)} for s in skills_src if _s(s.name)]

    lingue = [
        {"lingua": _s(l.lang)[:1].upper() + _s(l.lang)[1:], "livello": _s(l.level)}
        for l in cv.languages
        if _s(l.lang)
    ]

    return {
        "codice": _s(cv.trittico).upper(),
        "data_nascita": cv.birth_date.strftime("%d/%m/%Y") if cv.birth_date else "",
        "citta": _s(cv.location),
        # In alto il ruolo del catalogo MC (coerente con MCFlash); senza, quello scritto nel CV.
        "ruolo": display_role(cv.catalog_role) or _s(cv.role),
        "formazione": formazione,
        "training": training,
        "esperienze": esperienze,
        "skills": skills,
        "lingue": lingue,
    }


# Dati personali che restano sul record MCFlash e non entrano nel CV anonimo.
_NOT_IN_CV = ("email", "phone", "age", "first_name", "last_name", "full_name", "mcflash")


def context_from_form(data: dict[str, Any]) -> dict[str, Any]:
    """Contesto del modello dai dati del form della dashboard.

    Email, telefono, eta' e nome si tolgono prima della validazione: non vanno
    nel CV, e un'email scritta male nel form non deve bloccare il CV.
    """
    clean = {k: v for k, v in data.items() if k not in _NOT_IN_CV}
    try:
        cv = CVExtraction.model_validate(clean)
    except ValidationError as exc:
        parts = [f"{'.'.join(str(p) for p in err.get('loc', ()))}: {err.get('msg')}" for err in exc.errors()[:5]]
        raise InvalidInputError("Dati non validi per il CV: " + "; ".join(parts)) from exc
    return build_context(cv)


def render_docx(context: dict[str, Any], template_path: Path | str = TEMPLATE_PATH) -> bytes:
    from docxtpl import DocxTemplate  # import locale: serve solo a chi genera il CV

    tpl = DocxTemplate(str(template_path))
    # autoescape: testo dei CV con &, < e > non deve rompere l'XML del documento.
    tpl.render(context, autoescape=True)
    buf = io.BytesIO()
    tpl.save(buf)
    return buf.getvalue()
