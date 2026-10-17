import json

import pytest

from press_review import article_io, contracts, hashing

EXAMPLE_TXT = (
    "회사 돈 빼돌린 대표, 경찰 수사\n\n경찰, 횡령 혐의로 사실관계 확인\n\n"
    "경찰은 A사 대표 김씨의 횡령 혐의를 수사하고 있다고 밝혔다. 혐의와 관련된 금액이나 구체적인 범행 경위는 아직 확인되지 않았다.\n\n"
    "김씨는 본지에 “횡령한 사실이 없다”고 말했다.\n"
)


def blocks(a):
    return [(b["block_id"], b["section"], b["text"]) for b in a["blocks"]]


def test_plaintext_reproduces_example_blocks(article):
    a, warnings = article_io.parse_plaintext_ex(EXAMPLE_TXT, article_id="synthetic-001", publication_date="2026-10-05")
    assert blocks(a) == blocks(article)
    assert contracts.validate_article(a) == []
    assert any("휴리스틱" in w for w in warnings)


def test_no_blank_lines_each_line_is_a_paragraph():
    a = article_io.parse_plaintext("제목입니다\n부제 한 줄\n본문 첫 문장입니다.\n본문 둘째 문장입니다.", publication_date="2026-10-05")
    assert blocks(a) == [("title", "title", "제목입니다"), ("subtitle", "subtitle", "부제 한 줄"),
                         ("p1", "body", "본문 첫 문장입니다."), ("p2", "body", "본문 둘째 문장입니다.")]


def test_subtitle_needs_following_body_and_short_lines():
    # 제목 + 짧은 한 줄뿐이면 그 줄은 본문
    a = article_io.parse_plaintext("제목\n\n짧은 줄", publication_date="2026-10-05")
    assert [b["section"] for b in a["blocks"]] == ["title", "body"]
    # 문장형 줄은 부제가 아니다
    a = article_io.parse_plaintext("제목\n\n이것은 문장이다.\n\n다음 문단이다.", publication_date="2026-10-05")
    assert [b["section"] for b in a["blocks"]] == ["title", "body", "body"]
    # 부제 여러 줄
    a = article_io.parse_plaintext("제목\n부제1\n부제2\n\n본문이다.", publication_date="2026-10-05")
    assert [b["block_id"] for b in a["blocks"]] == ["title", "subtitle-1", "subtitle-2", "p1"]


def test_subtitle_guard_rejects_quote_lines_and_unpunctuated_sentences():
    # 실측 결함: 인용행·마침표 없는 서술행을 부제로 판정
    for line in ("“정말 그렇습니다”", "피고인은 부인했다", "검찰은 수사 중이에요"):
        a = article_io.parse_plaintext(f"제목\n\n{line}\n\n본문 문장이다.", publication_date="2026-10-05")
        assert [b["section"] for b in a["blocks"]] == ["title", "body", "body"], line
    a = article_io.parse_plaintext("제목\n\n경찰, 횡령 혐의로 사실관계 확인\n\n본문 문장이다.", publication_date="2026-10-05")
    assert [b["section"] for b in a["blocks"]] == ["title", "subtitle", "body"]


def test_captions_detected():
    txt = "제목\n\n본문 문장이다.\n사진=가상통신\n▲ 현장 모습\n\n[사진 제공] 가상\n\n마지막 문장이다."
    a = article_io.parse_plaintext(txt, publication_date="2026-10-05")
    assert [(b["block_id"], b["section"]) for b in a["blocks"]] == [
        ("title", "title"), ("p1", "body"), ("caption-1", "caption"), ("caption-2", "caption"), ("caption-3", "caption"), ("p2", "body")]


def test_title_only_warns_and_fails_validation():
    a, w = article_io.parse_plaintext_ex("제목만", publication_date="2026-10-05")
    assert any("본문 블록이 없습니다" in x for x in w)
    assert "body block required" in " ".join(contracts.validate_article(a))


def test_empty_text_raises_and_defaults_filled():
    with pytest.raises(ValueError):
        article_io.parse_plaintext("  \n\n ")
    import datetime as dt
    a, w = article_io.parse_plaintext_ex("제목\n\n본문이다.", today=dt.date(2026, 10, 8))
    assert a["publication_date"] == "2026-10-08" and any("오늘 날짜" in x for x in w)
    assert a["article_id"].startswith("text-")
    assert a["context"]["genre"] == "unknown"


def test_crlf_and_blank_variants():
    a = article_io.parse_plaintext("제목\r\n\r\n본문 하나이다.\r\n\r\n\r\n본문 둘이다.\r\n", publication_date="2026-10-05")
    assert [b["text"] for b in a["blocks"]] == ["제목", "본문 하나이다.", "본문 둘이다."]


def test_load_json_uses_file_bytes(tmp_path, article_bytes):
    p = tmp_path / "a.json"
    p.write_bytes(article_bytes)
    loaded = article_io.load_article(p)
    assert loaded.raw_bytes == article_bytes
    assert hashing.article_sha256(loaded.article, loaded.raw_bytes)[0].startswith("ac38fd55")


def test_load_txt_and_md(tmp_path):
    for name in ("기사 1.txt", "a.md"):
        p = tmp_path / name
        p.write_text(EXAMPLE_TXT, encoding="utf-8")
        loaded = article_io.load_article(p)
        assert loaded.raw_bytes == EXAMPLE_TXT.encode("utf-8")
        assert loaded.article["blocks"][0]["text"].startswith("회사 돈")
        assert loaded.article["article_id"] in ("기사-1", "a")


def test_load_errors(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"x": 1}), encoding="utf-8")
    with pytest.raises(article_io.ArticleLoadError) as e:
        article_io.load_article(bad)
    assert e.value.errors
    (tmp_path / "x.bin").write_bytes(b"\xff\xfe")
    with pytest.raises(article_io.ArticleLoadError):
        article_io.load_article(tmp_path / "x.bin")
    (tmp_path / "x.txt").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(article_io.ArticleLoadError, match="UTF-8"):
        article_io.load_article(tmp_path / "x.txt")
    with pytest.raises(article_io.ArticleLoadError, match="cannot read"):
        article_io.load_article(tmp_path / "missing.txt")
    (tmp_path / "bad2.json").write_text("{", encoding="utf-8")
    with pytest.raises(article_io.ArticleLoadError, match="invalid JSON"):
        article_io.load_article(tmp_path / "bad2.json")
    big = tmp_path / "big.txt"
    big.write_bytes(b"a" * (article_io.MAX_FILE_BYTES + 1))
    with pytest.raises(article_io.ArticleLoadError, match="larger"):
        article_io.load_article(big)


def test_subtitle_block_port_matches_original_behavior():
    f = article_io.is_subtitle_block
    assert f("짧은 부제") is True
    assert f("문장이다.") is False and f("정말요.") is False and f("정말입니까?") is False
    assert f("가" * 41) is False and f("") is False
