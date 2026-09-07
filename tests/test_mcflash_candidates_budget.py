from __future__ import annotations

import pytest

from infra.mcflash_candidates import MCFlashCandidatesClient


class _DummyMCFlashClient(MCFlashCandidatesClient):
    def __init__(self, records: list[dict]):
        super().__init__(base_url="https://example.test", api_key="test")
        self._records = records

    async def fetch_page(self, *, limit: int, offset: int) -> list[dict]:
        start = max(0, int(offset))
        end = start + max(1, int(limit))
        return self._records[start:end]


@pytest.mark.asyncio
async def test_numeric_budget_is_hard_cap_even_without_role_and_seniority():
    client = _DummyMCFlashClient(
        [
            {"Id": "1", "Budget": "300", "Ruolo": "Java Developer", "Seniority": "senior"},
            {"Id": "2", "Budget": "420", "Ruolo": "Java Developer", "Seniority": "senior"},
        ]
    )

    items = await client.filter_candidates(
        limit=20,
        offset=0,
        budget="350",
    )

    assert [row.get("Id") for row in items] == ["1"]


@pytest.mark.asyncio
async def test_qualitative_budget_requires_role_and_seniority_for_placeholder_filter():
    client = _DummyMCFlashClient(
        [
            {"Id": "1", "Budget": "250", "Ruolo": "Java Developer", "Seniority": "senior"},
            {"Id": "2", "Budget": "420", "Ruolo": "Java Developer", "Seniority": "senior"},
        ]
    )

    items = await client.filter_candidates(
        limit=20,
        offset=0,
        role="java developer",
        seniority="senior",
        budget="contenuto",
    )

    # java+senior placeholder cap is 360 -> "contenuto" (low-direction qualifier)
    # tightens it by 20% to 288, so only candidate 1 survives.
    assert [row.get("Id") for row in items] == ["1"]


@pytest.mark.asyncio
async def test_qualitative_budget_without_role_or_seniority_does_not_filter():
    client = _DummyMCFlashClient(
        [
            {"Id": "1", "Budget": "300", "Ruolo": "Java Developer", "Seniority": "senior"},
            {"Id": "2", "Budget": "420", "Ruolo": "Java Developer", "Seniority": "senior"},
        ]
    )

    items = await client.filter_candidates(
        limit=20,
        offset=0,
        budget="basso",
    )

    assert [row.get("Id") for row in items] == ["1", "2"]


@pytest.mark.asyncio
async def test_qualitative_budget_uses_composite_role_placeholder_tokens():
    old_table = MCFlashCandidatesClient._ROLE_BUDGET_PLACEHOLDER_TABLE
    old_reduced = MCFlashCandidatesClient._ROLE_BUDGET_PLACEHOLDER_REDUCED
    old_dummy_table = _DummyMCFlashClient._ROLE_BUDGET_PLACEHOLDER_TABLE
    old_dummy_reduced = _DummyMCFlashClient._ROLE_BUDGET_PLACEHOLDER_REDUCED
    try:
        # Only composite role exists in table; request uses one role token.
        MCFlashCandidatesClient._ROLE_BUDGET_PLACEHOLDER_TABLE = {
            "qa guru/dev lead": {
                "senior": 500.0,
            }
        }
        MCFlashCandidatesClient._ROLE_BUDGET_PLACEHOLDER_REDUCED = None
        _DummyMCFlashClient._ROLE_BUDGET_PLACEHOLDER_TABLE = MCFlashCandidatesClient._ROLE_BUDGET_PLACEHOLDER_TABLE
        _DummyMCFlashClient._ROLE_BUDGET_PLACEHOLDER_REDUCED = None

        client = _DummyMCFlashClient(
            [
                # "contenuto" is a low-direction qualifier, so the 500 composite
                # placeholder cap is tightened by 20% (400) before filtering.
                {"Id": "1", "Budget": "350", "Ruolo": "QA Guru", "Seniority": "senior"},
                {"Id": "2", "Budget": "520", "Ruolo": "QA Guru", "Seniority": "senior"},
            ]
        )

        items = await client.filter_candidates(
            limit=20,
            offset=0,
            role="qa guru",
            seniority="senior",
            budget="contenuto",
        )

        assert [row.get("Id") for row in items] == ["1"]
    finally:
        MCFlashCandidatesClient._ROLE_BUDGET_PLACEHOLDER_TABLE = old_table
        MCFlashCandidatesClient._ROLE_BUDGET_PLACEHOLDER_REDUCED = old_reduced
        _DummyMCFlashClient._ROLE_BUDGET_PLACEHOLDER_TABLE = old_dummy_table
        _DummyMCFlashClient._ROLE_BUDGET_PLACEHOLDER_REDUCED = old_dummy_reduced


@pytest.mark.asyncio
async def test_qualitative_budget_low_phrase_tightens_placeholder_cap():
    client = _DummyMCFlashClient(
        [
            {"Id": "1", "Budget": "250", "Ruolo": "Java Developer", "Seniority": "senior"},
            {"Id": "2", "Budget": "300", "Ruolo": "Java Developer", "Seniority": "senior"},
        ]
    )

    # java+senior placeholder cap is 360 -> "non troppo elevato" tightens it to 288.
    items = await client.filter_candidates(
        limit=20,
        offset=0,
        role="java developer",
        seniority="senior",
        budget="non troppo elevato",
    )

    assert [row.get("Id") for row in items] == ["1"]


@pytest.mark.asyncio
async def test_qualitative_budget_high_phrase_loosens_placeholder_cap():
    client = _DummyMCFlashClient(
        [
            {"Id": "1", "Budget": "300", "Ruolo": "Java Developer", "Seniority": "senior"},
            {"Id": "2", "Budget": "420", "Ruolo": "Java Developer", "Seniority": "senior"},
        ]
    )

    # java+senior placeholder cap is 360 -> "importante" loosens it by 20% to 432,
    # so candidate 2 (420), excluded under the neutral/low cap, now clears the bar too.
    items = await client.filter_candidates(
        limit=20,
        offset=0,
        role="java developer",
        seniority="senior",
        budget="budget importante",
    )

    assert sorted(row.get("Id") for row in items) == ["1", "2"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("non troppo elevato", "low"),
        ("budget contenuto", "low"),
        ("basso", "low"),
        ("budget alto", "high"),
        ("molto elevato", "high"),
        ("importante", "high"),
        ("50000 euro", None),
        ("", None),
    ],
)
def test_classify_budget_qualifier(text, expected):
    assert MCFlashCandidatesClient._classify_budget_qualifier(text) == expected


@pytest.mark.asyncio
async def test_find_candidate_by_id_short_circuits_even_with_name_homonyms():
    client = _DummyMCFlashClient(
        [
            {"Id": "1", "Nome": "Mario Rossi", "Ruolo": "Java Developer"},
            {"Id": "2", "Nome": "Mario Rossi", "Ruolo": "Data Analyst"},
        ]
    )

    found = await client.find_candidate("2")

    assert found is not None and found.get("Id") == "2"


@pytest.mark.asyncio
async def test_find_candidates_returns_all_homonyms_by_name():
    client = _DummyMCFlashClient(
        [
            {"Id": "1", "Nome": "Mario Rossi", "Ruolo": "Java Developer"},
            {"Id": "2", "Nome": "Mario Rossi", "Ruolo": "Data Analyst"},
            {"Id": "3", "Nome": "Altra Persona", "Ruolo": "PM"},
        ]
    )

    matches = await client.find_candidates("Mario Rossi")

    assert sorted(row.get("Id") for row in matches) == ["1", "2"]


@pytest.mark.asyncio
async def test_find_candidate_falls_back_to_first_homonym_for_backward_compat():
    client = _DummyMCFlashClient(
        [
            {"Id": "1", "Nome": "Mario Rossi", "Ruolo": "Java Developer"},
            {"Id": "2", "Nome": "Mario Rossi", "Ruolo": "Data Analyst"},
        ]
    )

    found = await client.find_candidate("Mario Rossi")

    assert found is not None and found.get("Id") == "1"


@pytest.mark.asyncio
async def test_find_candidates_empty_for_unknown_key():
    client = _DummyMCFlashClient(
        [{"Id": "1", "Nome": "Mario Rossi", "Ruolo": "Java Developer"}]
    )

    assert await client.find_candidates("Nessuno") == []
    assert await client.find_candidate("Nessuno") is None
