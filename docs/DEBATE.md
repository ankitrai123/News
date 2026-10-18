# Phase 3: viewpoints, claims, agreement and disagreement

The `debate` stage turns each event's research package (Phase 2) into a **debate package**:
what the sources agree on, where they contradict each other, which claims rest on one source,
which interpretive questions are genuinely argued, how strong the evidence on each side is,
who is (and is not) represented, and what remains uncertain.

It runs after `research` and before `report`:

```
ingest → cluster → rank → review → research → debate → report
```

```bash
intelpod run                         # everything, including Phase 3
intelpod debate [--run-id ID]        # Phase 3 on an already researched run (rewrites the report)
intelpod debate --refresh            # analyse again events already done in that run
intelpod debate --no-llm             # claim map and diversity only (no model questions/positions)
intelpod run --no-debate             # skip Phase 3
intelpod sources [--gaps]            # the source registry; --gaps lists feed publishers it lacks
```

## Principles (enforced in code, not just in prompts)

* **No invented sources or viewpoints.** Every source in a debate package is a page that Phase 2
  (or a Phase 3 viewpoint search, through the same checks) actually retrieved. A position the
  model proposes without a valid source is shown as "no retrieved source argues this", never as
  an evidenced view.
* **Claims are verbatim.** A claim is a headline, a short extract or an attributed sentence taken
  from a source. The model never writes claims.
* **No false balance.** A question is presented as a debate only if at least two positions are
  each backed by retrieved sources. Evidence strength (strong / moderate / weak / none) is
  computed by IntelPod from the cited sources, and lopsided evidence is labelled as such
  ("Evidence is asymmetric: …"). One small outlet contradicting a primary source and several
  publishers does not turn a fact into a 50/50 dispute.
* **No manufactured disagreement.** "No meaningful disagreement was found" is a normal result.
* **No ideological labels from intuition.** Orientation fields in the registry count only when set
  *and* backed by references; the shipped registry leaves them all `unknown`. Diversity is judged
  from facts (country, economy, source type, who is quoted) and from what sources argue.
* **Limitations are stated.** Perspectives looked for and not found, coverage gaps, model
  problems and rejected model output are all recorded.

## Steps for one event

1. **Claim map** (`intelpod/debate/claims.py`, deterministic). Statements are typed:
   `factual`, `attributed` (someone said), `interpretation` (explains why / what it means),
   `forecast` (future or intentions) and `value_judgement` (should / wrong / welcome).
   Headlines that label a piece ("Analysis: …") or ask a question are not claims. Statements
   from different sources that state the same thing are grouped (word overlap, or the same
   specific figure about the same subject; different figures for the same unit never merge;
   "X happened" and "officials denied X" never merge). Contradictions are linked
   conservatively: same subject and different values for the same unit (5.25% vs 5.5%), or one
   side explicitly denying. Copies of one publisher count once.
2. **Confidence** (calibrated language):

   | Label | Meaning |
   |---|---|
   | confirmed | stated by a primary (official) source, or by ≥ 2 independent publishers, and not contradicted — or contradicted only by a clear minority (primary source and ≥ 2× the publishers, or ≥ 3× the publishers, with no primary source against) |
   | reported | one source; not independently confirmed (value judgements are always "reported": the source's view) |
   | plausible | an interpretation argued by a source |
   | contested | sources contradict each other without such a clear majority |
   | speculative | a forecast; cannot be verified yet |

3. **Who is represented** (`diversity.py`): countries and regions of the sources (registry, else
   the provider's country), advanced vs emerging economies (IMF WEO grouping), source and
   content types, and the stakeholders quoted (government, corporate, investor, labour, consumer,
   expert, civil society, international bodies). Gaps: a party country with no source, no
   emerging-economy source when one is involved, no analysis/opinion/research, no primary source,
   missing voices (government and experts; companies and investors for economic topics).
4. **Model analysis** (optional, `analysis.py`, task `debate_analysis`): the model sees numbered
   sources (publisher, country, kind, date, short extracts) and proposes up to 3 interpretive
   questions with 2–4 positions each (summary, strongest argument, sources arguing it, sources
   challenging it, and the type of disagreement: factual, causal, predictive or value), up to 4
   uncertainties, up to 3 kinds of evidence that would change the assessment, and up to 3
   missing perspectives with search keywords.
5. **Viewpoint searches**: up to `viewpoint_searches_per_event` (3) searches for the most
   important gaps and model-named missing perspectives — e.g. GDELT `sourcecountry:china` for a
   party with no local source, cue words for missing voices, `domainis:` official sites for a
   missing primary source. They go through Phase 2's own pipeline (robots.txt, paywalls,
   publisher opt-outs, dates, relevance ≥ `research.min_relevance`, quality) and add up to
   `sources_added_per_search` (2) sources each to the event's research package, marked
   "added by viewpoint search". If sources were added, the claim map and diversity are rebuilt
   and the model asked again.
6. **Validation**: unknown source ids are dropped; position labels, summaries and arguments are
   rejected if they name a person, organisation or number absent from the sources they cite
   (uncertainties and "would change" items: absent from all sources); a position with nothing
   left after checking is dropped; a question with fewer than two evidenced positions is not
   shown as a debate. Everything refused is listed under `rejected` with the reason.
7. **Overall confidence**: `high` (≥ 2 confirmed statements including a primary source, ≥ 5
   publishers from ≥ 2 regions, few contested claims), `low` (< 3 publishers or nothing
   confirmed), else `medium` — with the reason shown.

Without a model (none configured, `--no-llm`, provider failure, budget used) the claim map,
diversity and viewpoint searches for coverage gaps still run; the package status is `partial`
and says that interpretive questions were not assessed.

## Source registry (`config/sources.yaml`)

One entry per publisher: domains, country, region, source type, ownership, optional
orientation fields (`editorial_orientation`, `economic_orientation`,
`foreign_policy_orientation`), strengths, limitations, corrections-policy URL, **references**
(URLs supporting the entry), confidence, `disputed`, `last_reviewed` and notes. The shipped
entries record only facts with a Wikipedia or official reference (country, ownership, type);
every orientation is `unknown`. To add an orientation, cite a published, reviewable source (e.g.
a media-bias study) in `references`; without references it is ignored. Registry facts fill each
research result's `editorial_metadata` and the corrections-policy component of its quality score.
`intelpod sources --gaps` lists feed publishers the registry does not describe.

## Output

* Database: `debate_packages` (one per run and event) and `claims`.
* Dashboard: the event page's **Viewpoints and evidence** section (questions and positions,
  agreed / contested / single-source / forecast claims with their sources, uncertainties, who is
  represented, viewpoint searches, limitations, rejected model output); one summary line per
  event on the briefing.
* Report: a "Viewpoints and evidence" block per event in `phase1_events.md`, and `debate.json`.
* API: `GET /api/runs/{run_id}/debate`, `GET /api/events/{event_id}/debate?run=`.

## Configuration (`debate:`)

| Key | Default | Meaning |
|---|---|---|
| `enabled` | true | run the stage |
| `sources_file` | `config/sources.yaml` | source registry |
| `viewpoint_searches_per_event` | 3 | 0 disables searching |
| `sources_added_per_search` | 2 | |
| `gdelt_requests_per_run` | 12 | extra GDELT requests (on top of research's), throttled and cached |
| `model_analysis` | true | questions and positions |
| `max_model_calls` | 16 | at most 2 per event |
| `max_sources_to_model` | 20 | most relevant sources sent to the model |

## Known limitations

* Claim grouping and contradiction detection are lexical: paraphrases with little word overlap
  ("RBI" vs "Reserve Bank of India") may stay separate, and subtle contradictions without a
  differing figure or an explicit denial are missed. Contradictions are found only when stated.
* Stakeholder detection is keyword-based on the quoted speaker or sentence.
* Orientation is unknown for every shipped source by design; position diversity therefore rests
  on what sources argue, which depends on what the searches could reach (no general web search).
* Model-proposed positions are checked for invented names and numbers, not for whether the
  summary fairly represents the cited source; the sources are linked so a reader can check.
