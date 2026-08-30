"""清小搭接入服务：选课指南 agent 的 OpenAI 兼容 HTTP 封装。

按《自研 Agent 接入清小搭广场 · 开发者指南》实现：
- L0：GET /v1/models + POST /v1/chat/completions，Bearer 鉴权（401）、严格布尔 stream、
  流式帧序列 role→content→stop(usage)→[DONE]、finish_reason 白名单、usage 必返
- L1：delta.reasoning 思考过程
- L2：x_soda.attachments 文件产物（markdown 报告），file 文本输入按 URL 当次拉取
- sessionId：记录日志（agent 无状态，约束从全量 messages 解析）

运行：uvicorn app:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import time
import uuid

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

import agent

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("course-agent")

API_KEY = os.environ.get("AGENT_API_KEY")
if not API_KEY:
    raise RuntimeError(
        "AGENT_API_KEY 环境变量未设置：服务拒绝启动（防止使用默认弱密钥上线）。"
        "请先 export AGENT_API_KEY=... 再启动。")
MODEL_ID = "course-selection-guide"
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
if not PUBLIC_BASE_URL:
    log.warning("PUBLIC_BASE_URL 未设置：附件下载地址将按请求 Host 头拼接，请在生产环境显式配置公网域名")

MAX_BODY_BYTES = 1 * 1024 * 1024  # 请求体上限：messages 历史足够，防超大 body 占用内存

app = FastAPI(title="course-selection-guide for qingxiaoda", version="1.1.0")


@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    """有 Content-Length 时提前拒绝超大请求；chunked（无长度头）由 endpoint 内兜底。"""
    cl = request.headers.get("content-length")
    if cl:
        try:
            if int(cl) > MAX_BODY_BYTES:
                return JSONResponse({"detail": "request body too large"}, status_code=413)
        except ValueError:
            pass
    return await call_next(request)


def check_auth(authorization: str | None, x_api_key: str | None):
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[len("Bearer "):]
    elif x_api_key:
        token = x_api_key
    if not token or not hmac.compare_digest(token, API_KEY):
        raise HTTPException(status_code=401, detail="invalid credential")


def request_base_url(request: Request) -> str:
    """附件 fileUrl 用：优先 PUBLIC_BASE_URL，否则取请求自带的 scheme+host。"""
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    host = request.headers.get("host") or request.url.netloc
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    return f"{scheme}://{host}"


def collect_user_input(messages: list) -> tuple[str, str, list[dict]]:
    """从 messages 提取：全部用户发言拼接、最新一条用户消息的文本、最新一条用户消息的 content parts。

    - 约束解析用「全部用户发言」（无状态多轮）
    - 意图识别用「最新一条用户消息的文本」（可能为空串，如纯文件消息）
    - 多模态输入（file/image_url/input_audio）从「最新一条用户消息的 parts」解析——
      即使该消息没有文本（如用户只传培养方案文件），文件也不能丢

    role=tool 直接跳过；content 兼容字符串与多模态数组两种形态。
    """
    all_user_texts: list[str] = []
    last_text = ""
    last_parts: list[dict] = []
    for m in messages:
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str):
            parts = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            parts = content
        else:
            parts = []
        texts = " ".join(str(p.get("text", "")) for p in parts if p.get("type") == "text").strip()
        if texts:
            all_user_texts.append(texts)
        last_text, last_parts = texts, parts  # 始终以最新一条用户消息为准
    return "\n".join(all_user_texts), last_text, last_parts


def estimate_usage(prompt_text: str, completion_text: str) -> dict:
    return {
        "prompt_tokens": max(1, len(prompt_text) // 2),
        "completion_tokens": max(1, len(completion_text) // 2),
        "total_tokens": max(2, (len(prompt_text) + len(completion_text)) // 2),
    }


@app.get("/v1/models")
def models(authorization: str | None = Header(None), x_api_key: str | None = Header(None)):
    check_auth(authorization, x_api_key)
    return {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "owned_by": "wuqiong-course-eval"}]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request,
                           authorization: str | None = Header(None),
                           x_api_key: str | None = Header(None)):
    check_auth(authorization, x_api_key)
    try:
        raw = await request.body()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid body")
    if len(raw) > MAX_BODY_BYTES:  # chunked（无 Content-Length）时由这里兜底
        raise HTTPException(status_code=413, detail="request body too large")
    try:
        body = json.loads(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    # stream 严格按 JSON 布尔解析（指南 §3：不要把字符串 "false" 当真）
    stream = body.get("stream", False)
    stream = stream if isinstance(stream, bool) else False

    session_id = body.get("sessionId")  # 同一通对话每轮相同；缺失按新会话
    messages = body.get("messages") or []
    all_user_text, last_text, parts = collect_user_input(messages)
    base_url = request_base_url(request)

    try:
        result = agent.run_agent(all_user_text, last_text, parts, base_url)
    except Exception as e:  # 未产出内容即失败：非 2xx，不发半截响应
        log.exception("agent failed")
        raise HTTPException(status_code=500, detail=f"agent error: {type(e).__name__}")

    log.info("session=%s stream=%s last_text=%r attachments=%d",
             session_id, stream, last_text[:50], len(result.attachments))
    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    usage = estimate_usage(all_user_text, result.answer)
    x_soda = {"attachments": result.attachments} if result.attachments else None

    if not stream:
        resp = {
            "id": cid, "object": "chat.completion", "created": created, "model": MODEL_ID,
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": result.answer},
                         "finish_reason": "stop"}],
            "usage": usage,
        }
        if x_soda:
            resp["x_soda"] = x_soda
        return JSONResponse(resp)

    def sse():
        def frame(delta=None, finish=None, usage=None, x_soda=None, error=None):
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": created,
                     "model": MODEL_ID, "choices": [{"index": 0,
                                                     "delta": delta or {},
                                                     "finish_reason": finish}]}
            if usage:
                chunk["usage"] = usage
            if x_soda:
                chunk["x_soda"] = x_soda
            if error:
                chunk["error"] = error
            return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

        try:
            yield frame({"role": "assistant"})                          # role 帧（恰好一次）
            for step in result.reasoning:                               # L1 思考过程（0..N）
                yield frame({"reasoning": step})
            for i in range(0, len(result.answer), 24):                  # content 增量
                yield frame({"content": result.answer[i:i + 24]})
            yield frame({}, finish="stop", usage=usage,                 # stop 帧 + usage + attachments
                       x_soda=x_soda)
        except Exception as e:  # 头已发出：stop 帧附 error，不发 finish_reason:"error"
            yield frame({}, finish="stop",
                       error={"type": "upstream_error", "message": f"{type(e).__name__}: {e}"})
        yield "data: [DONE]\n\n"                                        # 终止哨兵

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/files/{uid}/{expiry}/{token}/{filename:path}")
def serve_file(uid: str, expiry: str, token: str, filename: str):
    """产物文件下载（签名 + 过期校验；清小搭收到 attachments 后会立即转存到自己的 OSS）。"""
    rec = agent.FILE_REGISTRY.get(uid)
    if not rec or not rec["path"].exists():
        raise HTTPException(status_code=404, detail="file not found or expired")
    if not agent.verify_file_token(uid, token, expiry):
        raise HTTPException(status_code=404, detail="file not found or expired")
    return FileResponse(rec["path"], media_type=rec["mime"], filename=rec["filename"])


@app.get("/")
def root():
    return {"service": MODEL_ID, "docs": "/docs", "health": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
