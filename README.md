# AI Visibility Monitor

一个面向品牌 AI 可见度监测的本地 Web 应用与采集 Agent。用户输入目标品牌名和消费者意图后，系统会先生成一组真实消费者问题，再到已启用的 AI 平台逐一查询，保存完整回答、截图或 API 原始响应，并自动提取竞品品牌，生成目标品牌与竞品在不同 AI 平台中的提及率和推荐排名分析。

## 当前版本能力

- 用户登录：手机号短信验证码登录，本地开发模式验证码固定为 `123456`，首次登录需要填写公司名称。
- 创建 AI 监测：输入目标品牌名和消费者意图，系统按后台配置生成消费者问题，默认 15 个，用户可逐条编辑确认。
- 平台选择：普通用户可从管理后台启用的 AI 网站中选择本次要监测的平台，至少选择一个。
- 浏览器采集：使用 Playwright Chromium 打开 AI 网站，每个平台每个问题新开一轮对话，保存完整回答文本、HTML、页面截图和引用信源。
- API 采集：保留 AstraFlow / ModelVerse 文本生成接口模式，可开启 `web_search` 能力。
- 竞品提取：所有回答采集完成后，调用 `gpt-5.5` 提取目标品牌别名和累计出现最多的竞品品牌，并纳入关键词统计。
- 引用信源统计：抓取每条回答中的来源网页，按平台、媒体网站和网页 URL 统计引用次数。
- 关键词统计：支持品牌名和 alias 独立词/短语匹配，避免 `deli` 被 `remodeling` 误命中；同一回答多次出现只计一次。
- 排名统计：优先按显式推荐列表/表格排序；没有明确列表时按首次出现位置排序。
- 结果查看：结果页展示目标品牌提及率、竞品提及率、平均排名、各平台表现、问题列表、引用信源、回答详情和截图入口。
- 重试机制：支持批量重试失败请求，也支持在回答详情中对 `failed` / `partial_success` 单条重试；重试后重新提取竞品和统计。
- 用户配额：普通用户右上角显示可用监控次数；管理员可在后台调整每个用户可创建监测总次数。
- 系统管理：`/admin` 页面使用管理密码进入，配置查询方式、生成问题数量、启用 AI 网站、引用信源标识、API 模型、并发数量和用户配额。

## 目录结构

```text
geomonitor/
  config_loader.py         # 读取和校验配置
  ai_platform_runner.py    # Playwright 浏览器采集、登录准备、截图和 HTML 保存
  astraflow_runner.py      # AstraFlow / ModelVerse API 查询
  llm_client.py            # GPT-5.5 问题生成和竞品提取
  monitor_service.py       # 用户监测任务后台执行、并发、失败重试、最终统计
  answer_storage.py        # run 目录、JSONL、截图、HTML、API 原始响应落盘
  keyword_analyzer.py      # 关键词/别名边界匹配、列表排名、首次出现排名
  statistics_reporter.py   # CSV 汇总和 report.md
  user_store.py            # SQLite 用户、短信码、会话、配额和监测任务存储
  scheduler.py             # interval / daily / weekly / cron 调度
  web_server.py            # 本地 Web 应用与 API
  platform_templates.py    # 默认 AI 网站配置
  models.py                # 数据结构
  cli.py                   # 命令行入口
configs/
  sample_config.json       # 示例配置
data/
  app.sqlite3              # 本地用户和监测任务数据库
  browser-profiles/        # Playwright 持久化登录状态
  ai_visibility_monitor/
    runs/                  # 每次监测输出
```

## 环境变量

复制示例文件：

```bash
cp .env.example .env
```

必填：

```text
ASTRAFLOW_API_KEY=your_api_key_here
```

常用可选项：

```text
ASTRAFLOW_API_BASE_URL=https://api.modelverse.cn/v1/chat/completions
ADMIN_PASSWORD=yunzhigeo
```

HTTPS 企业代理环境可选：

```text
ASTRAFLOW_CA_BUNDLE=/path/to/company-ca-bundle.pem
ASTRAFLOW_VERIFY_SSL=false
```

`ADMIN_PASSWORD` 未配置时默认使用 `yunzhigeo`。AstraFlow 的 `gpt-5.5` 用于生成问题和提取竞品，但普通用户界面不会展示具体模型名。

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
```

如果系统里没有 `python` 或 `pip` 命令，请使用 `python3` 和 `python3 -m pip`。

## 启动 Web 应用

```bash
python3 -m geomonitor.cli serve \
  --config configs/sample_config.json \
  --runs-dir ./data/ai_visibility_monitor/runs \
  --host 127.0.0.1 \
  --port 8765
```

打开：

```text
http://127.0.0.1:8765/
```

管理后台：

```text
http://127.0.0.1:8765/admin
```

## 使用流程

### 1. 管理员配置平台

进入 `/admin`，输入管理密码后可配置：

- 查询方式：`浏览器模式` 或 `接口模式`。一次监测内所有请求使用同一种方式。
- AI 网站：配置网站 ID、名称、访问地址和是否启用。
- 引用信源标识：每个 AI 网站可配置多行引用入口关键词，例如 `引用`、`来源`、`参考`、`篇资料`；采集时会同时判断关键词命中和元素是否可点击。
- 登录准备：浏览器模式下点击某个平台的“准备登录”，在打开的 Playwright Chromium 中手动登录。登录状态会保存到 `data/browser-profiles/`。
- API 模型：接口模式下配置平台 ID、显示名称、模型 ID、API Base URL 和是否启用。
- 并发数量：浏览器模式按平台并发、同一平台内问题串行；API 模式按请求并发。
- 用户管理：查看所有注册用户、手机号、公司名称、注册时间、最近登录时间、监测任务列表，并修改用户可创建监测总次数。

默认浏览器平台包括 ChatGPT、Gemini、DeepSeek、豆包、腾讯元宝、通义千问、Kimi、文心一言。ChatGPT / Gemini 可能因为浏览器自动化环境触发登录限制；国内 AI 平台通常可先使用 Playwright Chromium 登录态采集。

### 2. 用户登录

普通用户访问首页，输入公司名称、手机号并获取验证码。当前本地开发验证码固定为：

```text
123456
```

每个手机号默认可创建 3 次监测任务，管理员可在 `/admin` 用户管理中调整总次数。

### 3. 创建 AI 监测

用户输入：

- 目标品牌名：最多 20 个字，允许空格。
- 消费者意图：最多 50 个字。

系统会按后台配置生成消费者问题，默认 15 个，并展示给用户确认。用户可以编辑单个问题；确认后开始监测。

### 4. 查询与分析

后台任务会：

1. 按用户选择的平台和确认后的问题执行查询。
2. 保存每条回答的全文、状态、错误原因、截图/HTML 或 API 原始响应。
3. 浏览器模式下，在截图完成后点击引用入口，抓取引用网页标题、网站名称和链接。
4. 完成后把全部回答发给 `gpt-5.5`，提取目标品牌别名和最多 10 个竞品品牌。
5. 将目标品牌和竞品一起纳入关键词分析。
6. 生成平台级统计、全局统计、引用信源统计和 Markdown 报告。

用户可以离开页面，任务会在后台继续执行。当前短信通知为本地模拟信息，写入监测任务的 `notification_message`。

### 5. 查看结果与重试

结果页包含：

- 监测列表：品牌、意图、状态、创建时间、完成时间。
- 详情概览：目标品牌与竞品的提及率、平均排名、最佳排名。
- 各平台表现：按平台查看每个品牌的出现次数、出现率和平均排名。
- 问题列表：按平台查看每个问题的成功情况。
- 引用信源：按全部平台或单个平台筛选，先展示媒体网站引用次数；点击媒体后展示该网站下被引用网页，网页按引用次数降序排列，点击可打开原网页。
- 回答详情：按平台和问题筛选回答，可打开截图原图；`failed` 和 `partial_success` 回答支持单条重试。
- 失败重试：如果本次监测有失败请求，详情页会显示“重试失败请求”按钮。点击后只重跑失败的平台/问题组合；API 模式按请求并发，浏览器模式按平台并发、同一平台内问题串行；重试完成后重新提取竞品、引用信源和统计。

## 配置说明

`configs/sample_config.json` 支持两类平台配置：

```json
{
  "run_mode": "browser",
  "browser_platforms": [
    {
      "platform_id": "deepseek",
      "platform_name": "DeepSeek",
      "url": "https://chat.deepseek.com",
      "method": "browser",
      "enabled": true,
      "citation_triggers": ["个网页", "来源", "source"]
    }
  ],
  "api_platforms": [
    {
      "platform_id": "chatgpt_api",
      "platform_name": "ChatGPT API",
      "method": "api",
      "enabled": true,
      "model": "gpt-5.1-chat",
      "web_search": true,
      "api_base_url": "https://api.modelverse.cn/v1/chat/completions"
    }
  ],
  "runner": {
    "timeout_seconds": 120,
    "min_delay_seconds": 2,
    "max_delay_seconds": 5,
    "headless": false,
    "pause_on_blocked_seconds": 20,
    "browser_concurrency": 5,
    "api_concurrency": 5,
    "question_count": 15
  }
}
```

说明：

- `run_mode=browser` 时只使用启用的 `browser_platforms`。
- `run_mode=api` 时只使用启用的 `api_platforms`。
- `runner.question_count` 控制用户输入品牌和意图后生成的问题数量，默认 15，后台页面可修改，最大 50。
- `citation_triggers` 是浏览器模式下的引用入口标识，支持多个关键词。管理员也可以在 `/admin` 页面逐个平台维护。
- `browser_concurrency` 控制同时运行的平台数量。
- `api_concurrency` 控制接口请求并发数量。
- 失败重试会沿用同一套并发配置：浏览器模式使用 `browser_concurrency`，API 模式使用 `api_concurrency`。
- `pause_on_blocked_seconds` 控制疑似风控/无法输入时等待观察的秒数。
- 浏览器模式会自动保存截图、HTML 和引用信源；API 模式不提供截图和引用信源，但会保存 API 原始响应。

## 输出文件

每次监测生成独立 run 目录：

```text
data/ai_visibility_monitor/runs/{run_id}/
  raw_answers.jsonl
  keyword_analysis.jsonl
  platform_summary.csv
  global_summary.csv
  citation_summary.csv
  citation_pages.csv
  report.md
  screenshots/
  html/
  api_responses/
```

核心文件：

- `raw_answers.jsonl`：每个平台、每个问题的原始回答记录，成功和失败都会记录。
- `keyword_analysis.jsonl`：每条回答的关键词出现判断、首次位置和排名。
- `platform_summary.csv`：平台级关键词出现率、平均排名、最佳排名。
- `global_summary.csv`：全平台关键词出现率、平均排名、最佳排名。
- `citation_summary.csv`：按平台和媒体网站统计引用次数。
- `citation_pages.csv`：按平台、媒体网站和 URL 统计被引用网页次数。
- `report.md`：人类可读的 Markdown 报告。

## 命令行用法

单次执行配置中的问题和关键词：

```bash
python3 -m geomonitor.cli run --config configs/sample_config.json
```

按配置调度执行：

```bash
python3 -m geomonitor.cli schedule --config configs/sample_config.json
```

准备某个平台的浏览器登录状态：

```bash
python3 -m geomonitor.cli login --config configs/sample_config.json --platform-id deepseek
```

只分析已有 `raw_answers.jsonl`：

```bash
python3 -m geomonitor.cli analyze \
  --config configs/sample_config.json \
  --run-dir ./data/ai_visibility_monitor/runs/20260509_090000
```

## 异常处理

- 平台无法访问、登录失效、验证码、风控、回答超时、空回答都会写入 `raw_answers.jsonl`。
- 浏览器模式下若找不到输入框，会先尝试新建对话；仍找不到则记录为 blocked 或 failed。
- 如果有输入框但存在弹窗，会尝试关闭弹窗；无法关闭则记录 blocked。
- 截图失败但文本保存成功时，记录为 `partial_success` 并写入 `screenshot_error`。
- 引用信源抓取失败但正文已成功时，记录为 `partial_success` 并写入 `citation_error`。
- 批量重试只针对失败请求；单条重试可针对 `failed` 和 `partial_success` 回答，重试会重新提问、截图、抓取引用信源，并重算竞品和统计。

## 开发验证

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. pytest -q
node --check geomonitor/web/app.js
```
