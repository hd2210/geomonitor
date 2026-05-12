from __future__ import annotations

from copy import deepcopy
from typing import Any


def _generic_selectors() -> dict[str, str]:
    return {
        "new_chat": "a[href*='new_chat'] || a[href*='new'] || button[aria-label*='New'] || button[aria-label*='新建'] || button[title*='新建'] || text=New chat || text=新建会话 || text=新对话 || text=开启新对话",
        "input": "textarea || [contenteditable='true'] || .ql-editor",
        "submit": "button[type='submit'] || button[aria-label*='Send'] || button[aria-label*='发送'] || a[id*='send'] || .send-button-container",
        "answer_container": "main || body",
        "answer_item": "[data-message-author-role='assistant'] || .markdown || .answer || .message",
        "stop_generating": "button[aria-label*='Stop'] || button[aria-label*='停止'] || [data-auto-test='stop_response'] || .stopDealBtn",
        "login_indicator": "text=/登录|登陆|Sign in|Log in/i",
        "blocked_indicator": "text=/验证码|验证|风控|安全检查|安全审核|内容安全|风险提示|verify|Cloudflare/i",
    }


BROWSER_PLATFORM_TEMPLATES: dict[str, dict[str, Any]] = {
    "chatgpt": {
        "platform_id": "chatgpt",
        "platform_name": "ChatGPT",
        "url": "https://chatgpt.com",
        "method": "browser",
        "enabled": True,
        "new_chat_url": "https://chatgpt.com/",
        "selectors": {
            "new_chat": "[data-testid='create-new-chat-button'] || a[aria-label*='New chat'] || button[aria-label*='New chat']",
            "input": "#prompt-textarea || textarea || [contenteditable='true']",
            "submit": "[data-testid='send-button'] || button[aria-label*='Send'] || button[type='submit']",
            "answer_container": "main",
            "answer_item": "[data-message-author-role='assistant']",
            "stop_generating": "[data-testid='stop-button'] || button[aria-label*='Stop']",
            "login_indicator": "text=/Log in|Sign up|登录|注册/i",
            "blocked_indicator": "text=/verify you are human|请验证您是真人|Cloudflare/i",
        },
    },
    "gemini": {
        "platform_id": "gemini",
        "platform_name": "Gemini",
        "url": "https://gemini.google.com/app",
        "method": "browser",
        "enabled": True,
        "selectors": {
            "new_chat": "button[aria-label*='New chat'] || a[aria-label*='New chat'] || button[aria-label*='新建'] || text=New chat || text=新聊天",
            "input": "rich-textarea div[contenteditable='true'] || div[contenteditable='true'] || textarea",
            "submit": "button[aria-label*='Send'] || button[aria-label*='发送'] || button.send-button",
            "answer_container": "main",
            "answer_item": "message-content || .model-response-text || [data-response-index]",
            "stop_generating": "button[aria-label*='Stop'] || button[aria-label*='停止']",
            "login_indicator": "text=/Sign in|登录/i",
            "blocked_indicator": "text=/unusual traffic|verify|验证码|安全检查/i",
        },
    },
    "deepseek": {
        "platform_id": "deepseek",
        "platform_name": "DeepSeek",
        "url": "https://chat.deepseek.com",
        "method": "browser",
        "enabled": False,
        "selectors": {
            **_generic_selectors(),
            "new_chat": "a[href='/'] || button[aria-label*='New'] || button[aria-label*='新建'] || text=新对话",
            "input": "textarea || [contenteditable='true']",
            "submit": "button[type='submit'] || button[aria-label*='发送'] || button[aria-label*='Send']",
            "answer_item": ".ds-markdown || .markdown || [class*='markdown'] || [data-message-author-role='assistant']",
            "stop_generating": "button[aria-label*='停止'] || button[aria-label*='Stop'] || [class*='stop']",
        },
    },
    "doubao": {
        "platform_id": "doubao",
        "platform_name": "豆包",
        "url": "https://www.doubao.com/chat/",
        "method": "browser",
        "enabled": False,
        "selectors": {
            **_generic_selectors(),
            "answer_item": "[data-testid*='message'] || [class*='message'] || [class*='answer'] || [class*='markdown']",
            "blocked_indicator": "text=/安全审核|内容安全|安全验证|风险提示|验证码|风控/i",
        },
    },
    "yuanbao": {
        "platform_id": "yuanbao",
        "platform_name": "腾讯元宝",
        "url": "https://yuanbao.tencent.com/",
        "method": "browser",
        "enabled": False,
        "new_chat_url": "https://yuanbao.tencent.com/",
        "selectors": {
            **_generic_selectors(),
            "input": "#search-bar .ql-editor || .ql-editor[contenteditable='true'] || [contenteditable='true'] || textarea",
            "submit": "#yuanbao-send-btn || a[id*='send'] || button[aria-label*='发送']",
            "answer_item": ".agent-chat__conv--ai || [class*='conv--ai'] || #answer_text_id || [class*='markdown']",
            "stop_generating": "[data-auto-test='stop_response'] || .stopDealBtn || [class*='stop']",
        },
    },
    "tongyi": {
        "platform_id": "tongyi",
        "platform_name": "通义千问",
        "url": "https://tongyi.aliyun.com/qianwen/",
        "method": "browser",
        "enabled": False,
        "selectors": {
            **_generic_selectors(),
            "input": "[contenteditable='true'] || textarea",
            "submit": "button[aria-label='发送消息'] || button[aria-label*='发送'] || button[type='submit']",
            "answer_item": ".markdown || [class*='markdown'] || [class*='response']",
            "stop_generating": "button[aria-label*='停止'] || [class*='stop']",
        },
    },
    "kimi": {
        "platform_id": "kimi",
        "platform_name": "Kimi",
        "url": "https://www.kimi.com/",
        "method": "browser",
        "enabled": False,
        "new_chat_url": "https://www.kimi.com/?chat_enter_method=new_chat",
        "selectors": {
            **_generic_selectors(),
            "new_chat": "a.new-chat-btn[href*='new_chat'] || a[href*='chat_enter_method=new_chat'] || text=新建会话",
            "input": ".chat-input-editor[contenteditable='true'] || [contenteditable='true'] || textarea",
            "submit": ".send-button-container:not(.disabled) || button[aria-label*='发送'] || button[aria-label*='Send']",
            "answer_item": ".markdown || [class*='markdown'] || [class*='message-content']",
            "stop_generating": "[class*='stop'] || button[aria-label*='停止'] || button[aria-label*='Stop']",
        },
    },
    "wenxin": {
        "platform_id": "wenxin",
        "platform_name": "文心一言",
        "url": "https://yiyan.baidu.com/",
        "method": "browser",
        "enabled": False,
        "selectors": {
            **_generic_selectors(),
            "input": "[data-slate-editor='true'][contenteditable='true'] || [contenteditable='true'] || textarea",
            "submit": ".send__slzHSuja || button[aria-label*='发送'] || button[type='submit']",
            "answer_item": "#answer_text_id || .md-stream || [class*='answer']",
            "stop_generating": "[data-auto-test='stop_response'] || .stopDealBtn || [class*='stop']",
        },
    },
}


def browser_platform_defaults() -> list[dict[str, Any]]:
    return [deepcopy(item) for item in BROWSER_PLATFORM_TEMPLATES.values()]


def merge_browser_template(item: dict[str, Any]) -> dict[str, Any]:
    platform_id = str(item.get("platform_id", "")).strip()
    template = deepcopy(BROWSER_PLATFORM_TEMPLATES.get(platform_id, {}))
    selectors = {**template.get("selectors", {}), **(item.get("selectors") or {})}
    merged = {**template, **item}
    merged["method"] = "browser"
    merged["selectors"] = selectors
    return merged
