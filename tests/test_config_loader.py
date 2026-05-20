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


def test_parse_browser_accounts():
    config = parse_config(
        {
            "run_mode": "browser",
            "questions": [{"question_id": "Q001", "question": "Which brands?"}],
            "target_keywords": ["deli"],
            "browser_platforms": [
                {
                    "platform_id": "doubao",
                    "platform_name": "豆包",
                    "url": "https://www.doubao.com/chat/",
                    "method": "browser",
                    "enabled": True,
                    "browser_mode": "cdp",
                    "accounts": [
                        {
                            "account_id": "doubao_a",
                            "account_name": "豆包账号A",
                            "cdp_url": "http://127.0.0.1:9222",
                            "chrome_user_data_dir": "./data/cdp-profiles/doubao/a",
                        }
                    ],
                }
            ],
            "output_dir": "./runs/{run_id}",
        }
    )

    platform = config.browser_platforms[0]
    assert platform.browser_accounts[0].account_id == "doubao_a"
    assert platform.browser_accounts[0].account_name == "豆包账号A"
    assert platform.browser_accounts[0].cdp_url == "http://127.0.0.1:9222"
