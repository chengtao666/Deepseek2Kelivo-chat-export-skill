#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DeepSeek 分享链接 → Kelivo 可导入的对话文件（Chatbox 兼容 JSON）。

用法：
    python3 deepseek_share_bridge.py "https://chat.deepseek.com/share/xxxxxxxx"
    python3 deepseek_share_bridge.py xxxxxxxx --out ./my-chat.json
    python3 deepseek_share_bridge.py --selftest

只用标准库（urllib / json / re），不需要 pip 安装任何东西。

数据来源（DeepSeek 网页版自己用的接口，公开分享无需登录）：
    GET https://chat.deepseek.com/api/v0/share/content?share_id=<id>
    -> {"code":0,"data":{"biz_code":0,"biz_data":{"title":...,"model_type":...,
        "messages":[{"message_id","parent_id","role","fragments":[...],"inserted_at"}]}}}

输出（Kelivo 的 Chatbox 导入器接受的旧版 JSON 结构）：
    {"__exported_at", "chat-sessions-list":[...], "session:<id>":{...}}
"""

import argparse
import datetime
import json
import re
import sys
import urllib.error
import urllib.request
import uuid

API_URL = 'https://chat.deepseek.com/api/v0/share/content'
SHARE_HOST = 'https://chat.deepseek.com/'
USER_AGENT = (
    'Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36'
)

# biz_code 含义（来自 DeepSeek 前端的 GetShareContentErrorCode）
BIZ_CODE_MESSAGES = {
    0: 'OK',
    1: '分享不存在（链接错误，或已被作者删除）',
    2: '分享已被删除或封禁',
    3: '原始对话已被删除',
}

# fragment 类型：正文/推理/其它
USER_TEXT_TYPES = {'REQUEST'}
ASSISTANT_TEXT_TYPES = {'RESPONSE', 'TEMPLATE_RESPONSE'}
REASONING_TYPES = {'THINK'}
FILE_TYPES = {'FILE'}


class BridgeError(Exception):
    """可预期的失败（链接问题、网络问题、格式不符），带可直接展示给用户的说明。"""


def extract_share_id(raw):
    """从链接或裸 id 中取出 share_id。"""
    text = (raw or '').strip().strip('"\'')
    if not text:
        raise BridgeError('没有拿到链接。请把 DeepSeek 的分享链接整条粘贴进来。')

    m = re.search(r'/share/([A-Za-z0-9_-]{4,64})', text)
    if m:
        return m.group(1)

    m = re.search(r'share_id=([A-Za-z0-9_-]{4,64})', text)
    if m:
        return m.group(1)

    if re.fullmatch(r'[A-Za-z0-9_-]{4,64}', text):
        return text

    raise BridgeError(
        '这条内容里没有识别到 share_id。分享链接形如：'
        'https://chat.deepseek.com/share/xxxxxxxxxxxxxxxx'
    )


def fetch_share(share_id, timeout=30):
    """请求 DeepSeek 分享内容接口，返回解析后的 JSON。"""
    url = '%s?share_id=%s' % (API_URL, share_id)
    request = urllib.request.Request(
        url,
        headers={
            'User-Agent': USER_AGENT,
            'Accept': 'application/json, text/plain, */*',
            'Referer': SHARE_HOST + 'share/' + share_id,
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        },
        method='GET',
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        raise BridgeError('DeepSeek 接口返回 HTTP %s，稍后重试或换一个网络。' % error.code)
    except urllib.error.URLError as error:
        raise BridgeError('连不上 chat.deepseek.com（%s）。检查网络后重试。' % error.reason)
    except OSError as error:
        raise BridgeError('网络请求失败：%s' % error)

    try:
        return json.loads(body.decode('utf-8', 'replace'))
    except ValueError:
        raise BridgeError('接口返回的不是 JSON，可能被网络拦截或被要求人机验证。')


def _iso_from_ts(value):
    """时间戳 → ISO8601（UTC，Z 结尾）。

    DeepSeek 的 inserted_at 实测是**秒**（带小数，如 1748311499.84），
    但不同版本可能给毫秒或微秒，所以按数量级判断，不要写死单位。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    if number < 1e11:          # 秒（当前纪元的合理上界）
        seconds = number
    elif number < 1e14:        # 毫秒
        seconds = number / 1e3
    else:                      # 微秒
        seconds = number / 1e6
    try:
        stamp = datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return stamp.strftime('%Y-%m-%dT%H:%M:%S.') + '%03dZ' % (stamp.microsecond // 1000)


def _linear_chain(messages):
    """按 parent_id 还原一条主线（分享可能有分支，只取最后一条回复所在的链）。"""
    by_id = {}
    order = []
    for message in messages:
        message_id = message.get('message_id')
        if not message_id or message_id in by_id:
            continue
        by_id[message_id] = message
        order.append(message_id)

    if not order:
        return [], 0

    leaf = order[-1]
    chain = []
    seen = set()
    cursor = leaf
    while cursor and cursor in by_id and cursor not in seen:
        seen.add(cursor)
        chain.append(by_id[cursor])
        cursor = by_id[cursor].get('parent_id')
    chain.reverse()

    if len(chain) <= 1:
        # parent_id 缺失或全为 null 时退回原顺序
        chain = [by_id[message_id] for message_id in order]
    return chain, max(0, len(order) - len(chain))


def _message_text(message):
    """抽出 (正文, 推理, 附件标记列表)。"""
    fragments = message.get('fragments') or []
    user_text, assistant_text, reasoning = [], [], []
    attachments = []
    skipped = 0

    for fragment in fragments:
        if not isinstance(fragment, dict):
            continue
        ftype = str(fragment.get('type') or '').upper()
        content = fragment.get('content')
        content = content if isinstance(content, str) else ''
        if ftype in USER_TEXT_TYPES:
            if content.strip():
                user_text.append(content)
        elif ftype in ASSISTANT_TEXT_TYPES:
            if content.strip():
                assistant_text.append(content)
        elif ftype in REASONING_TYPES:
            if content.strip():
                reasoning.append(content)
        elif ftype in FILE_TYPES:
            name = fragment.get('file_name') or fragment.get('name') or '文件'
            attachments.append(str(name))
        else:
            skipped += 1

    if not user_text and not assistant_text and not reasoning:
        # 兜底：某些消息只在 content 里带文本
        plain = message.get('content')
        if isinstance(plain, str) and plain.strip():
            if str(message.get('role') or '').upper() == 'ASSISTANT':
                assistant_text.append(plain)
            else:
                user_text.append(plain)

    return ('\n\n'.join(user_text), '\n\n'.join(assistant_text),
            '\n\n'.join(reasoning), attachments, skipped)


def build_kelivo_bundle(payload, include_thinking=True, provider='', model=''):
    """把 DeepSeek 分享响应转成 Kelivo（Chatbox 导入器）能读的 JSON 对象。"""
    if not isinstance(payload, dict):
        raise BridgeError('接口返回结构异常。')

    code = payload.get('code')
    if code not in (0, None):
        raise BridgeError('接口报错 code=%s msg=%s' % (code, payload.get('msg')))

    data = payload.get('data') or {}
    biz_code = data.get('biz_code', 0)
    if biz_code not in (0, None):
        hint = BIZ_CODE_MESSAGES.get(biz_code, '未知原因')
        raise BridgeError('这块分享读不到：%s（biz_code=%s）' % (hint, biz_code))

    biz = data.get('biz_data') or {}
    title = str(biz.get('title') or '').strip()
    model_type = str(biz.get('model_type') or '').strip()
    messages = biz.get('messages') or []
    if not messages:
        raise BridgeError('分享里没有消息内容，可能是空对话或已被收回。')

    if not title or title.lower() in ('shared conversation', 'deepseek'):
        # 分享默认标题没有信息量，用首条用户消息前 40 字代替
        for candidate in messages:
            if str(candidate.get('role') or '').upper() != 'USER':
                continue
            for fragment in candidate.get('fragments') or []:
                if str(fragment.get('type') or '').upper() == 'REQUEST':
                    snippet = str(fragment.get('content') or '').strip()
                    if snippet:
                        title = snippet.splitlines()[0][:40]
                        break
            if title:
                break
    if not title:
        title = 'DeepSeek 对话'

    chain, dropped = _linear_chain(messages)
    now = datetime.datetime.now(datetime.timezone.utc)
    exported_at = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    base_ms = int(now.timestamp() * 1000)

    converted = []
    for index, message in enumerate(chain):
        role_raw = str(message.get('role') or '').upper()
        role = 'assistant' if role_raw == 'ASSISTANT' else 'user'
        user_text, assistant_text, reasoning, attachments, _skipped = _message_text(message)

        primary = assistant_text if role == 'assistant' else user_text
        parts = []
        if include_thinking and reasoning:
            parts.append({'type': 'reasoning', 'text': reasoning})
        if primary:
            parts.append({'type': 'text', 'text': primary})
        if attachments:
            parts.append({'type': 'text', 'text': '（附件：%s）' % '、'.join(attachments)})
        if not parts:
            continue

        stamp = _iso_from_ts(message.get('inserted_at'))
        if not stamp:
            stamp = _iso_from_ts(base_ms + index)

        converted.append({
            'id': str(message.get('message_id') or uuid.uuid4().hex),
            'role': role,
            'contentParts': parts,
            'timestamp': stamp,
        })

    if not converted:
        raise BridgeError('分享里没有可用的文本消息（可能全是图片或空回复）。')

    session_id = uuid.uuid4().hex
    thread_id = uuid.uuid4().hex
    first_stamp = converted[0].get('timestamp')

    session = {
        'id': session_id,
        'name': title,
        'settings': {
            'provider': provider,
            'modelId': model or model_type,
            'stream': True,
            'maxContextMessageCount': 64,
        },
        'threads': [{
            'id': thread_id,
            'name': title,
            'createdAt': first_stamp,
            'messages': converted,
        }],
        'messages': converted,
    }

    bundle = {
        '__exported_at': exported_at,
        'chat-sessions-list': [{'id': session_id, 'name': title, 'starred': False}],
        'session:%s' % session_id: session,
    }
    return bundle, {
        'title': title,
        'share_model': model_type,
        'messages': len(converted),
        'user_messages': sum(1 for msg in converted if msg['role'] == 'user'),
        'assistant_messages': sum(1 for msg in converted if msg['role'] == 'assistant'),
        'thinking_parts': sum(
            1 for msg in converted for part in msg['contentParts'] if part['type'] == 'reasoning'
        ),
        'dropped_branch_messages': dropped,
        'raw_messages': len(messages),
    }


def validate_bundle(bundle):
    """按 Kelivo 导入器的要求自检，返回问题列表（空表示可用）。"""
    problems = []
    sessions = bundle.get('chat-sessions-list')
    if not isinstance(sessions, list) or not sessions:
        problems.append('缺少 chat-sessions-list')
        return problems

    for meta in sessions:
        if not isinstance(meta, dict) or not meta.get('id'):
            problems.append('chat-sessions-list 条目缺少 id')
            continue
        session = bundle.get('session:%s' % meta['id'])
        if not isinstance(session, dict):
            problems.append('缺少 session:%s 对象' % meta['id'])
            continue
        if str(session.get('id')) != str(meta['id']):
            problems.append('session id 与列表不一致')
        threads = session.get('threads')
        if not isinstance(threads, list) or not threads:
            problems.append('session 缺少 threads')
            continue
        for thread in threads:
            if not thread.get('id'):
                problems.append('thread 缺少 id')
            messages = thread.get('messages')
            if not isinstance(messages, list) or not messages:
                problems.append('thread 缺少 messages')
                continue
            for message in messages:
                if message.get('role') not in ('user', 'assistant', 'system', 'tool'):
                    problems.append('非法 role: %r' % message.get('role'))
                parts = message.get('contentParts')
                if not isinstance(parts, list) or not parts:
                    problems.append('消息缺少 contentParts')
                    continue
                for part in parts:
                    if part.get('type') not in ('text', 'reasoning', 'image', 'file', 'info'):
                        problems.append('非法 part type: %r' % part.get('type'))
    return problems


def _selftest():
    payload = {
        'code': 0,
        'msg': '',
        'data': {
            'biz_code': 0,
            'biz_msg': '',
            'biz_data': {
                'title': '设定测试对话',
                'model_type': 'deepseek_chat',
                'messages': [
                    {
                        'message_id': 'm1', 'parent_id': None, 'role': 'USER',
                        'inserted_at': 1710000000000,
                        'fragments': [{'type': 'REQUEST', 'content': '先把角色卡设定读一遍。'}],
                    },
                    {
                        'message_id': 'm2', 'parent_id': 'm1', 'role': 'ASSISTANT',
                        'inserted_at': 1710000005000,
                        'fragments': [
                            {'type': 'THINK', 'content': '先确认设定范围。'},
                            {'type': 'RESPONSE', 'content': '读完了，主线是旧港口。'},
                            {'type': 'TOOL_SEARCH', 'content': ''},
                        ],
                    },
                    {
                        # 分支：另一条回复，应被丢弃
                        'message_id': 'm2b', 'parent_id': 'm1', 'role': 'ASSISTANT',
                        'inserted_at': 1710000006000,
                        'fragments': [{'type': 'RESPONSE', 'content': '这是被放弃的分支。'}],
                    },
                    {
                        'message_id': 'm3', 'parent_id': 'm2', 'role': 'USER',
                        'inserted_at': 1710000010000,
                        'fragments': [{'type': 'REQUEST', 'content': '继续，从雨夜开始。'}],
                    },
                ],
            },
        },
    }
    bundle, stats = build_kelivo_bundle(payload)
    problems = validate_bundle(bundle)
    assert not problems, problems
    assert stats['messages'] == 3, stats
    assert stats['thinking_parts'] == 1, stats
    session = bundle['session:%s' % bundle['chat-sessions-list'][0]['id']]
    roles = [msg['role'] for msg in session['threads'][0]['messages']]
    assert roles == ['user', 'assistant', 'user'], roles
    assert '被放弃的分支' not in json.dumps(bundle, ensure_ascii=False)
    print('SELFTEST OK')
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='把 DeepSeek 分享链接转成 Kelivo 可导入的对话 JSON。'
    )
    parser.add_argument('link', nargs='?', help='DeepSeek 分享链接或 share_id')
    parser.add_argument('--out', help='输出文件路径（默认 ./deepseek-share-<id>.json）')
    parser.add_argument('--no-thinking', action='store_true', help='丢弃思考内容，只保留正文')
    parser.add_argument('--provider', default='', help='写入会话的 provider（可留空）')
    parser.add_argument('--model', default='', help='写入会话的 modelId（默认用分享里的 model_type）')
    parser.add_argument('--from-json', help='不联网，直接读一份接口响应 JSON（调试用）')
    parser.add_argument('--selftest', action='store_true', help='用内置样例自检转换逻辑')
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()

    if args.from_json:
        with open(args.from_json, 'r', encoding='utf-8') as handle:
            payload = json.load(handle)
        share_id = 'local'
    else:
        if not args.link:
            parser.error('需要分享链接，或使用 --selftest / --from-json')
        share_id = extract_share_id(args.link)
        payload = fetch_share(share_id)

    bundle, stats = build_kelivo_bundle(
        payload,
        include_thinking=not args.no_thinking,
        provider=args.provider,
        model=args.model,
    )

    problems = validate_bundle(bundle)
    if problems:
        raise BridgeError('生成的 JSON 自检未通过：%s' % '；'.join(problems))

    if args.out:
        out_path = args.out
    else:
        suffix = share_id[:8] if share_id != 'local' else uuid.uuid4().hex[:8]
        out_path = 'deepseek-share-%s.json' % suffix

    with open(out_path, 'w', encoding='utf-8') as handle:
        json.dump(bundle, handle, ensure_ascii=False, indent=2)

    result = dict(stats)
    result['share_id'] = share_id
    result['output'] = out_path
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except BridgeError as error:
        print('ERROR: %s' % error, file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)
