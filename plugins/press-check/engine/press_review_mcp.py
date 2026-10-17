"""MCP 서버 기동 스크립트. ``python -m`` 대신 스크립트로 실행해 작업 폴더가 sys.path 앞에 들어가지 않게 한다."""
from press_review.mcp_server import main

raise SystemExit(main())
