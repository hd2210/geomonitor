# AI Platform Keyword Visibility Monitor

一个用于监测目标关键词在不同 AI 平台回答中可见度、出现率和排名的串行 Agent。

## 技术方案

### 目标

给定问题列表、关键词/别名列表、AI 平台配置和执行频率，系统按计划逐个平台、逐个问题提问，保存完整回答文本、页面截图、HTML 和结构化元数据，然后生成关键词排名分析、平台级统计、全平台统计和 Markdown 报告。

### 模块划分

```text
geomonitor/
  config_loader.py         # 读取和校验配置
  ai_platform_runner.py    # 浏览器自动化，提问、等待、提取文本、截图
  answer_storage.py        # run 目录、JSONL、HTML、截图、元数据落盘
  keyword_analyzer.py      # 关键词/别名匹配、列表排名、首次出现排名
  statistics_reporter.py   # CSV 汇总和 report.md
  scheduler.py             # interval / daily / weekly / cron 调度
  models.py                # 数据结构
  cli.py                   # 命令行入口
configs/
  sample_config.json       # 示例配置
tests/
  test_keyword_analyzer.py
  test_statistics_reporter.py
```

### 关键设计

1. **串行执行与节流**
   每个平台、每个问题默认串行执行。问题之间按配置随机等待，例如 10-30 秒，降低触发风控的概率。

2. **浏览器会话**
   使用 Playwright persistent context。首次运行如遇登录，用户可以手动登录；后续可复用 `browser_profile_dir`。系统不会绕过验证码、登录风控或平台限制，只记录 `login_required`、`blocked`、`failed` 等状态。

3. **平台适配**
   每个平台支持配置输入框、提交按钮、回答容器、完成信号、登录提示、验证码提示等 CSS selector。没有配置时使用通用兜底选择器，但生产使用建议为每个平台维护选择器。

4. **排名规则**
   优先识别明确列表/表格中的目标关键词顺序，例如 `1. Bosch`、`- Makita`、Markdown 表格行等。若没有可解释的列表排名，则按正文首次出现位置排序。

5. **完整落盘**
   每条回答生成一条 `raw_answers.jsonl` 记录。成功或部分成功时保存文本、HTML、截图路径；失败也写记录，不静默跳过。

6. **关键词边界匹配**
   英文/数字品牌名和 alias 必须作为独立词或短语出现才算命中。例如 `Deli` 会命中，`Deli Tools` 会命中，但 `remodeling` 里的 `deli` 不会命中。中文 alias 这类无空格文本按精确子串匹配。

## 数据结构

### 输入配置

```json
{
  "questions": [
    {"question_id": "Q001", "question": "What are the best tool brands for home repair?"}
  ],
  "target_keywords": [
    {"keyword": "deli", "aliases": ["Deli", "DELI", "Deli Tools", "得力"]},
    "Bosch"
  ],
  "ai_platforms": [
    {
      "platform_id": "chatgpt",
      "platform_name": "ChatGPT",
      "url": "https://chatgpt.com",
      "method": "browser",
      "selectors": {
        "input": "textarea, [contenteditable='true']",
        "submit": "button[type='submit']",
        "answer_container": "main",
        "login_indicator": "text=Log in",
        "blocked_indicator": "text=verify you are human"
      }
    }
  ],
  "schedule": {"type": "interval", "every_seconds": 21600},
  "output_dir": "./data/ai_visibility_monitor/runs/{run_id}",
  "runner": {
    "timeout_seconds": 120,
    "min_delay_seconds": 10,
    "max_delay_seconds": 30,
    "headless": false,
    "browser_profile_dir": "./data/browser-profiles",
    "browser_channel": null,
    "browser_executable_path": null,
    "browser_cdp_url": null
  }
}
```

### 单条回答记录

```json
{
  "run_id": "20260509_090000",
  "timestamp": "2026-05-09T09:00:00+08:00",
  "platform_id": "chatgpt",
  "platform_name": "ChatGPT",
  "question_id": "Q001",
  "question": "What are the best tool brands for home repair?",
  "answer_text": "完整 AI 回答文本",
  "screenshot_path": "screenshots/chatgpt_Q001.png",
  "raw_html_path": "html/chatgpt_Q001.html",
  "status": "success",
  "error_message": null
}
```

### 输出文件

每次执行生成独立 run 目录：

```text
{output_dir}/
  raw_answers.jsonl
  keyword_analysis.jsonl
  platform_summary.csv
  global_summary.csv
  report.md
  screenshots/
  html/
```

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
```

如果 Playwright 浏览器下载受网络影响失败，可以在配置中使用本机 Chrome：

```json
"browser_executable_path": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
```

### 使用已登录的 Chrome

如果你已经在 Chrome 里登录了 ChatGPT/Gemini，可以启动一个带远程调试端口的 Chrome，然后让 Agent 连接它。先关闭普通 Chrome，再运行：

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/Library/Application Support/Google/Chrome"
```

如果 Chrome 提示 profile 正在使用，推荐复制一个专用监测 profile：

```bash
cp -R "$HOME/Library/Application Support/Google/Chrome" "$HOME/chrome-geomonitor-profile"
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/chrome-geomonitor-profile"
```

在这个 Chrome 窗口里手动确认 ChatGPT/Gemini 已登录，然后把配置改成：

```json
"browser_cdp_url": "http://127.0.0.1:9222"
```

这种方式不会绕过验证码或风控，只是复用你手动登录后的浏览器会话。

## 使用

单次执行：

```bash
python3 -m geomonitor.cli run --config configs/sample_config.json
```

按配置调度执行：

```bash
python3 -m geomonitor.cli schedule --config configs/sample_config.json
```

查看结果 Dashboard：

```bash
python3 -m geomonitor.cli serve --config configs/sample_config.json --runs-dir ./data/ai_visibility_monitor/runs --port 8765
```

然后打开：

```text
http://127.0.0.1:8765
```

Dashboard 提供三个一级入口：

- `结果查看`：查看每次 run 的统计、问题、关键词和回答详情。
- `配置管理`：用长文本批量编辑问题列表，每行自动识别为一个问题并生成 `Q001/Q002/...`；也可以编辑并保存 `target_keywords`。
- `运行任务`：用当前配置立即触发一次监测，并查看运行日志。

只分析已有 `raw_answers.jsonl`：

```bash
python3 -m geomonitor.cli analyze --config configs/sample_config.json --run-dir ./data/ai_visibility_monitor/runs/20260509_090000
```

## 验收覆盖

- 每个平台、每个问题都会写入执行记录。
- 成功回答保存完整文本、截图和 HTML。
- 失败、超时、登录、验证码和空回答均写入状态。
- 每条回答写入关键词分析。
- 平台级和全平台 CSV 汇总均生成。
- `report.md` 包含概览、平台、关键词表现、排名和失败列表。
