from __future__ import annotations

import abc
from typing import Any


class AnalysisProvider(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    def analyze(self, article: dict, corpus: Any) -> tuple[list[dict], dict]:
        """(findings, explanations). findings는 review.schema의 finding에서 서버가 채우는 값
        (evidence[].verified, detailed_explanation_ref)을 뺀 형태, explanations는 {finding_id: {...}}."""
