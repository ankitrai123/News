import pytest

from intelpod.llm.parsing import extract_json, strip_reasoning, unsupported_terms


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('Sure! ```json\n{"a": {"b": [1, 2]}}\n``` done') == {"a": {"b": [1, 2]}}
    assert extract_json('<think>maybe {"x": 0}</think>\nHere: {"a": "}{"} trailing') == {"a": "}{"}
    assert extract_json('noise {bad json} then {"ok": true}') == {"ok": True}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_strip_reasoning_handles_truncated_think():
    assert strip_reasoning("<think>long reasoning cut off") == ""
    assert strip_reasoning("<THINK>x</THINK> answer") == "answer"


def test_unsupported_terms_catches_invented_numbers_and_names():
    src = "RBI cuts repo rate by 25 basis points as inflation cools. The Reserve Bank of India lowered its key rate."
    assert unsupported_terms("Reserve Bank of India cuts repo rate by 25 basis points", src) == []
    assert unsupported_terms("RBI cuts rate by 50 basis points", src) == ["50"]
    assert unsupported_terms("RBI cuts rate; Governor Sharma warns of slowdown", src) == ["Governor", "Sharma"]
    assert unsupported_terms("India's central bank eases policy", src) == []


def test_lone_closing_think_and_special_tokens():
    assert strip_reasoning("reasoning text</think>{\"a\": 1}<|return|>") == '{"a": 1}'
    assert extract_json('analysis...</think> {"ok": true}<|end|>') == {"ok": True}


@pytest.mark.parametrize("generated,source", [
    ("Modi announces export curbs", "Commodities slump as exporters face curbs"),
    ("RBI hikes rates", "Turbine maker raises prices"),
    ("UN and EU condemn strike", "Reuters: talks under way after strike"),
    ("Putin backs Oman talks", "Computing firm and a woman hold talks"),
    ("Niger coup leaders meet", "Nigeria coup leaders meet"),
])
def test_names_must_match_whole_words(generated, source):
    assert unsupported_terms(generated, source), (generated, source)


def test_whole_word_forms_still_accepted():
    src = "Indians vote as India's opposition gains; China-made EVs face EU duties (Reuters)"
    assert unsupported_terms("Indian voters back opposition as India's EU duties on China-made EVs bite", src) == []
