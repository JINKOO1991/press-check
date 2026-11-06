"""press_review: 한국 언론분쟁 예방 검토 엔진(결정적 검사 + 근거 검색).

런타임 의존성은 표준 라이브러리뿐이다. 기사 분석(문제 후보·수정안 생성)은 호스트 모델의 몫이고,
이 패키지는 위치·해시·근거·수정안 검사와 렌더링만 한다.
"""

from pkgutil import extend_path

# 개발자는 PYTHONPATH 에 devtools/engine 을 더해 같은 이름의 press_review.ingestion 을 쓴다(플러그인 폴더에는 수집 코드가 없다).
__path__ = extend_path(__path__, __name__)

__version__ = "0.1.0"
