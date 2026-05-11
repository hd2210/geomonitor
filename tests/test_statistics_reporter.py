from geomonitor.models import AIPlatform, AnswerRecord, KeywordAnalysisRecord, KeywordMatch, TargetKeyword
from geomonitor.statistics_reporter import StatisticsReporter


def test_platform_and_global_summary_count_successes_only():
    answers = [
        AnswerRecord("run", "ts", "chatgpt", "ChatGPT", "Q001", "q", "a", status="success"),
        AnswerRecord("run", "ts", "chatgpt", "ChatGPT", "Q002", "q", None, status="failed"),
        AnswerRecord("run", "ts", "gemini", "Gemini", "Q001", "q", "a", status="success"),
    ]
    analyses = [
        KeywordAnalysisRecord("run", "chatgpt", "Q001", (KeywordMatch("deli", True, 2, 10, "deli"),)),
        KeywordAnalysisRecord("run", "gemini", "Q001", (KeywordMatch("deli", False, None, None, None),)),
    ]
    reporter = StatisticsReporter("run", [AIPlatform("chatgpt", "ChatGPT", "https://x"), AIPlatform("gemini", "Gemini", "https://y")], [TargetKeyword("deli")])

    platform_rows = reporter.build_platform_summary(answers, analyses)
    global_rows = reporter.build_global_summary(platform_rows)

    chatgpt = next(row for row in platform_rows if row.platform_id == "chatgpt")
    gemini = next(row for row in platform_rows if row.platform_id == "gemini")
    assert chatgpt.total_questions == 1
    assert chatgpt.appeared_count == 1
    assert chatgpt.appearance_rate == 1.0
    assert gemini.total_questions == 1
    assert gemini.appeared_count == 0
    assert global_rows[0].total_answers == 2
    assert global_rows[0].appeared_count == 1
