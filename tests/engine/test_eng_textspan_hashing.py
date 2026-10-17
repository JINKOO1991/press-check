import hashlib
import json
import unicodedata

import pytest

from press_review import hashing, textspan


def test_code_point_length_hangul_emoji_combining():
    assert textspan.cp_len("한글") == 2
    assert textspan.cp_len("😀") == 1                      # UTF-16으로는 2
    assert len("😀".encode("utf-16-le")) // 2 == 2
    nfc = "한"
    nfd = unicodedata.normalize("NFD", nfc)
    assert (textspan.cp_len(nfc), textspan.cp_len(nfd)) == (1, 3)  # 정규화하지 않는다
    assert textspan.cp_len("e\u0301") == 2                   # 결합문자는 별도 code point
    assert textspan.cp_len("👨‍👩‍👧") == 5                      # ZWJ 시퀀스: 3 이모지 + 2 ZWJ


def test_cp_slice_half_open_and_bounds():
    s = "가😀나\u0301다"
    assert textspan.cp_slice(s, 1, 2) == "😀"
    assert textspan.cp_slice(s, 2, 4) == "나\u0301"
    assert textspan.cp_slice(s, 0, len(s)) == s
    for bad in [(-1, 2), (0, len(s) + 1), (2, 2), (3, 2)]:
        with pytest.raises(ValueError):
            textspan.cp_slice(s, *bad)
    with pytest.raises(ValueError):
        textspan.cp_slice(s, True, 2)


def test_find_all_overlapping_and_no_normalization():
    assert textspan.find_all("가나가나가", "가나가") == [(0, 3), (2, 5)]
    assert textspan.find_all("abc", "") == []
    nfd = unicodedata.normalize("NFD", "한글")
    assert textspan.find_all(nfd, "한글") == []              # NFC quote는 NFD 본문에 일치하지 않는다
    s = "😀😀"
    assert textspan.find_all(s, "😀") == [(0, 1), (1, 2)]


def test_lone_surrogate_detected():
    assert textspan.has_lone_surrogate("a\ud83dz")
    assert not textspan.has_lone_surrogate("😀")


def test_file_bytes_hash_matches_example_and_differs_from_object_hash(article, article_bytes):
    digest, mode = hashing.article_sha256(article, article_bytes)
    assert mode == "file_bytes"
    assert digest == hashlib.sha256(article_bytes).hexdigest()
    assert digest.startswith("ac38fd55")
    obj_digest, obj_mode = hashing.article_sha256(article)
    assert obj_mode == "cjson-1"
    assert obj_digest != digest


def test_canonical_json_is_key_order_independent_and_pinned():
    a = {"b": 1, "a": ["한", {"y": 2, "x": 1}]}
    b = {"a": ["한", {"x": 1, "y": 2}], "b": 1}
    assert hashing.canonical_json(a) == hashing.canonical_json(b)
    assert hashing.canonical_json(a) == '{"a":["한",{"x":1,"y":2}],"b":1}'.encode("utf-8")
    assert hashing.article_sha256(a)[0] == hashing.article_sha256(b)[0]
    assert hashing.article_sha256(a)[0] == hashlib.sha256('{"a":["한",{"x":1,"y":2}],"b":1}'.encode()).hexdigest()


def test_object_hash_changes_with_content(article):
    h1 = hashing.article_sha256(article)[0]
    article["blocks"][0]["text"] += "!"
    assert hashing.article_sha256(article)[0] != h1


def test_whitespace_in_file_changes_file_hash_not_object_hash(article, article_bytes):
    pretty = json.dumps(article, ensure_ascii=False, indent=4).encode("utf-8")
    assert hashing.article_sha256(article, pretty)[0] != hashing.article_sha256(article, article_bytes)[0]
    assert hashing.article_sha256(json.loads(pretty))[0] == hashing.article_sha256(article)[0]
