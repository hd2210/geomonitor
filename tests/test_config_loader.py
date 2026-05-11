from geomonitor.config_loader import parse_config


def test_parse_api_platform_config():
    config = parse_config(
        {
            "questions": [{"question_id": "Q001", "question": "Which brands?"}],
            "target_keywords": ["deli"],
            "ai_platforms": [
                {
                    "platform_id": "astraflow",
                    "platform_name": "AstraFlow",
                    "method": "api",
                    "model": "gpt-5.1-chat",
                    "api_base_url": "https://api.modelverse.cn/v1/chat/completions",
                    "web_search": True,
                }
            ],
            "output_dir": "./runs/{run_id}",
        }
    )

    platform = config.ai_platforms[0]
    assert platform.method == "api"
    assert platform.model == "gpt-5.1-chat"
    assert platform.web_search is True
    assert platform.web_search_vendor is None
