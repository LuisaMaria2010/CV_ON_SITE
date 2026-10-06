"""Anteprima HTML del CV nel modello MCFlash, per la dashboard.

Usa lo stesso contesto di services/cv_mcflash_renderer.build_context, quindi
contenuti e ordine delle sezioni sono quelli del .docx; cambia solo
l'impaginazione (una pagina continua, senza salti pagina). Layout e colori
seguono scripts/build_mcflash_template.py: barre grigie con il titolo blu in
maiuscoletto, etichette in corsivo a destra, valori a sinistra.

check_context() elenca cio' che conviene completare prima di confermare.
"""
from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment

ASSETS = Path(__file__).resolve().parent.parent / "templates" / "assets"

_TEMPLATE = """
<style>
.mc-cv { font-family: Calibri, Carlito, "Segoe UI", Arial, sans-serif; color: #2C2C2C; }
.mc-cv-page { background: #FFFFFF; border: 1px solid #D3DCEB; border-radius: 6px;
  box-shadow: 0 2px 10px rgba(31, 56, 100, .12); padding: 18px 26px 0 30px; font-size: 13px; line-height: 1.35; }
.mc-cv-head { display: flex; justify-content: space-between; align-items: flex-end; }
.mc-cv-head .logo { width: 52%; } .mc-cv-head .fig { width: 17%; }
.mc-cv-title { text-align: center; color: #1F3864; font-family: Bahnschrift, "Segoe UI", Arial, sans-serif;
  font-variant: small-caps; font-weight: 700; font-size: 20px; text-decoration: underline; margin: 4px 0 6px; }
.mc-cv-bar { background: #CCCCCC; color: #1F3864; font-family: Bahnschrift, "Segoe UI", Arial, sans-serif;
  font-variant: small-caps; font-weight: 700; font-size: 16px; padding: 1px 6px; margin: 12px 0 6px; }
.mc-cv table { border-collapse: collapse; width: 100%; }
.mc-cv td { vertical-align: top; padding: 1px 0 3px; }
.mc-cv td.lbl { width: 24%; font-style: italic; text-align: right; padding-right: 14px; }
.mc-cv .code { font-weight: 700; font-size: 16px; }
.mc-cv .title { font-weight: 700; font-size: 12.5px; color: #2C2C2C; }
.mc-cv .row-gap td { padding-bottom: 8px; }
.mc-cv .just { text-align: justify; }
.mc-cv .prj { margin-top: 7px; }
.mc-cv-foot { margin: 16px -26px 0 -30px; } .mc-cv-foot img { width: 100%; display: block; }
</style>
<div class="mc-cv"><div class="mc-cv-page">
<div class="mc-cv-head"><img class="logo" src="{{ img.logo }}" alt="MC Engineering"><img class="fig" src="{{ img.fig }}" alt=""></div>
<div class="mc-cv-title">Candidate Profile</div>

<div class="mc-cv-bar">Information</div>
<table>
<tr><td class="lbl">Nome</td><td><span class="code">{{ c.codice }}</span></td></tr>
<tr><td class="lbl">Data di Nascita</td><td>{{ c.data_nascita }}</td></tr>
<tr><td class="lbl">Città</td><td>{{ c.citta }}</td></tr>
<tr><td class="lbl">Ruolo</td><td><b>{{ c.ruolo }}</b></td></tr>
</table>

{% if c.formazione %}<div class="mc-cv-bar">Education</div><table>
{% for e in c.formazione %}<tr class="row-gap"><td class="lbl">{{ e.anno }}</td><td><div class="title">{{ e.titolo }}</div>{{ e.istituto }}</td></tr>{% endfor %}
</table>{% endif %}

{% if c.training %}<div class="mc-cv-bar">Trainings</div><table>
{% for t in c.training %}<tr class="row-gap"><td class="lbl">{{ t.anno }}</td><td><div class="title">{{ t.titolo }}</div>{% if t.ente %}{{ t.ente }}{% endif %}</td></tr>{% endfor %}
</table>{% endif %}

{% if c.esperienze %}<div class="mc-cv-bar">Work Experiences</div><table>
{% for x in c.esperienze %}<tr class="row-gap"><td class="lbl">{{ x.periodo }}</td><td>
<b>{{ x.azienda }}</b>
{% if x.ruolo %}<div>Ruolo: {{ x.ruolo }}</div>{% endif %}
{% for riga in x.righe %}<div class="just">{{ riga }}</div>{% endfor %}
{% if x.tecnologie %}<div class="just">Tecnologie utilizzate: {{ x.tecnologie }}</div>{% endif %}
{% for pr in x.progetti %}<div class="prj">
{% if pr.periodo %}<div>{{ pr.periodo }}</div>{% endif %}
<div>{{ pr.nome }}</div>
{% if pr.ruolo %}<div>Ruolo: {{ pr.ruolo }}</div>{% endif %}
{% for riga in pr.righe %}<div class="just">{{ riga }}</div>{% endfor %}
{% if pr.tecnologie %}<div class="just">Tecnologie utilizzate: {{ pr.tecnologie }}</div>{% endif %}
</div>{% endfor %}
</td></tr>{% endfor %}
</table>{% endif %}

{% if c.skills %}<div class="mc-cv-bar">Synthesis of Technical Skills</div><table>
{% for s in c.skills %}<tr><td class="lbl">{{ s.nome }}</td><td>{{ s.livello }}</td></tr>{% endfor %}
</table>{% endif %}

{% if c.lingue %}<div class="mc-cv-bar">Languages</div><table>
{% for l in c.lingue %}<tr><td class="lbl">{{ l.lingua }}</td><td><b>{{ l.livello }}</b></td></tr>{% endfor %}
</table>{% endif %}

<div class="mc-cv-foot"><img src="{{ img.footer }}" alt="MC Engineering, sedi"></div>
</div></div>
"""

_env = Environment(autoescape=True, trim_blocks=True, lstrip_blocks=True)
_template = _env.from_string(_TEMPLATE)


@lru_cache(maxsize=1)
def _images() -> dict[str, str]:
    def data_uri(name: str) -> str:
        return "data:image/png;base64," + base64.b64encode((ASSETS / name).read_bytes()).decode("ascii")

    return {"logo": data_uri("mc_logo.png"), "fig": data_uri("mc_vitruviano.png"),
            "footer": data_uri("mc_footer_sedi.png")}


def render_html(context: dict[str, Any]) -> str:
    """HTML autonomo (stile e immagini inclusi) del CV MC. Il testo e' escapato."""
    return _template.render(c=context, img=_images())


def check_context(context: dict[str, Any]) -> list[str]:
    """Cosa manca nel CV MC: avvisi da mostrare accanto all'anteprima."""
    issues: list[str] = []
    if not context.get("codice"):
        issues.append("Manca il trittico: servono nome e cognome.")
    if not context.get("ruolo"):
        issues.append("Manca il ruolo in alto: scegli un ruolo del catalogo MC.")
    if not context.get("data_nascita"):
        issues.append("Manca la data di nascita.")
    if not context.get("citta"):
        issues.append("Manca la città.")
    if not context.get("esperienze"):
        issues.append("Nessuna esperienza lavorativa.")
    for x in context.get("esperienze") or []:
        name = x.get("azienda") or "un'esperienza"
        if not x.get("ruolo"):
            issues.append(f"{name}: manca il ruolo.")
        if not x.get("periodo"):
            issues.append(f"{name}: manca il periodo.")
        undated = [p.get("nome") or "senza nome" for p in x.get("progetti") or [] if not p.get("periodo")]
        if undated:
            issues.append(f"{name}: progetti senza date ({', '.join(undated)}).")
    no_level = [s["nome"] for s in context.get("skills") or [] if not s.get("livello")]
    if no_level:
        issues.append(f"Skill senza livello: {', '.join(no_level)}.")
    no_year = [t["titolo"] for t in context.get("training") or [] if not t.get("anno")]
    if no_year:
        issues.append(f"Trainings senza anno: {', '.join(no_year)}.")
    return issues
