# -*- coding: utf-8 -*-
"""飞书自建应用机器人：收到“登录”指令后回复群消息，再发邮件。

安装依赖：python -m pip install lark-oapi
运行：python 影刀自动触发/小红书登录触发.py
运行前在脚本顶部填写 APP_ID、APP_SECRET 和 ALLOWED_CHAT_ID。
"""

import json
import smtplib
import ssl
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from urllib.parse import quote
from urllib.request import Request, urlopen

import lark_oapi as lark


# 在本地填写你的飞书自建应用凭据。
APP_ID = ""
APP_SECRET = ""

# 必填：只接收此群聊中 @本机器人 的文本。
ALLOWED_CHAT_ID = ""

EMAIL_ADDRESS = ""
EMAIL_AUTH_CODE = ""
SMTP_HOST = "smtp.yeah.net"
SMTP_PORT = 465
SMTP_SECURITY = "ssl"  # 支持 ssl 或 starttls
TO_EMAIL = ""
MAIL_SUBJECT = "影刀-小红书登录"
MAIL_BODY = """
"""

BOT_OPEN_ID = ""
_TOKEN = ""
_TOKEN_DEADLINE = 0.0
_TOKEN_LOCK = threading.Lock()
_SEEN = OrderedDict()
_SEEN_LOCK = threading.Lock()
# 在后台串行处理群回复和邮件，避免阻塞飞书事件回调。
_WORKER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="feishu-command")


def _api_json(path, token=None, body=None):
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = "Bearer " + token
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(
        "https://open.feishu.cn/open-apis" + path,
        data=payload,
        headers=headers,
        method="GET" if body is None else "POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            result = json.load(response)
    except Exception:
        raise RuntimeError("飞书接口访问失败，请检查网络及应用配置") from None
    if result.get("code", 0) != 0:
        raise RuntimeError("飞书接口错误码：" + str(result.get("code")))
    return result


def _get_bot_open_id(app_id, app_secret):
    token = _get_token(app_id, app_secret)
    info = _api_json("/bot/v3/info", token=token)
    open_id = (info.get("bot") or {}).get("open_id")
    if not open_id:
        raise RuntimeError("未取得机器人身份，请确认应用已启用机器人能力")
    return open_id


def _get_token(app_id=None, app_secret=None):
    global _TOKEN, _TOKEN_DEADLINE
    with _TOKEN_LOCK:
        if _TOKEN and time.monotonic() < _TOKEN_DEADLINE:
            return _TOKEN
        auth = _api_json(
            "/auth/v3/tenant_access_token/internal",
            body={"app_id": app_id or APP_ID.strip(),
                  "app_secret": app_secret or APP_SECRET.strip()},
        )
        token = auth.get("tenant_access_token")
        if not token:
            raise RuntimeError("未取得应用访问令牌")
        _TOKEN = token
        _TOKEN_DEADLINE = time.monotonic() + max(0, int(auth.get("expire", 7200)) - 120)
        return token


def _reply(message_id, text):
    _api_json(
        "/im/v1/messages/" + quote(message_id, safe="") + "/reply",
        token=_get_token(),
        body={"msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)},
    )


def _send_email():
    if not EMAIL_ADDRESS.strip() or not EMAIL_AUTH_CODE.strip():
        raise ValueError("请填写 EMAIL_ADDRESS 和 EMAIL_AUTH_CODE")
    security = SMTP_SECURITY.strip().lower()
    if security not in ("ssl", "starttls"):
        raise ValueError("SMTP_SECURITY 必须为 ssl 或 starttls")
    recipient = TO_EMAIL.strip() or EMAIL_ADDRESS.strip()
    mail = EmailMessage()
    mail["From"] = EMAIL_ADDRESS.strip()
    mail["To"] = recipient
    mail["Subject"] = MAIL_SUBJECT
    mail.set_content(MAIL_BODY)
    context = ssl.create_default_context()
    if security == "ssl":
        smtp = smtplib.SMTP_SSL(SMTP_HOST.strip(), int(SMTP_PORT), timeout=20, context=context)
    else:
        smtp = smtplib.SMTP(SMTP_HOST.strip(), int(SMTP_PORT), timeout=20)
    with smtp:
        if security == "starttls":
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
        smtp.login(EMAIL_ADDRESS.strip(), EMAIL_AUTH_CODE.strip())
        smtp.send_message(mail, from_addr=EMAIL_ADDRESS.strip(), to_addrs=[recipient])


def _process_command(text, message_id):
    is_login = text == "登录"
    reply = "进入登录流程，稍后请你扫描二维码登录" if is_login else "暂不支持该指令"
    try:
        # 必须先完成飞书回复，再开始发送邮件。
        _reply(message_id, reply)
    except Exception:
        print("飞书回复失败，请检查应用发送消息权限及网络；本次未发邮件", file=sys.stderr, flush=True)
        return
    if not is_login:
        return
    try:
        _send_email()
        print("登录邮件已提交给 SMTP 服务器", flush=True)
    except Exception:
        print("登录邮件发送失败，请检查邮箱参数、授权码及网络", file=sys.stderr, flush=True)
        try:
            _reply(message_id, "邮件发送失败，请检查邮箱配置")
        except Exception:
            print("邮件失败提示回复失败", file=sys.stderr, flush=True)


def on_message(data):
    try:
        event = data.event
        message = event.message
        # 仅接收指定群里用户 @本机器人 的消息。
        if event.sender.sender_type != "user":
            return
        if message.chat_type != "group":
            return
        if message.chat_id != ALLOWED_CHAT_ID.strip():
            return
        mentions = message.mentions or []
        bot_mention = next(
            (item for item in mentions
             if getattr(getattr(item, "id", None), "open_id", None) == BOT_OPEN_ID),
            None,
        )
        if bot_mention is None or not bot_mention.key:
            return

        text = ""
        if message.message_type == "text":
            content = json.loads(message.content)
            raw_text = content.get("text", "")
            if not isinstance(raw_text, str):
                return
            # 飞书将 @ 替换为 mentions.key；定位本机器人后的正文。
            _, found, text = raw_text.partition(bot_mention.key)
            if not found:
                return
            for item in sorted(mentions, key=lambda item: len(item.key or ""), reverse=True):
                if item.key:
                    is_bot = getattr(getattr(item, "id", None), "open_id", None) == BOT_OPEN_ID
                    text = text.replace(item.key, "" if is_bot else "@" + (item.name or ""))
            text = text.strip()
        message_id = message.message_id
        if not message_id:
            return
        # 同一消息事件重投时不重复发邮件；重启程序后记录会清空。
        with _SEEN_LOCK:
            if message_id in _SEEN:
                return
            _SEEN[message_id] = None
            if len(_SEEN) > 5000:
                _SEEN.popitem(last=False)
        _WORKER.submit(_process_command, text, message_id)
    except Exception:
        # 不打印原始事件或异常详情，避免凭据和消息元数据被额外记录。
        print("消息处理失败，请检查消息格式", file=sys.stderr, flush=True)


def main():
    global BOT_OPEN_ID
    app_id = APP_ID.strip()
    app_secret = APP_SECRET.strip()
    if not app_id or not app_secret:
        raise RuntimeError("App ID 和 App Secret 必须填写")
    if not ALLOWED_CHAT_ID.strip():
        raise RuntimeError("ALLOWED_CHAT_ID 必须填写目标群的 chat_id")
    BOT_OPEN_ID = _get_bot_open_id(app_id, app_secret)
    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )
    client = lark.ws.Client(
        app_id,
        app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.ERROR,
    )
    print("正在监听，请保持程序运行，在群里 @机器人 后发送文字。", file=sys.stderr, flush=True)
    client.start()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("监听已停止")
    except Exception:
        print("启动失败，请检查依赖、应用凭据、机器人能力及长连接配置", file=sys.stderr)
        sys.exit(1)
