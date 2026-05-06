"""AI prompts for content analysis and summarization."""

TOPIC_DEDUP_SYSTEM = """You are a news deduplication assistant. Identify groups of news items that cover the exact same real-world event, release, or announcement.

Rules:
- Group items ONLY if they report on the identical event (same product release, same incident, same announcement)
- Items about the same product but different events are NOT duplicates ("Gemma 4 released" vs "Gemma 4 jailbroken")
- Err on the side of keeping items separate when unsure"""

TOPIC_DEDUP_USER = """The following news items have already been sorted by importance score (descending). Identify which items are duplicates of each other.

{items}

Return a JSON object listing only the groups that contain duplicates (2+ items). Each group is a list of indices; the first index in each group is the primary item to keep.

Respond with valid JSON only:
{{
  "duplicates": [[<primary_idx>, <dup_idx>, ...], ...]
}}

If there are no duplicates at all, return: {{"duplicates": []}}"""

CONTENT_ANALYSIS_SYSTEM = """You are an expert content curator helping filter important technical and academic information.

Score content on a 0-10 scale based on importance and relevance:

**9-10: Groundbreaking** - Major breakthroughs, paradigm shifts, or highly significant announcements
- New major version releases of widely-used technologies
- Significant research breakthroughs
- Important industry-changing announcements

**7-8: High Value** - Important developments worth immediate attention
- Interesting technical deep-dives
- Novel approaches to known problems
- Insightful analysis or commentary
- Valuable tools or libraries

**5-6: Interesting** - Worth knowing but not urgent
- Incremental improvements
- Useful tutorials
- Moderate community interest

**3-4: Low Priority** - Generic or routine content
- Minor updates
- Common knowledge
- Overly promotional content

**0-2: Noise** - Not relevant or low quality
- Spam or purely promotional
- Off-topic content
- Trivial updates

Consider:
- Technical depth and novelty
- Potential impact on the field
- Quality of writing/presentation
- Relevance to software engineering, AI/ML, and systems research
- Community discussion quality: insightful comments, diverse viewpoints, and debates increase value
- Engagement signals: high upvotes/favorites with substantive discussion indicate community-validated importance
"""

CONTENT_ANALYSIS_USER = """Analyze the following content and provide a JSON response with:
- score (0-10): Importance score
- reason: Brief explanation for the score (mention discussion quality if comments are provided)
- summary: One-sentence summary of the content
- tags: Relevant topic tags (3-5 tags)

Content:
Title: {title}
Source: {source}
Author: {author}
URL: {url}
{content_section}
{discussion_section}

Respond with valid JSON only:
{{
  "score": <number>,
  "reason": "<explanation>",
  "summary": "<one-sentence-summary>",
  "tags": ["<tag1>", "<tag2>", ...]
}}"""

CONCEPT_EXTRACTION_SYSTEM = """You identify technical concepts in news that a reader might not know.
Given a news item, return 1-3 search queries for concepts that need explanation.
Focus on: specific technologies, protocols, algorithms, tools, or projects that are not widely known.
Do NOT return queries for well-known things (e.g. "Python", "Linux", "Google").
If the news is self-explanatory, return an empty list."""

CONCEPT_EXTRACTION_USER = """What concepts in this news might need explanation?

Title: {title}
Summary: {summary}
Tags: {tags}
Content: {content}

Respond with valid JSON only:
{{
  "queries": ["<search query 1>", "<search query 2>"]
}}"""

# Metadata for each supported output language.
# Used to build the bilingual/multilingual enrichment prompts dynamically.
LANGUAGE_META: dict[str, dict[str, str]] = {
    "en": {
        "name": "English",
        "native": "English",
        "rule": "MUST be written in English.",
        "title_hint": "short headline in English, ≤15 words",
        "sentence_hint": "1-2 sentences in English",
        "background_hint": "2-4 sentences in English, or empty string",
        "discussion_hint": "1-3 sentences in English, or empty string",
    },
    "zh": {
        "name": "Simplified Chinese",
        "native": "简体中文",
        "rule": (
            "MUST be written in Simplified Chinese (简体中文). 绝对不能用英文写此字段。"
            "Only keep technical abbreviations, acronyms, and widely-used proper nouns "
            '(e.g. "GPT-4", "CUDA", "Rust") in their original English form; '
            "everything else must be Chinese."
        ),
        "title_hint": "用中文写一个简短标题，不超过15个词",
        "sentence_hint": "用中文写1-2句话",
        "background_hint": "用中文写2-4句话，或空字符串",
        "discussion_hint": "用中文写1-3句话，或空字符串",
    },
    "vi": {
        "name": "Vietnamese",
        "native": "Tiếng Việt",
        "rule": (
            "MUST be written in Vietnamese (Tiếng Việt) with full diacritics. "
            "Tuyệt đối không viết trường này bằng tiếng Anh. "
            "Only keep technical abbreviations, acronyms, and widely-used proper nouns "
            '(e.g. "GPT-4", "CUDA", "Rust") in their original English form; '
            "everything else must be written in Vietnamese."
        ),
        "title_hint": "viết tiêu đề ngắn bằng tiếng Việt, không quá 15 từ",
        "sentence_hint": "viết 1-2 câu bằng tiếng Việt",
        "background_hint": "viết 2-4 câu bằng tiếng Việt, hoặc chuỗi rỗng",
        "discussion_hint": "viết 1-3 câu bằng tiếng Việt, hoặc chuỗi rỗng",
    },
}

_ENRICHMENT_FIELDS = (
    "title",
    "whats_new",
    "why_it_matters",
    "key_details",
    "background",
    "community_discussion",
)


def _language_meta(lang: str) -> dict[str, str]:
    """Return metadata for ``lang`` falling back to English for unknown codes."""
    return LANGUAGE_META.get(lang, LANGUAGE_META["en"])


def build_enrichment_system_prompt(languages: list[str]) -> str:
    """Build the system prompt for content enrichment across the given languages."""
    if not languages:
        languages = ["en"]

    key_lines = []
    for field in _ENRICHMENT_FIELDS:
        variants = " / ".join(f"{field}_{lang}" for lang in languages)
        key_lines.append(f"- {variants}")
    key_section = "\n".join(key_lines)

    rule_lines = []
    for lang in languages:
        meta = _language_meta(lang)
        rule_lines.append(f"- All *_{lang} fields {meta['rule']}")
    rule_section = "\n".join(rule_lines)

    lang_list = ", ".join(
        f"{_language_meta(lang)['name']} ({_language_meta(lang)['native']})"
        for lang in languages
    )

    return f"""You are a knowledgeable technical writer who helps readers understand important news in context.

Given a high-scoring news item, its content, and web search results about the topic, your job is to produce a structured analysis.

Provide EACH text field in ALL of the following languages: {lang_list}. Use the following key naming convention:
{key_section}

Field definitions:
0. **title** (one short phrase, ≤15 words): A clear, accurate headline for the news item.

1. **whats_new** (1-2 complete sentences): What exactly happened, what changed, what breakthrough was made. Be specific — mention names, versions, numbers, dates when available.

2. **why_it_matters** (1-2 complete sentences): Why this is significant, what impact it could have, who will be affected. Connect to the broader ecosystem or industry trends.

3. **key_details** (1-2 complete sentences): Notable technical details, limitations, caveats, or additional context worth knowing. Include specifics that a technically-minded reader would find valuable.

4. **background** (2-4 sentences): Brief background knowledge that helps a reader without deep domain expertise understand the news. Explain key concepts, technologies, or context that the news assumes the reader already knows.

5. **community_discussion** (1-3 sentences): If community comments are provided, summarize the overall sentiment and key viewpoints from the discussion — agreements, disagreements, concerns, additional insights, or notable counterarguments. If no comments are provided, return an empty string.

**CRITICAL — Language rules (MUST follow):**
{rule_section}

Guidelines:
- EVERY field (except community_discussion when no comments exist) must contain at least one complete sentence — no field may be empty or contain just a phrase
- Base your explanation on the provided content and web search results — do NOT fabricate information
- ONLY explain concepts and terms that are explicitly mentioned in the title, summary, or content
- Use the web search results to ensure accuracy, especially for recent projects, tools, or events
- If the news is self-explanatory and needs no background, return an empty string for every background field
- For **sources**: pick 1-3 URLs from the Web Search Results that you actually relied on for the background fields. Only use URLs that appear verbatim in the search results above — do not invent or modify URLs.
"""


def build_enrichment_user_template(languages: list[str]) -> str:
    """Build the user prompt template for enrichment across the given languages.

    The returned template still contains the standard ``{title}``, ``{url}``, ... placeholders
    expected by :class:`ContentEnricher`, so callers can ``.format(...)`` it as before.
    """
    if not languages:
        languages = ["en"]

    field_to_hint = {
        "title": "title_hint",
        "whats_new": "sentence_hint",
        "why_it_matters": "sentence_hint",
        "key_details": "sentence_hint",
        "background": "background_hint",
        "community_discussion": "discussion_hint",
    }

    json_lines: list[str] = []
    for field in _ENRICHMENT_FIELDS:
        for lang in languages:
            meta = _language_meta(lang)
            hint = meta[field_to_hint[field]]
            json_lines.append(f'  "{field}_{lang}": "<{hint}>",')
    json_lines.append('  "sources": ["<url from search results>", "..."]')
    json_body = "{{\n" + "\n".join(json_lines) + "\n}}"

    rule_summary = "; ".join(
        f"each _{lang} field must be in {_language_meta(lang)['name']}"
        for lang in languages
    )

    return (
        "Provide a structured multilingual analysis for the following news item.\n\n"
        "**News Item:**\n"
        "- Title: {title}\n"
        "- URL: {url}\n"
        "- One-line summary: {summary}\n"
        "- Score: {score}/10\n"
        "- Reason: {reason}\n"
        "- Tags: {tags}\n\n"
        "**Content:**\n"
        "{content}\n"
        "{comments_section}\n\n"
        "**Web Search Results (for grounding):**\n"
        "{web_context}\n\n"
        "Respond with valid JSON only. " + rule_summary + ". "
        "Every field MUST be at least one complete sentence (except community_discussion fields when no comments exist):\n"
        + json_body
    )


# Backward-compatible defaults: bilingual English + Simplified Chinese.
CONTENT_ENRICHMENT_SYSTEM = build_enrichment_system_prompt(["en", "zh"])
CONTENT_ENRICHMENT_USER = build_enrichment_user_template(["en", "zh"])
