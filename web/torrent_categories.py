#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""种子条目分类 —— 配置读取 + 纯函数分类。

设计约束(改动前请先读, 每条都是为了堵一类静默失效):

1. **实时读盘, 禁止模块级缓存**。`load_categories()` 每次调用都重新读文件。
   理由: 用户在设置页改完规则必须立刻生效; 一旦缓存就会产生"改完不生效"
   这类不可见失败。若将来有人为性能加缓存, 变异体 2 会让测试 8 变红。
2. **无 Flask 依赖, import 期零 IO**。便于单测直接 import; 不得在此模块
   mkdir / 读文件 / 连网络(历史教训: app.py 在 import 期 mkdir 曾让 CI 全挂)。
3. **不调用 scan_sources()**。items 由调用方抓取后传入, 本模块不抓外网。
4. **配置损坏必须可见**。解析失败写进 `report`, 不能静默退化成"没有分类"。

分类结果**只影响展示**, 不改变下载与存盘路径。
"""
import json
import os
import re
from pathlib import Path

REPO_DIR = Path(os.environ.get("ISO_REPO_DIR", "/app/iso_download"))
DATA_DIR = Path(os.environ.get("ISO_DATA_DIR", "/data"))

# 预置分类随镜像分发(只读); 用户配置在持久卷(可写, 独立保存不被刷新覆盖)
PRESET_JSON = REPO_DIR / "torrent_categories.json"
USER_JSON = DATA_DIR / "torrent_categories.json"

UNCATEGORIZED = "__uncategorized__"
UNCATEGORIZED_NAME = {"zh": "未分类", "en": "Uncategorized"}
# 未分类恒排在最后: 取一个必然大于任何用户 order 的值
UNC_ORDER = 10 ** 6

_OVERRIDE_KEYS = ("enabled", "name", "keywords", "exclude", "order")

# ---- 用户配置写入的硬约束(设置页提交的内容一律按不可信输入处理) ----
USER_CAT_ID_RE = re.compile(r"^user_[a-z0-9]{1,32}$")
MAX_USER_CATEGORIES = 50
MAX_KEYWORDS = 50
MAX_WORD_LEN = 40
MAX_NAME_LEN = 40


def _read_json(path: Path, default, report, label: str):
    """容错读取 JSON。解析失败必须进 report —— 不能静默返回 default 就走。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except Exception as e:  # noqa: BLE001  (权限/编码/JSON 语法都算配置损坏)
        if report is not None:
            report.append({"file": label, "path": str(path),
                           "error": "%s: %s" % (type(e).__name__, e)})
        return default


def load_categories(report=None) -> list:
    """实时读取并合并分类配置。**不做任何缓存。**

    合并顺序: 预置(REPO_DIR) → 应用 override → 追加用户分类 → 按 (order, id) 排序

    Args:
        report: 可选 list。传入时, 读取过程中的异常会被 append 进去,
                供接口返回给前端 —— 配置损坏必须可见。

    Returns:
        合并后的分类列表。某个文件缺失/损坏时返回**其余可用的**配置, 而非空列表。
    """
    preset = _read_json(PRESET_JSON, {"version": 1, "categories": []}, report, "preset")
    user = _read_json(USER_JSON, {}, report, "user")
    if not isinstance(preset, dict):
        preset = {}
    if not isinstance(user, dict):
        user = {}

    cats = []
    for c in (preset.get("categories") or []):
        if isinstance(c, dict) and c.get("id"):
            cats.append(dict(c, builtin=True))

    # override: 允许禁用/改词/改名/改排序, 但不允许新增
    overrides = user.get("overrides")
    if isinstance(overrides, dict):
        for c in cats:
            ov = overrides.get(c["id"])
            if isinstance(ov, dict):
                for k in _OVERRIDE_KEYS:
                    if k in ov:
                        c[k] = ov[k]

    for c in (user.get("user_categories") or []):
        if isinstance(c, dict) and c.get("id"):
            cats.append(dict(c, builtin=False))

    cats.sort(key=lambda c: (c.get("order", 999), str(c.get("id", ""))))
    return cats


def load_show_empty() -> bool:
    """空分类是否显示(前端用)。默认 False —— 不显示空分组。"""
    user = _read_json(USER_JSON, {}, None, "user")
    if isinstance(user, dict):
        return bool(user.get("show_empty", False))
    return False


def load_user_doc() -> dict:
    """读原始用户配置(设置页回显用)。缺失/损坏返回空骨架, 不抛。"""
    doc = _read_json(USER_JSON, {}, None, "user")
    if not isinstance(doc, dict):
        doc = {}
    return {
        "version": 1,
        "show_empty": bool(doc.get("show_empty", False)),
        "user_categories": doc.get("user_categories")
                           if isinstance(doc.get("user_categories"), list) else [],
        "overrides": doc.get("overrides")
                     if isinstance(doc.get("overrides"), dict) else {},
    }


def _clean_words(words) -> list:
    out = []
    for w in words or []:
        if not isinstance(w, str):
            continue
        w = w.strip().lower()
        if w and len(w) <= MAX_WORD_LEN and w not in out:
            out.append(w)
        if len(out) >= MAX_KEYWORDS:
            break
    return out


def validate_user_doc(doc) -> list:
    """校验用户配置, 返回错误信息列表(空列表 = 通过)。

    这里是**信任边界**: 设置页提交的内容按不可信输入处理。
    只接受 user_categories + overrides + show_empty 三个键, 别的一律丢弃。
    """
    errs = []
    if not isinstance(doc, dict):
        return ["配置必须是 JSON 对象"]

    cats = doc.get("user_categories") or []
    if not isinstance(cats, list):
        errs.append("user_categories 必须是数组")
        cats = []
    if len(cats) > MAX_USER_CATEGORIES:
        errs.append("用户分类过多(上限 %d)" % MAX_USER_CATEGORIES)

    seen = set()
    for i, c in enumerate(cats):
        tag = "第 %d 个用户分类" % (i + 1)
        if not isinstance(c, dict):
            errs.append(tag + " 不是对象")
            continue
        cid = c.get("id")
        if not isinstance(cid, str) or not USER_CAT_ID_RE.match(cid):
            errs.append(tag + " 的 id 非法(必须形如 user_xxxxxx, 避免与预置冲突)")
        elif cid in seen:
            errs.append(tag + " 的 id 重复: " + cid)
        else:
            seen.add(cid)
        name = c.get("name")
        if isinstance(name, str):
            name = {"zh": name, "en": name}
        if not isinstance(name, dict) or not str(name.get("zh") or "").strip():
            errs.append(tag + " 缺少中文名")
        elif len(str(name["zh"])) > MAX_NAME_LEN:
            errs.append(tag + " 的中文名过长(上限 %d)" % MAX_NAME_LEN)
        kws = _clean_words(c.get("keywords"))
        if not kws:
            errs.append(tag + " 至少需要一个关键词")

    ov = doc.get("overrides") or {}
    if not isinstance(ov, dict):
        errs.append("overrides 必须是对象")
    else:
        for k, v in ov.items():
            if not isinstance(k, str) or not k:
                errs.append("overrides 的键必须是非空字符串")
            elif not isinstance(v, dict):
                errs.append("overrides[%s] 必须是对象" % k)
    return errs


def save_user_doc(doc) -> None:
    """校验并原子写入用户配置。校验不过抛 ValueError。"""
    errs = validate_user_doc(doc)
    if errs:
        raise ValueError("; ".join(errs))

    cats = []
    for c in (doc.get("user_categories") or []):
        name = c.get("name")
        if isinstance(name, str):
            name = {"zh": name, "en": name}
        cats.append({
            "id": c["id"],
            "name": {"zh": str(name.get("zh") or "").strip(),
                     "en": str(name.get("en") or name.get("zh") or "").strip()},
            "enabled": bool(c.get("enabled", True)),
            "keywords": _clean_words(c.get("keywords")),
            "exclude": _clean_words(c.get("exclude")),
            "order": c.get("order", 100) if isinstance(c.get("order"), int) else 100,
        })

    out = {
        "version": 1,
        "show_empty": bool(doc.get("show_empty", False)),
        "user_categories": cats,
        "overrides": doc.get("overrides") or {},
    }

    path = Path(USER_JSON)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)          # 原子替换: 半截文件不会污染配置


def _prefix_match(text: str, words) -> bool:
    """**词首匹配**(只看前边界) —— 给关键词用, **求召回**。

    'ubuntu' 必须能覆盖 'ubuntukylin'/'ubuntucinnamon'/'ubuntustudio' 这类
    单 token 派生版, 否则 Ubuntu 家族会被拆散(实测踩过)。
    同时仍挡住真正的误伤: 'source' 不命中 'resource'(前面是 'e'),
    'arch' 不命中 'search'/'research'。

    注意: text 必须是**已小写**的字符串。
    """
    for w in words or []:
        if not isinstance(w, str) or not w.strip():
            continue
        w = w.strip().lower()
        if re.search(r"(?<![a-z0-9])" + re.escape(w), text):
            return True
    return False


def _word_match(text: str, words) -> bool:
    """**完整词匹配**(前后边界都看) —— 给排除词用, **求精确**。

    排除词一命中就整个分类判负, 属于破坏性操作, 宁可少排不可误排:
    'src' 不该命中 'srcfoo', 'source' 不该命中 'resource'。

    注意: text 必须是**已小写**的字符串。
    """
    for w in words or []:
        if not isinstance(w, str) or not w.strip():
            continue
        w = w.strip().lower()
        if re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", text):
            return True
    return False


def classify_one(title: str, categories: list):
    """把单个标题归入一个分类。返回 (category_id, reason)。

    匹配语义(两种刻意不同, 别统一):
      * 关键词 `_prefix_match`  -> 词首匹配, 求召回('ubuntu' 要覆盖 'ubuntukylin')
      * 排除词 `_word_match`    -> 完整词匹配, 求精确('source' 不误伤 'resource')
    """
    t = (title or "").lower()
    cands = []
    for cat in categories or []:
        if not isinstance(cat, dict) or not cat.get("id"):
            continue
        if not cat.get("enabled", True):
            continue
        # 排除词优先: 命中即判负(整个分类跳过)
        if _word_match(t, cat.get("exclude")):
            continue
        hits = [k for k in (cat.get("keywords") or []) if _prefix_match(t, [k])]
        if hits:
            cands.append((max(len(h) for h in hits),
                          cat.get("order", 999), str(cat.get("id", "")), cat))
    if not cands:
        return UNCATEGORIZED, "no_match"
    # 冲突裁决: 最长关键词(更具体者胜) → order → id(保证同分时结果确定)
    cands.sort(key=lambda x: (-x[0], x[1], x[2]))
    return cands[0][3]["id"], "keyword"


def classify(items: list, categories: list) -> dict:
    """纯函数: 不抓网、不读文件、不依赖全局状态。

    Args:
        items:      [{"title","url","pubDate","source","builtin"}, ...]
        categories: load_categories() 的返回值

    Returns:
        {"categories": [{"id","name","fold_key","order","builtin","items"}...],
         "counts": {category_id: n}}
        未分类恒在最后; 空分类也会返回(由前端按 show_empty 决定是否显示)。
    """
    buckets = {}
    order = {}
    meta = {}
    for c in categories or []:
        if isinstance(c, dict) and c.get("id"):
            buckets.setdefault(c["id"], [])
            order[c["id"]] = c.get("order", 999)
            meta[c["id"]] = c
    buckets.setdefault(UNCATEGORIZED, [])
    order.setdefault(UNCATEGORIZED, UNC_ORDER)

    for it in items or []:
        if not isinstance(it, dict):
            continue
        # 既无 title 又无 url 的条目不是条目(接口层同样过滤): 丢弃而不是
        # 造出一条空标题的幽灵行。"绝不丢条目"针对的是**真实条目**。
        raw = it.get("title") or it.get("url") or ""
        if not isinstance(raw, str) or not raw.strip():
            continue
        cid, _reason = classify_one(raw, categories)
        buckets.setdefault(cid, []).append(it)     # 兜底: 未知 id 也不丢条目

    out = []
    for cid in sorted(buckets, key=lambda c: (order.get(c, UNC_ORDER), c)):
        m = meta.get(cid) or {}
        out.append({
            "id": cid,
            "name": m.get("name") or UNCATEGORIZED_NAME,
            "fold_key": "tcat:" + cid,          # 后端下发, 前端不拼字符串
            "order": order.get(cid, UNC_ORDER),
            "builtin": bool(m.get("builtin", cid == UNCATEGORIZED)),
            "items": buckets[cid],
        })
    return {"categories": out,
            "counts": {c["id"]: len(c["items"]) for c in out}}
