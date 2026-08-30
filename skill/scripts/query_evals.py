#!/usr/bin/env python3
"""课程评价查询：只读访问无穹书院课程评价系统（course-eval）的公开数据。

数据来源与致谢：https://zsx08.github.io/course-eval （学生自发维护，10 分制自评）
凭据说明：使用该站前端 JS 内嵌的公开 anon key（JWT role=anon，只读），可用环境变量
COURSE_EVAL_URL / COURSE_EVAL_KEY 覆盖。本脚本只做查询，不写入、不改删。

用法：
  python3 query_evals.py --course 微积分      # 按课程名/编号模糊查询
  python3 query_evals.py --teacher 艾颖华     # 按老师姓名模糊查询
  python3 query_evals.py --summary            # 全部课程评价总览
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

BASE_URL = os.environ.get(
    "COURSE_EVAL_URL", "https://fxkzcbwdzfboiypyxtbz.supabase.co/rest/v1")
API_KEY = os.environ.get(
    "COURSE_EVAL_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZ4a3pjYndkemZib2l5cHl4dGJ6Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODY5NTcyNTksImV4cCI6MjEwMjUzMzI1OX0.57x1NZLRqWqnXXY8s5T_UyBSikWvGfMBWF5_IaaBr7w")
CATALOG_PATH = Path(__file__).resolve().parent.parent / "assets" / "course-catalog.json"
MAX_ROWS = 2000          # 防御性上限：评价库单表超此规模时应改用服务端过滤
COMMENT_BRIEF = 80       # 展示评论时的截断长度

SOURCE_NOTE = "数据来源：无穹书院课程评价系统（学生自发维护，10 分制自评）· 仅校内参考"

# 四维信号规则：评论原文关键词 → 信号标签（仅转述，不推断分数）
SIGNAL_RULES = [
    ("给分好", ["调分", "给分好", "卡绩不", "优秀率"]),
    ("工作量小", ["作业少", "任务量小", "作业极少", "周只有", r"只有\d+次作业"]),
    ("工作量大", ["任务量大", "作业多", "任务重"]),
    ("讲课好", ["讲得很好", "讲得好", "笔记清晰", "严谨", "课件好", "很全面"]),
    ("讲课一般", ["照念", "念ppt", "无聊", "睡觉"]),
    ("难度高", ["难度", "逆天", "硬核", "很难"]),
    ("水", ["比较水", "很水", "上课水"]),
]


def extract_signals(comments):
    """从评论原文提取四维信号标签（仅转述，不推断分数），返回「、」连接的标签串。"""
    blob = " ".join(comments)
    found = []
    for label, kws in SIGNAL_RULES:
        if any(re.search(k, blob) for k in kws):
            found.append(label)
    return "、".join(found)


def fetch(table):
    """拉取整表（该库为百级规模，两请求即可完成任意查询，避免拼复杂过滤）。"""
    req = urllib.request.Request(
        f"{BASE_URL}/{table}?select=*&limit={MAX_ROWS}",
        headers={"apikey": API_KEY, "Authorization": f"Bearer {API_KEY}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_catalog():
    """课程目录快照：编号 → {课程名, 学期}。"""
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    courses = {}
    for sem in data["semesters"].values():
        for c in sem["courses"]:
            courses[c["id"]] = {"name": c["name"], "semester": sem["name"]}
    return courses


def aggregate(teachers, ratings, catalog):
    """按老师聚合：每人均分、样本数、代表性评论；课程名由目录解析。"""
    by_teacher = {}
    for r in ratings:
        by_teacher.setdefault(r.get("teacher_id"), []).append(r)
    rows = []
    for t in teachers:
        rs = by_teacher.get(t["id"], [])
        scores = [r["score"] for r in rs if isinstance(r.get("score"), (int, float))]
        info = catalog.get(t.get("course_id"),
                           {"name": f"未收录课程({t.get('course_id')})", "semester": ""})
        rows.append({
            "course": info["name"],
            "semester": info["semester"],
            "teacher": t.get("name", ""),
            "count": len(scores),
            "avg": round(sum(scores) / len(scores), 1) if scores else None,
            "comments": [r["comment"].strip() for r in rs
                         if (r.get("comment") or "").strip()],
        })
    return rows


def brief(text, limit=COMMENT_BRIEF):
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_row(row):
    score = f"{row['avg']:g}/10" if row["avg"] is not None else "暂无评分"
    head = f"### {row['course']}（{row['semester']}）· {row['teacher']} —— {score}（{row['count']} 条评价）"
    lines = [head]
    for c in row["comments"][:3]:
        lines.append(f"- 「{brief(c)}」")
    if row["count"] == 0:
        lines.append("- 暂无评价：不代表课程差，只是还没人评，建议询问上过该课的同学")
    elif row["count"] < 3:
        lines.append("- ⚠️ 样本量少（不足 3 条），评分参考价值有限，请结合评论内容判断")
    sig = extract_signals(row["comments"])
    if sig:
        lines.append(f"- 评论信号：{sig}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="查询无穹书院课程评价（只读）")
    ap.add_argument("--course", help="课程名或课程编号关键词，如：微积分、30420095")
    ap.add_argument("--teacher", help="老师姓名关键词，如：艾颖华")
    ap.add_argument("--summary", action="store_true", help="输出全部课程评价总览")
    args = ap.parse_args()
    if not (args.course or args.teacher or args.summary):
        ap.error("请指定 --course / --teacher / --summary 之一")

    try:
        teachers = fetch("teachers")
        ratings = fetch("ratings")
        catalog = load_catalog()
    except urllib.error.URLError as e:
        sys.exit(f"评价库暂不可达（{e}）。请改用选课方法论建议，并如实告知用户评价数据暂时查不到。")
    except (OSError, json.JSONDecodeError, KeyError) as e:
        sys.exit(f"评价数据解析失败：{e}")

    rows = aggregate(teachers, ratings, catalog)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"# 课程评价查询结果\n\n> {SOURCE_NOTE} · 查询时间 {now} · 库内共 {len(ratings)} 条评价\n")

    if args.summary:
        rated = sorted([r for r in rows if r["count"] > 0],
                       key=lambda r: (-r["avg"], -r["count"]))
        print("## 有评价的课程（按均分排序）\n")
        print("| 学期 | 课程 | 老师 | 均分 | 样本 |")
        print("|---|---|---|---|---|")
        for r in rated:
            print(f"| {r['semester']} | {r['course']} | {r['teacher']} | {r['avg']:g} | {r['count']} |")
        no_rating = [r for r in rows if r["count"] == 0]
        if no_rating:
            print("\n## 已收录老师但暂无评价\n")
            for r in no_rating:
                print(f"- {r['course']} · {r['teacher']}")
        known_ids = {t.get("course_id") for t in teachers}
        missing = [c["name"] for cid, c in catalog.items() if cid not in known_ids]
        if missing:
            print("\n## 尚无任何老师记录的课程\n")
            print("、".join(missing))
    else:
        if args.teacher:
            hits = [r for r in rows if args.teacher in r["teacher"]]
            label = f"老师「{args.teacher}」"
        else:
            kw = args.course.strip()
            names = {c["name"] for cid, c in catalog.items()
                     if kw == cid or kw in c["name"]}
            hits = [r for r in rows if r["course"] in names]
            label = f"课程「{args.course}」"
        if not hits:
            all_names = "、".join(sorted({r["course"] for r in rows}))
            sys.exit(f"未查到{label}的相关评价。可尝试换关键词，"
                     f"或直接 --summary 查看全部。库内已有老师记录的课程：{all_names}")
        print(f"## {label} 的评价（{len(hits)} 条匹配）\n")
        for r in sorted(hits, key=lambda r: -(r["avg"] or 0)):
            print(render_row(r))
            print()


if __name__ == "__main__":
    main()
