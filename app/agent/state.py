from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentState:
    question: str
    tool_results: list[Any] = field(default_factory=list)
    called_signatures: set[str] = field(default_factory=set)
    steps: int = 0

    def already_called(self, signature: str) -> bool:
        return signature in self.called_signatures

    def mark_called(self, signature: str) -> None:
        self.called_signatures.add(signature)
