# AI Platform Keyword Visibility Monitor

一个用于监测目标关键词在 AI 回答中可见度、出现率和排名的 Agent。当前默认采集方式为 AstraFlow / ModelVerse 文本生成 API，并开启 `web_search` 能力。

## 技术方案

### 目标

给定问题列表、关键词/别名列表、API 模型配置和执行频率，系统按计划逐个平台/模型、逐个问题调用文本生成接口，保存完整回答文本和 API 原始响应，然后生成关键词排名分析、平台级统计、全平台统计和 Markdown 报告。

### 模块划分

```text
geomonitor/
  config_loader.py         # 读取和校验配置
  astraflow_runner.py      # AstraFlow 文本生成 API 调用
  ai_platform_runner.py    # 兼容保留的浏览器自动化 runner
  answer_storage.py        # run 目录、JSONL、API 原始响应落盘
  keyword_analyzer.py      # 关键词/别名边界匹配、列表排名、首次出现排名
  statistics_reporter.py   # CSV 汇总和 report.md
  scheduler.py             # interval / daily / weekly / cron 调度
  web_server.py            # 本地 Dashboard 和配置/运行 API
  models.py                # 数据结构
  cli.py                   # 命令行入口
configs/
  sample_config.json       # 示例配置
```

### 关键设计

1. **API 串行执行与节流**
   每个平台/模型、每个问题默认串行执行。问题之间按配置随机等待，例如 2-5 秒。

2. **AstraFlow API**
   API key 从 `.env` 读取。默认请求 `https://api.modelverse.cn/v1/chat/completions`，也可以在配置中为平台单独设置 `api_base_url`。

3. **Web Search**
   API 请求中默认加入：

   ```json
  {
    "web_search": {
      "enable": true
    }
  }
  ```

4. **排名规则**
   优先识别明确列表/表格中的目标关键词顺序，例如 `1. Bosch`、`- Makita`、Markdown 表格行等。若没有可解释的列表排名，则按正文首次出现位置排序。

5. **关键词边界匹配**
   英文/数字品牌名和 alias 必须作为独立词或短语出现才算命中。例如 `Deli` 会命中，`Deli Tools` 会命中，但 `remodeling` 里的 `deli` 不会命中。中文 alias 这类无空格文本按精确子串匹配。

6. **完整落盘**
   每条回答生成一条 `raw_answers.jsonl` 记录。成功或失败都写记录。成功时保存 `api_responses/{platform_id}_{question_id}.json`。

## 环境变量

创建 `.env`：

```bash
cp .env.example .env
```

然后填入：

```text
ASTRAFLOW_API_KEY=your_api_key_here
```

可选：

```text
ASTRAFLOW_API_BASE_URL=https://api.modelverse.cn/v1/chat/completions
```

## 配置示例

```json
{
  "ai_platforms": [
    {
      "platform_id": "astraflow",
      "platform_name": "AstraFlow Web Search",
      "method": "api",
      "model": "gpt-5.1-chat",
      "api_base_url": "https://api.modelverse.cn/v1/chat/completions",
      "web_search": true
    }
  ]
}
```

## 输出文件

每次执行生成独立 run 目录：

```text
{output_dir}/
  raw_answers.jsonl
  keyword_analysis.jsonl
  platform_summary.csv
  global_summary.csv
  report.md
  api_responses/
```

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

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
- `配置管理`：配置多个 API 模型，支持 ChatGPT/GPT、Gemini、豆包、DeepSeek、Kimi、Qwen 等常用预设，也可输入自定义 model id；用长文本批量编辑问题列表，每行自动识别为一个问题并生成 `Q001/Q002/...`；也可以编辑并保存 `target_keywords`。
- `运行任务`：用当前配置立即触发一次监测，并查看运行日志。

只分析已有 `raw_answers.jsonl`：

```bash
python3 -m geomonitor.cli analyze --config configs/sample_config.json --run-dir ./data/ai_visibility_monitor/runs/20260509_090000
```
