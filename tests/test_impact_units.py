"""Phase 4 units: impact evidence tagging, figures, context, connections, model-output checks,
forecast ledger."""
from datetime import datetime, timedelta, timezone

from intelpod.db import Database, EventRow
from intelpod.debate.claims import build_claims
from intelpod.debate.models import Claim
from intelpod.impact import analysis
from intelpod.impact.context import build_context, earlier_events, lead_claim, whats_new
from intelpod.impact.links import find_links
from intelpod.impact.models import Forecast, ImpactAnalysis
from intelpod.impact.store import ImpactStore
from intelpod.impact.tagging import figures, tag_dimensions, timeframe
from test_debate_units import NOW, brief, res


def claim(cid, text, kind="factual", confidence="confirmed", sources=("a",)):
    return Claim(claim_id=cid, text=text, kind=kind, confidence=confidence, source_ids=list(sources),
                 independent_sources=len(sources))


CLAIMS = [
    claim("k1", "The Reserve Bank of India cut its repo rate to 5.25% on Friday.", sources=("a", "b")),
    claim("k2", "Oil prices rose 3% after OPEC+ agreed to cut output.", confidence="reported", sources=("c",)),
    claim("k3", "Economists expect inflation to ease to 4% next year.", kind="forecast", confidence="speculative"),
    claim("k4", "Banks are expected to pass on the cut to borrowers within weeks.", kind="forecast",
          confidence="speculative", sources=("b",)),
    claim("k5", "Lower rates should support car and home loan demand, analysts said.", kind="attributed",
          confidence="reported", sources=("d",)),
]


# -- tagging ----------------------------------------------------------------------------------
def test_dimensions_sectors_india_come_from_statements():
    dims, secs, india = tag_dimensions(CLAIMS)
    keys = {d.key for d in dims}
    assert {"inflation_rates", "energy_commodities"} <= keys and "tech_investment" not in keys
    rates = next(d for d in dims if d.key == "inflation_rates")
    assert "k1" in rates.claim_ids and set(rates.source_ids) >= {"a", "b"}
    assert {"banking", "energy"} <= {s.key for s in secs}
    assert india is not None and india.claim_ids == ["k1"]


def test_figures_are_quoted_and_forecasts_marked():
    figs = {(f.figure, f.kind) for f in figures(CLAIMS)}
    assert ("5.25%", "measured") in figs and ("3%", "measured") in figs and ("4%", "forecast") in figs
    assert not any(f.figure.startswith("2027") for f in figures([claim("y", "Growth will slow in 2027.")]))
    assert figures([claim("z", "A ₹500 crore subsidy was announced.")])[0].figure == "₹500 crore"


def test_timeframe():
    assert timeframe("Inflation will ease next year") == "next year"
    assert timeframe("Output to recover by 2027, analysts say") == "by 2027"
    assert timeframe("Prices could rise") is None


# -- context ----------------------------------------------------------------------------------
def test_lead_claim_prefers_confirmed_primary():
    assert lead_claim(CLAIMS).claim_id == "k1"
    assert lead_claim([claim("f", "x y z", kind="forecast", confidence="speculative")]) is None


def test_whats_new_against_previous_run():
    prev = ImpactAnalysis(run_id="r0", event_id="e1", event_title="t", status="completed",
                          created_at=NOW - timedelta(days=1))
    out = whats_new(CLAIMS, prev, CLAIMS[:2], None)
    assert out[0].startswith("Since run r0") and any("k" not in x and "ease to 4%" in x for x in out)
    assert "No new statements" in whats_new(CLAIMS[:2], prev, CLAIMS[:2], None)[0]
    assert whats_new(CLAIMS, None, None, NOW)[0].startswith("First time")


def test_earlier_events_need_shared_actors(tmp_path):
    db = Database(tmp_path / "x.db")
    with db.session() as s:
        s.add(EventRow(event_id="old1", title="RBI holds repo rate steady", topic="markets_monetary", countries=["India"],
                       entities=["Reserve Bank of India"], centroid={}, first_seen_at=NOW - timedelta(days=20)))
        s.add(EventRow(event_id="old2", title="Indian cricket win", topic="other", countries=["India"], entities=[],
                       centroid={}, first_seen_at=NOW - timedelta(days=10)))
        s.add(EventRow(event_id="old3", title="Too old", topic="markets_monetary", countries=["India"],
                       entities=["Reserve Bank of India"], centroid={}, first_seen_at=NOW - timedelta(days=200)))
    store = ImpactStore(db)
    b = brief()
    rows = store.earlier_events("e1", before=b.first_article_at, since=b.first_article_at - timedelta(days=45))
    refs = earlier_events(b, rows)
    assert [r.event_id for r in refs] == ["old1"] and "not a demonstrated cause" in refs[0].why
    from intelpod.debate.models import DebatePackage
    dp = DebatePackage(run_id="r1", event_id="e1", event_title="t", status="completed", claims=CLAIMS, created_at=NOW)
    ctx = build_context(b, dp, [], store, None, None, NOW)
    assert ctx.what_happened == CLAIMS[0].text and ctx.timeline[0].ref == "old1"
    assert ctx.timeline[-1].kind == "this_event"


# -- links ------------------------------------------------------------------------------------
def test_links_evidenced_vs_shared_driver_vs_none():
    rbi = brief(event_id="rbi", title="RBI cuts repo rate", entities=["Reserve Bank of India"],
                countries=["India"], centroid={"repo": 1.0})
    oil = brief(event_id="oil", title="OPEC+ agrees deeper output cut", entities=["OPEC"],
                countries=["Saudi Arabia"], topic="energy_commodities", centroid={"opec": 1.0, "output": 0.8})
    cricket = brief(event_id="ckt", title="India wins cricket series", entities=["BCCI"], countries=["India"],
                    topic="other", centroid={"cricket": 1.0})
    srcs = {
        "rbi": [res("r1", "Mint", "RBI cuts repo rate as inflation eases",
                    extracts=["The RBI warned that higher oil prices after the OPEC cut could lift inflation."])],
        "oil": [res("o1", "Reuters", "Oil jumps after OPEC cut", extracts=["Brent crude rose 3% on Thursday."])],
        "ckt": [res("c1", "ESPN", "India wins cricket series against Australia")],
    }
    links = find_links("run", [(rbi, srcs["rbi"]), (oil, srcs["oil"]), (cricket, srcs["ckt"])])
    assert len(links) == 1
    link = links[0]
    assert {link.event_a, link.event_b} == {"rbi", "oil"} and link.kind == "evidenced"
    assert "OPEC cut" in link.evidence[0].text and link.evidence[0].event_id == "rbi"

    tariffs_a = brief(event_id="a", title="US tariffs on Chinese steel", entities=["Commerce Department"],
                      countries=["United States", "China"], centroid={"steel": 1.0})
    tariffs_b = brief(event_id="b", title="China export controls on gallium", entities=["MOFCOM"],
                      countries=["China"], centroid={"gallium": 1.0})
    sa = [res("x1", "AP", "US raises tariffs on Chinese steel", extracts=["The tariffs take effect next month."])]
    sb = [res("y1", "FT", "China tightens export controls", extracts=["New tariffs and export controls widen the trade war."])]
    link = find_links("run", [(tariffs_a, sa), (tariffs_b, sb)])[0]
    assert link.kind == "shared_driver" and "trade and tariffs" in link.channels
    assert "not evidence" in link.note and not link.evidence


# -- model output validation ------------------------------------------------------------------
def _out(chains=(), actors=(), interps=(), **kw):
    return analysis.ImpactOut.model_validate({"chains": list(chains), "actors": list(actors),
                                              "interpretations": list(interps), **kw})


def _ids():
    return {f"c{i}": c for i, c in enumerate(CLAIMS, 1)}


ALL = " ".join(c.text for c in CLAIMS)


def test_chain_confidence_decays_and_status_is_conditional():
    out = _out(chains=[{"title": "Cheaper loans", "horizon": "short_term", "direction": "positive",
                        "dimensions": ["inflation_rates", "nonsense"], "sectors": ["banking", "automobiles"],
                        "india": True, "steps": [
                            {"text": "The Reserve Bank of India cut its repo rate to 5.25%.", "claim_ids": ["c1"]},
                            {"text": "Banks are expected to pass on the cut to borrowers.", "claim_ids": ["c4"]},
                            {"text": "Cheaper loans could lift demand for cars and homes."}]}])
    rejected = []
    ch = analysis.to_chains(out, _ids(), ALL, rejected)[0]
    assert [s.basis for s in ch.steps] == ["reported", "reported", "inferred"]
    assert ch.steps[0].confidence == "confirmed" and ch.steps[1].confidence == "speculative"
    assert ch.confidence == "speculative" and ch.status == "conditional"
    assert ch.dimensions == ["inflation_rates"] and ch.sectors == ["banking", "automobiles"] and ch.india


def test_measured_chain_only_from_reported_facts():
    out = _out(chains=[{"title": "Rate cut and oil", "steps": [
        {"text": "The Reserve Bank of India cut its repo rate.", "claim_ids": ["c1"]},
        {"text": "Oil prices rose after OPEC+ agreed to cut output.", "claim_ids": ["c2"]}]}])
    ch = analysis.to_chains(out, _ids(), ALL, [])[0]
    assert ch.status == "measured" and ch.confidence == "reported"


def test_invented_numbers_and_names_kill_the_chain():
    rejected = []
    out = _out(chains=[
        {"title": "A", "steps": [{"text": "The repo rate was cut to 5.25%.", "claim_ids": ["c1"]},
                                 {"text": "This will add 0.4% to GDP growth."}]},                 # invented number
        {"title": "B", "steps": [{"text": "Goldman Sachs says the cut to 5.25% helps.", "claim_ids": ["c1"]},
                                 {"text": "Demand rises."}]},                                     # name not in claim
        {"title": "C", "steps": [{"text": "Demand for loans rises."}, {"text": "Banks lend more."}]},  # no sourced start
        {"title": "D", "steps": [{"text": "The repo rate was cut.", "claim_ids": ["c1", "c99"]}]},     # one step
    ])
    assert analysis.to_chains(out, _ids(), ALL, rejected) == []
    text = " ".join(rejected)
    assert "0.4%" in text and "Goldman" in text and "must start from a sourced statement" in text and "c99" in text


def test_actors_and_one_sided_interpretations():
    rejected = []
    out = _out(actors=[{"group": "Borrowers", "effect": "benefits", "reason": "Lower loan rates.", "claim_ids": ["c1"]},
                       {"group": "Tata Motors", "effect": "benefits", "reason": "More car loans."},
                       {"group": "Savers", "effect": "harmed", "reason": "Lower deposit rates."}],
               interps=[{"stance": "positive", "summary": "Lower rates should support loan demand.", "claim_ids": ["c5"]},
                        {"stance": "negative", "summary": "Inflation could return.", "claim_ids": []}])
    actors = analysis.to_actors(out, _ids(), ALL, rejected)
    assert [(a.group, a.basis) for a in actors] == [("Borrowers", "reported"), ("Savers", "inferred")]
    interps, note = analysis.to_interpretations(out, _ids(), rejected)
    assert [i.stance for i in interps] == ["positive"] and "does not add an opposing one" in note
    assert any("Tata" in r for r in rejected) and any("cites no statement" in r for r in rejected)


def test_odd_json_coerced():
    out = analysis.ImpactOut.model_validate({"chains": [{"title": None, "steps": ["Rates fell.", None],
                                                         "india": "yes"}, "junk"],
                                             "actors": None, "interpretations": {"stance": "positive"},
                                             "what_happened": None})
    assert len(out.chains) == 1 and out.chains[0].india and out.chains[0].steps[0].text == "Rates fell."


# -- forecast ledger --------------------------------------------------------------------------
def test_forecast_ledger_upsert_and_resolve(tmp_path):
    store = ImpactStore(Database(tmp_path / "f.db"))
    f = Forecast(forecast_id="f1", text="Inflation will ease next year.", event_id="e1", first_run_id="r1",
                 last_run_id="r1", first_seen_at=NOW, source_ids=["a"], timeframe="next year")
    assert store.upsert_forecasts([f]) == ["f1"]
    store.resolve_forecast("f1", "correct", "CPI fell to 3.9% (official data)")
    again = f.model_copy(update={"last_run_id": "r2", "source_ids": ["b"]})
    assert store.upsert_forecasts([again]) == []
    got = store.forecasts()[0]
    assert got.status == "resolved" and got.verdict == "correct" and got.last_run_id == "r2"
    assert set(got.source_ids) == {"a", "b"}                     # outcome kept, sources merged
    assert store.resolve_forecast("nope", "correct", "xyz") is None
    import pytest
    with pytest.raises(ValueError):
        store.resolve_forecast("f1", "incorrect", "overwrite attempt")       # recorded outcome is protected
    with pytest.raises(ValueError):
        store.resolve_forecast("f1", "incorrect", "  ", amend=True)          # empty outcome refused
    amended = store.resolve_forecast("f1", "partly", "Revised: CPI 4.1%", amend=True)
    assert amended.verdict == "partly" and amended.history[0]["verdict"] == "correct"


def test_claims_from_build_claims_feed_tagging():
    srcs = [res("a", "Mint", "RBI cuts repo rate to 5.25%", extracts=["Rupee steadied after the RBI decision."])]
    dims, _, india = tag_dimensions(build_claims(srcs))
    assert india is not None and {"inflation_rates", "currency_capital"} <= {d.key for d in dims}
    assert isinstance(datetime.now(timezone.utc), datetime)


def test_sentence_start_words_vs_invented_names():
    from intelpod.debate.analysis import unsupported_terms
    src = "the rate cut helps borrowers and banks"
    assert unsupported_terms("Savers lose out. Cheaper loans help exporters.", src) == []
    assert unsupported_terms("Tata Motors gains.", src) == ["Tata", "Motors"]
    assert unsupported_terms("Infosys gains.", src) == ["Infosys"]


# -- review regressions -------------------------------------------------------------------------
def test_no_link_from_shared_entities_titles_or_shared_sources():
    a = brief(event_id="a", title="Apple reports record iPhone sales in India", entities=["Apple"],
              countries=["India"], centroid={"iphone": 1.0})
    b = brief(event_id="b", title="EU fines Apple over App Store rules", entities=["Apple", "European Commission"],
              countries=["Belgium"], centroid={"fines": 1.0})
    sa = [res("x1", "Mint", "Apple reports record iPhone sales in India")]
    sb = [res("y1", "FT", "EU fines Apple over App Store rules")]
    assert [link.kind for link in find_links("r", [(a, sa), (b, sb)])] == []
    s1 = brief(event_id="s1", title="US tariffs on steel imports raise prices", entities=[], countries=["United States"])
    s2 = brief(event_id="s2", title="India steel tariffs to protect local mills", entities=[], countries=["India"])
    t1 = [res("p1", "AP", "US tariffs on steel imports raise prices")]
    t2 = [res("q1", "Mint", "India steel tariffs to protect local mills")]
    assert all(link.kind != "evidenced" for link in find_links("r", [(s1, t1), (s2, t2)]))
    rx = res("rx", "Reuters", "Roundup: OPEC cut and RBI decision")
    rbi = brief(event_id="rbi", title="RBI cuts repo rate", entities=["Reserve Bank of India"], countries=["India"])
    oil = brief(event_id="oil", title="OPEC agrees output cut", entities=["OPEC"], countries=["Saudi Arabia"])
    links = find_links("r", [(rbi, [rx]), (oil, [rx])])
    assert all(link.kind != "evidenced" for link in links)


def test_invented_one_word_names_and_quantities_rejected():
    rejected = []
    out = _out(actors=[{"group": "Pfizer", "effect": "benefits", "reason": "Gains share."},
                       {"group": "Boeing", "effect": "harmed", "reason": "Higher costs."},
                       {"group": "Exporters", "effect": "harmed", "reason": "Could lose a third of sales."}],
               chains=[{"title": "T", "steps": [{"text": "Oil prices rose 3% after OPEC+ agreed to cut output.",
                                                 "claim_ids": ["c2"]},
                                                {"text": "Fuel costs could rise 3% within a year."}]}])
    assert analysis.to_actors(out, _ids(), ALL, rejected) == []
    assert analysis.to_chains(out, _ids(), ALL, rejected) == []
    text = " ".join(rejected)
    assert "Pfizer" in text and "Boeing" in text and "quantity" in text


def test_forecast_cannot_be_restated_as_fact_and_value_chain_not_measured():
    rejected = []
    out = _out(chains=[{"title": "T", "steps": [
        {"text": "Economists expect inflation to ease to 4% next year.", "claim_ids": ["c3"]},
        {"text": "Borrowing gets cheaper."}]},
        {"title": "U", "steps": [{"text": "Inflation eases to 4%.", "claim_ids": ["c3"]}, {"text": "Demand rises."}]}])
    chains = analysis.to_chains(out, _ids(), ALL, rejected)
    assert len(chains) == 1 and chains[0].status == "conditional"            # hedged forecast kept, stated one dropped
    assert any("forecast as fact" in r for r in rejected)
    vj = {"v1": claim("v1", "The tariff is a reckless mistake.", kind="value_judgement", confidence="reported"),
          "v2": claim("v2", "The tariff is wrong for workers.", kind="value_judgement", confidence="reported")}
    out = _out(chains=[{"title": "V", "steps": [{"text": "The tariff is a reckless mistake.", "claim_ids": ["v1"]},
                                                {"text": "The tariff is wrong for workers.", "claim_ids": ["v2"]}]}])
    ch = analysis.to_chains(out, vj, "x", [])[0]
    assert ch.status == "conditional"


def test_explain_links_checked_against_quotes():
    from intelpod.impact.links import explain_links
    from intelpod.impact.models import EventLink, LinkEvidence
    link = EventLink(run_id="r", link_id="l", event_a="a", event_b="b", title_a="RBI cuts rate", title_b="OPEC cut",
                     kind="evidenced", evidence=[LinkEvidence(event_id="a", result_id="x", publisher="Mint", url="u",
                                                              text="The RBI said oil prices after the OPEC cut could lift inflation.")])
    bad = link.model_copy(deep=True)

    class Client:
        def __init__(self, text):
            self.text = text

        def chat_json(self, task, messages, schema):
            return schema.model_validate({"links": [{"link": "L1", "explanation": self.text}]}), "m"

    explain_links(Client("Oil prices after the OPEC cut could lift inflation, the RBI said."), [link])
    assert link.explanation and link.explanation_by == "m"
    _, rej = explain_links(Client("Morgan Stanley says the cut adds 0.5% to inflation."), [bad])
    assert not bad.explanation and rej


def test_brief_markdown_escapes_untrusted_text():
    from intelpod.debate.models import DebatePackage
    from intelpod.impact.models import EventLink, Figure, IntelligenceBrief, LinkEvidence
    from intelpod.impact.report import render_brief
    from intelpod.schemas import RankedEvent
    evil = "Evil [click](https://evil.test) <img src=x onerror=alert(1)>"
    c = claim("k1", "Prices rose 5% [see](https://evil.test).", sources=("s1",))
    a = ImpactAnalysis(run_id="r", event_id="e1", event_title="t", status="completed", created_at=NOW,
                       figures=[Figure(figure="5%", claim_id="k1", text=c.text, source_ids=["s1"], kind="measured")])
    dp = DebatePackage(run_id="r", event_id="e1", event_title="t", status="completed", claims=[c], consensus=["k1"],
                       created_at=NOW)
    src = res("s1", evil, "x")
    link = EventLink(run_id="r", link_id="l", event_a="e1", event_b="e2", title_a="A", title_b="B", kind="evidenced",
                     evidence=[LinkEvidence(event_id="e1", result_id="s1", publisher=evil, url="u", text=c.text)])
    brief_ = IntelligenceBrief(run_id="r", generated_for=NOW, links=[link], created_at=NOW)
    ranked = [RankedEvent.model_construct(event_id="e1", rank=1, title="T [x](https://evil.test)")]
    md = render_brief("r", NOW, ranked, {"e1": a}, {"e1": dp}, {"e1": {"s1": src}}, brief_, [])
    import re
    assert not re.search(r"(?<!\\)\]\(https://evil", md) and not re.search(r"(?<!\\)<img", md)
    assert "## Connections between events" in md and "evidenced" in md
