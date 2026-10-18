# Phase 5: the podcast script

The `script` stage turns the day's checked material into a two-host script, verifies every
line against the evidence it cites, and writes a source-linked transcript, episode notes and an
evidence report. It runs after `impact` and before `report`:

```
ingest → cluster → rank → review → research → debate → impact → script → report
```

```bash
intelpod run                          # everything
intelpod script [--run-id ID] [--refresh]   # Phase 5 on an existing run
intelpod script --no-llm              # template script (verified quotes only)
intelpod run --no-script              # skip Phase 5
```

Output in `data/runs/<run_id>/`: `script.md`, `transcript.json`, `episode_notes.md`,
`evidence_report.md`. Dashboard: **Episode** page. API: `GET /api/runs/{run_id}/episode`.

## Hosts

| | Role |
|---|---|
| **Host A** (default "Maya", anchor) | introduces each story, sets out the timeline, asks clear questions, makes transitions, challenges unsupported claims |
| **Host B** (default "Arjun", analyst) | interpretations, competing views, business and economic consequences, India implications, uncertainty, second-order effects |

Names are configurable (`script.host_a`, `script.host_b`).

## How it is built

1. **Story selection and order (no model).** A selected event becomes a story only if Phases 3–4
   produced at least two checked items, one a sourced statement. The number of stories is capped
   by the word budget (`target_words`, default 1,800 ≈ 13 minutes at 140 words/minute;
   `min_story_words` per story), so fewer stories get more depth. The strongest story
   (importance, plus disagreement, India relevance and evidenced connections) comes first; a story
   with an evidenced connection to the previous one comes next.
2. **Fact sheet per story.** Numbered items: verbatim confirmed and single-source statements,
   disputed statements, debate positions, effect chains (each step marked sourced or inference),
   India statements, forecasts and uncertainties, each with its confidence and publishers.
3. **Writing.** With a model: one call per story (dialogue that cites fact-sheet items, plus
   pronunciation suggestions and short term explanations), one call for the opening, "what to
   watch" and closing. Without a model: a template that quotes verified statements with their
   sources — accurate, not conversational.
4. **Verification (no model)**, line by line, against the items the line cites:

   | Check | Rule |
   |---|---|
   | Citations | fact and uncertainty lines must cite; analysis must cite a position, chain or statement; cited items must exist and belong to the story |
   | Names and numbers | every capitalised name and every number must be in the cited items; a line citing nothing may contain no number |
   | Quotations | words in quotation marks must appear verbatim in the cited items |
   | Hedging | a line resting on a forecast, speculation or inference must sound like one ("could", "expected") |
   | Disputes | a line resting on a contested statement must say it is disputed |
   | Attribution | a line resting only on single-source statements must name the source — added automatically ("According to Mint, ...") |
   | Causality | causal wording ("led to", "because") needs a cited effect chain or a cited statement that itself makes the causal link |

5. **Revision.** Lines that fail go back to the model once, with their problems and cited items;
   the rewrite is checked again. Lines that still fail are **removed** and listed, with the reason,
   in the evidence report.

## Outputs

* `script.md` — the dialogue by segment, with numbered source references `[n]`; analysis,
  uncertainty and explainer lines are labelled; pronunciation notes (suggestions for the voices,
  only for names actually used); terms explained (general knowledge, not from the sources, no
  numbers allowed); numbered source list with links and dates.
* `transcript.json` — every line with speaker, segment, kind, source numbers and check status.
* `episode_notes.md` — stories and why they were chosen, what was left out, sources, how the
  episode was made, limitations.
* `evidence_report.md` — counts, every removed line with the reason, every automatically fixed or
  model-revised line (before and after), and line-by-line support.

## Configuration (`script:`)

| Key | Default | Meaning |
|---|---|---|
| `enabled` | true | run the stage |
| `target_words` | 1800 | 12–15 minutes ≈ 1,600–2,000 words |
| `words_per_minute` | 140 | for the duration estimate |
| `max_stories` | 4 | upper limit; the word budget may allow fewer |
| `min_story_words` | 300 | depth per story |
| `host_a`, `host_b` | Maya, Arjun | host names |
| `model_writing` | true | false = template script |
| `revise` | true | one model revision pass for failing lines |
| `max_model_calls` | 14 | per run |

## Known limitations

* The checks are lexical: they catch invented names, numbers, quotations, missing hedges and
  unsupported causal wording, not every subtle distortion of meaning. Each line's support is listed
  in the evidence report so a person can check it before publishing.
* A name written differently from the sources ("RBI" for "Reserve Bank of India") is rejected as
  unsupported; the prompt asks the model to use names as written.
* The template script (no model) is accurate but reads like a list of quotes, and is shorter than
  the target when material is thin; the episode says so rather than padding.
* Pronunciation notes are model suggestions, not verified.
