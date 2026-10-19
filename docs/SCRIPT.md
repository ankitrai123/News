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
3. **Length.** Default about 19 minutes (`target_words: 2700`); `intelpod run --minutes 25` or
   `intelpod script --minutes 25 --refresh` changes it. Each story's share is capped at ~180 words
   per fact-sheet item, because a longer episode must come from more evidence discussed, not
   padding (padding would only be removed by the checks). If a story's dialogue comes out under
   75% of its share while fact-sheet items are still undiscussed, the model gets one "go deeper"
   pass on exactly those items. If the day's material is thin, the episode is shorter and says so.
4. **Writing.** With a model: one call per story (dialogue that cites fact-sheet items, plus
   pronunciation suggestions and short term explanations), one call for the opening, "what to
   watch" and closing. Without a model: a template that quotes verified statements with their
   sources — accurate, not conversational.
5. **Verification (no model)**, line by line, against the items the line cites:

   | Check | Rule |
   |---|---|
   | Citations | fact and uncertainty lines must cite; analysis must cite a position, chain or statement; cited items must exist and belong to the story (opening / watch / closing: the planned stories only; "what to watch" must cite a forecast or open question) |
   | Names and numbers | every capitalised name and every number must be in the cited items; a line citing nothing may contain no number; headlines never support numbers and support names only in transition / opening lines of their own story |
   | Quantities, dates, negation | numbers written as words, dates and times, and negations must also be in the cited items; a figure may not change its preposition ("to 5.25%" is not "by 5.25%") |
   | Quotations | words in any kind of quotation marks must appear verbatim in a cited *source statement* (never in a model-written summary) |
   | Hedging | a line resting on a forecast, speculation or inference must say so ("could", "expected", "likely"); attribution or words like "if"/"risk" do not count |
   | Disputes | a line resting on a contested statement must say it is disputed ("disputed", "unconfirmed", "accounts differ") |
   | Attribution | a line resting only on single-source statements must name the publisher or say "reported" — added automatically ("According to Mint, ...") |
   | Causality | causal wording ("led to", "because", "sparked", "fuelled", "pushed") needs a cited effect chain or a cited statement that itself makes the causal link |
   | Questions | a line labelled a question that does not end in "?" is checked as a statement |

6. **Revision.** Lines that fail go back to the model once, with their problems and cited items;
   the rewrite is checked again. Lines that still fail are **removed** and listed, with the reason,
   in the evidence report. If every model line of a story fails, that story falls back to the
   template segment (the episode is then marked `partial`), so a previewed story never vanishes.
7. **Reuse.** A stored episode is reused only if it was written from exactly the same research,
   debate and impact material, settings and writing mode; otherwise it is rewritten. If a rewrite
   produces no script, the old script files are deleted.

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
| `target_words` | 2700 | ≈ 19 minutes at 140 words/minute |
| `target_minutes` | none | set (or `--minutes 20`) to target minutes instead of words |
| `words_per_minute` | 140 | for the duration estimate |
| `max_stories` | 5 | upper limit; the word budget may allow fewer |
| `min_story_words` | 300 | depth per story |
| `host_a`, `host_b` | Maya, Arjun | host names |
| `model_writing` | true | false = template script |
| `revise` | true | one model revision pass for failing lines |
| `max_model_calls` | 24 | per run (writing, one "go deeper" pass per story, revision) |

## Known limitations

* The checks are lexical: they catch invented names, numbers, quotations, missing hedges and
  unsupported causal wording, not every subtle distortion of meaning. Each line's support is listed
  in the evidence report so a person can check it before publishing.
* A name written differently from the sources ("RBI" for "Reserve Bank of India") is rejected as
  unsupported; the prompt asks the model to use names as written.
* The template script (no model) is accurate but reads like a list of quotes, and is shorter than
  the target when material is thin; the episode says so rather than padding.
* Pronunciation notes are model suggestions, not verified.
* Model summaries (debate positions, the India summary) are labelled "as summarised by IntelPod" in
  the template and can never be quoted as a publisher's words.
* A company named like an ordinary word ("Apple") can pass the sentence-start name check if
  IntelPod's stored news uses the word in lower case more often than as a name.
