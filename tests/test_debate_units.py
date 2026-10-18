"""Phase 3 units: claim map, calibration, registry, diversity, model-output validation, planning."""
from datetime import datetime, timedelta, timezone

import pytest

from intelpod.config import PROJECT_ROOT
from intelpod.debate import analysis
from intelpod.debate.claims import build_claims, classify_claim, speaker_of, stakeholder_of
from intelpod.debate.diversity import diversity_report
from intelpod.debate.models import SourceProfile
from intelpod.debate.registry import Registry, country_code, economy
from intelpod.debate.service import (no_disagreement_note, overall_confidence, plan_searches, sort_claims)
from intelpod.research.models import EventBrief, EvidenceNote, ResearchResult
from intelpod.research.providers import gdelt_query_problems

NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


def res(rid, publisher, title, extracts=(), claims=(), content_type="reporting", source_type="news",
        domain=None, country=None):
    return ResearchResult(result_id=rid, event_id="e1", title=title, url=f"https://{domain or rid + '.test'}/a",
                          canonical_url=f"https://{domain or rid + '.test'}/a", publisher=publisher,
                          domain=domain or f"{rid}.test", content_type=content_type, source_type=source_type,
                          evidence_notes=[EvidenceNote(text=e) for e in extracts], key_claims=list(claims),
                          source_country=country, selected=True, relevance_score=0.5, retrieved_at=NOW)


def brief(**kw):
    base = dict(event_id="e1", run_id="r1", title="RBI cuts repo rate to 5.25%", topic="markets_monetary",
                countries=["India"], entities=["Reserve Bank of India"],
                centroid={"repo": 1.0, "rate": 0.8, "rbi": 0.9}, first_article_at=NOW - timedelta(hours=6))
    base.update(kw)
    return EventBrief(**base)


# -- claims ---------------------------------------------------------------------------------
@pytest.mark.parametrize("text,ctype,kind", [
    ("The central bank cut its policy rate to 5.25% on Friday.", "reporting", "factual"),
    ("Inflation will ease to 4% next year, the governor said.", "reporting", "forecast"),
    ("The finance minister said the cut would support lending.", "reporting", "forecast"),
    ("The governor said the economy remained resilient.", "reporting", "attributed"),
    ("The cut signals a shift towards supporting growth.", "analysis", "interpretation"),
    ("The government should have acted much sooner on prices.", "opinion", "value_judgement"),
])
def test_classify_claim(text, ctype, kind):
    assert classify_claim(text, ctype)[0] == kind


def test_speaker_and_stakeholder():
    assert speaker_of("Governor Sanjay Malhotra said the economy was resilient.") == "Governor Sanjay Malhotra"
    assert stakeholder_of("Governor Sanjay Malhotra") == "government"
    assert stakeholder_of("Economists at a large bank") == "expert"
    assert stakeholder_of("anything", "official") == "government"


def test_claims_grouped_across_sources_and_contradictions_calibrated():
    srcs = [
        res("a", "Reuters", "RBI cuts repo rate to 5.25% in surprise move",
            extracts=["The Reserve Bank of India cut its repo rate to 5.25% on Friday."]),
        res("b", "The Hindu", "Reserve Bank of India lowers repo rate to 5.25%",
            extracts=["The Reserve Bank of India cut the repo rate to 5.25% on Friday, its first cut this year."]),
        res("c", "Reserve Bank of India", "Monetary policy statement",
            extracts=["The Monetary Policy Committee decided to reduce the policy repo rate to 5.25%."],
            content_type="primary", source_type="official"),
        res("d", "Tiny Blog", "RBI cuts repo rate to 5.5% on Friday",
            extracts=["The Reserve Bank of India cut its repo rate to 5.5% on Friday."]),
    ]
    claims = build_claims(srcs)
    main = next(c for c in claims if "5.25%" in c.text and len(c.source_ids) >= 2)
    assert {"a", "b"} <= set(main.source_ids)
    # One small source disagreeing does not turn well-supported, primary-backed evidence into a 50/50 dispute.
    assert main.contradicted_by == ["d"] or "d" in main.contradicted_by
    assert main.confidence == "confirmed" and "report differently" in main.confidence_reason
    odd = next(c for c in claims if "5.5%" in c.text)
    assert odd.confidence == "contested" and odd.independent_sources == 1
    assert all(c.text in " ".join(s.title + " " + " ".join(n.text for n in s.evidence_notes) for s in srcs)
               for c in claims), "claims must be verbatim source text"
    kinds = sort_claims(claims)
    assert main.claim_id in kinds["consensus"] and odd.claim_id in kinds["contested"]


def test_even_contradiction_is_contested_and_single_source_is_reported():
    srcs = [res("a", "Reuters", "Talks collapsed on Monday, officials confirmed to reporters",
                extracts=["Officials said trade talks between the two sides collapsed on Monday."]),
            res("b", "AP", "Officials denied that talks collapsed",
                extracts=["Officials denied that trade talks between the two sides collapsed on Monday."]),
            res("c", "Bloomberg", "Copper prices hit a record high on supply worries")]
    claims = build_claims(srcs)
    collapsed = next(c for c in claims if c.text.startswith("Officials said trade talks"))
    assert collapsed.confidence == "contested"
    copper = next(c for c in claims if "Copper" in c.text)
    assert copper.confidence == "reported" and copper.claim_id in sort_claims(claims)["unverified"]


def test_copies_of_one_publisher_count_once():
    srcs = [res("a", "Reuters", "Oil rose 3% after the OPEC meeting ended without a deal"),
            res("b", "Reuters", "Oil rose 3% after the OPEC meeting ended without a deal")]
    c = build_claims(srcs)[0]
    assert c.independent_sources == 1 and c.confidence == "reported"


# -- registry -------------------------------------------------------------------------------
def test_shipped_registry_documents_facts_and_leaves_orientation_unknown():
    reg = Registry.load(PROJECT_ROOT / "config" / "sources.yaml")
    assert len(reg.profiles) >= 20
    for p in reg.profiles:
        assert p.references, f"{p.publisher}: every entry needs a reference"
        for f in ("editorial_orientation", "economic_orientation", "foreign_policy_orientation"):
            assert getattr(p, f) == "unknown" or p.documented(f)
    assert reg.lookup("www.bbc.co.uk").publisher == "BBC"
    assert reg.lookup("news.bbc.co.uk").publisher == "BBC"          # subdomain
    assert reg.lookup("unknown.example") is None
    meta = reg.editorial_metadata("thehindu.com", None)
    assert meta["status"] == "registry" and meta["editorial_orientation"] == "unknown"
    assert reg.editorial_metadata("unknown.example", "X")["status"] == "not in registry"


def test_orientation_without_reference_is_not_reported(tmp_path):
    p = SourceProfile(publisher="X", domains=["x.test"], editorial_orientation="centre-left")
    assert not p.documented("editorial_orientation")
    reg = Registry([p])
    assert reg.editorial_metadata("x.test", None)["editorial_orientation"] == "unknown"
    p2 = SourceProfile(publisher="Y", domains=["y.test"], editorial_orientation="centre-left",
                       references=["https://example.org/study"])
    assert Registry([p2]).editorial_metadata("y.test", None)["editorial_orientation"] == "centre-left"
    bad = tmp_path / "s.yaml"
    bad.write_text("sources:\n  - {publisher: Ok, domains: [ok.test]}\n  - {domains: [missing-publisher.test]}\n")
    assert [p.publisher for p in Registry.load(bad).profiles] == ["Ok"]  # bad entry skipped, not fatal


def test_country_and_economy():
    assert country_code("India") == "IN" and country_code("us") == "US" and country_code("Atlantis") is None
    assert economy("IN") == "emerging" and economy("JP") == "advanced" and economy(None) == "unknown"


# -- diversity ------------------------------------------------------------------------------
def test_diversity_gaps_name_missing_parties_and_voices():
    reg = Registry([SourceProfile(publisher="BBC", domains=["bbc.co.uk"], country="GB", references=["r"])])
    srcs = [res("a", "BBC", "Rate cut announced in Mumbai today", domain="bbc.co.uk"),
            res("b", "CNBC", "Rate cut lifts Asian markets on Friday", country="US"),
            res("c", "BBC", "Rate cut: what it means for borrowers", domain="bbc.co.uk")]
    rep = diversity_report(brief(), srcs, build_claims(srcs), reg)
    assert rep.countries == {"GB": 2, "US": 1}
    text = " ".join(rep.gaps)
    assert "No source based in IN" in text and "emerging economy" in text
    assert "No primary source" in text and "No quoted voices from" in text
    assert "Editorial orientation is not documented" in text


# -- model output validation ----------------------------------------------------------------
def _ids():
    return {"s1": res("a", "Reuters", "RBI cuts rate to 5.25% to support growth",
                      extracts=["The RBI cut the rate to 5.25% to support growth, the governor said."]),
            "s2": res("b", "The Hindu", "Economists warn rate cut risks inflation",
                      extracts=["Economists warned the cut could push inflation higher."], content_type="analysis"),
            "s3": res("c", "Reserve Bank of India", "Policy statement",
                      extracts=["The committee reduced the repo rate to 5.25% to support growth."],
                      content_type="primary", source_type="official")}


def test_to_questions_validates_ids_grounding_and_balance():
    ids = _ids()
    out = analysis.AnalysisOut.model_validate({"questions": [
        {"question": "Will the cut support growth or push inflation higher?", "disagreement_type": "predictive",
         "positions": [
             {"label": "Supports growth", "summary": "The cut to 5.25% aims to support growth.",
              "strongest_argument": "The committee reduced the rate to support growth.",
              "source_ids": ["s1", "s3", "s9"], "challenged_by": ["s2"]},
             {"label": "Inflation risk", "summary": "Economists warn the cut could push inflation higher, "
                                                    "says Goldman Sachs.",
              "strongest_argument": "Economists warned the cut could push inflation higher.",
              "source_ids": ["s2"]}]},
        {"question": "Is the cut too small?", "disagreement_type": "value",
         "positions": [{"label": "Too small", "summary": "x", "source_ids": ["s7"]},
                       {"label": "Right size", "summary": "The cut supports growth.", "source_ids": ["s1"]}]},
    ]})
    qs, rejected = analysis.to_questions(out, ids, {})
    assert len(qs) == 1                                             # 2nd question: only one evidenced position
    q = qs[0]
    a, b = q.positions
    assert a.source_ids == ["a", "c"] and a.challenged_by == ["b"]  # unknown s9 dropped
    assert a.evidence_strength == "strong"                          # computed: primary + 2 publishers
    assert b.summary == "" and b.evidence_strength == "weak"        # "Goldman Sachs" not in its source
    assert "asymmetric" in q.asymmetry
    joined = " ".join(rejected)
    assert "s9" in joined and "Goldman" in joined and "fewer than two positions" in joined


def test_grounded_list_rejects_invented_facts():
    rejected = []
    kept = analysis.grounded_list(["Whether inflation rises", "Whether Morgan Stanley cuts its 7% forecast"],
                                  "inflation could rise", "uncertainty", rejected)
    assert kept == ["Whether inflation rises"] and rejected


def test_no_disagreement_is_reported_not_manufactured():
    assert "does not manufacture" in no_disagreement_note(True, [], [], 5)
    assert "about facts" in no_disagreement_note(True, [], ["x"], 5)
    assert "not assessed" in no_disagreement_note(False, [], [], 5)


def test_overall_confidence_levels():
    srcs = [res(x, f"P{x}", f"Statement number {x} about the repo rate decision") for x in "ab"]
    claims = build_claims(srcs)
    rep = diversity_report(brief(), srcs, claims, Registry([]))
    assert overall_confidence(claims, srcs, rep, sort_claims(claims))[0] == "low"


# -- viewpoint search planning --------------------------------------------------------------
def test_plan_searches_build_valid_provider_queries(taxonomy):
    srcs = [res("a", "BBC", "RBI cuts repo rate", country="GB"), res("b", "CNBC", "RBI rate cut", country="US"),
            res("c", "Reuters", "RBI cuts rates", country="GB")]
    rep = diversity_report(brief(), srcs, build_claims(srcs), Registry([]))
    out = analysis.AnalysisOut.model_validate({"missing_perspectives": [
        {"perspective": "Indian borrowers", "keywords": ["home loans", "EMI", "borrowers"]}]})
    plans = plan_searches(brief(), taxonomy, rep, out, NOW, 7, ["archive", "gdelt", "guardian"])
    names = [p[0] for p in plans]
    assert names[0] == "sources based in India" and "Indian borrowers" in names
    for _, _, queries in plans:
        for q in queries:
            if q.provider == "gdelt":
                assert not gdelt_query_problems(q.text), q.text
    india = plans[0][2]
    assert any("sourcecountry:india" in q.text for q in india if q.provider == "gdelt")
    assert all("sourcecountry" not in q.text and "domainis" not in q.text for _, _, qs in plans
               for q in qs if q.provider == "guardian")


def test_labels_and_questions_are_not_claims():
    srcs = [res("a", "X", "Analysis: what new chip export controls mean for suppliers"),
            res("b", "Y", "Will the rate cut revive lending in India?"),
            res("c", "Z", "Copper prices hit a record high on supply worries")]
    assert [c.text for c in build_claims(srcs)] == ["Copper prices hit a record high on supply worries"]


def test_position_with_nothing_grounded_is_dropped():
    ids = _ids()
    out = analysis.AnalysisOut.model_validate({"questions": [{"question": "Is growth helped?", "positions": [
        {"label": "Yes", "summary": "The cut supports growth.", "source_ids": ["s1"]},
        {"label": "No, says Nomura", "summary": "Nomura expects 7% inflation.", "source_ids": ["s2"]}]}]})
    qs, rejected = analysis.to_questions(out, ids, {})
    assert qs == [] and any("dropped" in r for r in rejected)
