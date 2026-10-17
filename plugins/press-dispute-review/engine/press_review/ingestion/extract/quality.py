"""페이지 품질 지표와 OCR 줄 검수 표시(P2_DESIGN §2).

지표: 글자 수(공백 제외), 한글 비율, 라틴 비율, 깨진 문자 비율, 한글 100자당 잡음(G-D28 ``broken_layer``). 임계 미만이면 ``reasons`` 에 사유가 들어가고 그 페이지만 OCR 대상이 된다.
OCR 줄은 숫자·사건번호·부정어가 있으면 ``needs_review`` 표시(flags)를 단다.
"""
from __future__ import annotations

import re

MIN_CHARS = 20            # 공백 제외 글자 수 미만이면 too_short
GARBLE_MAX = 0.03         # 깨진 문자 비율 초과면 garbled
HANGUL_MIN = 0.20         # 한글 비율이 이 미만이고
LATIN_MIN = 0.30          # 라틴 비율도 이 미만이면 low_text_ratio (숫자표·글리프 깨짐)
# G-D28: 깨진 텍스트층. 한글 글자 100개당 '잡음'(한글 사이에 낀 라틴 문자·기호, 전각 쉼표·마침표, 화살표·캐럿, 제→저 오독 '저 18조'(3배))이 이 값 이상이면 그 쪽의 텍스트층을 믿지 않고 OCR 을 시도한다.
# 장부의 표본으로 맞춘 값이다(docs/coverage/P2_extractor_compare.md '## G-D28 비교'): 표본 PDF 25개의 비OCR 쪽 1,831개 중 제17대 총선 백서(깨진 층)의 60쪽만 걸린다.
# 판정과 OCR 강제는 ``ingestion.extract.BROKEN_LAYER_SOURCES`` 소스에만 켠다(assess 의 broken 인자). 다른 소스에 켜면 국내언론관계판결집 쪽의 약 3%가 걸린다.
# 한글 비율·사전 단어 비율 기준은 시험했으나 갈리지 않아 쓰지 않았다(깨진 층도 한글 비율이 정상이고, 고유명사가 많은 정상 쪽의 사전 단어 비율이 더 낮다).
BROKEN_NOISE_MAX = 2.0
BROKEN_MIN_HANGUL = 120   # 이보다 한글이 적은 쪽은 잡음 비율이 불안정해 판정하지 않는다
OCR_CONF_REVIEW = 0.60    # OCR 페이지 평균 신뢰도가 이 미만이면 페이지 전체 검수
OCR_BETTER_FACTOR = 1.2

_HANGUL = re.compile(r"[가-힣]")
_LATIN = re.compile(r"[A-Za-z]")
_DIGIT = re.compile(r"[0-9]")
# 깨진 문자: 대체 문자, 사용자 정의 영역, 제어 문자(탭·개행 제외), 호환 자모 단독(CID 매핑 실패에서 흔함)
_GARBLE = re.compile(r"[�-\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009fㄱ-ㆎ]")
_WS = re.compile(r"\s+")
_MIX = re.compile(r"[가-힣][A-Za-z^~|↑\\\[\]{}\u00c0-\u024f]+[가-힣]")        # 한글 단어 한가운데 낀 라틴 문자·기호(숫자는 '제2021호' 같은 정상 표기가 있어 뺀다)
_FULLWIDTH = re.compile(r"[，．]")                                           # 전각 쉼표·마침표(전각 콜론은 '정당：무소속' 같은 정상 표기가 있어 뺀다)
_ARROW = re.compile(r"[↑↓^]")
_JE_MISREAD = re.compile(r"저\s*[0-9lI↑]+\s*[조항호]")                          # '제 18조'를 '저 18조'로 읽은 오독


def metrics(text: str) -> dict:
    s = _WS.sub("", text)
    n = len(s)
    if n == 0:
        return {"chars": 0, "hangul_ratio": 0.0, "latin_ratio": 0.0, "digit_ratio": 0.0, "garble_ratio": 0.0, "hangul_chars": 0, "noise_per_100": 0.0, "meaningful": 0}
    h, la, d, g = (len(p.findall(s)) for p in (_HANGUL, _LATIN, _DIGIT, _GARBLE))
    noise = len(_MIX.findall(text)) + len(_FULLWIDTH.findall(text)) + len(_ARROW.findall(text)) + 3 * len(_JE_MISREAD.findall(text))      # 공백이 있는 원문에서 센다(단어 경계가 필요하다)
    return {"chars": n, "hangul_ratio": round(h / n, 4), "latin_ratio": round(la / n, 4), "digit_ratio": round(d / n, 4),
            "garble_ratio": round(g / n, 4), "hangul_chars": h, "noise_per_100": round(noise * 100 / h, 2) if h else 0.0, "meaningful": h + la + d}


def reasons_of(m: dict, broken: bool = True) -> list:
    out = []
    if m["chars"] < MIN_CHARS:
        out.append("too_short")
    if m["garble_ratio"] > GARBLE_MAX:
        out.append("garbled")
    if m["chars"] >= MIN_CHARS and m["hangul_ratio"] < HANGUL_MIN and m["latin_ratio"] < LATIN_MIN:
        out.append("low_text_ratio")
    if broken and m["hangul_chars"] >= BROKEN_MIN_HANGUL and m["noise_per_100"] >= BROKEN_NOISE_MAX:
        out.append("broken_layer")
    return out


def assess(text: str, broken: bool = True) -> tuple:
    """(quality 0..1, 지표 dict, 사유 list). quality 는 참고값이고 OCR 여부는 사유 유무로 정한다. broken=False 이면 깨진 텍스트층 사유(``broken_layer``)를 내지 않는다(지표는 그대로 남는다)."""
    m = metrics(text)
    reasons = reasons_of(m, broken)
    if m["chars"] == 0:
        return 0.0, m, reasons
    q = min(1.0, m["chars"] / MIN_CHARS) * (1.0 - min(1.0, m["garble_ratio"] * 10)) * min(1.0, (m["meaningful"] / m["chars"]) / 0.5)
    return round(q, 3), m, reasons


def ocr_is_better(text_m: dict, text_reasons: list, ocr_m: dict, ocr_reasons: list) -> bool:
    """OCR 결과를 채택할지. 텍스트 층이 더 낫거나 같으면 텍스트 층을 지킨다(영문·숫자 페이지를 OCR 잡음으로 덮어쓰지 않는다)."""
    if len(ocr_reasons) < len(text_reasons):
        return True
    if len(ocr_reasons) > len(text_reasons):
        return False
    return ocr_m["meaningful"] > OCR_BETTER_FACTOR * text_m["meaningful"] + 5


# ---------------------------------------------------------------- OCR 줄 검수
_CASE_NO = re.compile(r"\d{2,4}\s?[가-힣]{1,4}\s?\d{1,7}|제\s?\d{4}\s?-\s?\d+\s?호")
_NEGATION_KO = ("아니", "않", "없", "못하", "못한", "못해", "불인정", "불가", "부인", "부정", "허용되지", "기각", "각하", "아님")
_NEGATION_EN = re.compile(r"\b(?:not|no|never|without|cannot|neither|nor)\b", re.I)


def line_flags(line: str) -> list:
    flags = []
    if _CASE_NO.search(line):
        flags.append("case_number")
    if _DIGIT.search(line):
        flags.append("number")
    if any(w in line for w in _NEGATION_KO) or _NEGATION_EN.search(line):
        flags.append("negation")
    return flags
