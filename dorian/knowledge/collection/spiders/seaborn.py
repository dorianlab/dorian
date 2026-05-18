import scrapy

from dorian.knowledge.collection.spiders.base import BaseDocSpider


class SeabornSpider(BaseDocSpider):
    """Seaborn uses dropdown links instead of a JSON versions endpoint."""

    name = "seaborn_spider"
    language = "python"
    allowed_domains = ["seaborn.pydata.org"]
    start_urls = ["https://seaborn.pydata.org/"]
    versions_url = None  # no JSON endpoint
    category_xpath = None

    def parse(self, response):
        """Override: scrape version links from the dropdown menu."""
        yield response.follow(
            "https://seaborn.pydata.org",
            callback=self.parse_versions,
            dont_filter=True,
        )

    def parse_versions(self, response):
        """Extract version links from dropdown + the current stable page."""
        version_links = response.xpath(
            '//*[@aria-labelledby="dropdownMenuLink"]/a'
        )
        stable_url = response.url
        stable_version = response.xpath("//*[@id='version']/text()").get()

        for link in version_links:
            href = link.xpath("@href").get()
            version = link.xpath("text()").get()
            yield response.follow(
                href,
                callback=self.parse_selected_version,
                meta={"version": version},
                dont_filter=True,
            )

        # Also crawl the stable landing page
        if stable_version:
            yield response.follow(
                stable_url,
                callback=self.parse_selected_version,
                meta={"version": stable_version},
                dont_filter=True,
            )
