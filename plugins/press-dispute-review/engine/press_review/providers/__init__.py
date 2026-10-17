"""분석 제공자. P0 구현은 MockProvider(합성 예제 전용 규칙 기반)뿐이다.

외부 API 제공자는 P3 이후, 계약·정책 확인 뒤에만 추가한다. 미공개 원고는 제3 제공자로 보내지 않는다.
"""
from .base import AnalysisProvider
from .mock import MockProvider

PROVIDERS = {"mock": MockProvider}

__all__ = ["AnalysisProvider", "MockProvider", "PROVIDERS"]
