"""协议层测试：TestClient 直接打 app（agent.run_agent 打桩），覆盖指南 L0/L2 关键点。

- 鉴权：Bearer / x-api-key / 401
- stream 严格布尔解析（字符串 "false" 按非流式处理）
- 流式 SSE 帧序列：role → reasoning → content → stop(usage/attachments) → [DONE]
- 非流式顶层挂 x_soda.attachments；max_tokens:1 可接受
- collect_user_input：纯文件消息不丢文件
"""
import json
import os
import time

os.environ["AGENT_API_KEY"] = "test-key"  # 必须在 import app 前设置

import pytest
from fastapi.testclient import TestClient

import agent
import app as app_mod
from agent import AgentResult

KEY = "test-key"
H = {"Authorization": f"Bearer {KEY}"}


def fake_run(all_user_text, last_text, parts, base_url):
    return AgentResult(
        answer="测试回答内容",
        reasoning=["步骤一", "步骤二"],
        attachments=[{"fileUrl": f"{base_url}/files/uid/report.md",
                      "fileName": "report.md", "fileType": "text",
                      "mimeType": "text/markdown", "fileSize": 10}])


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(app_mod.agent, "run_agent", fake_run)
    return TestClient(app_mod.app)


def parse_sse(text: str) -> list[dict | None]:
    """把 SSE 文本解析成帧列表；[DONE] 帧记为 None。"""
    frames = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data: "):
            continue
        payload = line[len("data: "):]
        frames.append(None if payload == "[DONE]" else json.loads(payload))
    return frames


# ---------- 鉴权 ----------

def test_models_ok(client):
    r = client.get("/v1/models", headers=H)
    assert r.status_code == 200
    assert r.json()["data"][0]["id"] == "course-selection-guide"


def test_models_401_without_key(client):
    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_chat_x_api_key(client):
    r = client.post("/v1/chat/completions", headers={"x-api-key": KEY},
                    json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200


# ---------- 非流式 ----------

def test_chat_non_stream(client):
    r = client.post("/v1/chat/completions", headers=H,
                    json={"messages": [{"role": "user", "content": "微积分哪个老师好"}]})
    assert r.status_code == 200
    data = r.json()
    assert data["choices"][0]["message"]["content"] == "测试回答内容"
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["usage"]["total_tokens"] >= 1
    # L2：非流式 attachments 挂响应顶层
    assert data["x_soda"]["attachments"][0]["fileName"] == "report.md"


def test_chat_invalid_json_400(client):
    r = client.post("/v1/chat/completions", headers=H, content="{bad json")
    assert r.status_code == 400


# ---------- 流式 ----------

def test_chat_stream_frame_sequence(client):
    body = {"stream": True, "messages": [{"role": "user", "content": "你好"}]}
    with client.stream("POST", "/v1/chat/completions", headers=H, json=body) as resp:
        assert resp.status_code == 200
        text = "".join(resp.iter_text())
    frames = parse_sse(text)
    assert frames, "应有 SSE 帧"
    assert frames[-1] is None, "末帧应为 [DONE]"

    # role 帧（恰好一次，且在最前）
    assert frames[0]["choices"][0]["delta"].get("role") == "assistant"

    # L1 reasoning 帧
    reasoning = [f["choices"][0]["delta"]["reasoning"] for f in frames
                 if f and f["choices"][0]["delta"].get("reasoning")]
    assert reasoning == ["步骤一", "步骤二"]

    # content 增量拼接 = 完整回答
    content = "".join(f["choices"][0]["delta"].get("content", "") for f in frames
                      if f and f["choices"][0]["delta"].get("content"))
    assert content == "测试回答内容"

    # stop 帧：finish_reason 白名单值 + usage + L2 attachments 挂 stop 帧
    stop = frames[-2]
    assert stop["choices"][0]["finish_reason"] == "stop"
    assert stop["usage"]["total_tokens"] >= 1
    assert stop["x_soda"]["attachments"][0]["fileName"] == "report.md"


def test_stream_string_false_is_non_stream(client):
    """指南 §3：字符串 "false" 不能当 truthy，按非流式 JSON 返回。"""
    body = {"stream": "false", "messages": [{"role": "user", "content": "hi"}]}
    r = client.post("/v1/chat/completions", headers=H, json=body)
    assert r.status_code == 200
    data = r.json()  # 是 JSON 而非 SSE
    assert data["choices"][0]["message"]["content"] == "测试回答内容"


def test_max_tokens_1_accepted(client):
    body = {"stream": True, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}
    r = client.post("/v1/chat/completions", headers=H, json=body)
    assert r.status_code == 200


# ---------- 输入收集（collect_user_input） ----------

def test_collect_user_input_file_only():
    """最新一条消息只有 file、没有 text：parts 必须保留文件。"""
    msgs = [
        {"role": "user", "content": "帮我看看"},
        {"role": "user", "content": [{"type": "file",
                                      "file": {"url": "http://x/plan.txt", "filename": "plan.txt"}}]},
    ]
    all_t, last_t, parts = app_mod.collect_user_input(msgs)
    assert all_t == "帮我看看"
    assert last_t == ""  # 最新消息无文本
    assert [p["type"] for p in parts] == ["file"]


def test_collect_user_input_text_and_file_same_message():
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "看看培养方案"},
        {"type": "file", "file": {"url": "http://x/plan.txt"}}]}]
    _, last_t, parts = app_mod.collect_user_input(msgs)
    assert last_t == "看看培养方案"
    assert [p["type"] for p in parts] == ["text", "file"]


def test_collect_user_input_skips_tool_and_junk():
    msgs = [
        {"role": "tool", "content": "skip"},
        {"role": "user", "content": 123},  # 异常形态不崩
        {"role": "user", "content": "真消息"},
    ]
    all_t, last_t, parts = app_mod.collect_user_input(msgs)
    assert all_t == "真消息"
    assert last_t == "真消息"
    assert parts == [{"type": "text", "text": "真消息"}]


def test_run_agent_receives_joined_history(client, monkeypatch):
    """run_agent 收到的是全量发言拼接 + 最新一条文本（无状态多轮的输入契约）。"""
    calls = {}

    def spy(all_user_text, last_text, parts, base_url):
        calls.update(all=all_user_text, last=last_text, parts=parts)
        return AgentResult(answer="x", reasoning=[], attachments=[])

    monkeypatch.setattr(app_mod.agent, "run_agent", spy)
    body = {"sessionId": "s1", "messages": [
        {"role": "user", "content": "帮我选课"},
        {"role": "assistant", "content": "好的，请问哪学期？"},
        {"role": "user", "content": "秋季，保GPA"},
    ]}
    r = client.post("/v1/chat/completions", headers=H, json=body)
    assert r.status_code == 200
    assert calls["all"] == "帮我选课\n秋季，保GPA"
    assert calls["last"] == "秋季，保GPA"


# ---------- 安全加固 ----------

def test_attachment_signed_download(client):
    """附件 URL 带签名+过期：正确签名 200，篡改/过期/未知 uid 一律 404。"""
    att = agent.make_attachment("# 报告\n测试", "r.md", "http://testserver")
    uid, expiry, token, filename = att["fileUrl"].replace("http://testserver/files/", "").split("/", 3)
    assert agent.FILE_REGISTRY[uid]["expiry"] == int(expiry)
    try:
        path = att["fileUrl"].replace("http://testserver", "")
        r = client.get(path)
        assert r.status_code == 200
        assert r.content == "# 报告\n测试".encode()

        bad = path.replace(f"/{token}/", f"/{'0' * 64}/", 1)
        assert client.get(bad).status_code == 404

        old = path.replace(f"/{expiry}/", f"/{int(time.time()) - 1}/", 1)
        assert client.get(old).status_code == 404

        assert client.get(f"/files/deadbeef/{expiry}/{token}/{filename}").status_code == 404
    finally:
        agent.FILE_REGISTRY[uid]["path"].unlink(missing_ok=True)
        agent.FILE_REGISTRY.pop(uid, None)


def test_startup_requires_api_key():
    """AGENT_API_KEY 未设置时服务必须拒绝启动（不再有默认弱密钥）。"""
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    env = {k: v for k, v in os.environ.items() if k != "AGENT_API_KEY"}
    env["PYTHONIOENCODING"] = "utf-8"  # 子进程 stderr 统一 UTF-8，避免 Windows 控制台编码干扰
    r = subprocess.run([sys.executable, "-c", "import app"], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", env=env, cwd=repo)
    assert r.returncode != 0
    assert "AGENT_API_KEY" in r.stderr


def test_body_limit_content_length(client):
    big = "x" * (app_mod.MAX_BODY_BYTES + 1)
    r = client.post("/v1/chat/completions", headers=H, content=big)
    assert r.status_code == 413


def test_body_limit_chunked(client):
    """无 Content-Length 的 chunked 超大 body 由 endpoint 兜底拒绝。"""

    def gen():
        yield b'{"messages":[{"role":"user","content":"'
        yield b"x" * (app_mod.MAX_BODY_BYTES + 10)
        yield b'"}]}'

    r = client.post("/v1/chat/completions", headers=H, content=gen())
    assert r.status_code == 413
