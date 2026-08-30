"""选课指南 agent 核心：意图识别 → 查评价库 → 按 SKILL.md 方法论组装回答。

设计要点：
- 无状态：清小搭每轮带全量 messages，约束（学期/目标/已锁课程）从全部用户发言中解析，
  不依赖服务端会话存储；sessionId 仅由 app 层记录日志。
- 评价库不可达时如实告知并转方法论建议，绝不编造评分（硬性禁令）。
- 多模态输入：file 文本类下载解析；image_url / input_audio 优雅降级为提示。
- 文件产物：总览/求推荐/明确要报告时，生成 markdown 报告经 x_soda.attachments 回传。
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import ssl
import time
import uuid
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlparse

import evals_client
from evals_client import EvalsUnavailable

SKILL_DIR = Path(__file__).resolve().parent / "skill"
FILES_DIR = Path(__file__).resolve().parent / "generated_files"
FILE_REGISTRY: dict[str, dict] = {}   # uid -> {"path","filename","mime"}
FILE_TTL = 24 * 3600                  # 产物只需在清小搭转存前的短时间内可下载

FOOTER = ("> 评价数据来自无穹书院课程评价系统（同学自发维护，10 分制自评），"
          "以最新评价为准，选课前可再查一次。")

INTRO = """你好！我是**选课指南**智能体 📚

我能帮你：
- **查具体课程/老师**：如「微积分哪个老师好」「艾颖华怎么样」「线性代数避雷」
- **求推荐/规划**：如「下学期帮我选课」「秋季求稳怎么选」——我会先确认学期、目标导向和已锁定的课
- **总览**：发「总览」看库内全部课程评价排行

数据边界：评价库覆盖无穹书院培养方案课程（秋季 10 门 / 春季 12 门 / 夏季 2 门），数据为同学自发提交（10 分制），样本有限、持续积累中；**没有评价 ≠ 课程差**。库外课程我查不到，但可以聊选课方法论。"""

BOUNDARY_REPLY = """这句话里我没识别出评价库覆盖的具体课程或老师，先说清边界再给你出路：

- 评价库目前只覆盖**无穹书院培养方案课程**（秋季 10 门 / 春季 12 门 / 夏季 2 门），库外课程查不到评价，我不会编造。
- 想查具体的：把**课程全名**（如「微积分A(1)」「概率论」）或**老师姓名**发我。
- 想看全貌：发「**总览**」，我把库内全部课程按均分排给你。
- 想聊方法论：试听周策略、押注分散（一学期 2 难 + 2 中 + 1 稳）、四维评估（给分/工作量/讲课质量/实质收获）都可以问。"""

# 课程别名（小写）→ 目录课程名片段；裸词如「微积分」可同时命中 微积分A/高等微积分 系列
ALIASES = {
    "高数": "高等微积分", "高微": "高等微积分", "微积分": "微积分",
    "线代": "线性代数", "程设": "程序设计", "oop": "面向对象",
    "概统": "概率论", "概率": "概率论",
    "大物": "大学物理", "物理": "大学物理",
    "史纲": "中国近现代史纲要", "近代史": "中国近现代史纲要",
    "思修": "思想道德与法治",
    "英语": "英语", "体育": "体育", "写作": "写作与沟通",
    "形势政策": "形势与政策",
}
SEMESTER_PATTERNS = [
    (r"秋季|秋天|秋学期|下学期.*秋|大一上", "秋季学期", "autumn"),
    (r"春季|春学期|大一下", "春季学期", "spring"),
    (r"夏季|暑期|夏学期|暑假", "夏季学期", "summer"),
]
GOAL_PATTERNS = [
    (r"保研|保 ?gpa|gpa|绩点|求稳|稳过|卡绩", "gpa", "保 GPA/求稳"),
    (r"兴趣|学东西|打基础|学扎实|长远", "interest", "兴趣/打基础"),
    (r"已满|很满|求轻|时间紧|少花时间|太累", "workload", "学期已满求轻"),
]
GOAL_LABELS = {"gpa": "保 GPA/求稳", "interest": "兴趣/打基础", "workload": "学期已满求轻"}
RECOMMEND_PAT = re.compile(r"推荐|怎么选|选什么|哪门|哪门课|总览|全部|排行|排名|帮我选|选课|课表|规划|求稳|避雷")
# 课程「查询味」的句子（与约束短回复区分，如「线代锁了」不是查询）
QUERY_PAT = re.compile(r"哪个|怎么样|怎么选|如何|啥样|推荐|查|评价|避雷")
GREETING_PAT = re.compile(r"^\s*(你好|您好|hi|hello|在吗|你是谁|你能做什么|介绍.*自己|帮我什么)[!！。~\s]*$", re.I)
SEMESTER_Q_PAT = re.compile(r"哪.*学期|什么学期|什么时候上|哪个学期|几学期")
REPORT_PAT = re.compile(r"报告|导出|文件|清单|下载|发我.*文件")
SIGNAL_RULES = [
    ("给分好", ["调分", "给分好", "卡绩不", "优秀率"]),
    ("工作量小", ["作业少", "任务量小", "作业极少", "周只有", r"只有\d+次作业"]),
    ("工作量大", ["任务量大", "作业多", "任务重"]),
    ("讲课好", ["讲得很好", "讲得好", "笔记清晰", "严谨", "课件好", "很全面"]),
    ("讲课一般", ["照念", "念ppt", "无聊", "睡觉"]),
    ("难度高", ["难度", "逆天", "硬核", "很难"]),
    ("水", ["比较水", "很水", "上课水"]),
]
DOWNLOAD_TIMEOUT = 20
MAX_FILE_BYTES = 25 * 1024 * 1024


def _ssl_ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return None


@dataclass
class AgentResult:
    answer: str
    reasoning: list[str] = field(default_factory=list)
    attachments: list[dict] = field(default_factory=list)


def _load_catalog() -> dict[str, dict]:
    data = json.loads((SKILL_DIR / "assets" / "course-catalog.json").read_text(encoding="utf-8"))
    flat = {}
    for sem in data["semesters"].values():
        for c in sem["courses"]:
            flat[c["id"]] = {"name": c["name"], "id": c["id"], "semester": sem["name"]}
    return flat


CATALOG = _load_catalog()


# ---------- 意图识别 ----------

def find_course_mentions(text: str) -> list[dict]:
    """在文本中识别培养方案课程（含别名、编号），返回目录条目。"""
    hits: dict[str, dict] = {}
    low = text.lower()
    for cid, c in CATALOG.items():
        if cid in text or c["name"].lower() in low:
            hits[c["name"]] = c
    for alias, target in ALIASES.items():
        if alias in low:
            for c in CATALOG.values():
                if target in c["name"]:
                    hits[c["name"]] = c
    return sorted(hits.values(), key=lambda c: c["id"])


def find_teacher_mentions(text: str, names: list[str]) -> list[str]:
    return [n for n in names if n and n in text]


def parse_semester(text: str) -> str | None:
    for pat, label, _key in SEMESTER_PATTERNS:
        if re.search(pat, text, re.I):
            return label
    return None


def parse_goal(text: str) -> str | None:
    for pat, key, _label in GOAL_PATTERNS:
        if re.search(pat, text, re.I):
            return key
    return None


# 已锁定/不想动的课：出现在同一句话里的课程名视为锁定（如「线代锁了」「英语已选」）
LOCKED_PAT = re.compile(r"已锁定|锁了|锁定|已选|选了|不想动|躲不开|跑不掉")


def parse_locked(text: str) -> list[str]:
    """从全部用户发言中解析「已锁定、不想动的课」，返回目录课程名（含别名识别）。"""
    locked: dict[str, str] = {}
    for line in text.splitlines():
        if not LOCKED_PAT.search(line):
            continue
        for c in find_course_mentions(line):
            locked[c["name"]] = c["name"]
    return sorted(locked)


def extract_signals(comments: list[str]) -> str:
    """从评论原文提取四维信号标签（仅转述，不推断分数）。"""
    blob = " ".join(comments)
    found = []
    for label, kws in SIGNAL_RULES:
        if any(re.search(k, blob) for k in kws):
            found.append(label)
    return "、".join(found)


# ---------- 文件产物 ----------

def _cleanup_old_files():
    now = time.time()
    for uid, rec in list(FILE_REGISTRY.items()):
        try:
            if now - rec["path"].stat().st_mtime > FILE_TTL:
                rec["path"].unlink(missing_ok=True)
                FILE_REGISTRY.pop(uid, None)
        except OSError:
            pass


def make_attachment(text: str, filename: str, base_url: str) -> dict:
    """把 markdown 报告落盘并生成 x_soda.attachments 条目（只回传 URL，不嵌字节）。"""
    FILES_DIR.mkdir(exist_ok=True)
    _cleanup_old_files()
    uid = uuid.uuid4().hex[:16]
    path = FILES_DIR / f"{uid}.md"
    path.write_text(text, encoding="utf-8")
    FILE_REGISTRY[uid] = {"path": path, "filename": filename, "mime": "text/markdown"}
    return {
        "fileUrl": f"{base_url.rstrip('/')}/files/{uid}/{quote(filename)}",
        "fileName": filename,
        "fileType": "text",
        "mimeType": "text/markdown",
        "fileSize": path.stat().st_size,
    }


# ---------- 多模态输入 ----------

def _host_allowed(host: str) -> bool:
    if os.environ.get("ALLOW_PRIVATE_FILE_HOSTS") == "1":
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    return True


def download_text_file(url: str) -> tuple[str | None, str]:
    """按 URL 拉取用户上传的文件，文本类返回内容，二进制返回 None。

    遵循清小搭多模态准入：只收 URL、当次立即拉取（防签名过期）、限制大小、
    校验主机防 SSRF。返回 (文本或 None, 给用户看的说明)。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None, f"文件链接协议不受支持（{parsed.scheme or '空'}），已跳过"
    if not _host_allowed(parsed.hostname or ""):
        return None, "文件链接主机不可达或不被允许，已跳过"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "course-guide-agent/1.0"})
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT, context=_ssl_ctx()) as resp:
            raw = resp.read(MAX_FILE_BYTES + 1)
    except Exception as e:  # 网络/超时/过大统一降级，不 500
        return None, f"文件拉取失败（{type(e).__name__}），本次先忽略该文件"
    if len(raw) > MAX_FILE_BYTES:
        return None, "文件超过 25MB 上限，本次先忽略该文件"
    text = raw.decode("utf-8", errors="replace")
    if text.count("\x00") > 2 or sum(1 for b in raw[:4096] if b < 9 or 13 < b < 32) > 64:
        return None, "该文件是二进制格式（如 pdf/word），我目前只能直接读文本类（txt/markdown），可以把内容粘贴给我"
    return text, "ok"


# ---------- 回答组装 ----------

def _risk_line(rows: list[dict]) -> str:
    risks = []
    if any(r["count"] < 3 for r in rows):
        risks.append("样本量不足 3 条的评分仅作参考，请以评论中的具体事实为准")
    if any(r["count"] == 0 for r in rows):
        risks.append("**没有评价 ≠ 课程差**，只是还没人评过；可选可试听的第一周先试听、问上过的同学")
    risks.append("给分存在年际波动，去年的「调分大」不代表今年")
    return "- " + "\n- ".join(risks)


def _goal_advice(rows: list[dict]) -> str:
    """分目标建议：同一选择对不同目标答案不同。"""
    rated = [r for r in rows if r["count"] > 0]
    lines = []
    if not rated:
        return "库内暂无该课任何评价，无法给数据建议；建议试听第一周 + 问上过的同学再定。"
    best = max(rated, key=lambda r: (r["count"] >= 3, r["avg"] or 0))
    safe_sig = extract_signals(best["comments"])
    lines.append(f"- **求稳 / 保 GPA**：优先 **{best['teacher']}**（均分 {best['avg']:g}/10、{best['count']} 条评价"
                 + (f"，评论信号：{safe_sig}" if safe_sig else "") + "）")
    teach = [r for r in rated if "讲课好" in extract_signals(r["comments"])]
    hard = [r for r in rated if "难度高" in extract_signals(r["comments"])]
    if teach:
        lines.append(f"- **想学扎实 / 打基础**：看讲课质量信号，{'、'.join(r['teacher'] for r in teach)} 的评论提到讲课或笔记正面；"
                     + (f"{'、'.join(r['teacher'] for r in hard)} 难度信号强，长期收获可能最高但要投入" if hard else ""))
    elif hard:
        lines.append(f"- **想学扎实 / 打基础**：{'、'.join(r['teacher'] for r in hard)} 难度信号强，长期收获可能最高，但需预留时间")
    light = [r for r in rated if "工作量小" in extract_signals(r["comments"])]
    if light:
        lines.append(f"- **学期已满求轻**：{'、'.join(r['teacher'] for r in light)} 评论明确提到作业少/任务量小")
    return "\n".join(lines)


def _course_answer(names: list[str]) -> AgentResult:
    """具体课程/老师查询：数据引用 + 分目标建议 + 风险提示 + 致谢。"""
    rows: list[dict] = []
    label = "课程"
    import evals_client as ec
    for n in names:
        rows.extend(ec.query_course(n))
    if not rows and names:
        rows = ec.query_teacher(names[0])
        label = "老师"
    rendered = evals_client.render_rows(rows)
    head = f"## {'、'.join(names)} 的评价数据\n\n{rendered}" if rows else \
        f"## {'、'.join(names)}：暂无评价记录\n\n库内没有匹配的评价。**没有评价 ≠ 差**，建议试听第一周、问上过该课的同学。"
    answer = (f"{head}\n\n### 分目标建议\n\n{_goal_advice(rows)}\n\n"
              f"### 风险提示\n\n{_risk_line(rows)}\n\n{FOOTER}")
    return AgentResult(answer=answer, reasoning=[
        f"查询评价库：{'、'.join(names)}",
        f"命中 {len(rows)} 条「课+老师」记录" if rows else "未命中任何评价记录，如实告知",
        "按四维框架组装分目标建议",
    ])


def _overview_report(semester: str | None, rows_rated, rows_none, advice: str) -> str:
    from datetime import date
    sem_line = f"（{semester}）" if semester else "（全部学期）"
    lines = [f"# 选课参考报告{sem_line}", "",
             f"> {evals_client.SOURCE_NOTE} · 生成时间 {date.today().isoformat()}", "",
             "## 有评价的课程（按均分排序）", "",
             "| 学期 | 课程 | 老师 | 均分 | 样本 | 评论信号 |", "|---|---|---|---|---|---|"]
    for r in rows_rated:
        if semester and r["semester"] != semester:
            continue
        sig = extract_signals(r["comments"]) or "—"
        lines.append(f"| {r['semester']} | {r['course']} | {r['teacher']} | {r['avg']:g} | {r['count']} | {sig} |")
    if rows_none:
        lines += ["", "## 已收录老师但暂无评价", ""]
        lines += [f"- {r['course']}（{r['semester']}）· {r['teacher']}" for r in rows_none]
    lines += ["", "## 针对性建议", "", advice, "", FOOTER]
    return "\n".join(lines)


def _overview_answer(semester: str | None, goal: str | None, base_url: str,
                     locked: list[str] | None = None) -> AgentResult:
    """总览/求推荐：先给数据表，再按目标导向给建议，附完整报告文件。

    locked: 用户已锁定、不想动的课程名列表，从数据表与建议中排除。
    """
    rated, none_rated = evals_client.summary_rows()
    locked_names = set(locked or [])
    candidate = [r for r in rated if not semester or r["semester"] == semester]
    sem_rows = [r for r in candidate if r["course"] not in locked_names]
    if not sem_rows:
        if candidate:
            return AgentResult(
                answer=f"{semester or '本季'} 带评价的课程里，你锁定的课程已全部排除"
                       f"（{'、'.join(sorted(locked_names))}），剩余暂无其他可参考的评价数据。\n\n{FOOTER}",
                reasoning=[f"查询总览（{semester or '全部'}）并排除锁定课程：结果为空"])
        return AgentResult(
            answer=f"{semester} 目前库内还没有带评价的课程记录。\n\n{FOOTER}",
            reasoning=[f"查询总览（{semester or '全部'}）：暂无评价数据"])
    table = ["| 课程 | 老师 | 均分 | 样本 | 评论信号 |", "|---|---|---|---|---|"]
    for r in sem_rows:
        sig = extract_signals(r["comments"]) or "—"
        table.append(f"| {r['course']} | {r['teacher']} | {r['avg']:g} | {r['count']} | {sig} |")
    if goal == "gpa":
        picks = sorted([r for r in sem_rows if r["count"] >= 3], key=lambda r: -r["avg"])[:3]
        advice = ("保 GPA 导向：优先「均分 ≥ 8 且样本 ≥ 3」——本次符合的有 "
                  + "、".join(f"{r['course']}·{r['teacher']}（{r['avg']:g}）" for r in picks)
                  + ("；样本不足 3 条的高分仅作参考，重点看评论事实。" if len(picks) < 3 else "。"))
    elif goal == "interest":
        teach = [r for r in sem_rows if "讲课好" in extract_signals(r["comments"])]
        advice = ("兴趣/打基础导向：讲课质量信号正面的有 "
                  + ("、".join(f"{r['course']}·{r['teacher']}" for r in teach) or "暂无明确正面信号的课程")
                  + "；给分权重可以放低，难度大 + 讲得好的课长期收益最高。")
    elif goal == "workload":
        light = [r for r in sem_rows if "工作量小" in extract_signals(r["comments"])]
        advice = ("学期已满求轻导向：评论明确提到作业少/任务量小的有 "
                  + ("、".join(f"{r['course']}·{r['teacher']}" for r in light) or "暂无明确「任务量小」信号的记录")
                  + "；同时避开难度信号强的课。")
    else:
        top = sorted(sem_rows, key=lambda r: (-(r["avg"] or 0), -r["count"]))[:3]
        advice = ("综合来看数据最充分的三个选择："
                  + "、".join(f"{r['course']}·{r['teacher']}（{r['avg']:g}/10）" for r in top)
                  + "。搭配上建议一学期「2 难 + 2 中 + 1 稳」，别全押高难度或全押水课。")
    if locked_names:
        advice = f"已排除你锁定的课程：{'、'.join(sorted(locked_names))}。\n\n" + advice
    advice_block = (f"### {GOAL_LABELS.get(goal, '综合均衡')}建议\n\n{advice}\n\n"
                    f"### 通用策略\n\n- 必修/限选先落位，再填任选；同一门课**老师 > 课程**，按「课+老师」粒度决策\n"
                    f"- 试听周多试听，退改选窗口内保留调整余地（时间以教务通知为准）\n\n"
                    f"### 风险提示\n\n{_risk_line(sem_rows + none_rated)}")
    answer = (f"## {'库内课程总览' + (f'·{semester}' if semester else '')}\n\n"
              f"{chr(10).join(table)}\n\n{advice_block}\n\n"
              f"📎 完整数据与建议已生成报告附件，可直接下载。\n\n{FOOTER}")
    from datetime import date
    report = _overview_report(semester, rated, none_rated, advice + "\n\n通用策略与风险提示见对话内回答。")
    att = make_attachment(report, f"选课参考报告-{date.today().strftime('%Y%m%d')}.md", base_url)
    return AgentResult(answer=answer, reasoning=[
        f"查询总览：{len(rated)} 门有评价、{len(none_rated)} 门暂无评价",
        f"按目标导向（{GOAL_LABELS.get(goal, '综合均衡')}）筛选建议",
        "生成 markdown 报告附件",
    ], attachments=[att])


def _constraints_question() -> AgentResult:
    return AgentResult(answer="""好的，我来帮你规划选课。先一次问全三件事：

1. **哪个学期**？（秋季 / 春季 / 夏季）
2. **目标导向**？（保 GPA 求稳 / 兴趣打基础 / 学期已满求轻）
3. **有没有已锁定、不想动的课**？（有就列出来，我按剩余位置给建议）

> 数据边界：评价库覆盖无穹书院培养方案课程（秋 10 / 春 12 / 夏 2 门），库外课程查不到评价，我不会编造。""")


def _db_down_answer(e: EvalsUnavailable) -> AgentResult:
    return AgentResult(answer=f"""评价库暂时查不到（{e}），我不编数据，先给方法论建议：

- **决策顺序**：必修/限选先落位 → 明确目标（保 GPA / 兴趣 / 求轻）→ 同一门课按「老师」粒度比较 → 核对时间不冲突
- **试听周策略**：前两周多试听再定，退改选窗口内保留调整余地
- **押注分散**：一学期建议「2 难 + 2 中 + 1 稳」，别全押高难度或全押水课
- 稍后再来查一次，评价库恢复后我给你具体数据和分老师建议

{FOOTER}""", reasoning=["评价库不可达，转方法论建议（如实告知，不编造）"])


# ---------- 主入口 ----------

def _recommend_answer(notes_block: str, all_user_text: str, base_url: str) -> AgentResult:
    """求推荐流程：约束不全则一次问全并回显已记下的；齐全则出总览+分目标建议+报告附件。

    已锁定课程（parse_locked）从数据表与建议中排除。
    """
    semester = parse_semester(all_user_text)
    goal = parse_goal(all_user_text)
    locked = parse_locked(all_user_text)
    if not semester or not goal:
        q = _constraints_question()
        got = [x for x in (semester, GOAL_LABELS.get(goal)) if x]
        if locked:
            got.append(f"已锁定：{'、'.join(locked)}")
        q.answer = notes_block + (f"已记下：{'、'.join(got)}。\n\n" if got else "") + q.answer
        q.reasoning = ["泛泛求推荐，约束不全（学期/目标），一次性问全三件事"]
        return q
    res = _overview_answer(semester, goal, base_url, locked)
    res.answer = notes_block + res.answer
    return res


def run_agent(all_user_text: str, last_text: str, parts: list[dict], base_url: str) -> AgentResult:
    """执行一轮 agent。

    all_user_text: 本通对话全部用户发言拼接（用于约束解析，无状态多轮）
    last_text:     最新一条用户发言的文本
    parts:         最新用户消息的 content parts（text/file/image_url/input_audio）
    base_url:      本服务对外地址，用于拼附件 fileUrl
    """
    reasoning: list[str] = []
    want_report = bool(REPORT_PAT.search(last_text))

    # --- 多模态输入处理 ---
    file_notes: list[str] = []
    file_courses: list[dict] = []
    for p in parts:
        if p["type"] == "file":
            f = p.get("file") or {}
            fname = f.get("filename") or "未命名文件"
            if f.get("url"):
                reasoning.append(f"收到文件《{fname}》，按 URL 当次拉取")
                text, note = download_text_file(f["url"])
                if text is None:
                    file_notes.append(f"📎 《{fname}》：{note}。")
                else:
                    found = find_course_mentions(text)
                    if found:
                        file_courses.extend(found)
                        file_notes.append(f"📎 已读取《{fname}》（{len(text)} 字符），识别出 {len(found)} 门培养方案课程：{'、'.join(c['name'] for c in found)}。")
                    else:
                        file_notes.append(f"📎 已读取《{fname}》（{len(text)} 字符），未识别出评价库覆盖的课程；可以把课程名直接发我。")
            else:
                file_notes.append(f"📎 收到文件《{fname}》（file_id 方式），该承载方式暂未接入，请改用 URL 或直接粘贴内容。")
        elif p["type"] in ("image_url", "input_audio"):
            kind = "图片" if p["type"] == "image_url" else "音频"
            file_notes.append(f"📎 收到{kind}附件：我目前专注选课文本场景，暂时看不了{kind}，文字描述给我即可。")

    notes_block = ("\n".join(file_notes) + "\n\n") if file_notes else ""

    # --- 文本意图 ---
    text = last_text.strip()
    if not text and not file_courses:
        if file_notes:
            return AgentResult(answer=notes_block.rstrip() + "\n\n想查什么课程或老师，直接发名字给我～")
        return AgentResult(answer=INTRO, reasoning=["无实质输入，返回能力介绍"])

    if GREETING_PAT.match(text) and not file_courses:
        return AgentResult(answer=(notes_block + INTRO) if notes_block else INTRO,
                           reasoning=["问候/能力询问 → 返回介绍"])

    try:
        if file_courses:
            names = sorted({c["name"] for c in file_courses})
            res = _course_answer(names)
            res.answer = notes_block + res.answer
            res.reasoning = ["从上传文件中识别课程，逐门查询评价库"] + res.reasoning
            if want_report or len(names) >= 3:
                from datetime import date
                report = f"# 选课查询报告\n\n> {evals_client.SOURCE_NOTE} · {date.today().isoformat()}\n\n" + res.answer
                res.attachments.append(make_attachment(report, f"选课查询报告-{date.today().strftime('%Y%m%d')}.md", base_url))
                res.reasoning.append("生成查询报告附件")
            return res

        teachers = find_teacher_mentions(text, evals_client.all_teacher_names())
        courses = find_course_mentions(text)

        # 求推荐语境：全量发言里有推荐信号，或学期+目标约束已齐（如首句即「秋季，保GPA」）
        recommend_ctx = bool(RECOMMEND_PAT.search(all_user_text)) or (
            parse_semester(all_user_text) and parse_goal(all_user_text))
        # 约束回答语境：最新一句是学期/目标/锁定课的短回复（「秋季，保GPA」「线代锁了」）——
        # 即使提到课程名（锁定语境），也走求推荐流程而非课程查询
        constraint_reply = bool(parse_semester(text) or parse_goal(text) or parse_locked(text)) \
            and not QUERY_PAT.search(text)

        if recommend_ctx and constraint_reply:
            return _recommend_answer(notes_block, all_user_text, base_url)

        if teachers:
            names = sorted(set(teachers))
            res = _course_answer(names)
            res.answer = notes_block + res.answer
            return res

        if courses:
            if SEMESTER_Q_PAT.search(text) and len(courses) == 1:
                c = courses[0]
                return AgentResult(answer=notes_block + (
                    f"**{c['name']}**（编号 {c['id']}）是**{c['semester']}**的培养方案课程。\n\n"
                    f"想看这门课的老师评价和选老师建议，直接回我「{c['name']} 怎么样」。"))
            names = sorted({c["name"] for c in courses})
            res = _course_answer(names)
            res.answer = notes_block + res.answer
            return res

        if recommend_ctx:
            # 此处 text 必非空（空文本+无文件课程已在上方提前返回）
            return _recommend_answer(notes_block, all_user_text, base_url)

        return AgentResult(answer=notes_block + BOUNDARY_REPLY,
                           reasoning=["未识别出库内课程/老师，也不是求推荐 → 说明数据边界"])

    except EvalsUnavailable as e:
        res = _db_down_answer(e)
        res.answer = (notes_block + res.answer) if notes_block else res.answer
        return res
