from press_review import edit_checks as ec


def codes(issues, level=None):
    return [i.code for i in issues if level is None or i.level == level]


TITLE = "회사 돈 빼돌린 대표, 경찰 수사"
SUGGESTED = "회사 돈 빼돌린 혐의…경찰, 대표 수사"


def test_example_replacement_is_clean(article):
    assert ec.check_replacement(TITLE, SUGGESTED, article, block_id="title", start=0) == []
    assert ec.check_replacement(TITLE, SUGGESTED, article) == []


def test_noop_and_empty(article):
    assert codes(ec.check_replacement(TITLE, TITLE, article)) == ["NO_CHANGE"]
    assert codes(ec.check_replacement(TITLE, "", article), "error") == ["EMPTY_REPLACEMENT"]


def test_new_number_is_error(article):
    issues = ec.check_replacement(TITLE, "회사 돈 3억 빼돌린 혐의…경찰, 대표 수사", article)
    assert codes(issues, "error") == ["NEW_NUMBER"]
    assert ec.has_errors(issues)


def test_existing_numbers_ok_comma_and_unit_handling(mk_article):
    art = mk_article([("title", "title", "A사 3억원 횡령"), ("p1", "body", "A사는 10월 5일 1,200만 원을 3억원 규모로 집행했다고 밝혔다.")])
    ok = ec.check_replacement("3억원 규모", "3억원 규모", art)
    assert codes(ok) == ["NO_CHANGE"]
    # 같은 숫자 재배치: 오류 없음
    assert not ec.has_errors(ec.check_replacement("10월 5일", "5일 10월", art))
    # 쉼표 숫자는 쉼표를 지우고 비교
    assert not ec.has_errors(ec.check_replacement("1,200만 원", "1200만 원", art))
    # 새 날짜 / 새 금액
    assert "NEW_NUMBER" in codes(ec.check_replacement("10월 5일", "10월 6일", art), "error")
    assert "NEW_NUMBER" in codes(ec.check_replacement("3억원 규모", "30억원 규모", art), "error")
    # 숫자는 원문에 있으나 단위가 다름: 경고
    issues = ec.check_replacement("3억원 규모", "3천만 원 규모", art)
    assert not ec.has_errors(issues) and "NUMBER_UNIT_CHANGED" in codes(issues, "warning")
    # 사라진 숫자: 경고
    assert "NUMBER_REMOVED" in codes(ec.check_replacement("3억원 규모", "거액 규모", art), "warning")


def test_num_in_does_not_match_digit_substrings():
    assert not ec.num_in("3", "13")
    assert ec.num_in("3", "x 3 y")
    assert not ec.num_in("3", "3.5")


def test_new_direct_quote_is_error_existing_quote_ok(article):
    quote = "김씨는 본지에 “횡령한 사실이 없다”고 말했다."
    issues = ec.check_replacement(quote, "김씨는 본지에 “횡령 사실을 인정한다”고 말했다.", article)
    assert "NEW_QUOTE" in codes(issues, "error")
    same = ec.check_replacement(quote, "김씨는 본지에 “횡령한 사실이 없다”고 밝혔다.", article)
    assert codes(same, "error") == []
    # 따옴표 없이 서술로 바꾸는 것은 허용(기자 서술)
    assert codes(ec.check_replacement(quote, "김씨는 횡령 사실을 부인했다고 본지에 말했다.", article), "error") == []


def test_new_straight_and_corner_quotes(article):
    assert "NEW_QUOTE" in codes(ec.check_replacement(TITLE, '회사 돈 빼돌린 "사장", 경찰 수사', article), "error")
    assert "NEW_QUOTE" in codes(ec.check_replacement(TITLE, "회사 돈 빼돌린 「새 발언」, 경찰 수사", article), "error")
    assert "NEW_SINGLE_QUOTE" in codes(ec.check_replacement(TITLE, "회사 돈 ‘빼돌린 의혹’ 경찰 수사", article), "warning")


def test_direct_quote_internal_change_is_error(article):
    q = "횡령한 사실이 없다"
    issues = ec.check_replacement(q, "횡령한 사실이 있다", article)
    assert codes(issues, "error") == ["DIRECT_QUOTE_CHANGED"]
    # 위치를 지정해도 같다
    p2 = article["blocks"][3]["text"]
    s = p2.index(q)
    issues = ec.check_replacement(q, "횡령한 사실이 있다", article, block_id="p2", start=s)
    assert "DIRECT_QUOTE_CHANGED" in codes(issues, "error")
    # 인용 일부만 걸친 수정도 오류
    part = "사실이 없다”고"
    assert "DIRECT_QUOTE_CHANGED" in codes(ec.check_replacement(part, "사실이 있다”고", article), "error")
    # 인용 바깥만 수정하면 오류 아님
    assert codes(ec.check_replacement("고 말했다", "고 밝혔다", article), "error") == []


def test_direct_quote_must_be_kept_verbatim_when_span_covers_it(article):
    q = "본지에 “횡령한 사실이 없다”고"
    assert codes(ec.check_replacement(q, "본지에 “횡령한 사실이 없다”라고", article), "error") == []
    assert "NEW_QUOTE" in codes(ec.check_replacement(q, "본지에 “횡령 사실이 없다”고", article), "error")


def test_single_quote_change_is_warning_only(mk_article):
    art = mk_article([("title", "title", "‘새 제도’ 논란"), ("p1", "body", "‘새 제도’ 도입이 논란이다.")])
    issues = ec.check_replacement("새 제도", "신제도", art)
    assert not ec.has_errors(issues) and "SINGLE_QUOTE_CHANGED" in codes(issues, "warning")


def test_negation_count_change_is_warning(article):
    s = "혐의와 관련된 금액이나 구체적인 범행 경위는 아직 확인되지 않았다."
    issues = ec.check_replacement(s, "혐의와 관련된 금액이나 구체적인 범행 경위는 확인되었다.", article)
    assert "NEGATION_CHANGED" in codes(issues, "warning") and not ec.has_errors(issues)
    assert "NEGATION_CHANGED" not in codes(ec.check_replacement(s, "혐의와 관련된 금액이나 구체적인 범행 경위는 아직 알려지지 않았다.", article))


def test_negation_counter_ignores_bu_mu_prefix_and_counts_forms():
    c = ec.count_negations
    assert c("불가능하고 무관하다") == 0
    assert c("하지 않았다") == 1 and c("사실이 없다") == 1 and c("그는 아니다") == 1
    assert c("안 간다") == 1 and c("안전하다") == 0 and c("못 갔다") == 1
    assert c("않았고 없고 아니다") == 3


def test_new_proper_noun_candidate_is_warning(article):
    issues = ec.check_replacement(TITLE, "회사 돈 빼돌린 혐의…경찰, 박씨 수사", article)
    assert any(i.code == "NEW_PROPER_NOUN" and "박씨" in i.message for i in issues)
    assert not ec.has_errors(issues)
    issues = ec.check_replacement(TITLE, "회사 돈 빼돌린 혐의…경찰, B사 대표 수사", article)
    assert any(i.code == "NEW_PROPER_NOUN" and "B사" in i.message for i in issues)
    # 원문에 있는 이름은 조사가 붙어도 통과
    assert not any(i.code == "NEW_PROPER_NOUN" for i in ec.check_replacement("경찰은 A사 대표 김씨의 횡령 혐의를", "경찰은 A사 대표 김씨가 받는 횡령 혐의를", article))


def test_errors_sorted_first(article):
    issues = ec.check_replacement(TITLE, "회사 돈 7억 빼돌린 박씨 수사", article)
    levels = [i.level for i in issues]
    assert levels == sorted(levels, key=lambda l: 0 if l == "error" else 1)
    assert levels[0] == "error"


def test_quote_regions_pairs_and_unclosed():
    assert ec.quote_regions("가 “나” 다 「라」 ‘마’ \"바\"") == [(2, 5, "direct"), (8, 11, "direct"), (12, 15, "single"), (16, 19, "direct")]
    assert ec.quote_regions("그는 “끝나지 않은 말") == []
    assert ec.quote_regions("“바깥 ‘안’ 바깥”") == [(0, 11, "direct")]
