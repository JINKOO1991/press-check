import io
import logging
import os
import stat

import pytest

from press_review import logsafe, secrets

FAKE = "fake-key-AbCdEf0123456789"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("LAW_OPEN_DATA_OC", raising=False)
    return tmp_path


def write_secret(home, rel, value, mode):
    p = home / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(value + "\n", encoding="utf-8")
    os.chmod(p, mode)
    return p


def test_env_wins_over_file(home, monkeypatch):
    write_secret(home, ".config/deepseek/api_key", "from-file-value-1234", 0o600)
    monkeypatch.setenv("DEEPSEEK_API_KEY", FAKE)
    assert secrets.load_secret("DEEPSEEK_API_KEY") == FAKE


def test_file_with_0600_is_read_and_stripped(home):
    write_secret(home, ".config/press-dispute-review/law_oc", "oc-value-777", 0o600)
    assert secrets.load_secret("LAW_OPEN_DATA_OC") == "oc-value-777"


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o666, 0o660, 0o400, 0o700, 0o601])
def test_any_mode_other_than_0600_rejected_without_leaking_value(home, mode, caplog):
    write_secret(home, ".config/deepseek/api_key", FAKE, mode)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(secrets.SecretError) as e:
            secrets.load_secret("DEEPSEEK_API_KEY")
    assert FAKE not in str(e.value) and FAKE not in repr(e.value) and FAKE not in caplog.text
    assert "exactly 0600" in str(e.value)
    assert str(home) not in str(e.value)


def test_missing_empty_directory_and_unknown_name(home):
    with pytest.raises(secrets.SecretError, match="not set"):
        secrets.load_secret("DEEPSEEK_API_KEY")
    write_secret(home, ".config/deepseek/api_key", "", 0o600)
    with pytest.raises(secrets.SecretError, match="empty"):
        secrets.load_secret("DEEPSEEK_API_KEY")
    (home / ".config/press-dispute-review/law_oc").parent.mkdir(parents=True)
    (home / ".config/press-dispute-review/law_oc").mkdir()
    os.chmod(home / ".config/press-dispute-review/law_oc", 0o700)
    with pytest.raises(secrets.SecretError, match="regular file"):
        secrets.load_secret("LAW_OPEN_DATA_OC")
    with pytest.raises(secrets.SecretError, match="unknown secret"):
        secrets.load_secret("PATH")


def test_blank_env_falls_back_to_file(home, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "   ")
    write_secret(home, ".config/deepseek/api_key", "file-value-4321", 0o600)
    assert secrets.load_secret("DEEPSEEK_API_KEY") == "file-value-4321"


def test_loaded_secret_is_redacted_from_logs(home, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", FAKE)
    secrets.load_secret("DEEPSEEK_API_KEY")
    buf = io.StringIO()
    lg = logging.getLogger("t.secret")
    lg.handlers = [logsafe.make_handler(buf)]
    lg.propagate = False
    lg.warning("calling with key %s now", FAKE)
    assert FAKE not in buf.getvalue() and logsafe.REDACTED in buf.getvalue()


def test_module_does_not_shadow_stdlib_secrets():
    import secrets as std
    assert hasattr(std, "token_hex")
    assert secrets.__name__ == "press_review.secrets"


# ---- logsafe --------------------------------------------------------------------------

def _logger(name):
    buf = io.StringIO()
    lg = logging.getLogger(name)
    lg.handlers = [logsafe.make_handler(buf)]
    lg.propagate = False
    lg.setLevel(logging.DEBUG)
    return lg, buf


def test_article_text_is_redacted(article):
    logsafe.register_article("h1", article)
    lg, buf = _logger("t.a")
    p1 = article["blocks"][2]["text"]
    lg.info("got %s", p1[5:30])                          # args 치환 후 검사
    lg.info("direct " + p1[10:20])
    lg.info("title=%s", article["blocks"][0]["text"])
    lg.info("ok request_id=abc count=3 sha=ac38fd55")
    out = buf.getvalue()
    assert out.count(logsafe.REDACTED) == 3
    assert p1[10:20] not in out and "빼돌린" not in out
    assert "request_id=abc count=3" in out


def test_boundary_eight_code_points(mk_article):
    art = mk_article([("t", "title", "제목입니다"), ("p", "body", "가나다라마바사아자차")])
    logsafe.register_article("h", art)
    lg, buf = _logger("t.b")
    lg.info("가나다라마바사")          # 7자: 통과
    lg.info("나다라마바사아자")        # 8자: 치환
    out = buf.getvalue()
    assert "가나다라마바사" in out and out.count(logsafe.REDACTED) == 1


def test_exception_text_is_redacted(article):
    logsafe.register_article("h", article)
    lg, buf = _logger("t.c")
    try:
        raise ValueError("bad: " + article["blocks"][2]["text"][:20])
    except ValueError:
        lg.exception("failed")
    out = buf.getvalue()
    assert logsafe.REDACTED in out and article["blocks"][2]["text"][:20] not in out and "Traceback" not in out


def test_forget_and_capacity(article):
    logsafe.register_article("h", article)
    logsafe.forget_article("h")
    lg, buf = _logger("t.d")
    lg.info(article["blocks"][2]["text"][:20])
    assert logsafe.REDACTED not in buf.getvalue()


def test_log_event_only_allowed_fields():
    lg, buf = _logger("t.e")
    logsafe.log_event(lg, "submit", request_id="r1", article_sha256="ab", count=2, text="원고 본문 전체", api_key="zzz")
    out = buf.getvalue()
    assert "request_id=r1" in out and "count=2" in out and "원고" not in out and "zzz" not in out


def test_configure_attaches_filtered_handler_once():
    lg = logsafe.configure(stream=io.StringIO())
    n = len(lg.handlers)
    logsafe.configure(stream=io.StringIO())
    assert len(lg.handlers) == n
    for h in list(lg.handlers):
        lg.removeHandler(h)
