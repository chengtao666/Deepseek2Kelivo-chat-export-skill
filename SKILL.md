---
name: deepseek-share-bridge
description: 把 DeepSeek 网页版分享链接转成 Kelivo 可导入的对话文件。用户粘贴 chat.deepseek.com/share/… 链接，或说要把 DeepSeek 对话搬到 Kelivo、迁移聊天记录时使用。
---

# DeepSeek 分享 → Kelivo 导入

把一条 DeepSeek 分享链接变成 Kelivo 能直接导入的对话文件。用户只需要粘链接，剩下是跑脚本、给路径、说清导入三步。

Kelivo 和 DeepSeek 是两个独立客户端，不能互相直接读数据库。这里走**转换器**路线：DeepSeek 的公开分享接口 → 中间 JSON → Kelivo 自带的「从 Chatbox 导入」。所以结论永远是「生成文件 + 在 Kelivo 里导入」，不要声称能一键直接落库。

## 什么时候用

- 用户给出 `https://chat.deepseek.com/share/xxxxxxxx` 这类链接，想把这段对话搬到 Kelivo。
- 用户说「迁移聊天记录」「把 DeepSeek 的对话导进 Kelivo」「恢复那段对话」。
- 用户只有分享链接，没有导出文件。

不适用：DeepSeek 里没生成过分享链接的私有会话——只能让用户先去 DeepSeek 点「分享」拿到链接。

## 步骤

1. **拿到链接**。用户没给就问一句，不要自己猜 id。
2. **跑转换脚本**（需要能执行命令的 workspace）：

   ```bash
   python3 scripts/deepseek_share_bridge.py "https://chat.deepseek.com/share/xxxxxxxx"
   ```

   可选参数：`--out <路径>`、`--no-thinking`（丢掉思考内容）、`--provider` / `--model`（写进会话设置）、`--selftest`（自检，不联网）。

   脚本只用 Python 标准库，不需要 pip。成功时 stdout 输出一段 JSON 摘要，含 `output`（生成文件路径）、`messages`、`thinking_parts` 等；失败时退出码 2，stderr 是一句可以直接转述给用户的中文原因。

3. **读摘要并核对**。把 `output` 路径、消息条数、是否保留思考内容告诉用户。`dropped_branch_messages` 大于 0 说明只取了最后一条回复所在的主线（分享里存在重新生成的分支），属预期行为。

4. **给导入路径**（Kelivo 里的真实菜单）：

   设置 → 数据与备份 → **从 Chatbox 导入** → 选刚生成的 `.json` → 导入模式选 **合并（Merge）**。

   覆盖模式会清掉现有会话；除非用户明确要求，一律建议合并。

5. **收尾**：说明导入后是一条独立会话（名字取分享标题），思考内容以折叠的 reasoning 段保留。

## 环境不满足时

按顺序降级，别直接说做不到：

- **没有 Python**：先试 `python`（Windows）或 `python3`；都没有就查 `node`，用等价的最小转换（脚本只做三件事：取 share_id、GET 接口、按 `references/format.md` 的结构拼 JSON）。
- **有网络但没 shell**：让模型自己取数据——请求 `GET https://chat.deepseek.com/api/v0/share/content?share_id=<id>`，把 `data.biz_data.messages` 按 `references/format.md` 的结构写成 JSON，用文件写入工具存成 `.json`，再走第 4 步导入。
- **完全离线**：说明这需要一次联网请求；可以让用户在电脑上跑脚本，把生成的 json 传到手机再导入。

## 三条硬规则

- **交付前自检**：确认输出文件里 `chat-sessions-list`、`session:<id>`、`threads[].messages[].contentParts` 都在。Kelivo 导入器只认 `contentParts`，消息没它就是空消息。
- **思考内容要说实话**：默认保留 `THINK` 为 reasoning 段；用了 `--no-thinking` 就讲清楚丢了什么。
- **时间不要盖平**：`timestamp` 取 DeepSeek 的 `inserted_at`；缺失时才用当前时间 + 序号兜底，否则导入后顺序看起来像乱序。

## 失败对照

| 现象 | 真实原因 | 处理 |
|---|---|---|
| `分享不存在（biz_code=1）` | 链接错、被作者删除、或没点过「分享」 | 让用户回 DeepSeek 重新生成分享链接 |
| `分享已被删除或封禁（biz_code=2）` | 平台侧封禁 | 无解，说明清楚 |
| 接口返回非 JSON | 网络拦截 / 人机验证 / 代理劫持 | 换网络，或先在浏览器打开一次分享页再重试 |
| `HTTP 403 / 429` | 风控或频率限制 | 等几分钟，别连发 |
| 导入成功但会话是空的 | JSON 里消息缺 `contentParts`，或全部消息被过滤 | 重跑脚本，看摘要里 `messages` 是否大于 0 |
| Kelivo 提示不是 Chatbox 备份 | 根对象缺 `chat-sessions-list` | 用脚本产物原样导入，不要手改结构 |

字段细节与接口契约见 `references/format.md`。
