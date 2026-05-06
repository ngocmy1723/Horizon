"""onehack.st (Discourse) scraper implementation.

Thin subclass of :class:`LinuxDoScraper`. onehack.st runs Discourse, so the
fetch/parse logic is identical — only the source type, ID prefix, and log
name differ.
"""

from .linuxdo import LinuxDoScraper
from ..models import SourceType


class OneHackScraper(LinuxDoScraper):
    """Scraper for onehack.st (a Discourse-based forum)."""

    SOURCE_TYPE = SourceType.ONEHACK
    SOURCE_ID_PREFIX = "onehack"
    LOG_NAME = "onehack.st"
