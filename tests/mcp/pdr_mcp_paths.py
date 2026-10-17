"""MCP 테스트 공용 경로. tests/engine 의 모듈과 이름이 겹치지 않게 별도로 둔다."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ENGINE = REPO / "plugins" / "press-dispute-review" / "engine"
ARTICLE_FIXTURE = ENGINE / "press_review" / "fixtures" / "article.example.json"

if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))
