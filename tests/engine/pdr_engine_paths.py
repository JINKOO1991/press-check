"""엔진 테스트 공용 경로. 다른 테스트 폴더의 conftest 와 이름이 겹치지 않게 별도 모듈로 둔다."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ENGINE = REPO / "plugins" / "press-check" / "engine"
CONTRACTS = REPO / "docs" / "handoff" / "contracts"
ARTICLE_FIXTURE = ENGINE / "press_review" / "fixtures" / "article.example.json"

# 엔진 루트(engine/)를 path에 둔다. engine/press_review 를 직접 넣으면 secrets.py 가 표준 secrets 를 가린다.
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))
