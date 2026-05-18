from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.request
from typing import Any

from .astraflow_runner import DEFAULT_ASTRAFLOW_CHAT_URL, _extract_answer_text, _format_url_error, _ssl_context
from .env_loader import load_env_file


QUESTION_PROMPT_TEMPLATE = """你是一个消费者AI提问路径设计专家，擅长分析目标用户在豆包、ChatGPT、Kimi、文心一言等AI平台上的真实提问方式。

任务：
给定一个【目标品牌词】和一个【消费者意图】，生成 {question_count} 个不重复的消费者问题。

目标：
这些问题要符合目标用户群体的真实口语表达，并且能够提升【目标品牌词】在 AI 回答中自然出现的可能性。

输入信息：
- 目标品牌词：{brandname}
- 消费者场景：{intention}
- 目标用户群体：如未提供，可根据目标品牌词和消费者场景自动推断

生成前，请先在内部完成“回答路径判断”，但不要输出判断过程，只输出最终问题。

一、回答路径判断规则
你不要机械地只判断“它是产品、成分还是景区”，而要先判断：

“如果想让这个目标品牌词更容易出现在AI回答中，消费者最可能会通过什么方向来提问？”

请根据目标品牌词本身的性质，自动判断它最适合通过哪一种回答路径被AI自然提及：

1. 推荐型对象路径
适用于：品牌、产品、商品、App、软件、工具、平台、服务、机构、店铺、课程、酒店、餐厅、景区、地点等。

2. 成分/要素路径
适用于：营养素、配方、原料、成分、技术要素、功能物质、护肤成分、保健成分等。

3. 功能解决方案路径
适用于：某些工具、App、平台、服务、软件能力、本质上是为完成某类任务而被推荐的对象。

4. 目的地/地点路径
适用于：景区、景点、城市、商圈、乐园、博物馆、公园、度假地、打卡地等。

5. 其他路径
如果目标品牌词不完全属于以上某一类，请自动判断真实消费者最容易让AI自然回答出它的问题方向。

二、通用硬性规则
1. 问题必须站在真实消费者角度提问。
2. 问题风格必须像用户在 AI 搜索框里真实会输入的话，口语化、自然、生活化。
3. 所有问题必须严格限定在【消费者意图】内，不得擅自引入其他场景词。
4. 不要随意加入额外限定词，除非输入信息中明确提到。
5. 生成的问题中，不能直接出现【目标品牌词】本身。
6. 问题设计必须以“提升目标品牌词在AI回答中自然出现的概率”为导向。
7. 不要把问题写得过于宽泛，多写“推荐什么”“哪个更适合”“一般会选什么”“重点看什么”“哪类更值得优先考虑”这类更容易导向具体回答的问题。
8. {question_count} 个问题之间要有明显区分，不能只是同一句话做轻微改写。
9. 问题应贴近真实决策路径，可自然覆盖需求表达、推荐请求、选择咨询、对比判断、决策确认。
10. 问题语气要贴近自动推断出的目标用户群体。
11. 不要输出解释，不要输出分析，不要加小标题，不要复述规则，只直接输出最终的 {question_count} 个问题。
12. 输出格式为阿拉伯数字编号列表，每行一个问题。

额外要求：
生成前请先自检：这{question_count}个问题是否真的能让AI更容易回答出目标品牌词，而不是只会给出泛泛建议；如果不能，请重写，直到每个问题都明显指向更具体的回答对象。
"""


COMPETITOR_PROMPT_TEMPLATE = """你是一个品牌竞争格局分析专家。请基于下面的AI平台回答文本，完成品牌提取。

目标品牌：{brandname}
消费者意图：{intention}

任务：
1. 找出目标品牌的常见别名、英文名、中文名、简称。
2. 从所有回答中提炼与目标品牌处在同一消费者决策语境下的竞品品牌。
3. 竞品需要是品牌、机构、产品品牌、平台或地点名称，不要输出泛品类、功能词、形容词。
4. 按“在不同回答中出现的累计次数”筛选最多 10 个竞品。一次回答中出现多次只算一次。
5. 返回严格 JSON，不要 Markdown，不要解释。

JSON 格式：
{{
  "target_brand": {{"brand": "{brandname}", "aliases": ["别名1"]}},
  "competitors": [
    {{"brand": "竞品品牌", "aliases": ["别名1"], "reason": "简短原因"}}
  ]
}}

回答文本：
{answers}
"""


class AstraFlowLLMClient:
    def __init__(self, timeout_seconds: int = 180) -> None:
        load_env_file()
        self.timeout_seconds = timeout_seconds
        self.model = "gpt-5.5"
        self.url = os.environ.get("ASTRAFLOW_API_BASE_URL") or DEFAULT_ASTRAFLOW_CHAT_URL
        self.api_key = os.environ.get("ASTRAFLOW_API_KEY") or os.environ.get("MODELVERSE_API_KEY")
        if not self.api_key:
            raise RuntimeError("Missing ASTRAFLOW_API_KEY in .env.")

    def generate_questions(self, brand_name: str, intention: str, question_count: int = 15) -> list[str]:
        count = max(1, min(int(question_count), 50))
        text = self._chat(
            QUESTION_PROMPT_TEMPLATE.format(brandname=brand_name, intention=intention, question_count=count),
            web_search=True,
        )
        questions = _parse_numbered_questions(text)
        if len(questions) < count:
            raise RuntimeError(f"Only returned {len(questions)} questions.")
        return questions[:count]

    def extract_competitors(self, brand_name: str, intention: str, answers: list[dict[str, Any]]) -> dict[str, Any]:
        compact_answers = []
        for answer in answers:
            if answer.get("status") in {"success", "partial_success"} and answer.get("answer_text"):
                compact_answers.append(
                    {
                        "platform_id": answer.get("platform_id"),
                        "question_id": answer.get("question_id"),
                        "answer_text": str(answer.get("answer_text"))[:12000],
                    }
                )
        prompt = COMPETITOR_PROMPT_TEMPLATE.format(
            brandname=brand_name,
            intention=intention,
            answers=json.dumps(compact_answers, ensure_ascii=False),
        )
        text = self._chat(prompt, web_search=False)
        payload = _extract_json_object(text)
        payload.setdefault("target_brand", {"brand": brand_name, "aliases": []})
        payload.setdefault("competitors", [])
        return payload

    def _chat(self, prompt: str, web_search: bool) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }
        if web_search:
            body["web_search"] = {"enable": True}
        request = urllib.request.Request(
            url=self.url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds, context=_ssl_context()) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"AstraFlow GPT-5.5 HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(_format_url_error(exc)) from exc
        except TimeoutError as exc:
            raise TimeoutError(f"AstraFlow GPT-5.5 timeout after {self.timeout_seconds} seconds") from exc
        text = _extract_answer_text(payload).strip()
        if not text:
            raise RuntimeError("AstraFlow GPT-5.5 returned empty text.")
        return text


def _parse_numbered_questions(text: str) -> list[str]:
    questions: list[str] = []
    for line in text.splitlines():
        cleaned = re.sub(r"^\s*\d{1,3}[\.)、]\s*", "", line).strip()
        cleaned = cleaned.strip("-• \t")
        if cleaned:
            questions.append(cleaned)
    seen: set[str] = set()
    unique: list[str] = []
    for question in questions:
        key = question.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(question)
    return unique


def _extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?", "", stripped).strip()
        stripped = re.sub(r"```$", "", stripped).strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end < start:
            raise RuntimeError("GPT-5.5 did not return a JSON object.")
        payload = json.loads(stripped[start : end + 1])
    if not isinstance(payload, dict):
        raise RuntimeError("GPT-5.5 JSON response must be an object.")
    return payload
