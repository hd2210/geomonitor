from geomonitor.keyword_analyzer import KeywordAnalyzer
from geomonitor.models import TargetKeyword


def test_explicit_list_ranking_uses_list_order():
    analyzer = KeywordAnalyzer(
        [
            TargetKeyword("deli", ("Deli Tools", "得力")),
            TargetKeyword("Bosch"),
            TargetKeyword("Makita"),
            TargetKeyword("Stanley"),
        ]
    )
    text = "Recommended brands:\n1. Bosch\n2. Makita\n3. Deli Tools\n4. Stanley\nDeli appears again."

    result = analyzer.analyze("run", "chatgpt", "Q001", text)
    ranks = {item.keyword: item.rank for item in result.keyword_analysis}

    assert ranks == {"deli": 3, "Bosch": 1, "Makita": 2, "Stanley": 4}


def test_first_position_ranking_when_no_list():
    analyzer = KeywordAnalyzer([TargetKeyword("deli", ("得力",)), TargetKeyword("Bosch"), TargetKeyword("Makita")])
    text = "Makita is common. Deli is affordable. Bosch is premium."

    result = analyzer.analyze("run", "chatgpt", "Q001", text)
    matches = {item.keyword: item for item in result.keyword_analysis}

    assert matches["Makita"].rank == 1
    assert matches["deli"].rank == 2
    assert matches["Bosch"].rank == 3
    assert matches["deli"].appeared is True
    assert matches["deli"].matched_alias == "deli"


def test_missing_keyword_has_null_rank():
    analyzer = KeywordAnalyzer([TargetKeyword("deli"), TargetKeyword("Bosch")])

    result = analyzer.analyze("run", "chatgpt", "Q001", "Bosch only.")
    matches = {item.keyword: item for item in result.keyword_analysis}

    assert matches["Bosch"].appeared is True
    assert matches["deli"].appeared is False
    assert matches["deli"].rank is None
    assert matches["deli"].first_position is None


def test_keyword_does_not_match_inside_larger_word():
    analyzer = KeywordAnalyzer([TargetKeyword("deli"), TargetKeyword("Makita")])

    result = analyzer.analyze("run", "gemini", "Q001", "Makita is listed. This paragraph mentions remodeling only.")
    matches = {item.keyword: item for item in result.keyword_analysis}

    assert matches["Makita"].appeared is True
    assert matches["deli"].appeared is False
    assert matches["deli"].rank is None


def test_alias_phrase_uses_brand_boundaries():
    analyzer = KeywordAnalyzer([TargetKeyword("deli", ("Deli Tools", "得力"))])

    phrase = analyzer.analyze("run", "gemini", "Q001", "Recommended: Deli Tools.")
    chinese = analyzer.analyze("run", "gemini", "Q001", "推荐品牌：得力。")
    embedded = analyzer.analyze("run", "gemini", "Q001", "Recommended: SuperDeli Toolshelf.")

    assert phrase.keyword_analysis[0].appeared is True
    assert chinese.keyword_analysis[0].appeared is True
    assert embedded.keyword_analysis[0].appeared is False
