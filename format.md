# 接口契约与导入格式

两端结构都以源码与线上接口核对过。改动任何一侧前先读这页。

## 一、DeepSeek 分享接口

```
GET https://chat.deepseek.com/api/v0/share/content?share_id=<share_id>
```

- 无需登录、无需 token，带常规浏览器 UA 即可。
- `share_id` 就是链接 `/share/` 后面那段。

响应：

```json
{
  "code": 0,
  "msg": "",
  "data": {
    "biz_code": 0,
    "biz_msg": "",
    "biz_data": {
      "title": "对话标题",
      "model_type": "deepseek_chat | deepseek_reasoner",
      "messages": []
    }
  }
}
```

- `code != 0`：传输层或参数错误。
- `data.biz_code`：业务码。`0` 成功；`1` 分享不存在；`2` 分享被删除或封禁；`3` 原始会话已删除。

### message 对象

| 字段 | 说明 |
|---|---|
| `message_id` | 消息 id（字符串） |
| `parent_id` | 父消息 id，根消息为 `null`；重新生成会产生分支 |
| `role` | `USER` / `ASSISTANT`（大写） |
| `inserted_at` | **秒**级时间戳（带小数，实测 `1748311499.84` → 2025-05-27）。按数量级自动判断单位，别写死毫秒 |
| `fragments` | 内容片段数组，正文实际在这里 |
| `accumulated_token_usage` | 可选，token 用量 |
| `thinking_enabled` | 可选，是否开了思考 |

### fragment 对象

片段类型（取自 DeepSeek 前端常量）：

- `REQUEST` —— 用户发出的正文
- `RESPONSE` / `TEMPLATE_RESPONSE` —— 助手正文
- `THINK` —— 思考内容
- `SEARCH` / `TOOL_SEARCH` / `TOOL_OPEN` / `TOOL_FIND` —— 联网检索与工具片段
- `FILE` —— 附件引用
- `TIP` / `READ_LINK` —— 提示与读链

文本取 `content` 字段。转换脚本只搬 `REQUEST` / `RESPONSE` / `TEMPLATE_RESPONSE` / `THINK` / `FILE`，其余忽略并计数。

### 标题

`biz_data.title` 对分享对话常常是占位值 `Shared Conversation`（DeepSeek 不保存分享标题）。
脚本在这种情况下用首条用户消息的第一行前 40 字作为会话名，否则导入后一堆会话全叫同一个名字。

### 分支处理

`messages` 是**扁平数组**，不是线程栈。同一个 `parent_id` 下可能有多条 `ASSISTANT`（重新生成过）。

正确做法：从数组最后一条消息沿 `parent_id` 往回走到根，再反转，得到「最后一条回复」所在主线。其余消息属于被放弃的分支，丢弃并在摘要里报数量。

## 二、Kelivo 的导入格式

Kelivo 自带两个外部导入器：Chatbox 与 Cherry Studio。这里用 **Chatbox 旧版 JSON**：纯 JSON、结构最简单，桌面与手机都支持（文件选择器允许 `.json` 与 `.zip`）。

### 根对象

```json
{
  "__exported_at": "2026-11-17T05:38:14Z",
  "chat-sessions-list": [
    { "id": "<sessionId>", "name": "对话标题", "starred": false }
  ],
  "session:<sessionId>": { }
}
```

校验规则（Kelivo 侧）：根对象必须至少有 `chat-sessions-list`（数组）**或** `settings.providers`（对象）；两者都没有即判定「不是 Chatbox 导出文件」。

### 会话对象

```json
{
  "id": "<sessionId>",
  "name": "对话标题",
  "settings": {
    "provider": "",
    "modelId": "deepseek_chat",
    "stream": true,
    "maxContextMessageCount": 64
  },
  "threads": [
    { "id": "<threadId>", "name": "对话标题", "createdAt": "…", "messages": [] }
  ],
  "messages": []
}
```

- `session:<id>` 里的 `id` 必须与 `chat-sessions-list` 的 `id` 完全一致，否则导入报错。
- 每个 `thread` 变成 Kelivo 里的一个「话题（topic）」。想一次搬多段对话就放多个 thread。
- `settings.provider` / `settings.modelId` 用于绑定助手模型；留空也能导入，只是新建会话用默认模型。
- 会话级 `messages` 是无 `threads` 时的兜底；两个都给最稳。

### 消息对象

```json
{
  "id": "a2",
  "role": "user | assistant | system | tool",
  "contentParts": [
    { "type": "reasoning", "text": "思考内容" },
    { "type": "text", "text": "正文" }
  ],
  "timestamp": "2025-11-16T13:33:25.000Z"
}
```

关键坑（来自 Kelivo 导入器源码）：

1. **正文只在 `contentParts` 里**。导入器不读 `msg['content']`；没有 `contentParts` 就是空消息。
2. `contentParts` 允许的 `type`：`text`、`reasoning`、`image`、`file`、`info`。
3. `reasoning` 渲染为可折叠的思考段——DeepSeek 的 `THINK` 应映射到这里，而不是拼进正文。
4. `role` 取值：`user` / `assistant` / `system` / `tool`。第一条 `system` 会成为助手系统提示词（不显示为消息），其余 `system` 变成可见备注。
5. `timestamp` 支持 ISO8601 字符串；解析失败时导入器退回「导出时间 + 序号毫秒」，不会崩，但顺序可能看起来奇怪。
6. id 必须唯一。重复 `message_id` 在合并模式会被去重，导致丢消息。

## 三、导入模式

「从 Chatbox 导入」会先问模式：

- **合并（Merge）**：追加到现有数据，仅 id 冲突时去重。默认选这个。
- **覆盖（Overwrite）**：清空现有会话。只有用户明确要求才用；覆盖模式还要求 JSON 里确实带 `session:*` 对象，否则导入器直接报错。

## 四、已知边界

- 分享只有文字层：图片附件只能拿到文件名/引用（`FILE` 片段），拿不到原始图片字节；要图片得让用户在 DeepSeek 里单独保存。
- 分享接口是网页版内部接口，DeepSeek 改版可能变动。症状是 `code` 正常但 `biz_data` 结构变了——那时只需改脚本里 `build_kelivo_bundle()` 的字段映射，其余不用动。
- 这是「把内容搬过去」，不是「把账号搬过去」：迁过去后是 Kelivo 里一条普通会话，与 DeepSeek 端不再同步。
