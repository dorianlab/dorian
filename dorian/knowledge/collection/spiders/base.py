"""
Base spider for Sphinx-documented Python libraries.

Subclasses only need to set class attributes (domain, version URL, etc.).
Override ``parse_versions`` for non-JSON version discovery (e.g. Seaborn dropdown).
Override ``parse_category_table`` for custom category navigation XPaths.
"""
from __future__ import annotations

from typing import Callable

import scrapy

from dorian.knowledge.collection import constants
from dorian.knowledge.collection.utils import (
    create_library_item,
    determine_page_type,
    fetch_class_title_with_url,
    fetch_documentation_url,
    get_scoped_attributes,
    get_scoped_methods,
    get_scoped_outputs,
    get_scoped_parameters,
)


class BaseDocSpider(scrapy.Spider):
    """Base spider for crawling Sphinx-documented Python library docs.

    Subclass contract:
    - Set ``name``, ``allowed_domains``, ``start_urls``, ``language``.
    - Set ``versions_url`` to the JSON endpoint listing versions.
    - Optionally set ``version_filter`` to restrict which versions to crawl.
    - Optionally set ``category_xpath`` for libraries that group API pages
      under category tables (e.g. pandas, numpy).
    - Override ``parse_versions`` only for non-JSON version discovery.
    """

    # ── Subclass config (override these) ─────────────────────────────
    language: str = "python"
    versions_url: str | None = None
    version_filter: Callable[[str, str], bool] | None = None
    category_xpath: str | None = None

    # ── Entry point ──────────────────────────────────────────────────

    def parse(self, response):
        """Discover versions. Override ``versions_url`` or ``parse_versions``."""
        if self.versions_url:
            yield response.follow(
                self.versions_url,
                callback=self.parse_versions,
                dont_filter=True,
            )
        else:
            # No version discovery — treat start URL as the sole version
            yield from self.parse_selected_version(response)

    # ── Version discovery ────────────────────────────────────────────

    def parse_versions(self, response):
        """Parse a JSON array of ``{version/name, url}`` objects.

        Override for libraries that don't expose a JSON versions endpoint.
        """
        data = response.json()
        for entry in data:
            version = entry.get("version") or entry.get("name", "")
            url = entry.get("url", "")
            if not version or not url:
                continue
            clean_version = version.split(" ")[0]
            if self.version_filter and not self.version_filter(version, url):
                continue
            yield response.follow(
                url,
                callback=self.parse_selected_version,
                meta={"version": clean_version},
                dont_filter=True,
            )

    # ── Navigate to API reference ────────────────────────────────────

    def parse_selected_version(self, response):
        """Follow the 'API' / 'Reference' link on the landing page."""
        doc_url = fetch_documentation_url(response, self.allowed_domains[0])
        if doc_url:
            if self.category_xpath:
                yield response.follow(
                    doc_url,
                    callback=self.parse_category_table,
                    meta=response.meta,
                    dont_filter=True,
                )
            else:
                yield response.follow(
                    doc_url,
                    callback=self.parse_documentation,
                    meta=response.meta,
                    dont_filter=True,
                )

    # ── Category navigation (optional) ───────────────────────────────

    def parse_category_table(self, response):
        """Follow links in a category table before parsing individual pages.

        Uses ``self.category_xpath`` to locate category links.
        """
        if not self.category_xpath:
            yield from self.parse_documentation(response)
            return
        for link in response.xpath(self.category_xpath):
            href = link.xpath("@href").get()
            if href:
                yield response.follow(
                    href,
                    callback=self.parse_documentation,
                    meta=response.meta,
                )

    # ── Documentation page (longtable with class/function links) ─────

    def parse_documentation(self, response):
        """Extract class/function page links from Sphinx longtable."""
        tables = response.xpath(
            constants.xPathConstants.PARAMTERS_PARENT_TABLE_LONGTABLE_CLASS
        )
        for table in tables:
            for xpath in constants.xPathConstants.PAREMETERS_TABLE_INSIDE_PARENT:
                links = table.xpath(xpath)
                if links:
                    for link in links:
                        yield response.follow(
                            link.get(),
                            callback=self.parse_class_page,
                            meta=response.meta,
                            dont_filter=True,
                        )
                    break

    # ── Individual class/function page ───────────────────────────────

    def parse_class_page(self, response):
        """Extract params, methods, outputs, attributes and yield LibraryItem."""
        name = fetch_class_title_with_url(response)
        item_type = determine_page_type(response)

        methods = get_scoped_methods(response, item_type)
        params = get_scoped_parameters(response, item_type)
        outputs = get_scoped_outputs(response, item_type)
        attributes = get_scoped_attributes(response, item_type)

        yield create_library_item(
            name=name,
            documentation=response.url,
            hyper_params=params,
            functions=methods,
            version=response.meta.get("version"),
            outputs=outputs,
            attributes=attributes,
            item_type=item_type,
        )
