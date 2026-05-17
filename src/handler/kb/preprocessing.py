"""Email content preprocessing for efficient token usage."""

import re
from typing import Optional
from html import unescape
import html2text


def preprocess_email_for_classification(
    body: str, body_html: Optional[str] = None, max_length: int = 2000, level: int = 1
) -> str:
    """
    Clean and truncate email content for API efficiency.

    Args:
        body: Plain text email body
        body_html: HTML email body (optional)
        max_length: Maximum character length
        level: Classification level (1=lightweight, 2=full, 3=deep)

    Returns:
        Cleaned and truncated email content
    """
    # Use HTML if available and we're at level 2+
    if body_html and level >= 2:
        body = html_to_plain(body_html)

    if not body:
        return ""

    # Apply cleaning transformations
    body = remove_email_signatures(body)
    body = remove_long_urls(body)
    body = remove_quoted_replies(body)
    body = remove_unsubscribe_footers(body)
    body = remove_excessive_whitespace(body)
    body = decode_html_entities(body)

    # Adjust max length by level
    if level == 1:
        max_length = 1000  # Very short for lightweight classification
    elif level == 2:
        max_length = 2000  # Medium for full context
    else:
        max_length = 5000  # Longer for deep analysis

    # Truncate intelligently
    if len(body) > max_length:
        # Keep beginning and end, skip middle
        keep_start = int(max_length * 0.7)
        keep_end = int(max_length * 0.3)
        body = (
            body[:keep_start] + "\n\n[... content truncated ...]\n\n" + body[-keep_end:]
        )

    return body.strip()


def html_to_plain(html_content: str) -> str:
    """Convert HTML email to plain text."""
    h = html2text.HTML2Text()
    h.ignore_links = False
    h.ignore_images = True
    h.ignore_emphasis = False
    h.body_width = 0  # Don't wrap lines
    return h.handle(html_content)


def remove_email_signatures(text: str) -> str:
    """Remove common email signature patterns."""
    signature_patterns = [
        r"\n\s*--\s*\n.*",  # Standard -- signature separator
        r"\n\s*_{3,}\s*\n.*",  # Underscores
        r"(?i)\n\s*sent from my (iphone|android|ipad|mobile).*",
        r"(?i)\n\s*get outlook for.*",
        r"(?i)\n\s*best regards?\s*,?\s*\n.*",
        r"(?i)\n\s*(sincerely|cheers|thanks|regards)\s*,?\s*\n.*",
        r"(?i)\n\s*thank you\s*,?\s*\n.*",
    ]

    for pattern in signature_patterns:
        text = re.sub(pattern, "", text, flags=re.DOTALL)

    return text


def remove_long_urls(text: str, max_url_length: int = 50) -> str:
    """Replace long URLs with shortened version."""

    def shorten_url(match):
        url = match.group(0)
        if len(url) > max_url_length:
            # Extract domain
            domain_match = re.search(r"https?://([^/]+)", url)
            if domain_match:
                return f"[LINK: {domain_match.group(1)}]"
        return url

    url_pattern = r'https?://[^\s<>"{}|\\^`\[\]]+'
    return re.sub(url_pattern, shorten_url, text)


def remove_quoted_replies(text: str) -> str:
    """Remove quoted text from email replies."""
    lines = text.split("\n")
    clean_lines = []
    in_quote = False

    for line in lines:
        # Check for quote markers
        if line.strip().startswith(">") or line.strip().startswith("|"):
            in_quote = True
            continue

        # Check for reply headers
        if re.match(r"(?i)^(on .* wrote:|from:.*sent:|>)", line.strip()):
            in_quote = True
            continue

        # If we see a blank line after quotes, reset
        if in_quote and not line.strip():
            in_quote = False
            continue

        if not in_quote:
            clean_lines.append(line)

    return "\n".join(clean_lines)


def remove_unsubscribe_footers(text: str) -> str:
    """Remove unsubscribe and footer boilerplate."""
    footer_patterns = [
        r"(?i)\n.*unsubscribe.*$",
        r"(?i)\n.*opt[ -]?out.*$",
        r"(?i)\n.*update (your )?preferences.*$",
        r"(?i)\n.*view (this email )?in (your )?browser.*$",
        r"(?i)\n.*\d{4} .* all rights reserved.*$",
        r"(?i)\n.*privacy policy.*$",
    ]

    for pattern in footer_patterns:
        text = re.sub(pattern, "", text, flags=re.MULTILINE)

    return text


def remove_excessive_whitespace(text: str) -> str:
    """Normalize whitespace."""
    # Replace multiple spaces with single space
    text = re.sub(r" +", " ", text)

    # Replace more than 2 newlines with 2
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Remove leading/trailing whitespace from each line
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)

    return text


def decode_html_entities(text: str) -> str:
    """Decode HTML entities like &amp; &lt; etc."""
    return unescape(text)


def extract_keywords(text: str, max_keywords: int = 10) -> list:
    """Extract important keywords from text."""
    # Remove common stop words
    stop_words = {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "up",
        "about",
        "into",
        "through",
        "is",
        "are",
        "was",
        "were",
        "been",
        "be",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "should",
        "could",
        "may",
        "might",
        "this",
        "that",
        "these",
        "those",
        "i",
        "you",
        "he",
        "she",
        "it",
        "we",
        "they",
    }

    # Extract words
    words = re.findall(r"\b[a-z]{3,}\b", text.lower())

    # Filter and count
    word_freq = {}
    for word in words:
        if word not in stop_words:
            word_freq[word] = word_freq.get(word, 0) + 1

    # Sort by frequency and return top keywords
    sorted_words = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)
    return [word for word, freq in sorted_words[:max_keywords]]


def extract_domain(email: str) -> Optional[str]:
    """Extract domain from email address."""
    match = re.search(r"@([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", email)
    return match.group(1).lower() if match else None


def clean_subject(subject: str) -> str:
    """Clean email subject line."""
    if not subject:
        return ""

    # Remove Re:, Fwd:, etc.
    subject = re.sub(r"(?i)^(re|fwd|fw):\s*", "", subject)

    # Remove excessive spaces
    subject = re.sub(r"\s+", " ", subject)

    return subject.strip()


def estimate_token_count(text: str) -> int:
    """Rough estimate of token count (words * 1.3)."""
    words = len(text.split())
    return int(words * 1.3)
