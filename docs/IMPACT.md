# Phase 4: context, business impact and connections → the intelligence brief

The `impact` stage runs after `debate` and before `report`:

```
ingest → cluster → rank → review → research → debate → impact → report
```

It turns each event's claim map (Phase 3) into context and impact analysis, links events to
each other, records forecasts in a ledger, and writes the **intelligence brief**:
`data/runs/<run_id>/intelligence_brief.md` and `intelligence_brief.json`.

```bash
intelpod run                              # everything
intelpod impact [--run-id ID] [--refresh] # Phase 4 on an existing run (rewrites the reports)
intelpod impact --no-llm                  # evidence tables only (no model effect chains)
intelpod run --no-impact                  # skip Phase 4
intelpod forecasts [--status open|resolved|all]
intelpod forecasts resolve ID --verdict correct|incorrect|partly|unresolvable --outcome "what happened (source)"
```

## Principles

* **Built on retrieved statements.** Everything cites claims from the Phase 3 claim map:
  verbatim sentences with calibrated confidence and their sources.
* **No invented numbers.** Figures are quoted from sources with their claim, and labelled
  *measured* (a reported fact) or *forecast*. IntelPod never estimates a number; model text
  containing a number or name that is not in the statements it cites is rejected.
* **Reporting vs reasoning.** In an effect chain, each step is either *sourced* (cites
  statements, checked against them) or *inference* (IntelPod/model reasoning in general words).
  Reasoning steps may contain no quantity at all (no digits, percentages or number words), so a
  number in a chain always comes from the statement a step cites. A step citing a forecast must
  keep its hedging ("could", "expected"); stating a forecast as fact drops the chain.
  Chains must start from a sourced step. Confidence decays along the chain (first inference:
  at most "plausible"; second: "speculative"), and a chain is *measured* only if every step is
  sourced reporting; otherwise it is *conditional*.
* **No false balance.** The strongest positive and negative readings are shown only when
  statements support them; if only one side is supported, the brief says so.
* **Connections need evidence.** Events are linked only when a source of one refers to the
  other (*evidenced*, quoted), or when both discuss the same economic channel and share a
  country or actor (*shared driver*, labelled as context, not causation). Coincidence in time
  is never a link. Evidence must name something specific to the *other* event: a name found in no
  other event of the run, or two independent signals (two names, a name and a distinctive title
  word, or three distinctive words). Months, weekdays, generic news words ("trade", "deal") and
  names common to three or more of the day's events ("Trump") never count. An event's own
  reports, and any source used by both events, never count either (a shared source is flagged as
  "may be one story").
* **Forecasts are not facts.** They go to a ledger with source, date and stated timeframe, and
  stay *open* until someone records the outcome. Only forecasts someone can be held to are
  ledgered: attributed to a speaker or an official source, or with a stated timeframe. The same
  sentence seen under several events is one entry listing all of them. A recorded outcome is never overwritten: changing
  it needs `--amend` (CLI) and the earlier verdict is kept in the entry's history.

## What the brief contains, per event

| Section | How it is built |
|---|---|
| What happened | the best-supported factual statement (confirmed first, primary sources first), or a one-sentence model summary checked against confirmed/reported factual statements only (never forecasts or contested claims) |
| What is new | compared with IntelPod's last analysis of the same event in a run that started earlier and had a claim map: new statements (verbatim); or "first time analysed" |
| Timeline | earlier related events from IntelPod's database (shared named entities, or same topic and country, within `context_lookback_days`), sources published before the event, and this event |
| Agreed / disputed / debated | from the Phase 3 debate package |
| Impact evidence | for 11 dimensions (growth; inflation and rates; energy and commodities; currencies and capital; trade and tariffs; corporate revenues and costs; competition; supply chains; technology investment; jobs and demand; budgets and regulation) and 12 sectors: which statements discuss them, from how many sources; and which dimensions **no source discusses** |
| India | statements concerning India (India, RBI, rupee, Sensex, Indian cities...), plus a checked model summary; if none, "not assessed" |
| Figures | numbers with a unit or currency quoted from statements: *measured* (an uncontested factual statement), *stated* (attributed or contested), or *forecast* |
| Effect chains | model-proposed, checked as above: horizon (short/long term), direction, dimensions, sectors, India flag |
| Beneficiaries, adversely affected, unintended consequences | model-proposed groups; *sourced* if they cite statements, else *inference*; invented names rejected |
| Strongest readings | positive / negative, each tied to statements |
| Uncertain / would change | from Phase 3 |
| Limitations, rejected model output | always listed |

Across events: **connections** (evidenced first, with quotes; a model may word the mechanism,
checked against the quotes) and the **new forecasts** recorded this run.

## Without a model

Context, timeline, impact evidence tables, India statements, figures, connections and the
forecast ledger are all deterministic. Effect chains, affected groups and readings need a model;
without one the analysis is `partial` and says so.

## Dashboard

* Event page → **Context and business impact** section.
* Briefing → an "Impact" line per event and a **Connections between events** card.
* **Forecasts** page: the ledger, with a form to record outcomes.
* API: `GET /api/runs/{run_id}/brief`, `GET /api/events/{event_id}/impact?run=`,
  `GET /api/forecasts?status=&event_id=`.

## Configuration (`impact:`)

| Key | Default | Meaning |
|---|---|---|
| `enabled` | true | run the stage |
| `model_analysis` | true | effect chains, groups, readings |
| `explain_links` | true | one model call per run to word evidenced links |
| `max_model_calls` | 12 | one per event plus one for links |
| `context_lookback_days` | 45 | how far back earlier related events are searched |

## Known limitations

* Dimension, sector and India tagging is keyword-based on the statements: it shows what sources
  *discuss*, not the size of any effect.
* Connection evidence is lexical (an entity name, or two distinctive title words, in the other
  event's sources); paraphrased references are missed.
* "What is new" compares statement wording with the previous run's claim map; rephrased old
  facts can appear as new.
* The name check treats a capitalised first word as an ordinary word only if it is on a fixed
  list of common words ("Savers", "Exporters", "Higher") or appears in lower case in the sources;
  anything else ("Pfizer", "Boeing") must appear in the sources. A real common word missing from
  the list can cause a harmless false rejection.
* Model reasoning steps are checked for invented names and numbers, not for economic soundness;
  they are labelled *inference* so a reader weighs them accordingly.
