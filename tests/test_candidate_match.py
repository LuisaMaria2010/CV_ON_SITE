"""Tests per la riga candidato, la ricerca e il match trittico + nome (domain/candidate_match)."""
from __future__ import annotations

import pytest

from domain.candidate_match import (
    ESITO_AGGIORNA,
    ESITO_NUOVO,
    ESITO_SCEGLI,
    candidate_row,
    filter_rows,
    match_candidate,
    normalize_text,
    same_person_name,
)


def _row(id_, tri, name, *, sede="Torino", ruolo="Sviluppatore Java", cv=None, eligible=True):
    record = {"id": id_, "nome": tri, "nomi": name, "sede": sede, "ruolo": ruolo,
              "eta": 30, "abilitato": True, "isTalentUp": False}
    return candidate_row(record, cv, eligible=eligible)


def test_candidate_row_maps_mcflash_fields():
    record = {"Id": 1884, "Nome": "dca", "Nomi": " Davide Carli ", "Ruolo": "PM", "isTalentUp": False}
    row = candidate_row(record, {"file": "x.pdf"}, eligible=True)
    assert row["id"] == "1884"
    assert row["trittico"] == "DCA"
    assert row["nome_completo"] == "Davide Carli"
    assert row["ruolo"] == "PM"
    assert row["is_talent_up"] is False
    assert row["cv"] == {"file": "x.pdf"}


@pytest.mark.parametrize("a, b", [
    ("Mario Rossi", "mario  rossi"),
    ("Niccolò Pàstore", "Niccolo Pastore"),
    ("Rossi Mario", "Mario Rossi"),
    ("Luca D'Amico", "luca d amico"),
])
def test_same_person_name(a, b):
    assert same_person_name(a, b)


@pytest.mark.parametrize("a, b", [("Mario Rossi", "Maria Rossi"), ("", ""), ("Mario Rossi", "Mario")])
def test_different_person_name(a, b):
    assert not same_person_name(a, b)


def test_normalize_text():
    assert normalize_text("  ÀnGelo   D'Amico ") == "angelo d amico"


def test_match_no_trittico_is_new():
    rows = [_row("1", "MRO", "Mario Rossi")]
    out = match_candidate("DCA", "Davide Carli", rows)
    assert out["esito"] == ESITO_NUOVO
    assert out["candidati"] == [] and out["stesso_trittico"] == []


def test_match_one_trittico_same_name_updates():
    rows = [_row("1", "DCA", "Davide Carli"), _row("2", "MRO", "Mario Rossi")]
    out = match_candidate("dca", "DAVIDE CARLI", rows)
    assert out["esito"] == ESITO_AGGIORNA
    assert [r["id"] for r in out["candidati"]] == ["1"]


def test_match_one_trittico_other_name_is_new_homonym():
    rows = [_row("1", "DCA", "Davide Carli")]
    out = match_candidate("DCA", "Daniela Caruso", rows)
    assert out["esito"] == ESITO_NUOVO
    assert out["candidati"] == []
    assert [r["id"] for r in out["stesso_trittico"]] == ["1"]


def test_match_many_trittici_one_name_updates():
    rows = [_row("1", "DCA", "Davide Carli"), _row("2", "DCA", "Daniela Caruso"), _row("3", "DCA", "Dario Cavallo")]
    out = match_candidate("DCA", "Daniela Caruso", rows)
    assert out["esito"] == ESITO_AGGIORNA
    assert [r["id"] for r in out["candidati"]] == ["2"]
    assert len(out["stesso_trittico"]) == 3


def test_match_many_trittici_no_name_is_new():
    rows = [_row("1", "DCA", "Davide Carli"), _row("2", "DCA", "Daniela Caruso")]
    assert match_candidate("DCA", "Diego Casini", rows)["esito"] == ESITO_NUOVO


def test_match_homonyms_user_chooses():
    rows = [_row("1", "MRO", "Mario Rossi", sede="Torino"), _row("2", "MRO", "Mario Rossi", sede="Roma"),
            _row("3", "MRO", "Marco Romano")]
    out = match_candidate("MRO", "Mario Rossi", rows)
    assert out["esito"] == ESITO_SCEGLI
    assert [r["id"] for r in out["candidati"]] == ["1", "2"]


def test_match_name_with_accents_and_order():
    rows = [_row("1", "NPA", "PÀSTORE Niccolò")]
    assert match_candidate("NPA", "Niccolo Pastore", rows)["esito"] == ESITO_AGGIORNA


def test_match_same_name_with_other_trittico_asks_user():
    # Trittico su MCFlash scritto a mano, diverso da quello calcolato dal nome.
    rows = [_row("1", "MGR", "Maria Grazia Rossi"), _row("2", "MRO", "Marco Romano")]
    out = match_candidate("MRO", "Maria Grazia Rossi", rows)
    assert out["esito"] == ESITO_SCEGLI
    assert [r["id"] for r in out["candidati"]] == ["1"]
    assert [r["id"] for r in out["stesso_trittico"]] == ["2"]


def test_filter_rows_query_sede_and_cv():
    rows = [
        _row("1", "DCA", "Davide Carli", sede="Torino", cv={"file": "a.pdf"}),
        _row("2", "MRO", "Mario Rossi", sede="Roma", ruolo="Data Engineer"),
        _row("3", "NPA", "Niccolò Pastore", sede="Torino / remoto"),
    ]
    assert [r["id"] for r in filter_rows(rows, query="niccolo")] == ["3"]
    assert [r["id"] for r in filter_rows(rows, query="data rossi")] == ["2"]
    assert [r["id"] for r in filter_rows(rows, query="dca")] == ["1"]
    assert [r["id"] for r in filter_rows(rows, sede="torino")] == ["1", "3"]
    assert [r["id"] for r in filter_rows(rows, has_cv=True)] == ["1"]
    assert [r["id"] for r in filter_rows(rows, has_cv=False, sede="torino")] == ["3"]
    assert len(filter_rows(rows)) == 3


@pytest.mark.parametrize("a, b", [("Luca D'Amico", "Luca Damico"), ("Mario De Luca", "Mario Deluca")])
def test_same_person_name_ignores_spaces_inside_surname(a, b):
    assert same_person_name(a, b)


@pytest.mark.parametrize("cv_name, mcflash_name", [
    ("Maria Grazia Rossi", "Maria Rossi"),
    ("Maria Rossi", "Maria Grazia Rossi"),
])
def test_match_partial_name_same_trittico_asks_user(cv_name, mcflash_name):
    rows = [_row("1", "MRO", mcflash_name), _row("2", "MRO", "Marco Romano")]
    out = match_candidate("MRO", cv_name, rows)
    assert out["esito"] == ESITO_SCEGLI
    assert [r["id"] for r in out["candidati"]] == ["1"]


def test_partial_name_needs_two_shared_words():
    rows = [_row("1", "MRO", "Mario Rossi")]
    assert match_candidate("MRO", "Mario", rows)["esito"] == ESITO_NUOVO
