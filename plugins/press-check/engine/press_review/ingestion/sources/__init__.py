"""수집 어댑터 패키지. 어댑터 계약(병렬 워커는 CLI 파일을 고치지 않는다).

파일 ``sources/<name>.py`` (이름은 ``^[a-z][a-z0-9_]*$``) 에 다음을 둔다::

    def add_arguments(parser): ...   # 선택. 어댑터 전용 플래그를 argparse 파서에 추가한다.
    def run(ctx, args) -> dict | None: ...   # 필수. 반환 dict는 CLI가 JSON 한 줄에 'result'로 싣는다(건수·경로만).

``python -m press_review.ingestion <name> [--max-seconds N] [--refresh] [--limit-pages N] [--dry-run]`` 가 ``run(ctx, args)`` 를 부른다.

``ctx`` (``press_review.ingestion.context.RunContext``)
  - ``ctx.http``: ``HttpClient``. ``get(url, *, with_oc=False, accept=None, refresh=None, max_bytes=None) -> Response``,
    ``head(url, ...) -> Response``, ``evict(url, *, with_oc=False)``. 허용 호스트·robots·간격·백오프·캐시는 클라이언트가 처리한다.
    법제처 DRF의 OC는 ``with_oc=True`` 로만 주입된다(URL에 OC 파라미터를 직접 쓰면 BlockedURL). 응답과 예외·로그의 URL은 OC가 ``[MASKED]`` 이다.
    ``accept(body: bytes, headers: dict) -> str | None`` 은 본문 표지로 성공을 판정한다. 오류 사유 문자열을 돌려주면 캐시에 쓰지 않고
    ``Response.content_error`` 에 담긴다(HTTP 200 오류 페이지 대비). with_oc 응답 본문에 OC가 ``OC=`` 가 아닌 형태로 되울리면 본문을 비우고
    ``content_error='oc_echo_unmasked'`` 로 돌려주며 캐시하지 않는다(어댑터는 이를 실패로 다루고 재시도 루프를 돌지 않는다). ``Response.ok`` 는 2xx이고 content_error가 없을 때만 참이다.
  - ``ctx.manifest``: ``Manifest`` (ensure_source / upsert_list_page / upsert_document / update_document_fields / set_document_status /
    upsert_attachment / update_attachment_head / set_rights / record_failure / resolve_failures / transaction() ...). 재발견은 멱등이다.
  - ``ctx.check_time()``: 시간 상한을 넘었으면 ``TimeBudgetExceeded`` 를 던진다. 페이지·문서 단위 루프마다 부른다. CLI가 이를 잡아
    ``paused_time_limit`` 으로 종료하고, 다음 실행은 장부와 HTTP 캐시로 이어받는다. ``ctx.time_left()`` 는 남은 초(상한 없으면 None).
  - ``ctx.logger``: logsafe 핸들러가 붙은 로거(``%s`` 포맷 인자로 쓴다. URL은 클라이언트가 준 마스킹 URL만 넣는다).
  - ``ctx.limit_pages``(int|None), ``ctx.refresh``, ``ctx.dry_run``, ``ctx.now()``(aware datetime), ``ctx.corpus_dir``, ``ctx.inventory_dir``.

``--dry-run``: 장부는 기존 manifest.sqlite의 메모리 사본이라 쓰기가 사라지고, HTTP는 캐시 적중만 가능하다. 캐시에 없는 URL이 나오면
``DryRunMiss`` 로 멈추고 CLI가 그 URL(마스킹)을 보고한다.

어댑터는 성공을 본문 표지로 판정하고, 의미 오류는 ``ctx.manifest.record_failure(target, stage, error_class, message)`` 로 남긴다.
"""
