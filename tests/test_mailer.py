"""邮件发送层的重试行为（离线，不联网、不真连 SMTP）。

⚠️ 为什么必须单独测这一层：`tests/test_server.py` 里的邮件夹具是把 `mailer._send`
（公共出口）整个换掉的 —— 它只验证"业务在什么时机发信"，**根本不走 SMTP 连接逻辑**。
于是 2026-09-11 这个缺陷被漏掉了：

    生产实测：`smtp.gmail.com` 的 DNS 在多个 IP 间轮转，部署机能连通的只是其中一部分，
    10 次全新解析里约 2 次连 465 直接超时（≈20% 失败率）。

`smtplib` 只解析一次、只连一个地址，所以"单次尝试"意味着**每 5 次注册就有 1 次
收不到验证码**，而本地用假 SMTP 服务器测试时永远复现不出来（本地不轮转、
也没有到 Gmail 的那条链路）。

这里用"先失败后成功"的假传输层来钉住重试语义，不需要网络。
"""

from __future__ import annotations

import smtplib

import pytest

from papershelf.server import mailer
from papershelf.server.config import Settings


@pytest.fixture()
def smtp_settings(monkeypatch) -> Settings:
    """一份"看起来配好了 SMTP"的 Settings（不会真的去连）。"""
    monkeypatch.setenv("PAPERSHELF_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("PAPERSHELF_SMTP_PORT", "465")
    monkeypatch.setenv("PAPERSHELF_SMTP_USER", "u@example.com")
    monkeypatch.setenv("PAPERSHELF_SMTP_PASS", "pw")
    monkeypatch.setenv("PAPERSHELF_SMTP_FROM", "u@example.com")
    from papershelf.server.config import get_settings

    return get_settings(refresh=True)


def test_retries_after_transient_timeout(smtp_settings, monkeypatch) -> None:
    """核心回归：**第一次超时、第二次成功**，`_send` 必须返回 True。

    这正是生产上观察到的形态（首次 20.1s 超时 → 第 2 次 2.5s 成功）。
    """
    monkeypatch.setattr(mailer, "_RETRY_BACKOFF_SECONDS", 0)  # 测试里不真睡
    calls: list[int] = []

    def flaky(settings, msg):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("timed out")

    monkeypatch.setattr(mailer, "_send_once", flaky)
    assert mailer._send(smtp_settings, "a@b.edu.cn", "主题", "正文") is True
    assert len(calls) == 2, "首次超时后必须重试，而不是直接放弃"


def test_gives_up_after_all_attempts(smtp_settings, monkeypatch) -> None:
    """全都失败时返回 False（调用方据此提示用户重试 / 撤掉验证码）。"""
    monkeypatch.setattr(mailer, "_RETRY_BACKOFF_SECONDS", 0)
    calls: list[int] = []

    def always_fail(settings, msg):
        calls.append(1)
        raise smtplib.SMTPException("nope")

    monkeypatch.setattr(mailer, "_send_once", always_fail)
    assert mailer._send(smtp_settings, "a@b.edu.cn", "主题", "正文") is False
    assert len(calls) == mailer._ATTEMPTS


def test_no_retry_when_smtp_not_configured(monkeypatch) -> None:
    """没配 SMTP 就直接返回 False，别去重试 —— 否则每次注册都白等 45 秒。"""
    monkeypatch.setenv("PAPERSHELF_SMTP_HOST", "")
    monkeypatch.setenv("PAPERSHELF_SMTP_FROM", "")
    from papershelf.server.config import get_settings

    settings = get_settings(refresh=True)
    monkeypatch.setattr(mailer, "_RETRY_BACKOFF_SECONDS", 0)

    def should_not_be_called(settings, msg):
        raise AssertionError("未配 SMTP 时不应尝试发送")

    monkeypatch.setattr(mailer, "_send_once", should_not_be_called)
    assert mailer._send(settings, "a@b.edu.cn", "主题", "正文") is False


def test_retry_budget_survives_measured_failure_rate(smtp_settings) -> None:
    """重试次数必须把实测失败率压到可忽略，且最坏总耗时仍在用户可等待范围。

    2026-09-11 实测：单个 IP 的抽取是**有偏的** —— 4 个 IP 里有一个 37.5% 概率
    被抽中且 100% 连不通，因此单次尝试失败率 ≈37.5%。
    5 次尝试把它压到 0.375^5 ≈ 0.7%；如果只重试 2 次（共 3 次）还剩 5.3%，
    实测就是"每 8 次仍然失败 1 次"，不能接受。
    """
    assert mailer._ATTEMPTS >= 4, "按实测失败率，3 次尝试仍有约 5% 失败"
    assert 0.375 ** mailer._ATTEMPTS < 0.01, "重试后残留失败率应低于 1%"
    assert mailer._TIMEOUT <= 8, "Gmail 正常握手 <0.5s，超时不该给太大"
    worst = mailer._ATTEMPTS * mailer._TIMEOUT + sum(
        mailer._RETRY_BACKOFF_SECONDS * i for i in range(1, mailer._ATTEMPTS)
    )
    assert worst <= 40, f"最坏耗时 {worst}s 太久，用户会以为页面卡死"


def test_465_uses_implicit_ssl() -> None:
    """465 是**隐式 TLS**，必须用 SMTP_SSL 构造（不能先明文再 starttls）。

    这条同时是给"分享通知那条分支忘了加 465"的回归 —— 两处共用 `_smtp()` 才不会漏。
    """
    import inspect

    src = inspect.getsource(mailer._smtp)
    assert "SMTP_SSL" in src and "465" in src
