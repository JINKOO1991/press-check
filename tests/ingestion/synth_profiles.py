"""구조화 공통 로직 시험용 합성 프로필·프롬프트·소스.

UDEC-37 후속: 공개 저장소에서 PAC 구조화 프로필·프롬프트를 뺐다. 작업 키·재개·게이트·인용 대조 같은 공통 시험은 실제 기관 자료와 무관하게
프로필 레지스트리에 항목이 있기만 하면 되므로, 시험 시간 동안만 합성 항목을 등록하고 프롬프트는 tmp 폴더에 둔다(저장소의 prompts/ 에 파일을 만들지 않는다).
institutions·authority_types 값은 계약(contracts)의 열거값이다.
"""
from press_review.ingestion import structure_records as sr

CASEBOOK = "synthetic_casebook"
ELECTION = "synthetic_election"
CORRECTION = "synthetic_correction"
STATUS = "synthetic_status"
JUDGMENT_CASES = "synthetic_judgment_cases"

PROFILES = {
    CASEBOOK: sr.Profile(CASEBOOK, f"{CASEBOOK}.md", ("pac",), ("pac_settlement", "pac_decision")),
    ELECTION: sr.Profile(ELECTION, f"{ELECTION}.md", ("pac",), ("pac_election_review",)),
    CORRECTION: sr.Profile(CORRECTION, f"{CORRECTION}.md", ("pac",), ("pac_decision",)),
    STATUS: sr.Profile(STATUS, f"{STATUS}.md", ("pac",), ("pac_decision",)),
    JUDGMENT_CASES: sr.Profile(JUDGMENT_CASES, f"{JUDGMENT_CASES}.md", ("pac", "other_body"), ("research",), links_ledger=True),
}

# 합성 소스 id -> 합성 프로필
CASEBOOK_SRC = "synthetic_casebook_set_a"
ELECTION_SRC = "synthetic_election_src"
CORRECTION_SRC = "synthetic_correction_set"
STATUS_SRC = "synthetic_status_src"
JUDGMENT_CASES_SRC = "synthetic_judgment_cases_src"
SOURCE_PROFILE = {CASEBOOK_SRC: CASEBOOK, ELECTION_SRC: ELECTION, CORRECTION_SRC: CORRECTION, STATUS_SRC: STATUS, JUDGMENT_CASES_SRC: JUDGMENT_CASES}


def prompt_text(name: str) -> str:
    return f"version: stub-1\n## Source profile\n합성 시험용 프롬프트({name}).\n"
