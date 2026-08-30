"""agent 层单元测试：意图识别、约束解析、锁定课排除、文件输入、评价库宕机降级。

评价库访问函数全部用 monkeypatch 替换，离线可跑（不依赖外网）。
"""
import pytest

import agent
from evals_client import EvalsUnavailable

# ---------- 模拟评价库数据（rows 为「课+老师」聚合粒度） ----------
ROWS = [
    {"course": "微积分A(1)", "semester": "秋季学期", "teacher": "艾老师",
     "count": 3, "avg": 9.0, "comments": ["给分好，作业少，讲得很好"]},
    {"course": "微积分A(1)", "semester": "秋季学期", "teacher": "王老师",
     "count": 0, "avg": None, "comments": []},
    {"course": "线性代数", "semester": "秋季学期", "teacher": "杨老师",
     "count": 2, "avg": 8.5, "comments": ["难度高，讲得好"]},
    {"course": "英语(1)", "semester": "秋季学期", "teacher": "李老师",
     "count": 1, "avg": 7.0, "comments": ["比较水"]},
]
TEACHERS = ["艾老师", "王老师", "杨老师", "李老师"]


@pytest.fixture(autouse=True)
def mock_evals(monkeypatch):
    """替换评价库访问：query/summary/render 全部走内存数据，零网络。"""

    def query_course(keyword):
        return [r for r in ROWS if r["course"] == keyword]

    def query_teacher(name):
        return [r for r in ROWS if name in r["teacher"]]

    monkeypatch.setattr(agent.evals_client, "all_teacher_names", lambda: TEACHERS)
    monkeypatch.setattr(agent.evals_client, "query_course", query_course)
    monkeypatch.setattr(agent.evals_client, "query_teacher", query_teacher)
    monkeypatch.setattr(agent.evals_client, "summary_rows",
                        lambda: ([r for r in ROWS if r["count"] > 0], []))
    monkeypatch.setattr(agent.evals_client, "render_rows",
                        lambda rows: "\n\n".join(f"### {r['course']}·{r['teacher']}" for r in rows))


@pytest.fixture(autouse=True)
def no_file_io(monkeypatch):
    """附件生成打桩，避免测试写 generated_files/。"""
    monkeypatch.setattr(agent, "make_attachment",
                        lambda text, filename, base_url: {"fileUrl": f"{base_url}/f",
                                                          "fileName": filename})


def run(text: str, parts=None):
    parts = parts or [{"type": "text", "text": text}]
    return agent.run_agent(text, text, parts, "http://test")


# ---------- 意图识别 ----------

def test_greeting():
    r = run("你好")
    assert "选课指南" in r.answer


def test_greeting_keeps_multimodal_note():
    parts = [{"type": "text", "text": "你好"},
             {"type": "image_url", "image_url": {"url": "http://x/a.png"}}]
    r = agent.run_agent("你好", "你好", parts, "http://test")
    assert "图片" in r.answer  # 多模态降级提示不能被问候分支吞掉


def test_course_query():
    r = run("微积分哪个老师好")
    assert "微积分A(1)" in r.answer
    assert "分目标建议" in r.answer
    assert "风险提示" in r.answer


def test_teacher_query():
    r = run("艾老师怎么样")
    assert "艾老师" in r.answer


def test_semester_question():
    r = run("概率论什么时候上")
    assert "春季学期" in r.answer


def test_boundary_reply():
    r = run("今天天气怎么样")
    assert "边界" in r.answer


# ---------- 约束解析 ----------

def test_parse_semester():
    assert agent.parse_semester("秋季") == "秋季学期"
    assert agent.parse_semester("春季，求稳") == "春季学期"
    assert agent.parse_semester("随便") is None


def test_parse_goal():
    assert agent.parse_goal("保GPA") == "gpa"
    assert agent.parse_goal("想学东西") == "interest"
    assert agent.parse_goal("时间紧") == "workload"
    assert agent.parse_goal("随便") is None


def test_parse_locked():
    assert agent.parse_locked("秋季，保GPA，线代锁了") == ["线性代数"]
    assert agent.parse_locked("英语已选，体育不想动") == ["体育(1)", "体育(2)", "英语(1)", "英语(2)"]
    assert agent.parse_locked("帮我选课") == []


# ---------- 求推荐流程 ----------

def test_recommend_asks_constraints():
    r = run("帮我选课")
    assert "一次问全三件事" in r.answer
    assert "秋季 / 春季 / 夏季" in r.answer


def test_recommend_short_reply_records_constraint():
    """追问后的短回复（锁定课）应被记下并继续追问，而不是被当作课程查询。"""
    all_t = "帮我选课\n线代锁了"
    r = agent.run_agent(all_t, "线代锁了", [{"type": "text", "text": "线代锁了"}], "http://test")
    assert "已锁定：线性代数" in r.answer
    assert "一次问全三件事" in r.answer


def test_recommend_full_constraints_overview():
    all_t = "帮我选课\n秋季，保GPA"
    r = agent.run_agent(all_t, "秋季，保GPA", [{"type": "text", "text": "秋季，保GPA"}], "http://test")
    assert "库内课程总览·秋季学期" in r.answer
    assert "线性代数" in r.answer  # 未锁定课程仍在表内
    assert len(r.attachments) == 1


def test_recommend_excludes_locked():
    """已锁定课程从数据表和建议中排除。"""
    all_t = "帮我选课\n秋季，保GPA，线代锁了"
    r = agent.run_agent(all_t, "秋季，保GPA，线代锁了",
                        [{"type": "text", "text": "秋季，保GPA，线代锁了"}], "http://test")
    assert "已排除你锁定的课程：线性代数" in r.answer
    assert "| 线性代数 |" not in r.answer
    assert len(r.attachments) == 1


def test_first_message_full_constraints():
    """首句即完整约束（无推荐词）也应走推荐流程，而非边界回复。"""
    r = run("秋季，保GPA")
    assert "库内课程总览·秋季学期" in r.answer


def test_course_query_not_swallowed_by_constraint_routing():
    """推荐语境中带课程查询味的句子仍走课程查询。"""
    all_t = "帮我选课\n微积分哪个老师好"
    r = agent.run_agent(all_t, "微积分哪个老师好",
                        [{"type": "text", "text": "微积分哪个老师好"}], "http://test")
    assert "微积分A(1)" in r.answer


# ---------- 文件输入 ----------

def test_file_only_message(monkeypatch):
    """最新一条消息只有文件、没有文本：文件必须被处理，不能丢。"""
    fake = "培养方案：高等微积分(1)、线性代数、英语(1)"
    monkeypatch.setattr(agent, "download_text_file", lambda url: (fake, "ok"))
    parts = [{"type": "file", "file": {"url": "http://x/plan.txt", "filename": "plan.txt"}}]
    r = agent.run_agent("", "", parts, "http://test")
    assert "高等微积分(1)" in r.answer
    assert len(r.attachments) == 1  # ≥3 门课自动生成查询报告


def test_file_with_binary_note(monkeypatch):
    """二进制文件如实降级，不影响文本对话。"""
    monkeypatch.setattr(agent, "download_text_file",
                        lambda url: (None, "该文件是二进制格式（如 pdf/word）"))
    parts = [{"type": "file", "file": {"url": "http://x/a.pdf", "filename": "a.pdf"}}]
    r = agent.run_agent("帮我看看", "帮我看看", parts, "http://test")
    assert "二进制" in r.answer


# ---------- 评价库宕机降级 ----------

def test_db_down_falls_back_to_methodology(monkeypatch):
    def boom():
        raise EvalsUnavailable("connection refused")

    monkeypatch.setattr(agent.evals_client, "summary_rows", boom)
    all_t = "帮我选课\n秋季，保GPA"
    r = agent.run_agent(all_t, "秋季，保GPA", [{"type": "text", "text": "秋季，保GPA"}], "http://test")
    assert "评价库暂时查不到" in r.answer
    assert "押注分散" in r.answer
