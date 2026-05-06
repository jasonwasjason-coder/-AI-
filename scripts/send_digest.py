#!/usr/bin/env python3
import os
import sys
import json
import requests
from datetime import datetime, timezone, timedelta

FEED_BASE = "https://raw.githubusercontent.com/zarazhangrui/follow-builders/main"
PROMPTS_BASE = f"{FEED_BASE}/prompts"
CST = timezone(timedelta(hours=8))
GITHUB_MODELS_URL = "https://models.inference.ai.azure.com/chat/completions"
MODEL = "gpt-4o-mini"
WECOM_LIMIT = 4096


def fetch_json(url):
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_text(url):
    resp = requests.get(url, timeout=30)
    return resp.text if resp.ok else ""


def fetch_feeds():
    feeds = {}
    for key, name in [("tweets", "feed-x"), ("blogs", "feed-blogs"), ("podcasts", "feed-podcasts")]:
        try:
            feeds[key] = fetch_json(f"{FEED_BASE}/{name}.json")
            count = len(feeds[key]) if isinstance(feeds[key], list) else "?"
            print(f"  {name}.json: {count} items")
        except Exception as e:
            print(f"  WARNING: Could not fetch {name}.json: {e}")
            feeds[key] = []
    return feeds


def fetch_prompts():
    names = ["digest-intro", "summarize-tweets", "summarize-blogs", "summarize-podcast", "translate"]
    return {name: fetch_text(f"{PROMPTS_BASE}/{name}.md") for name in names}


def build_system_prompt(prompts, today):
    return f"""你是一个AI行业资讯编辑，将英文AI资讯整理成每日中文推送，发送到企业微信群。

今天日期：{today}

【第一行格式（必须严格遵守）】
- 第一行固定为：**🤖 AI Builders Digest — {today}**
- 注意：🤖 后面有一个空格，整行加粗

【排版规则（企业微信Markdown）】
- 标题用 **粗体**，不用 # 号
- 列表用 - 或数字
- 引用用 >
- 超链接的位置和格式：在内容末尾紧跟超链接，不换行，超链接的文案为原文，原文两边加上[]，例如：Swyx 讨论了 OAI 估值，认为差距明显：[原文]，这里的原文是超链接
- 每条资讯不超过2-3句话，简洁有力
- 总字数控制在1800字以内
- 资讯按人物维度换行，相同人物的资讯不换行，不同人物的资讯换行

【内容规则】
{prompts.get('digest-intro', '')}

【推文摘要规则】
{prompts.get('summarize-tweets', '')}

【翻译规则】
{prompts.get('translate', '')}

【重要】
- 输出全中文，技术名词保留英文（AI、LLM、API、GPU、RAG、agent、token等）
- 人名、产品名、公司名保留英文
- 不要捏造或推测内容，只整理已有信息
- 某个板块（X/Twitter、Official Blogs、Podcasts）如果没有新内容，直接跳过，不要展示该板块
- 结尾不加任何来源说明
"""


def truncate_feeds(feeds, max_chars=6000):
    raw = json.dumps(feeds, ensure_ascii=False)
    if len(raw) <= max_chars:
        return raw
    result = {}
    for key, items in feeds.items():
        result[key] = items[:max(1, len(items) // 2)] if isinstance(items, list) else items
    return json.dumps(result, ensure_ascii=False)[:max_chars]


def call_github_models(token, system_prompt, user_content):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": 2048,
        "temperature": 0.6,
    }
    resp = requests.post(GITHUB_MODELS_URL, headers=headers, json=body, timeout=180)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def send_to_wecom(webhook_url, text):
    encoded = text.encode("utf-8")
    if len(encoded) <= WECOM_LIMIT:
        chunks = [text]
    else:
        paragraphs = text.split("\n\n")
        chunks, current = [], ""
        for para in paragraphs:
            candidate = current + "\n\n" + para if current else para
            if len(candidate.encode("utf-8")) > WECOM_LIMIT - 50:
                if current:
                    chunks.append(current)
                current = para
            else:
                current = candidate
        if current:
            chunks.append(current)

    for i, chunk in enumerate(chunks):
        if i > 0:
            chunk = "（续）\n\n" + chunk
        data = {"msgtype": "markdown", "markdown": {"content": chunk}}
        resp = requests.post(webhook_url, json=data, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        if result.get("errcode") != 0:
            raise RuntimeError(f"WeChat Work error on chunk {i+1}: {result}")
        print(f"  Chunk {i+1}/{len(chunks)} sent OK")


def main():
    github_token = os.environ.get("GITHUB_TOKEN")
    wecom_webhook = os.environ.get("WECOM_WEBHOOK_URL")

    if not github_token:
        print("ERROR: GITHUB_TOKEN not set", file=sys.stderr)
        sys.exit(1)
    if not wecom_webhook:
        print("ERROR: WECOM_WEBHOOK_URL not set", file=sys.stderr)
        sys.exit(1)

    today = datetime.now(CST).strftime("%Y年%m月%d日")
    print(f"Generating digest for {today}")

    print("Fetching feeds...")
    feeds = fetch_feeds()

    print("Fetching prompts...")
    prompts = fetch_prompts()

    print("Calling GitHub Models...")
    system_prompt = build_system_prompt(prompts, today)
    feed_content = truncate_feeds(feeds)
    user_message = f"请根据以下原始数据，生成 {today} AI资讯日报：\n\n{feed_content}"
    digest = call_github_models(github_token, system_prompt, user_message)
    # Strip footer injected by upstream prompts
    digest = "\n".join(
        line for line in digest.splitlines()
        if "follow-builders" not in line.lower() and "follow builders" not in line.lower()
    ).strip()
    print(f"Digest generated ({len(digest)} chars)")

    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    if dry_run:
        print("DRY RUN — digest output (not sent):")
        print("-" * 60)
        print(digest)
        print("-" * 60)
    else:
        print("Sending to WeChat Work...")
        send_to_wecom(wecom_webhook, digest)
    print("Done.")


if __name__ == "__main__":
    main()
