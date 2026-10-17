"""MockProvider: 합성 예제 전용 규칙. 실제 기사 분석 능력이 없다.

규칙 하나: 제목에 범행을 확정하는 어휘(아래 _ACTS)가 있고 완화 표지(_HEDGE)가 없는데 본문이 '혐의/의혹' 단계로
서술하면 제목 전체를 ★★★ 로 표시한다. 수정안은 ``<행위> <대상>, <기관> 수사`` 꼴 제목에만 만들고,
그 밖에는 verify_first 로 둔다.
"""
from __future__ import annotations

import re
from typing import Any

from .base import AnalysisProvider

_ACTS = re.compile(r"빼돌|횡령|가로챈|가로챘|착복|유용|속인|속였|훔친|훔쳤|조작|폭행|뇌물|배임|탈세|사기")
_HEDGE = re.compile(r"혐의|의혹|의심|주장|알려|전해|추정|가능성|논란|\?|？")
_BODY_STAGE = re.compile(r"혐의|의혹")
_TITLE_FORM = re.compile(r"^(?P<act>.+(?:린|한|낸|친|운|은))\s+(?P<who>[가-힣A-Za-z0-9]+),\s*(?P<auth>경찰|검찰|공수처)\s+수사$")
_VERIFY = "실제 수사 대상과 혐의 및 취재 내용을 발행 전에 확인한다."


class MockProvider(AnalysisProvider):
    name = "mock"

    def analyze(self, article: dict, corpus: Any) -> tuple[list[dict], dict]:
        blocks = article["blocks"]
        body = "\n".join(b["text"] for b in blocks if b["section"] == "body")
        findings: list[dict] = []
        explanations: dict[str, dict] = {}
        if not _BODY_STAGE.search(body):
            return findings, explanations
        for b in blocks:
            if b["section"] != "title" or _HEDGE.search(b["text"]) or not _ACTS.search(b["text"]):
                continue
            fid = f"finding-{len(findings) + 1:03d}"
            m = _TITLE_FORM.match(b["text"])
            if m:
                edit = {
                    "mode": "replace",
                    "replacement": f"{m['act']} 혐의…{m['auth']}, {m['who']} 수사",
                    "related_edits": [],
                    "required_verification": [_VERIFY],
                }
            else:
                edit = {"mode": "verify_first", "replacement": None, "related_edits": [], "required_verification": [_VERIFY]}
            hits = corpus.search("기사 전체 인상 제목 본문 단정 혐의 수사", authority_types=["judgment"], risk_channel="civil", limit=1)
            findings.append({
                "finding_id": fid,
                "span": {"block_id": b["block_id"], "start": 0, "end": len(b["text"]), "quote": b["text"]},
                "priority_stars": 3,
                "risk_channels": ["civil", "correction"],
                "issue_category": "headline_body_certainty_mismatch",
                "review_confidence": "medium",
                "reason_short": "제목이 본문에 기재된 혐의 수사보다 범행을 확정합니다.",
                "suggested_edit": edit,
                "evidence": [{"source_id": h["source_id"], "passage_id": h["passage_id"]} for h in hits],
                "counter_considerations": ["기업의 자금 집행에 대한 공익적 취재와 본문의 혐의·부인 설명은 유지한다."],
            })
            explanations[fid] = {
                "applicable_issue": "제목만 읽은 독자에게 범행이 확정된 것처럼 보이는지, 본문의 혐의 단계와 어긋나는지 본다.",
                "article_context": "본문은 혐의를 수사 중이라고만 서술하고 금액·경위는 확인되지 않았다고 밝힌다.",
                "support": "연결된 passage는 합성 코퍼스의 시험용 자리표시이며 실제 판결의 내용이 아니다.",
                "differences": "합성 예제이므로 실제 사건과의 사실관계 비교는 하지 않았다.",
                "required_checks": [_VERIFY],
            }
        return findings, explanations
