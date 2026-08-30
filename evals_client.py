"""评价库只读访问层：复用 skill/scripts/query_evals.py 的聚合与渲染逻辑，
叠加 TTL 缓存与 certifi SSL 修复（Python 3.14 官方安装缺根证书时默认上下文会失败）。

数据来源：无穹书院课程评价系统（学生自发维护，10 分制自评）
"""
import importlib.util
import json
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent / "skill"
QUERY_SCRIPT = SKILL_DIR / "scripts" / "query_evals.py"

_spec = importlib.util.spec_from_file_location("query_evals", QUERY_SCRIPT)
qe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qe)

# certifi 存在则显式指定 CA，避免部分系统默认证书路径不全
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:  # pragma: no cover
    _SSL_CTX = None

CACHE_TTL = 300  # 评价库为百级小表，5 分钟缓存足够新鲜且不打爆上游
_lock = threading.Lock()
_cache = {"ts": 0.0, "teachers": None, "ratings": None}


class EvalsUnavailable(Exception):
    """评价库不可达/解析失败，agent 需如实告知并转方法论建议。"""


def _fetch_table(table: str) -> list:
    req = urllib.request.Request(
        f"{qe.BASE_URL}/{table}?select=*&limit={qe.MAX_ROWS}",
        headers={"apikey": qe.API_KEY, "Authorization": f"Bearer {qe.API_KEY}"})
    with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _load_data() -> tuple[list, list, dict]:
    """返回 (teachers, ratings, rows)。rows 为按『课+老师』粒度的聚合结果。"""
    with _lock:
        if _cache["teachers"] is None or time.time() - _cache["ts"] > CACHE_TTL:
            try:
                teachers, ratings = _fetch_table("teachers"), _fetch_table("ratings")
            except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
                if _cache["teachers"] is None:
                    raise EvalsUnavailable(f"评价库暂不可达（{e}）")
                teachers, ratings = _cache["teachers"], _cache["ratings"]  # 过期兜底
            else:
                _cache.update(ts=time.time(), teachers=teachers, ratings=ratings)
        teachers, ratings = _cache["teachers"], _cache["ratings"]
    catalog = qe.load_catalog()
    return teachers, ratings, qe.aggregate(teachers, ratings, catalog)


def warm_cache() -> int:
    """启动预热：拉取评价库并填充缓存，返回老师数；失败抛 EvalsUnavailable。"""
    teachers, _, _ = _load_data()
    return len(teachers)


def all_teacher_names() -> list[str]:
    """库内全部老师姓名（供意图识别做关键词匹配）。"""
    teachers, _, _ = _load_data()
    return sorted({t.get("name", "") for t in teachers if t.get("name")})


def query_course(keyword: str) -> list[dict]:
    """按课程名/编号关键词查询，返回聚合 rows（含 course/semester/teacher/count/avg/comments）。"""
    _, _, rows = _load_data()
    kw = keyword.strip()
    names = {c["name"] for cid, c in qe.load_catalog().items()
             if kw == cid or kw in c["name"]}
    return [r for r in rows if r["course"] in names]


def query_teacher(name: str) -> list[dict]:
    """按老师姓名关键词查询。"""
    _, _, rows = _load_data()
    return [r for r in rows if name in r["teacher"]]


def summary_rows() -> tuple[list[dict], list[dict]]:
    """全部课程总览：返回 (有评价 rows 按均分排序, 无评价 rows)。"""
    _, _, rows = _load_data()
    rated = sorted([r for r in rows if r["count"] > 0],
                   key=lambda r: (-r["avg"], -r["count"]))
    return rated, [r for r in rows if r["count"] == 0]


def render_rows(rows: list[dict]) -> str:
    """把聚合 rows 渲染成脚本同款 markdown。"""
    parts = [qe.render_row(r) for r in sorted(rows, key=lambda r: -(r["avg"] or 0))]
    return "\n\n".join(parts)


SOURCE_NOTE = qe.SOURCE_NOTE
