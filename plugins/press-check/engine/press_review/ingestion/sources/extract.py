"""추출 단계 어댑터(P2_DESIGN §2): ``python -m press_review.ingestion extract [--source ID] [--tier A,B,C] [--limit N] [--max-seconds N]``.

외부 바이너리(pdfinfo·pdftotext·pdftoppm·tesseract·kordoc)는 수집·운영 쪽 전용이다. kordoc 경로는 env KORDOC_BIN(코드에 넣지 않는다).
구현은 ``ingestion/extract/``.
"""
from __future__ import annotations

from .. import extract as extract_stage


def add_arguments(parser) -> None:
    parser.add_argument("--source", default=None, help="source_id 하나만 처리")
    parser.add_argument("--tier", default=None, help="처리 순위 A,B,C (쉼표로 여러 개, 기본 전부 A->B->C 순)")
    parser.add_argument("--limit", type=int, default=None, help="이번 실행에서 시도할 첨부 수 상한")
    parser.add_argument("--att", action="append", dest="atts", help="att_id 하나만(반복 가능). 순위·소스 지정 없이도 고른다(게이트 표본용)")
    parser.add_argument("--retry-exhausted", action="store_true", help="재시도 상한(3회)에 걸려 건너뛰던 실패 첨부도 다시 시도")
    parser.add_argument("--extractor", choices=("pdftotext", "pdftotext_layout", "pdftotext_raw", "column_crop", "kordoc"), default=None,
                        help="이번 실행의 PDF 추출기(소스별 상수를 덮어쓴다). pdftotext=기본 읽기 순서, pdftotext_layout=-layout, pdftotext_raw=-raw, "
                             "column_crop=2단 쪽만 단별로 자름(나머지는 -layout), kordoc=쪽별 kordoc. "
                             "선택이 바뀐 첨부는 pages·cases·chunks 를 지우고 다시 추출한다")
    parser.add_argument("--force", action="store_true", help="검증 완료(verified/indexed 또는 검증된 사건이 있는) 첨부도 다시 추출한다(검수 기록이 사라진다)")
    parser.add_argument("--no-ocr", action="store_true", help="품질 미달 쪽도 OCR 하지 않는다(사유는 quality_json 에 남는다)")


def run(ctx, args) -> dict:
    return extract_stage.run_extract(ctx, source=args.source, tier=args.tier, limit=args.limit,
                                     retry_exhausted=args.retry_exhausted, ocr=not args.no_ocr,
                                     att_ids=args.atts, extractor=args.extractor, force=args.force)
