import httpx

from cpm.providers.keycheck import HttpKeyValidator, MockKeyValidator, check_format

KEY = "sk-ant-api03-" + "Zq9Xw8Vu" * 4


def _client(status, seen):
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(status, json={})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_format():
    assert check_format(KEY).ok
    assert not check_format("nope").ok
    assert not check_format("sk-ant-admin01-" + "a" * 30).ok


def test_mock_makes_no_network_call():
    assert MockKeyValidator().validate(KEY).ok
    assert not MockKeyValidator().validate("sk-ant-api03-invalid-" + "a" * 20).ok


def test_http_validator_statuses():
    for status, ok in ((200, True), (401, False), (403, False), (500, False)):
        seen: list[httpx.Request] = []
        res = HttpKeyValidator(_client(status, seen)).validate(KEY)
        assert res.ok is ok and KEY not in res.message
        assert seen[0].url.host == "api.anthropic.com" and seen[0].method == "GET"


def test_http_validator_network_error():
    def boom(_req):
        raise httpx.ConnectError("x")

    res = HttpKeyValidator(httpx.Client(transport=httpx.MockTransport(boom))).validate(KEY)
    assert not res.ok and KEY not in res.message


def test_http_validator_skips_network_on_bad_format():
    seen: list[httpx.Request] = []
    assert not HttpKeyValidator(_client(200, seen)).validate("bad").ok
    assert seen == []
