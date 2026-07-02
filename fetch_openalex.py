#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
研究雷达 · OpenAlex 每日抓取脚本
Fetch recent papers from OpenAlex, score them against your research profile,
build APA citations, (optionally) translate to Chinese via Claude, and write
papers.json for the Research Radar front-end.

用法 Usage
---------
1. 去 https://openalex.org/settings/api 注册免费 API key
2. 设环境变量：
     export OPENALEX_API_KEY="你的key"
     export ANTHROPIC_API_KEY="你的key"   # 可选：开启自动中译 + 推荐理由
3. 运行：
     python fetch_openalex.py --days 30 --max 8
   离线自测（用内置样例，不联网）：
     python fetch_openalex.py --mock

输出 papers.json 与 research-radar.html 放同一目录，本地起服务即可看到实时数据：
     python -m http.server 8000   然后浏览器打开 http://localhost:8000/research-radar.html
"""

import os, json, argparse, urllib.request, urllib.parse, datetime, re, sys

# ---------------------------------------------------------------------------
# 1) 你的研究画像：主题 -> 权重 + 英文关键词（打分与自动标签都基于此）
# ---------------------------------------------------------------------------
PROFILE = {
    "价值共创":     {"w": 12, "kw": ["value co-creation", "value cocreation", "co-creation", "cocreation",
                                    "value creation", "service-dominant", "service dominant", "s-d logic"],
                     "kw_zh": ["价值共创", "共同创造", "服务主导逻辑", "价值创造"]},
    "居民参与":     {"w": 12, "kw": ["resident participation", "resident involvement", "resident engagement",
                                    "community participation", "local participation", "host community"],
                     "kw_zh": ["居民参与", "社区参与", "居民感知", "东道主", "本地居民"]},
    "节事/事件":    {"w": 12, "kw": ["festival", "event tourism", "community event", "cultural event",
                                    "mega-event", "celebration", "eventful"],
                     "kw_zh": ["节事", "节庆", "事件旅游", "文化节", "庙会", "大型活动"]},
    "互动仪式链":   {"w": 10, "kw": ["interaction ritual", "emotional energy", "collective effervescence",
                                    "co-presence", "copresence", "ritual chain"],
                     "kw_zh": ["互动仪式", "情感能量", "集体欢腾", "共同在场", "仪式链"]},
    "情感互动品质": {"w": 10, "kw": ["emotional interaction", "emotional engagement", "affective",
                                    "emotional quality", "emotional solidarity"],
                     "kw_zh": ["情感互动", "情感卷入", "情感团结", "情感体验"]},
    "推-拉动因":    {"w": 8,  "kw": ["push-pull", "push and pull", "push factor", "pull factor",
                                    "travel motivation", "participation motivation"],
                     "kw_zh": ["推拉", "推力", "拉力", "旅游动机", "参与动机"]},
    "自我决定理论": {"w": 8,  "kw": ["self-determination", "intrinsic motivation", "extrinsic motivation",
                                    "motivation internalization", "autonomy", "psychological need"],
                     "kw_zh": ["自我决定", "内在动机", "外在动机", "动机内化", "心理需要"]},
    "社区/文化遗产":{"w": 8,  "kw": ["cultural heritage", "sense of community", "place attachment",
                                    "cultural identity", "community wellbeing", "social capital"],
                     "kw_zh": ["文化遗产", "非物质文化遗产", "社区感", "地方依恋", "文化认同", "社会资本"]},
    "澳门":         {"w": 10, "kw": ["macau", "macao"],
                     "kw_zh": ["澳门", "粤港澳", "大湾区"]},
}

# 用于 OpenAlex 关键词检索的查询串（覆盖核心概念即可，命中后再本地精打分）
SEARCH_TERMS = [
    "value co-creation festival",
    "resident participation event tourism",
    "emotional interaction festival community",
    "interaction ritual tourism",
    "community festival participation motivation",
]

# 中文通道：用中文关键词 + 语言过滤(language:zh)再搜一轮，抓中文期刊论文。
# 注意：OpenAlex 只收录有 DOI 的中文刊（旅游学刊等核心刊多数有），纯知网独家的刊覆盖不到。
FETCH_CHINESE = True
CN_SEARCH_TERMS = [
    "价值共创 节事",
    "居民参与 节庆",
    "文化节 社区 参与",
    "互动仪式 情感能量",
    "事件旅游 动机",
]

# 可选：只保留这些顶刊（ISSN）。留空 [] 表示不限期刊。
JOURNAL_ISSNS = [
    # "0261-5177",  # Tourism Management
    # "0160-7383",  # Annals of Tourism Research
]

# ---- 免费翻译（无需任何 key）----
# 没有设置 ANTHROPIC_API_KEY 时，用 MyMemory 免费引擎把标题/摘要译成中文。
USE_FREE_TRANSLATE = True
# 强烈建议填一个你的邮箱：匿名每天仅 5000 字符且按 IP 计（GitHub 的 IP 是共享的，很容易被占满）；
# 填邮箱后额度提到每天 50000 字符、且按邮箱计，稳得多。只是个字符串，不用注册。
MYMEMORY_EMAIL = "1905872@gmail.com"

OPENALEX = "https://api.openalex.org/works"
MYMEMORY = "https://api.mymemory.translated.net/get"

# ---------------------------------------------------------------------------
# 2) 工具函数
# ---------------------------------------------------------------------------
def reconstruct_abstract(inv):
    """把 OpenAlex 的 abstract_inverted_index 还原成正常文本。"""
    if not inv:
        return ""
    pairs = []
    for word, positions in inv.items():
        for p in positions:
            pairs.append((p, word))
    pairs.sort(key=lambda x: x[0])
    return " ".join(w for _, w in pairs)


def score_and_tag(title, abstract):
    """基于研究画像给一篇论文打分并生成主题标签（中英关键词都认）。"""
    t, a = title.lower(), abstract.lower()
    raw, tags = 0.0, []
    for topic, cfg in PROFILE.items():
        kws = cfg["kw"] + cfg.get("kw_zh", [])
        in_title = any(k.lower() in t for k in kws)
        in_abs = any(k.lower() in a for k in kws)
        if in_title:
            raw += cfg["w"]
            tags.append(topic)
        elif in_abs:
            raw += cfg["w"] * 0.5
            tags.append(topic)
    rel = min(97, round(48 + raw))       # 48 起底，命中越多越高，封顶 97
    return rel, tags[:4], raw


def format_author(display_name):
    """'Robert F. Lusch' -> 'Lusch, R. F.'"""
    parts = display_name.strip().split()
    if len(parts) == 1:
        return parts[0]
    last = parts[-1]
    initials = " ".join(f"{p[0]}." for p in parts[:-1] if p)
    return f"{last}, {initials}"


def to_apa(work):
    """由 OpenAlex work 对象构造 APA 7 引用（含 <i> 斜体标记，供网页渲染）。"""
    auths = [a["author"]["display_name"] for a in work.get("authorships", []) if a.get("author")]
    if not auths:
        who = "Anonymous."
    else:
        fmt = [format_author(x) for x in auths[:20]]
        if len(fmt) == 1:
            who = fmt[0]
        else:
            who = ", ".join(fmt[:-1]) + ", & " + fmt[-1]
        if not who.endswith("."):
            who += "."
    year = work.get("publication_year", "n.d.")
    title = (work.get("display_name") or "").rstrip(".")
    src = ((work.get("primary_location") or {}).get("source") or {}).get("display_name", "")
    biblio = work.get("biblio") or {}
    vol, issue = biblio.get("volume"), biblio.get("issue")
    fp, lp = biblio.get("first_page"), biblio.get("last_page")
    doi = work.get("doi") or ""
    seg = f"{who} ({year}). {title}. <i>{src}"
    if vol:
        seg += f", {vol}"
    seg += "</i>"
    if issue:
        seg += f"({issue})"
    if fp:
        seg += f", {fp}" + (f"–{lp}" if lp else "")
    seg += "."
    if doi:
        seg += f" {doi}"
    return seg


def short_authors(work, n=3):
    auths = [a["author"]["display_name"] for a in work.get("authorships", []) if a.get("author")]
    if not auths:
        return ""
    out = ", ".join(auths[:n])
    if len(auths) > n:
        out += " et al."
    return out


# ---------------------------------------------------------------------------
# 3) 可选：用 Claude 逐篇生成中文标题 / 摘要 / 推荐理由
# ---------------------------------------------------------------------------
def claude_insight(title, abstract):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    prompt = (
        "你在帮一位研究『社区文化节事居民参与的价值共创（IRC/VCC/SDT 框架，澳门情境）』的中文博士生。"
        "针对下面这篇英文论文，只返回一个 JSON 对象，字段：\n"
        "title_zh（中文标题）、abstract_zh（150字内中文摘要）、why（一句话说明为什么值得他读，扣住他的研究）、"
        "paras（3 个元素的数组，每个 {en, zh}，把摘要拆成对照段落）。不要多余文字。\n\n"
        f"标题：{title}\n摘要：{abstract[:1500]}"
    )
    body = json.dumps({
        "model": "claude-opus-4-8",
        "max_tokens": 1200,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        text = re.sub(r"^```json|```$", "", text.strip()).strip()
        return json.loads(text)
    except Exception as e:
        print(f"   ! Claude 翻译失败，改用英文原文：{e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# 3b) 免费翻译（MyMemory，无需 key）——没有 Anthropic key 时的中译方案
# ---------------------------------------------------------------------------
def _sentence_chunks(text, limit=480):
    """按句子切成 <=limit 字符的块（MyMemory 单次限制约 500 字节）。"""
    sents = re.split(r"(?<=[.!?])\s+", text.strip())
    chunks, cur = [], ""
    for s in sents:
        while len(s) > limit:                 # 超长单句硬切
            if cur:
                chunks.append(cur.strip()); cur = ""
            chunks.append(s[:limit]); s = s[limit:]
        if len(cur) + len(s) + 1 > limit:
            if cur:
                chunks.append(cur.strip())
            cur = s
        else:
            cur = (cur + " " + s).strip()
    if cur:
        chunks.append(cur.strip())
    return [c for c in chunks if c]


def _mm_translate(text):
    """调 MyMemory 翻一个块，失败返回 None。"""
    params = {"q": text, "langpair": "en|zh-CN"}
    if MYMEMORY_EMAIL:
        params["de"] = MYMEMORY_EMAIL
    url = MYMEMORY + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            d = json.load(r)
        if d.get("responseStatus") == 200:
            return d["responseData"]["translatedText"]
    except Exception as e:
        print(f"   ! 免费翻译失败一段，保留英文：{e}", file=sys.stderr)
    return None


def free_translate_paras(text):
    """把英文文本译成中英对照的段落数组 [{en, zh}, ...]，失败的段落回退为英文。"""
    import time
    paras = []
    for c in _sentence_chunks(text):
        zh = _mm_translate(c) or c
        paras.append({"en": c, "zh": zh})
        time.sleep(0.4)                        # 轻微限速，别把免费接口打太急
    return paras or [{"en": text, "zh": text}]



def build_url(since, per_page, terms=None, language=None):
    filters = [f"from_publication_date:{since}", "type:article", "has_abstract:true"]
    if language:
        filters.append(f"language:{language}")
    if JOURNAL_ISSNS:
        filters.append("primary_location.source.issn:" + "|".join(JOURNAL_ISSNS))
    params = {
        "search": " ".join(terms or SEARCH_TERMS),
        "filter": ",".join(filters),
        "sort": "publication_date:desc",
        "per-page": per_page,
        "select": "id,display_name,publication_year,publication_date,authorships,"
                  "primary_location,biblio,doi,abstract_inverted_index,cited_by_count,language",
    }
    key = os.environ.get("OPENALEX_API_KEY")
    if key:
        params["api_key"] = key
    return OPENALEX + "?" + urllib.parse.urlencode(params)


def fetch_works(days, per_page, terms=None, language=None):
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    url = build_url(since, per_page, terms, language)
    if not os.environ.get("OPENALEX_API_KEY"):
        print("! 未设置 OPENALEX_API_KEY —— 无 key 每天仅 100 credits（测试用），超出会报 409。",
              file=sys.stderr)
    req = urllib.request.Request(url, headers={"User-Agent": "ResearchRadar/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r).get("results", [])


# ---------------------------------------------------------------------------
# 5) 主流程：抓取 -> 打分 -> 组装 -> 写 papers.json
# ---------------------------------------------------------------------------
def build_papers(works, max_out, translate=True):
    scored = []
    for w in works:
        title = w.get("display_name") or ""
        abstract = reconstruct_abstract(w.get("abstract_inverted_index"))
        if not title:
            continue
        rel, tags, raw = score_and_tag(title, abstract)
        if raw < 8:                      # 相关度太低，丢弃噪声
            continue
        src = ((w.get("primary_location") or {}).get("source") or {}).get("display_name", "来源不详")
        year = w.get("publication_year", "")
        is_zh = w.get("language") == "zh" or bool(re.search(r"[\u4e00-\u9fff]", title))
        topic_line = f"命中你的主题：{('、'.join(tags)) or '相关领域'}。"
        if is_zh:                         # 中文论文：无需翻译，直接双语同文
            zh, abstract_zh, why = title, abstract, topic_line
            paras = [{"en": p, "zh": p} for p in _sentence_chunks(abstract, limit=300)] \
                    or [{"en": abstract, "zh": abstract}]
            src = f"{src}（中文刊）" if src != "来源不详" else "中文期刊"
        else:
            insight = claude_insight(title, abstract)
            if insight:                   # 首选：Claude 中译 + 推荐理由（需 ANTHROPIC_API_KEY）
                zh = insight.get("title_zh", title)
                abstract_zh = insight.get("abstract_zh", abstract)
                why = insight.get("why", topic_line)
                paras = insight.get("paras", [{"en": abstract, "zh": abstract_zh}])
            elif translate and USE_FREE_TRANSLATE:   # 免费翻译（MyMemory，无需 key）
                zh = _mm_translate(title) or title
                paras = free_translate_paras(abstract)
                abstract_zh = " ".join(p["zh"] for p in paras)
                why = topic_line
            else:                         # 兜底：全英文
                zh, abstract_zh, why = title, abstract, topic_line
                paras = [{"en": abstract, "zh": abstract}]
        scored.append({
            "id": "oa_" + (w.get("id", "").rsplit("/", 1)[-1] or str(len(scored))),
            "rel": rel,
            "venue": f"{src} · {year}",
            "type": "new",
            "zh": zh,
            "en": title,
            "authors": short_authors(w),
            "absEn": abstract,
            "abstract": abstract_zh,
            "why": why,
            "tags": tags or ["相关领域"],
            "apa": to_apa(w),
            "paras": paras,
            "url": w.get("doi") or w.get("id") or "",
        })
    scored.sort(key=lambda p: p["rel"], reverse=True)
    return scored[:max_out]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30, help="回溯天数（社科出版慢，建议 14-45）")
    ap.add_argument("--max", type=int, default=8, help="今日精选保留篇数")
    ap.add_argument("--per-page", type=int, default=50, help="每次向 OpenAlex 请求的条数")
    ap.add_argument("--out", default="papers.json")
    ap.add_argument("--mock", action="store_true", help="离线自测：用内置样例，不联网")
    args = ap.parse_args()

    if args.mock:
        works = MOCK_WORKS
        print(f"[mock] 使用 {len(works)} 条内置样例（不联网）")
    else:
        works = fetch_works(args.days, args.per_page)
        print(f"OpenAlex（英文通道）返回 {len(works)} 条")
        if FETCH_CHINESE:
            try:
                zh_works = fetch_works(args.days, args.per_page,
                                       terms=CN_SEARCH_TERMS, language="zh")
                print(f"OpenAlex（中文通道）返回 {len(zh_works)} 条")
                seen = {w.get("id") for w in works}
                works += [w for w in zh_works if w.get("id") not in seen]
            except Exception as e:
                print(f"! 中文通道抓取失败（不影响英文结果）：{e}", file=sys.stderr)
        print(f"合并去重后共 {len(works)} 条，开始打分…")

    papers = build_papers(works, args.max, translate=not args.mock)
    payload = {
        "generated_at": datetime.datetime.now().isoformat(timespec="minutes"),
        "source": "OpenAlex" + (" (mock)" if args.mock else ""),
        "count": len(papers),
        "papers": papers,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"✓ 写出 {len(papers)} 篇 -> {args.out}")
    for p in papers:
        print(f"   [{p['rel']}%] {p['en'][:70]}  <{'、'.join(p['tags'])}>")


# ---------------------------------------------------------------------------
# 内置样例（模拟 OpenAlex 返回，用于离线验证整条流水线）
# ---------------------------------------------------------------------------
def _invert(text):
    """把明文倒排成 abstract_inverted_index（仅用于构造样例）。"""
    idx = {}
    for i, word in enumerate(text.split()):
        idx.setdefault(word, []).append(i)
    return idx

MOCK_WORKS = [
    {
        "id": "https://openalex.org/W4400000010",
        "display_name": "节事价值共创中的居民情感能量：基于澳门社区文化节的质性研究",
        "publication_year": 2026, "publication_date": "2026-06-12",
        "language": "zh",
        "authorships": [
            {"author": {"display_name": "Wei Zhang"}},
            {"author": {"display_name": "Mei Lin Ho"}},
        ],
        "primary_location": {"source": {"display_name": "旅游学刊"}},
        "biblio": {"volume": "41", "issue": "6", "first_page": "88", "last_page": "102"},
        "doi": "https://doi.org/10.19765/j.cnki.1002-5006.2026.06.008",
        "cited_by_count": 0,
        "abstract_inverted_index": _invert(
            "本文以澳门社区文化节为案例 探讨居民参与中的价值共创机制。研究发现 互动仪式产生的情感能量是居民从被动参与转向主动共创的关键。"),
    },
    {
        "id": "https://openalex.org/W4400000001",
        "display_name": "Emotional interaction quality and resident value co-creation in community festivals",
        "publication_year": 2026, "publication_date": "2026-06-18",
        "authorships": [
            {"author": {"display_name": "Li Chen"}},
            {"author": {"display_name": "Maria Ferreira"}},
            {"author": {"display_name": "Ka Wong"}},
        ],
        "primary_location": {"source": {"display_name": "Tourism Management", "issn": ["0261-5177"]}},
        "biblio": {"volume": "103", "issue": "2", "first_page": "104988", "last_page": None},
        "doi": "https://doi.org/10.1016/j.tourman.2026.104988",
        "cited_by_count": 1,
        "abstract_inverted_index": _invert(
            "This study examines how emotional interaction quality shapes resident value co-creation "
            "in community festivals. Using three-wave panel data from residents, we find that "
            "high-intensity emotional interaction produces a multiplier effect on the shift from "
            "passive participation to active co-creation, extending interaction ritual theory."),
    },
    {
        "id": "https://openalex.org/W4400000002",
        "display_name": "From extrinsic to intrinsic: motivation internalization among festival participants in Macau",
        "publication_year": 2026, "publication_date": "2026-06-05",
        "authorships": [
            {"author": {"display_name": "Ana Novak"}},
            {"author": {"display_name": "Rahul Patel"}},
        ],
        "primary_location": {"source": {"display_name": "Annals of Tourism Research", "issn": ["0160-7383"]}},
        "biblio": {"volume": "109", "issue": None, "first_page": "103820", "last_page": None},
        "doi": "https://doi.org/10.1016/j.annals.2026.103820",
        "cited_by_count": 0,
        "abstract_inverted_index": _invert(
            "Integrating self-determination theory with service-dominant logic, this paper traces how "
            "extrinsic participation motivation becomes internalized into intrinsic motivation among "
            "festival participants in Macau, and how operant resources drive this shift."),
    },
    {
        "id": "https://openalex.org/W4400000003",
        "display_name": "A bibliometric review of blockchain adoption in supply chains",
        "publication_year": 2026, "publication_date": "2026-06-20",
        "authorships": [{"author": {"display_name": "John Smith"}}],
        "primary_location": {"source": {"display_name": "Journal of Operations Management"}},
        "biblio": {"volume": "70", "issue": "3", "first_page": "55", "last_page": "78"},
        "doi": "https://doi.org/10.1000/mock.3",
        "cited_by_count": 4,
        "abstract_inverted_index": _invert(
            "This paper reviews blockchain adoption in global supply chains using bibliometric methods. "
            "It maps clusters and research fronts unrelated to tourism or festivals."),  # 应被过滤
    },
]

if __name__ == "__main__":
    main()
