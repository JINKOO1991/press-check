"""p4_leak_check 지명 규칙: 시·도 이름은 허용, 실제 시 이름과 시·도 단위 실존 기관명은 차단."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "evals"))
import p4_leak_check as lk  # noqa: E402


def test_region_hits_blocks_real_city_and_agency():
    h = lk.region_hits("충북경찰청은 수원에 사는 A를 조사했다. 경기남부경찰청도 나섰다.")
    assert h["regions"] == ["수원"]
    assert h["real_agencies"] == ["경기남부경찰청", "충북경찰청"]


def test_region_hits_allows_province_with_fictional_city():
    h = lk.region_hits("경기 솔미경찰서는 28일 밝혔다. 충남 한내시와 강원 가온시, 청해도교육청도 함께 적었다.")
    assert h == {"regions": [], "real_agencies": []}
