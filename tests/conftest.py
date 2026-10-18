"""전체 테스트 공통: 데이터 디렉터리를 임시 위치로 고정한다.

실제 사용자 데이터 디렉터리(~/.local/share/press-dispute-review)에 색인이 생기면 ``corpus.load_default()`` 가 합성 코퍼스 대신 그 색인을
돌려준다. 테스트와 합성 평가가 사용자 PC의 색인 유무에 따라 달라지지 않도록 import 시점에 임시 디렉터리로 돌린다
(세션 범위 fixture 가 만들어지기 전에도 적용되어야 해서 모듈 수준에서 한다). 색인을 쓰는 테스트는 자기 data 디렉터리를 따로 준다.
"""
import atexit
import os
import shutil
import tempfile

_TMP = tempfile.mkdtemp(prefix="pdr-tests-data-")
atexit.register(shutil.rmtree, _TMP, True)
os.environ["PRESS_DISPUTE_DATA_DIR"] = _TMP
os.environ.pop("CLAUDE_PLUGIN_DATA", None)
os.environ["PRESS_DISK_MIN_FREE_GB"] = "0"          # 디스크 가드(diskguard) 기본 임계값 10GB 가 시험 PC 여유에 좌우되지 않게 한다. 가드 단위 시험은 monkeypatch 로 덮어쓴다
os.environ["PRESS_DISK_MARGIN_GB"] = "0"
os.environ["PRESS_REVIEW_ALLOW_SYNTHETIC"] = "1"      # 합성 코퍼스에 의존하는 기존 테스트용. 색인 없음 거부 동작은 개별 테스트가 끈다


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _installer_rights_opt_in(request, monkeypatch):
    """기존 테스트는 '설치자가 PAC·IENDC·법제처 계열 권리를 직접 기록한 장부'를 전제로 한다.

    공개본의 기본값은 unknown 이므로(ingestion/rights.py) 여기서만 allowed 기본값을 입힌다. 기본값 자체를 시험하는
    테스트는 ``real_default_rights`` fixture 를 요청해 이 대체를 끈다.
    """
    if "real_default_rights" in request.fixturenames:
        return
    try:
        from press_review.ingestion import rights as rights_mod
    except ImportError:
        return
    real = rights_mod.default_rights
    opted_in = {"read": "allowed", "store_raw": "allowed", "create_derivatives": "allowed", "external_api": "allowed",
                "redistribute_raw": "permission_required", "redistribute_derivatives": "permission_required",
                "fine_tune": "permission_required"}

    def _default_rights(source_family):
        if source_family in rights_mod.COLLECTED_FAMILIES:
            return {"rights": dict(opted_in),
                    "rights_basis": [{"scope": "테스트 전제: 설치자 결정 기록", "review_status": "pending",
                                      "url": "https://www.pac.or.kr/"}],
                    "review_status": "pending"}
        return real(source_family)

    monkeypatch.setattr(rights_mod, "default_rights", _default_rights)


@pytest.fixture()
def real_default_rights():
    """요청하면 `_installer_rights_opt_in` 대체가 꺼지고 공개본의 실제 기본값(unknown)이 쓰인다."""
    return None
