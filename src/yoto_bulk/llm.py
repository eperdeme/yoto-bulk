"""OpenAI-assisted metadata and grouping helpers."""

from __future__ import annotations

import json
import logging
from typing import Any

from openai import OpenAI
from openai import APIError, APIConnectionError, RateLimitError, AuthenticationError
from pydantic import BaseModel, Field
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# Ensure these imports match your actual project structure
from yoto_bulk.config import Settings
from yoto_bulk.models import YotoBook

logger = logging.getLogger(__name__)

# ---------------------------------------------------------
# Pydantic Schemas for OpenAI Structured Outputs
# ---------------------------------------------------------
class Series(BaseModel):
    series_name: str = Field(description="The inferred official name of the series.")
    titles: list[str] = Field(description="The books that belong to this series, in chronological reading order if possible.")

class SeriesGrouping(BaseModel):
    series: list[Series] = Field(
        description="A list of matched series and their corresponding book titles."
    )
    unmatched: list[str] = Field(
        description="A list of standalone book titles that do not belong to any series."
    )


class SeriesEntry(BaseModel):
    title: str = Field(description="The canonical title of the book.")
    owned: bool = Field(description="True if the user already owns this title.")


class CompleteSeries(BaseModel):
    series_name: str = Field(description="The official name of the series.")
    titles: list[SeriesEntry] = Field(description="The complete list of titles in chronological reading order.")


class SeriesCompletion(BaseModel):
    series: list[CompleteSeries] = Field(description="Each series with its full canonical title list.")

# ---------------------------------------------------------
# Main LLM Service
# ---------------------------------------------------------
class LlmService:
    def __init__(self, settings: Settings) -> None:
        if not settings.openai_api_key:
            raise ValueError("Missing YOTO_BULK_OPENAI_API_KEY")
        self.settings = settings
        self.client = OpenAI(api_key=settings.openai_api_key)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((RateLimitError, APIConnectionError)),
        reraise=True,
    )
    def guess_author(self, title: str) -> str:
        """Asks the LLM to guess the real-world author of a single audiobook."""
        cleaned_title = title.strip()
        if not cleaned_title:
            raise ValueError("Cannot guess an author for an empty title.")

        system_prompt = (
            "You are an expert children's librarian and audiobook bibliographer. "
            "Return only the most likely author's name for the given children's audiobook title."
        )
        user_prompt = (
            f"Who is the author of the children's audiobook '{cleaned_title}'? "
            "Return ONLY the author's name as a string."
        )

        try:
            response = self.client.chat.completions.create(
                model=self.settings.openai_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
            )
            
            content = response.choices[0].message.content or ""
            author = " ".join(content.strip().split())
            
            if not author:
                raise ValueError(f"OpenAI returned an empty author for title '{cleaned_title}'.")
            
            if "\n" in content.strip() or len(author) > 120:
                raise ValueError(f"OpenAI returned an invalid author string for title '{cleaned_title}': {content!r}")

            lowered = author.lower()
            invalid_markers = {
                "unknown", "not sure", "i don't know", "i do not know", "n/a", "none",
            }
            if lowered in invalid_markers or "cannot determine" in lowered:
                raise ValueError(f"OpenAI could not determine a valid author for title '{cleaned_title}'.")

            return author

        except AuthenticationError:
            logger.error("OpenAI API key is invalid or expired")
            raise
        except RateLimitError:
            logger.warning("OpenAI rate limit hit for '%s', will retry", cleaned_title)
            raise
        except APIConnectionError:
            logger.warning("OpenAI connection error for '%s', will retry", cleaned_title)
            raise
        except APIError as e:
            logger.error("OpenAI API Error while guessing author for '%s': %s", cleaned_title, e)
            raise

    def guess_authors_batch(self, titles: list[str]) -> dict[str, str]:
        """Guess authors for multiple titles in a single LLM call.

        Returns a mapping of title -> guessed author.  Titles that the LLM
        cannot resolve are omitted from the result.
        """
        if not titles:
            return {}

        # Deduplicate while preserving order
        unique_titles = list(dict.fromkeys(t.strip() for t in titles if t.strip()))
        if not unique_titles:
            return {}

        system_prompt = (
            "You are an expert children's librarian and audiobook bibliographer. "
            "For each children's audiobook title provided, return the most likely author."
        )
        numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(unique_titles))
        user_prompt = (
            f"For each children's audiobook title below, respond with a JSON object "
            f"mapping the EXACT title string to the author's name.\n\n{numbered}"
        )

        try:
            response = self.client.chat.completions.create(
                model=self.settings.openai_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
            )
            content = response.choices[0].message.content or "{}"
            raw: dict[str, Any] = json.loads(content)
            return {k: str(v) for k, v in raw.items() if isinstance(v, str) and v.strip()}
        except AuthenticationError:
            logger.error("OpenAI API key is invalid or expired")
            raise
        except (RateLimitError, APIConnectionError) as e:
            logger.warning("OpenAI transient error during batch author guessing: %s", e)
            raise
        except APIError as e:
            logger.error("OpenAI API Error during batch author guessing: %s", e)
            raise

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((RateLimitError, APIConnectionError)),
        reraise=True,
    )
    def suggest_series(self, books: list[YotoBook]) -> dict[str, Any]:
        """Groups a list of YotoBooks into logical series using OpenAI Structured Outputs."""
        if not books:
            raise ValueError("Cannot suggest series for an empty library selection.")

        system_prompt = (
            "You are an expert children's librarian and audiobook archivist. "
            "You have extensive knowledge of children's literature, authors, illustrators, and book universes. "
            "Use all provided metadata, especially author names, to disambiguate similarly named titles and infer the most likely official series."
        )

        library_entries = [
            {
                "title": book.title,
                "author": book.authors,
                "existing_series": book.series,
                "sequence_number": book.sequence_number,
            }
            for book in books
        ]

        user_prompt = f"""
            Analyze the following Yoto audiobook library entries and group them into their official series.

            CRITICAL INSTRUCTIONS:
            1. DO NOT just group books by matching title prefixes. Use your knowledge of children's literature to group books that belong to the same real-world series or fictional universe.
            2. Treat author metadata as a strong signal. If an author writes a known series, check if their titles belong together (e.g., group 'Knock Three Times' with 'The Wizards of Once' because they are both by Cressida Cowell).
            3. If a book belongs to a known, real-world series, put it in the 'series' output under the official series name, EVEN IF it is the only book from that series in this list.
            4. Order books chronologically within their series if you know the reading order. Use sequence_number only as a hint.
            5. The 'unmatched' array is strictly for true standalone books. If a book has sequels or prequels in the real world, it MUST go into a series.

            Library entries to process:
            {json.dumps(library_entries, indent=2, ensure_ascii=False)}
        """

        try:
            response = self.client.beta.chat.completions.parse(
                model=self.settings.openai_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                response_format=SeriesGrouping, 
                temperature=0.1
            )
            
            parsed_result = response.choices[0].message.parsed
            
            if not parsed_result:
                raise ValueError("Received empty or invalid response from OpenAI.")
                
            series_dict = {s.series_name: s.titles for s in parsed_result.series}
            
            return {
                "series": series_dict,
                "unmatched": parsed_result.unmatched
            }

        except AuthenticationError:
            logger.error("OpenAI API key is invalid or expired")
            raise
        except RateLimitError:
            logger.warning("OpenAI rate limit hit during series suggestion, will retry")
            raise
        except APIConnectionError:
            logger.warning("OpenAI connection error during series suggestion, will retry")
            raise
        except APIError as e:
            logger.error("OpenAI API Error during series suggestion: %s", e)
            raise

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((RateLimitError, APIConnectionError)),
        reraise=True,
    )
    def find_missing_in_series(self, series_mapping: dict[str, list[str]]) -> dict[str, Any]:
        """For each series, return the complete canonical title list marking owned vs missing."""
        if not series_mapping:
            raise ValueError("No series provided.")

        system_prompt = (
            "You are an expert children's librarian with encyclopaedic knowledge of "
            "children's audiobook series. You know which titles have been released as "
            "audiobooks (including on platforms like Audible, Yoto, Tonies, and Spotify) "
            "versus those that exist only in print."
        )

        payload = [
            {"series_name": name, "owned_titles": titles}
            for name, titles in series_mapping.items()
        ]

        user_prompt = f"""For each series below, return the list of titles that are available as AUDIOBOOKS in chronological reading order.
For every title, set `owned` to true if it appears in the user's owned_titles list, or false if it is missing from their library.

IMPORTANT:
- ONLY include titles that have been published as audiobooks. Exclude titles that are only available in print or ebook format.
- This is for a children's audio content platform (Yoto), so the user can only play audio content.
- Only mark a title as owned if it clearly matches one of the user's owned_titles (allow minor differences in punctuation or subtitle formatting).
- Do NOT invent titles. Only include real, published audiobooks.
- If you are unsure whether a title has an audiobook edition, err on the side of excluding it.

Series to complete:
{json.dumps(payload, indent=2, ensure_ascii=False)}
"""

        try:
            response = self.client.beta.chat.completions.parse(
                model=self.settings.openai_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format=SeriesCompletion,
                temperature=0.1,
            )

            parsed = response.choices[0].message.parsed
            if not parsed:
                raise ValueError("Received empty response from OpenAI.")

            result: dict[str, Any] = {"series": {}}
            for s in parsed.series:
                owned = [e.title for e in s.titles if e.owned]
                missing = [e.title for e in s.titles if not e.owned]
                result["series"][s.series_name] = {
                    "total": len(s.titles),
                    "owned": owned,
                    "missing": missing,
                }
            return result

        except AuthenticationError:
            logger.error("OpenAI API key is invalid or expired")
            raise
        except RateLimitError:
            logger.warning("OpenAI rate limit hit during series completion, will retry")
            raise
        except APIConnectionError:
            logger.warning("OpenAI connection error during series completion, will retry")
            raise
        except APIError as e:
            logger.error("OpenAI API Error during series completion: %s", e)
            raise

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((RateLimitError, APIConnectionError)),
        reraise=True,
    )
    def generate_description(self, title: str, author: str | None = None) -> str:
        """Generate a short book description suitable for the Yoto app.

        Returns a concise description (1-3 sentences, ≤500 chars).
        """
        cleaned_title = title.strip()
        if not cleaned_title:
            raise ValueError("Cannot generate a description for an empty title.")

        author_hint = f" by {author}" if author else ""
        system_prompt = (
            "You are an expert children's librarian. "
            "Write a short, engaging description for the given children's audiobook. "
            "The description should be 1-3 sentences, suitable for parents browsing a library. "
            "Do NOT include the title or author in the description. "
            "Return ONLY the description text, nothing else."
        )
        user_prompt = (
            f"Write a short description for the children's audiobook "
            f"'{cleaned_title}'{author_hint}."
        )

        try:
            response = self.client.chat.completions.create(
                model=self.settings.openai_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
            )

            content = response.choices[0].message.content or ""
            description = content.strip()

            if not description:
                raise ValueError(f"OpenAI returned an empty description for '{cleaned_title}'.")

            return description

        except AuthenticationError:
            logger.error("OpenAI API key is invalid or expired")
            raise
        except RateLimitError:
            logger.warning("OpenAI rate limit hit for '%s', will retry", cleaned_title)
            raise
        except APIConnectionError:
            logger.warning("OpenAI connection error for '%s', will retry", cleaned_title)
            raise
        except APIError as e:
            logger.error("OpenAI API Error while generating description for '%s': %s", cleaned_title, e)
            raise