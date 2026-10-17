# 데이터 계약 0.1

이 JSON Schema와 예제는 구현을 시작하기 위한 계약 초안이다. 코어 구현·검색 코퍼스·법률 검증이 완료된 산출물이 아니다. `review.example.json`은 가상 기사의 설명용 결과다.

- `article.schema.json`: 제목·부제·본문·캡션을 block 단위로 입력한다. 최소 하나의 제목과 본문이 있어야 하며, 이 조건과 block_id 유일성은 구현 validator에서 추가 확인한다. 취재 메모는 사실 검증 완료를 의미하지 않는다.
- `review.schema.json`: 별은 1~3 검토 필요도, review_confidence는 근거·적용 검토의 확신도다. 둘 다 발생확률이 아니다. 스키마에 확률 필드는 없다. `reason_short`는 100자 이하·한 문장으로 렌더링한다.
- `source-record.schema.json`: 자료 접근, robots, 각 이용행위 권한, 추출 상태, 판단·수정 자료 유형을 구별한다. source-seeds.json은 발견용 인벤토리이며 수집된 source record와 다른 계약이다.
- `case-record.schema.json`: 수집 문서에서 구조화한 사건 기록(ARCHITECTURE §5 case record, P2_DESIGN §4). 원문 인용 필드는 `{quote, page_id, start, end}`이고 authority_type은 review.schema의 evidence.authority_type(`guideline`은 심의기준·편람이며 법령이 아니다, G-D21), outcome_type·correction_pair_confidence는 source-record.schema의 outcome_type·before_after_status와 같은 값이다. 교차검증은 엔진의 `contracts.validate_case_record`(인용을 페이지 텍스트와 부분문자열로 대조, 합의·각하·취하·판결 라벨 분리, 당사자 주장과 기관·법원 판단의 speaker 분리). outcome_type과 outcome_scope의 짝 규칙(G-D16)은 `contracts.AUTHORITY_OUTCOMES`·`PROCEDURAL_SCOPE_OUTCOMES`와 이 함수 한 곳이 정본이다: 법원 판결은 `judgment`/`unknown`만, `dismissal_*`·`withdrawal`은 PAC·IENDC 절차에만, `dismissal_procedural`은 scope `procedural`과 짝이다. 엔진 `schemas/`의 사본과 바이트가 같아야 한다(테스트).
- `evaluation-scenarios.json`: 배선과 행동 검증용 가상 시나리오 12개다. 전문가 검증을 통과한 법률 정답셋이나 모델 성능 결과가 아니다.

## 구현 validator에 필요한 교차 검증

1. span의 start/end는 해당 block 텍스트의 Unicode code point 기준 `[start, end)`다. end>start이며 잘라낸 문자열이 quote와 정확히 같아야 한다. JavaScript UTF-16 index를 그대로 쓰지 않는다. 반복 구절은 block·위치로 구별한다. 정규화/OCR 교정 전후 대응표가 없으면 원문 위치를 확정하지 않는다.
2. 파일 입력의 article_sha256은 분석 당시 입력 파일의 정확한 바이트 SHA-256이다. 대화/객체 입력은 버전이 고정된 canonical JSON serializer로 객체의 해시를 만든다. 해시 기준을 모드별로 문서화하며, 검토 후 바뀐 파일에는 낡은 수정을 적용하지 않는다. 예제 해시는 article.example.json의 실제 파일 바이트로 계산했다.
3. replace는 replacement를 갖고 verify_first/none은 null을 갖는다. 추가 확인은 문장 대신 required_verification에 쓴다. related_edits의 위치도 원문과 일치해야 하며 모든 수정 구간의 겹침·충돌을 확인한다.
4. evidence의 source_id와 passage_id는 검색 시스템에서 제공한 레코드에 존재해야 한다. 모델이 URL을 새로 만들거나 verified=true로 자기 검증하는 것은 허용하지 않는다. URL/locator 일치와 해당 주장 지지 여부를 각각 검증한다.
5. 권한 unknown/permission_required/forbidden은 해당 이용행위의 허용이 아니다. read와 store_raw, external_api, fine_tune, redistribute는 서로 독립이다. 네이버의 현재 robots 제한처럼 목적 금지 또는 이용권한 보류가 있으면 자동 collection_enabled는 false다. 권한 변경은 근거와 검토 기록이 있어야 한다.
6. no_actionable_findings는 안전 보장이 아니다. 입력 범위·검색 범위가 부족하면 insufficient_information/partial과 coverage_notes를 쓴다. 전체 이슈 수와 UI 우선 표시 개수를 분리한다.
7. ephemeral 보고서는 세션 메모리의 짧은 TTL 안에서 상세 조회할 수 있다. expires_at은 활성 세션의 실제 만료시각으로 설정하고 디스크 원문을 남기지 않는다. 예제의 null은 비실행 예제라는 뜻이며 실제 세션에서 무기한 메모리를 허용하는 기본값이 아니다.

별의 운영 의미는 ★☆☆ 확인 권고, ★★☆ 수정·보강 검토, ★★★ 발행 전 집중 검토로 잠정 통일한다. 높은 별과 낮은 확신도는 동시에 있을 수 있다. 그 경우 이유와 추가 확인을 표시하며 근거 없는 교체문을 확정하지 않는다.
