"""邮件发送 —— 决策⑮ 的「SMTP 可选 + 自动降级」。

发两类信，都是**验证码**（⑭ 邮箱验证，对齐原型）：
- 注册：`send_code(..., purpose="register")`
- 忘记密码：`send_code(..., purpose="reset")`

**未配 SMTP 时返回 False**（而不是抛异常），调用方据此把注册口关掉。
"""

from __future__ import annotations

import logging
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr

from .config import Settings

log = logging.getLogger("papershelf.mailer")

# SMTP 连接与重试参数（数值来自 2026-09-11 对部署机的实测，不是拍的）。
#
# 实测：`smtp.gmail.com` 在部署机上的 DNS 池只有 4 个 IP，其中一个
# （`173.194.43.108`）**37.5% 概率被抽中且 100% 连不通**（超时），另外 3 个 100% 可用。
# 也就是说**单次尝试的失败率高达 37.5%**，5 次尝试把它压到 0.375^5 ≈ 0.7%。
#
# 超时取 6 秒：Gmail 正常握手 <0.5 秒，不可达的 IP 靠超时判定，
# 6 秒足够区分两者又不至于让"抽中坏 IP"的代价过大。
# 最坏情况（连续 5 次都抽中坏 IP）：5*6 + (0.4+0.8+1.2+1.6) ≈ 34 秒，且概率仅 0.7%。
_TIMEOUT = 6
_ATTEMPTS = 5
_RETRY_BACKOFF_SECONDS = 0.4

# 用途 → 邮件文案。主题可在 .env 里整条覆盖（PAPERSHELF_SMTP_SUBJECT）。
_SUBJECT = {
    "register": "papershelf 注册验证码",
    "reset": "papershelf 重置密码验证码",
}
_TITLE = {
    "register": "注册验证码",
    "reset": "重置密码验证码",
}
_ACTION = {
    "register": "完成注册",
    "reset": "重置密码",
}


def _from_header(settings: Settings) -> str:
    """`formataddr` 会正确转义显示名，避免名字里的逗号/引号破坏邮件头。"""
    if settings.smtp_from_name:
        return formataddr((settings.smtp_from_name, settings.smtp_from))
    return settings.smtp_from


def _subject(settings: Settings, purpose: str) -> str:
    return (settings.smtp_subject or "").strip() or _SUBJECT.get(purpose, "papershelf 验证码")


def _smtp(settings: Settings) -> smtplib.SMTP:
    """统一的连接/登录 —— **465 走 SMTP_SSL，其余走 SMTP(+STARTTLS)**。

    ⚠️ 这个分支必须两处共用：曾有的缺陷是"注册走 SSL、分享那条忘了分支"，
    在 `SMTP_PORT=465`（Gmail 的默认用法）下后者**必然失败**。

    ⚠️ 登录失败必须**显式关掉连接**：`smtplib` 的构造已经建立了 TCP 连接，
    这里抛异常的话对象不会被回收，socket 就漏了。
    """
    if settings.smtp_port == 465:
        s: smtplib.SMTP = smtplib.SMTP_SSL(
            settings.smtp_host, settings.smtp_port,
            context=ssl.create_default_context(), timeout=_TIMEOUT,
        )
    else:
        s = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=_TIMEOUT)
        try:
            if settings.smtp_tls:
                s.starttls(context=ssl.create_default_context())
        except Exception:
            s.close()
            raise
    if settings.smtp_user:
        try:
            s.login(settings.smtp_user, settings.smtp_pass)
        except Exception:
            s.close()
            raise
    return s


def _send_once(settings: Settings, msg: EmailMessage) -> None:
    with _smtp(settings) as s:
        s.send_message(msg)


def _send(settings: Settings, to_email: str, subject: str, body: str) -> bool:
    """所有发信的公共出口：连接 → 登录 → 发送 → 关闭，异常一律降级为 False。

    **为什么要重试**（2026-09-11 生产实测）：`smtp.gmail.com` 的 DNS 会在多个 IP
    之间轮转，而这台部署机能连通的只是其中一部分 —— 实测 10 次全新解析里有 2 次
    连 465 直接超时（约 20% 失败率）。`smtplib` 只解析一次、只连一个地址，
    所以"单次尝试"意味着**每 5 次注册就有 1 次收不到验证码**，而本地用假 SMTP
    服务器测试时永远复现不出来（本地不轮转、也没有到 Gmail 的链路）。

    每次重试都会重新做一次 DNS 解析，因此换到可用 IP 的概率很高：
    实测首次超时后第 2 次即成功。重试只增加延迟，不会造成重复投递
    （连接都没建立起来，邮件根本没发出去）。
    """
    if not settings.smtp_configured:
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = _from_header(settings)
    msg["To"] = to_email
    msg.set_content(body)

    last_exc: Exception | None = None
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            _send_once(settings, msg)
            if attempt > 1:
                log.info("邮件第 %d 次尝试成功（%s）", attempt, to_email)
            return True
        except Exception as exc:  # 邮件失败不该阻断业务流程（注册/重置会据此提示重试）
            last_exc = exc
            if attempt < _ATTEMPTS:
                time.sleep(_RETRY_BACKOFF_SECONDS * attempt)

    log.warning("邮件发送失败（%s，已试 %d 次）：%s", to_email, _ATTEMPTS, last_exc)
    return False


def send_code(settings: Settings, to_email: str, code: str, purpose: str) -> bool:
    """发送验证码邮件（`purpose` = register | reset）。"""
    minutes = max(1, settings.code_ttl_seconds // 60)
    body = (
        f"你的{_TITLE.get(purpose, '验证码')}是：{code}\n\n"
        f"请在 {minutes} 分钟内用它{_ACTION.get(purpose, '完成验证')}。\n"
        "为保护账号安全，请勿把验证码转发给任何人。\n\n"
        "如果这不是你本人的操作，忽略本邮件即可。\n"
    )
    return _send(settings, to_email, _subject(settings, purpose), body)


def send_share_notice(settings: Settings, to_email: str, url: str) -> bool:
    """分享链接通过邮件发送（可选能力，未配 SMTP 返回 False）。"""
    return _send(settings, to_email, "papershelf 分享链接",
                 f"有人向你分享了一份阅读计划：\n{url}\n")
