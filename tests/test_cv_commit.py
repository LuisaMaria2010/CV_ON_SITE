"""Tests per la conferma delle bozze (services/cv_commit.py), MCFlash finto e record MCFlash."""
from __future__ import annotations

import asyncio
import json

import pytest
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

from domain.mcflash_record import build_record, languages_text, missing_for_create, seniority_from_extraction
from infra.mcflash_writer import FAKE_ID_START, FakeMCFlashWriter, overlay_records
from services import cv_commit

EDITED = {
    "first_name": "Mario", "last_name": "Rossi", "full_name": "Mario Rossi", "trittico": "MRO",
    "email": "mario@example.com", "phone": "+39 333", "age": 39, "location": "Torino",
    "catalog_role": "software developer", "role": "Senior Engineer", "birth_date": "1987-05-12",
    "languages": [{"lang": "inglese", "level": "Avanzato"}, {"lang": "francese", "level": None}],
    "experiences": [{"company": "ACME", "role": "Dev", "start_date": "2020-01-01", "ongoing": True}],
    "cv_skills": [{"name": "Python", "level": "Ottima conoscenza"}],
    "mcflash": {"seniority": "Senior", "disponibilita": "Immediata", "budget": "350", "site": "mcflashrisorse"},
}


# ---------------------------------------------------------------- record MCFlash

def test_build_record():
    record = build_record(EDITED)
    assert record["nome"] == "MRO" and record["nomi"] == "Mario Rossi"
    assert record["ruolo"] == "Software Developer"
    assert record["lingue"] == "Inglese: Avanzato, Francese"
    assert record["eta"] == 39 and record["budget"] == 350 and record["site"] == "mcflashrisorse"
    assert record["email"] == "mario@example.com" and record["telefono"] == "+39 333"
    assert missing_for_create(record) == []
    assert set(missing_for_create(build_record({"trittico": "MRO"}))) == {"nome e cognome", "ruolo", "seniority", "site"}


def test_small_helpers():
    assert seniority_from_extraction("mid") == "Medium" and seniority_from_extraction(None) is None
    assert languages_text([{"lang": "", "level": "Base"}]) == ""


# ---------------------------------------------------------------- MCFlash finto

class _Table:
    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}

    def query_entities(self, flt):
        return list(self.rows.values())

    def create_entity(self, entity):
        key = (entity["PartitionKey"], entity["RowKey"])
        if key in self.rows:
            raise ResourceExistsError("exists")
        self.rows[key] = dict(entity)

    def upsert_entity(self, entity, mode=None):
        self.rows[(entity["PartitionKey"], entity["RowKey"])] = dict(entity)

    def get_entity(self, pk, rk):
        try:
            return self.rows[(pk, rk)]
        except KeyError:
            raise ResourceNotFoundError("missing")

    def delete_entity(self, pk, rk):
        self.rows.pop((pk, rk), None)


def test_fake_writer_create_update_and_overlay():
    writer = FakeMCFlashWriter(_Table())
    first = writer.create(build_record(EDITED), user="tester")
    second = writer.create({"nome": "ABC", "nomi": "Anna Bianchi"})
    assert int(first) == FAKE_ID_START + 1 and int(second) == FAKE_ID_START + 2
    writer.update("42", {"sede": "Roma", "nome": None})
    writer.update(first, {"sede": "Milano"})
    records = [{"id": 42, "nome": "DCA", "nomi": "Davide Carli", "sede": "Torino", "abilitato": True}]
    merged = {str(r["id"]): r for r in overlay_records(records, writer.rows())}
    assert merged["42"]["sede"] == "Roma" and merged["42"]["nome"] == "DCA"  # None non sovrascrive
    created = merged[first]
    assert created["nomi"] == "Mario Rossi" and created["sede"] == "Milano" and created["finto"] is True
    assert "email" not in created and created["abilitato"] is True


# ---------------------------------------------------------------- conferma

class _Storage:
    def __init__(self):
        self.blobs: dict[tuple[str, str], bytes] = {}

    async def upload_bytes(self, data, blob_name, folder=None, container=None):
        self.blobs[(container, blob_name)] = data

    async def download_bytes(self, blob_name, folder=None, container=None):
        try:
            return self.blobs[(container, blob_name)]
        except KeyError:
            raise ResourceNotFoundError("missing")

    async def delete(self, blob_name, folder=None, container=None):
        self.blobs.pop((container, blob_name), None)

    async def list_names(self, container=None, prefix=None):
        return [n for (c, n) in self.blobs if c == container]


class _Writer:
    mode = "finto"

    def __init__(self, fail=False):
        self.fail, self.created, self.updated = fail, [], []

    def create(self, record, *, user=None):
        if self.fail:
            raise RuntimeError("MCFlash giù")
        self.created.append(record)
        return "900001"

    def update(self, id_mcflash, record, *, user=None):
        if self.fail:
            raise RuntimeError("MCFlash giù")
        self.updated.append((id_mcflash, record))


class _World:
    """Storage, registro, mappa e MCFlash in memoria."""

    def __init__(self, writer=None):
        self.storage = _Storage()
        self.writer = writer or _Writer()
        self.docs: dict[str, dict] = {}
        self.map: dict[str, dict] = {}
        self.saved: list[dict] = []

    def deps(self):
        return cv_commit.CommitDeps(
            storage=self.storage, writer=self.writer, registry_docs=lambda: self.docs,
            normalize_doc_id=lambda d: (d or "").strip().lower().replace(" ", "-"),
            map_entries=lambda: [{"document_id": k, **v} for k, v in self.map.items()],
            map_upsert=lambda doc, idm, file, codice="": self.map.__setitem__(doc, {"id_mcflash": str(idm), "file": file}),
            map_delete=lambda doc: self.map.pop(doc, None),
            save_op=lambda op: self.saved.append(dict(op)),
        )

    def index(self, op):
        """Simula l'ingestione: il nuovo file risulta indicizzato, con una versione in piu'."""
        version = int((self.docs.get(op["document_id"]) or {}).get("version") or 0) + 1
        self.docs[op["document_id"]] = {"version": version, "status": "processed",
                                        "source_path": f"/incoming-cv/{op['incoming_blob']}"}


def _draft(kind="crea", id_mcflash=None, filename="Skill Tecnico_MRO_MC Engineering.pdf"):
    return {"draft_id": "d" * 32, "kind": kind, "id_mcflash": id_mcflash, "filename": filename,
            "original_blob": "2026/10/05/x_cv.docx", "edited": EDITED}


def _run(op, draft, world):
    return asyncio.run(cv_commit.advance(op, draft, world.deps()))


def test_create_runs_all_steps_and_waits_for_indexing():
    world, draft = _World(), _draft()
    op = cv_commit.new_operation("o" * 32, draft, "tester")
    op = _run(op, draft, world)
    assert op["status"] == "in_corso" and op["step_mappa"] == "in_attesa"
    assert [op[f"step_{s}"] for s in ("blob", "mcflash", "indice")] == ["ok", "ok", "ok"]
    assert op["id_mcflash"] == "900001" and op["original_blob"] == "2026/10/05/x_cv.docx"
    assert op["incoming_blob"] == "Skill Tecnico_MRO_MC Engineering.docx"
    assert op["document_id"] == "skill-tecnico_mro_mc-engineering"
    assert ("incoming-cv", "Skill Tecnico_MRO_MC Engineering.docx") in world.storage.blobs
    assert world.map == {}
    world.index(op)
    op = _run(op, draft, world)
    assert op["status"] == "completata" and op["completed_at"]
    assert world.map[op["document_id"]] == {"id_mcflash": "900001", "file": "Skill Tecnico_MRO_MC Engineering.docx"}
    assert len(world.writer.created) == 1  # nessun secondo candidato


def test_mcflash_failure_leaves_index_untouched_and_retry_resumes():
    world, draft = _World(_Writer(fail=True)), _draft()
    op = _run(cv_commit.new_operation("o" * 32, draft, None), draft, world)
    assert op["status"] == "fallita" and op["error_step"] == "mcflash" and "MCFlash giù" in op["error"]
    assert op["step_indice"] == "da_fare" and not any(c == "incoming-cv" for c, _ in world.storage.blobs)
    world.writer.fail = False
    op = _run(op, draft, world)
    assert op["step_mcflash"] == "ok" and op["step_indice"] == "ok" and op["error"] == ""


def test_create_refuses_a_file_name_used_by_another_cv():
    world, draft = _World(), _draft()
    world.storage.blobs[("incoming-cv", "Skill Tecnico_MRO_MC Engineering.pdf")] = b"altro"
    op = _run(cv_commit.new_operation("o" * 32, draft, None), draft, world)
    assert op["status"] == "fallita" and op["error_step"] == "indice" and "già usato" in op["error"]


def test_create_needs_mcflash_fields():
    world = _World()
    draft = {**_draft(), "edited": {**EDITED, "mcflash": {}}}
    op = _run(cv_commit.new_operation("o" * 32, draft, None), draft, world)
    assert op["error_step"] == "mcflash" and "seniority" in op["error"] and not world.writer.created


def test_update_keeps_history_and_replaces_the_cv():
    world = _World()
    world.storage.blobs[("incoming-cv", "Skill Tecnico_MRO_MC.pdf")] = b"vecchio CV"
    world.map["skill-tecnico_mro_mc"] = {"id_mcflash": "1884", "file": "Skill Tecnico_MRO_MC.pdf"}
    world.docs["skill-tecnico_mro_mc"] = {"version": 3, "status": "processed",
                                          "source_path": "/incoming-cv/Skill Tecnico_MRO_MC.pdf"}
    draft = _draft("aggiorna", "1884", filename="Skill Tecnico_MRO_MC.pdf")
    op = _run(cv_commit.new_operation("o" * 32, draft, None), draft, world)
    assert world.writer.updated and world.writer.updated[0][0] == "1884"
    history = [(c, n) for (c, n) in world.storage.blobs if c == "cv-history"]
    assert len(history) == 1 and history[0][1].startswith("1884/") and history[0][1].endswith("_Skill Tecnico_MRO_MC.pdf")
    assert ("incoming-cv", "Skill Tecnico_MRO_MC.pdf") not in world.storage.blobs   # tolto, e' nello storico
    assert ("incoming-cv", "Skill Tecnico_MRO_MC.docx") in world.storage.blobs
    assert op["document_id"] == "skill-tecnico_mro_mc"  # stesso documento: l'indice lo sostituisce
    # stesso nome del file: la riga della v3 e' gia' "processed", ma non e' il nuovo CV
    assert op["previous_version"] == "3" and op["step_mappa"] == "in_attesa"
    world.docs["skill-tecnico_mro_mc"]["source_path"] = "/incoming-cv/Skill Tecnico_MRO_MC.docx"
    assert _run(op, draft, world)["step_mappa"] == "in_attesa"
    world.index(op)
    op = _run(op, draft, world)
    assert op["status"] == "completata"
    assert world.map == {"skill-tecnico_mro_mc": {"id_mcflash": "1884", "file": "Skill Tecnico_MRO_MC.docx"}}


def test_update_with_new_name_leaves_one_cv_per_candidate():
    world = _World()
    world.map["skill-tecnico_mro_mc"] = {"id_mcflash": "1884", "file": "Skill Tecnico_MRO_MC.pdf"}
    draft = _draft("aggiorna", "1884", filename="Skill_Tecnico_MRO_MC_Engineering.pdf")
    op = _run(cv_commit.new_operation("o" * 32, draft, None), draft, world)
    world.index(op)
    op = _run(op, draft, world)
    assert op["status"] == "completata"
    assert list(world.map) == ["skill_tecnico_mro_mc_engineering"]


def test_steps_are_saved_as_they_complete():
    world, draft = _World(), _draft()
    _run(cv_commit.new_operation("o" * 32, draft, None), draft, world)
    # l'Id MCFlash e' salvato prima di passare all'indice
    assert any(s.get("id_mcflash") == "900001" and s.get("step_indice") == "da_fare" for s in world.saved)
    assert json.dumps(world.saved[-1])  # serializzabile


def test_steps_are_logged_with_operation_id(caplog):
    import logging

    from utils.request_context import request_context

    world, draft = _World(), _draft()
    with caplog.at_level(logging.INFO, logger="utils.observability"), request_context("req-123"):
        op = _run(cv_commit.new_operation("o" * 32, draft, None), draft, world)
    events = [r.getMessage() for r in caplog.records if "cv_commit_step" in r.getMessage()]
    assert len(events) == 4  # blob, mcflash, indice ok; mappa in attesa
    assert all("'operation_id': '" + "o" * 32 + "'" in e and "req-123" in e for e in events)
    assert op["last_request_id"] == "req-123"
