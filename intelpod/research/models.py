"""Typed contracts for Phase 2 (internet research).

Flow for one selected event:

    EventBrief ──queries.build_queries──▶ ResearchQuery*  (10 query types, per provider)
    ResearchQuery ──SearchProvider.search──▶ SearchHit*   (metadata only: url, title, date, snippet)
    SearchHit* ──dedup + relevance pre-score──▶ candidates
    candidate ──pages.fetch_page──▶ PageInfo              (metadata + text kept in memory only)
    PageInfo ──evidence.extract──▶ EvidenceNote*/claims   (short verbatim extracts, attributed)
    everything ──dates / credibility / relevance──▶ ResearchResult
    ResearchResult* ──select──▶ ResearchPackage           (10-15 results + explicit limitations)

Only metadata, short extracts (<= ~300 characters each, a few per result) and IntelPod's own
notes are stored. Full article text never leaves memory.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

QueryType = Literal[
    "latest_facts", "recent_history", "political_views", "economic_views", "business_impact",
    "india_impact", "expert_analysis", "primary_sources", "criticism", "open_questions",
]
QUERY_TYPES: tuple[str, ...] = (
    "latest_facts", "recent_history", "political_views", "economic_views", "business_impact",
    "india_impact", "expert_analysis", "primary_sources", "criticism", "open_questions",
)
QUERY_TYPE_LABELS = {
    "latest_facts": "Latest facts", "recent_history": "Recent history",
    "political_views": "Political interpretations", "economic_views": "Economic interpretations",
    "business_impact": "Business and industry", "india_impact": "India implications",
    "expert_analysis": "Expert analysis", "primary_sources": "Primary sources",
    "criticism": "Criticism and counterarguments", "open_questions": "Open questions",
}

ContentType = Literal["reporting", "analysis", "opinion", "primary", "research", "unknown"]
CONTENT_TYPES: tuple[str, ...] = ("reporting", "analysis", "opinion", "primary", "research", "unknown")

# Where a result's publication date came from, best first.
ResearchDateSource = Literal["page_metadata", "feed", "api", "gdelt_seendate", "missing"]
# How a result's publication date relates to the event it is evidence for.
EventTiming = Literal["before_event", "during_event", "after_event", "undated"]
PageStatus = Literal[
    "fetched",            # page fetched and text extracted
    "metadata_only",      # fetched, but only metadata may be used (paywall, noai/noarchive, non-HTML)
    "paywalled",          # publisher marks the content as not free / login wall: metadata only
    "robots_disallowed",  # robots.txt forbids fetching: never fetched
    "blocked_host",       # private/local address or non-http(s) URL: never fetched
    "error",              # HTTP error, timeout, too large, unreadable
    "not_fetched",        # over the page budget, or fetching disabled
]


class EventBrief(BaseModel):
    """What research knows about one selected event (built from Phase 1 output)."""

    event_id: str
    run_id: str
    rank: int | None = None
    title: str
    headline: str | None = None          # publisher headline when `title` is model-written
    summary: str | None = None
    description: str | None = None
    topic: str
    subtopic: str | None = None
    countries: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    centroid: dict[str, float] = Field(default_factory=dict)  # top clustering terms -> weight
    first_article_at: datetime | None = None
    latest_article_at: datetime | None = None
    article_ids: list[str] = Field(default_factory=list)       # the event's own Phase 1 reports
    article_urls: list[str] = Field(default_factory=list)      # canonical URLs of those reports


class ResearchQuery(BaseModel):
    query_id: str                        # stable hash of (event, type, provider, text, window)
    event_id: str
    query_type: QueryType
    covers: list[QueryType] = Field(default_factory=list)  # other types a combined query serves
    provider: str
    text: str                            # provider-specific query string
    terms: list[str] = Field(default_factory=list)  # plain keywords (local providers match on these)
    start: datetime
    end: datetime
    origin: Literal["template", "model"] = "template"


class SearchHit(BaseModel):
    """One result as a provider returned it (metadata only)."""

    provider: str
    query_id: str
    query_type: QueryType
    url: str
    title: str
    publisher: str | None = None
    domain: str | None = None
    published_at: datetime | None = None
    date_source: ResearchDateSource = "missing"
    snippet: str | None = None           # provider-supplied summary/trail text, already short
    language: str | None = None
    source_country: str | None = None
    source_type: str | None = None       # news | official | international_org | think_tank | academic
    section: str | None = None           # provider section/category, e.g. "commentisfree", "opinion"
    from_event: bool = False             # one of the event's own Phase 1 reports


class DateCandidate(BaseModel):
    value: datetime | None
    source: ResearchDateSource


class Candidate(BaseModel):
    """Search hits merged by canonical URL (one per distinct page) before pages are fetched."""

    canonical_url: str
    url: str
    title: str
    publisher: str
    domain: str | None = None
    providers: list[str] = Field(default_factory=list)
    query_ids: list[str] = Field(default_factory=list)
    query_types: list[QueryType] = Field(default_factory=list)
    dates: list[DateCandidate] = Field(default_factory=list)
    snippet: str | None = None
    language: str | None = None
    source_country: str | None = None
    source_type: str | None = None
    section: str | None = None
    from_event: bool = False
    syndicated_with: list[str] = Field(default_factory=list)  # publishers carrying the same headline
    prescore: float = 0.0                # relevance from metadata only, used to pick pages to fetch


class SearchResponse(BaseModel):
    provider: str
    query_id: str
    status: Literal["ok", "empty", "rate_limited", "error", "skipped", "cached"]
    hits: list[SearchHit] = Field(default_factory=list)
    requests: int = 0                    # network requests made (0 for cache hits / local search)
    error: str | None = None


class PageInfo(BaseModel):
    """Result of fetching one page. `text` lives in memory for the current run only."""

    url: str
    final_url: str | None = None
    status: PageStatus
    http_status: int | None = None
    title: str | None = None
    description: str | None = None
    author: str | None = None
    sitename: str | None = None
    published_at: datetime | None = None
    modified_at: datetime | None = None
    date_found_in: str | None = None     # e.g. "json-ld datePublished", "meta article:published_time"
    schema_type: str | None = None       # e.g. "OpinionNewsArticle" (JSON-LD @type) or trafilatura pagetype
    accessible_for_free: bool | None = None
    word_count: int = 0
    outbound_primary_links: int = 0      # links to official/international/academic domains or documents
    has_corrections_link: bool = False
    text: str | None = Field(default=None, exclude=True)  # never serialised or stored
    error: str | None = None


class EvidenceNote(BaseModel):
    text: str = Field(max_length=400)
    kind: Literal["extract", "model_note"] = "extract"   # extract = verbatim sentence from the page
    source_field: Literal["page", "snippet", "title"] = "page"


class CredibilityComponent(BaseModel):
    name: str
    value: float | None = Field(default=None, ge=0, le=1)  # None = unknown (excluded from the score)
    weight: float = 1.0
    evidence: str = ""


class CredibilityAssessment(BaseModel):
    score: float | None = Field(default=None, ge=0, le=1)  # weighted mean of known components
    components: list[CredibilityComponent] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)       # components that could not be assessed
    flags: list[str] = Field(default_factory=list)         # e.g. syndicated_copy, interested_party
    summary: str = ""


class ResearchResult(BaseModel):
    """The Module 3 research-result schema, plus the checks IntelPod applied."""

    result_id: str                       # stable hash of (event_id, canonical_url)
    event_id: str
    title: str
    url: str                             # the URL the provider returned (http/https only)
    canonical_url: str
    publisher: str
    domain: str | None = None
    published_at: datetime | None = None
    date_source: ResearchDateSource = "missing"
    date_notes: list[str] = Field(default_factory=list)    # conflicts, rejected dates, approximations
    event_date: datetime | None = None   # when the event's first report appeared
    event_timing: EventTiming = "undated"
    in_window: bool = True               # published inside the research window
    content_type: ContentType = "unknown"
    content_type_evidence: str = ""
    query_types: list[QueryType] = Field(default_factory=list)
    providers: list[str] = Field(default_factory=list)
    relevance_score: float = Field(default=0, ge=0, le=1)
    relevance_evidence: str = ""
    credibility_assessment: CredibilityAssessment = Field(default_factory=CredibilityAssessment)
    editorial_metadata: dict = Field(default_factory=lambda: {"status": "not_assessed (Phase 3)"})
    key_claims: list[str] = Field(default_factory=list)    # attributed statements, verbatim or grounded
    evidence_notes: list[EvidenceNote] = Field(default_factory=list)
    page_status: PageStatus = "not_fetched"
    language: str | None = None
    source_type: str | None = None
    from_event: bool = False             # one of the event's own Phase 1 reports
    syndicated_with: list[str] = Field(default_factory=list)  # other publishers carrying the same headline
    selected: bool = False
    selection_reason: str = ""
    retrieved_at: datetime


class QueryStatus(BaseModel):
    query_id: str
    query_type: QueryType
    covers: list[QueryType] = Field(default_factory=list)
    provider: str
    text: str
    status: str
    hits: int = 0
    error: str | None = None


class ResearchPackage(BaseModel):
    """Everything research produced for one event in one run."""

    run_id: str
    event_id: str
    event_title: str
    status: Literal["completed", "partial", "failed", "skipped"]
    window_start: datetime
    window_end: datetime
    event_date: datetime | None = None
    queries: list[QueryStatus] = Field(default_factory=list)
    result_ids: list[str] = Field(default_factory=list)          # selected, in display order
    candidate_count: int = 0
    coverage: dict[str, int] = Field(default_factory=dict)       # query type -> selected results
    content_mix: dict[str, int] = Field(default_factory=dict)    # content type -> selected results
    limitations: list[str] = Field(default_factory=list)
    stats: dict = Field(default_factory=dict)
    created_at: datetime
