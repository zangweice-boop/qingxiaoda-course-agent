# 选课指南 · 清小搭接入服务

把「选课指南」agent（查询无穹书院课程评价系统 + 方法论建议）封装成 **OpenAI 兼容 HTTP 服务**，
按《自研 Agent 接入清小搭广场 · 开发者指南》实现，可经「标准协议接入」向导零代码上架清小搭智能体广场。

## 运行

```bash
pip install -r requirements.txt
export AGENT_API_KEY="sk-换成你的密钥"        # 必设：未设置将拒绝启动（无默认弱密钥）
# export PUBLIC_BASE_URL="https://你的域名"   # 有反向代理/公网域名时设置，用于拼附件下载地址；缺省会告警并按请求 Host 头拼接
# export ALLOW_PRIVATE_FILE_HOSTS=1          # 仅本地联调放行内网文件 URL，生产勿开
uvicorn app:app --host 0.0.0.0 --port 8000
```

接入向导填写：智能体平台选「标准协议接入」，API 地址 `http://<host>:8000/v1`，API 密钥填 `AGENT_API_KEY`，鉴权方式 Bearer Token。

## 文件结构

| 文件 | 职责 |
|---|---|
| `app.py` | OpenAI 兼容协议层：`/v1/models`、`/v1/chat/completions`（流式 SSE + 非流式）、`/files/...` 产物下载 |
| `agent.py` | agent 核心：意图识别 → 查评价库 → 按 SKILL.md 方法论组装回答；多模态输入处理；报告附件生成 |
| `evals_client.py` | 评价库只读访问（复用 `skill/scripts/query_evals.py`，叠加 5 分钟缓存与 certifi SSL 修复） |
| `skill/` | 技能包原样拷贝（SKILL.md 方法论、课程目录、查询脚本） |

## 协议实现清单（对照开发者指南）

- **L0 必须**：`POST /chat/completions` + `GET /models`；Bearer 鉴权、无效凭证 401（兼容 `x-api-key`）；`stream` 严格按 JSON 布尔解析；流式帧序列 role → content → stop（usage 合并于 stop 帧）→ `data: [DONE]`；`finish_reason` 只用白名单值；`usage` 必返（字符数估算）；`model` 缺失/空时忽略；`role=tool` 跳过；接受 `max_tokens:1`
- **L1 思考过程**：查询/组装步骤经 `delta.reasoning` 流式输出
- **L2 多模态**：
  - 输入 `file`：按 URL **当次拉取**（防签名过期），文本类（txt/markdown 等）解析并识别培养方案课程；二进制（pdf/word）如实说明只支持文本粘贴
  - 输入 `image_url` / `input_audio`：优雅降级为提示，不影响文本对话
  - 输出 `x_soda.attachments`：总览/求推荐/明确要报告时生成 markdown 报告，非流式挂响应顶层、流式挂 stop 帧；只回传 `fileUrl`（本服务 `/files/{uid}/{expiry}/{token}/...` 提供**签名 + 过期校验**的下载，清小搭会转存到自己的 OSS）
- **sessionId**：接收并记日志；agent 设计为**无状态**（约束从全量 messages 解析），多轮追问天然可用
- **SSRF 防护**：文件 URL 拉取前解析主机，拒绝私网/环回/保留地址（`ALLOW_PRIVATE_FILE_HOSTS=1` 仅限本地联调）；**重定向逐跳校验**，任何一跳落到内网即拒绝
- **请求体上限**：`MAX_BODY_BYTES=1MB`，超大 body 返回 413（Content-Length 中间件 + chunked 兜底）
- **超时纪律**：评价库查询 30s 超时 + 5 分钟缓存；文件拉取 20s；全链路远低于网关 120s 上限

## 自测（指南 §8 清单）

```bash
BASE="http://127.0.0.1:8000/v1"; KEY="$AGENT_API_KEY"

# 1) 连通 + 凭证（期望 200；错误密钥期望 401）
curl -s "$BASE/models" -H "Authorization: Bearer $KEY"

# 2) 非流式（期望 choices[0].message.content + usage）
curl -s -X POST "$BASE/chat/completions" -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"微积分哪个老师好"}]}'

# 3) 流式（期望 role/内容增量/stop/[DONE]，stop 帧带 usage）
curl -N -X POST "$BASE/chat/completions" -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"stream":true,"max_tokens":1,"messages":[{"role":"user","content":"你好"}]}'

# 4) 多轮求推荐（sessionId 记忆 + 报告附件 x_soda.attachments）
curl -s -X POST "$BASE/chat/completions" -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"sessionId":"t1","messages":[{"role":"user","content":"帮我选课"}]}'      # → 追问三约束
curl -s -X POST "$BASE/chat/completions" -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"sessionId":"t1","messages":[{"role":"user","content":"帮我选课"},{"role":"assistant","content":"…"},{"role":"user","content":"秋季，保GPA"}]}'  # → 总览+分目标建议+附件

# 5) 文件输入（file.url，识别培养方案课程）
curl -s -X POST "$BASE/chat/completions" -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"看看我的培养方案里哪些课有评价","contentx":0,"content":[{"type":"text","text":"看看我的培养方案"},{"type":"file","file":{"url":"http://127.0.0.1:9000/plan.txt","filename":"培养方案.txt"}}]}]}'
```

> 注 5 中本机文件服务地址需 `ALLOW_PRIVATE_FILE_HOSTS=1`；生产环境清小搭下发的是其 OSS 公网域名，无需放开。

## 自动化测试

```bash
pip install -r requirements-dev.txt
python -m pytest          # 评价库访问已 mock，离线可跑
```

覆盖：协议层（Bearer/x-api-key 鉴权、stream 严格布尔、SSE 帧序 role→reasoning→content→stop(usage/attachments)→[DONE]、max_tokens:1）+ agent 层（意图识别、约束解析、已锁定课程排除、纯文件消息、评价库宕机降级）。

## 与清小搭多模态能力的对应

| 能力 | 本服务 |
|---|---|
| 文本对话 / 流式 / 思考过程 | ✅ |
| 文件输入 `file`（pdf/word/…） | 🟡 文本类直读；二进制如实降级（无重型解析依赖，可按需扩展 pdf 库） |
| 音频输入 `input_audio` / 图片输入 `image_url` | 🟡 优雅降级提示（选课场景以文本为主） |
| 文件产物输出 `x_soda.attachments` | ✅ markdown 报告（`fileType:"text"`） |
