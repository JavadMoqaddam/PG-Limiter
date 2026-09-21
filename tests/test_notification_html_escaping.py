"""Regression test: untrusted panel usernames are HTML-escaped before being
embedded in an HTML-parse-mode Telegram notification.

A panel- or MITM-controlled username containing Telegram inline markup used to
reach ``send_enable_notification`` unescaped and render live in the operator
channel (or trigger a parse error that dropped the notice). The username must
now be escaped at the interpolation site.
"""

import telegram_bot.send_message as sm


async def test_enable_notification_escapes_username(monkeypatch):
    captured = {}

    async def fake_send(msg):
        captured["msg"] = msg

    monkeypatch.setattr(sm, "send_disable_enable_log", fake_send)

    await sm.send_enable_notification("evil<b>x</b>")

    msg = captured["msg"]
    # The raw markup must not survive into the outgoing message...
    assert "evil<b>x</b>" not in msg
    # ...it is escaped instead.
    assert "evil&lt;b&gt;x&lt;/b&gt;" in msg
