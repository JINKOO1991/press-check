"""수집(ingestion) 패키지: P1 접근 범위와 권리 장부. 운영 CLI 전용이며 MCP 도구로 노출하지 않는다.

구성: ``http``(공용 HTTP 계층) · ``robots``(robots.txt 정책) · ``manifest``(SQLite 장부) · ``context``(실행 컨텍스트와 시간 상한) ·
``rights``(권리 기본값 unknown, 설치자 결정 기록) · ``sources/<name>.py``(어댑터). 실행: ``python -m press_review.ingestion --help``.
"""
