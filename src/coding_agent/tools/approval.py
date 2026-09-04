from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Literal


ApprovalDecision = Literal["approved", "denied", "required"]


@dataclass(frozen=True)
class WritePreview:
    """The information shown to a user before a write is performed."""

    operation: str
    path: str
    content: str | None = None
    old_text: str | None = None
    new_text: str | None = None
    existed: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class WriteApprovalGate:
    """Require an explicit decision for each write-tool invocation."""

    def __init__(
        self,
        ask: Callable[[WritePreview], bool] | None = None,
        record: Callable[..., None] | None = None,
    ) -> None:
        self._ask = ask
        self._record = record

    def approve(self, preview: WritePreview) -> ApprovalDecision:
        if self._record is not None:
            self._record(
                "approval_request",
                operation=preview.operation,
                path=preview.path,
                existed=preview.existed,
                report_payload={"preview": preview.as_dict()},
            )
        if self._ask is None:
            decision: ApprovalDecision = "required"
        else:
            try:
                decision = "approved" if self._ask(preview) else "denied"
            except Exception:
                decision = "required"
        if self._record is not None:
            self._record(
                "approval_result",
                operation=preview.operation,
                path=preview.path,
                decision=decision,
            )
        return decision
