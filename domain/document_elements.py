from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class DocumentElement:
    element_type: str
    text: str = ""
    level: int = 0
    list_ordered: bool = False
    rows: list[list[str]] = field(default_factory=list)
    page_number: int | None = None
    vertical_position: float | None = None
    horizontal_position: float | None = None
