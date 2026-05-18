from dorian.knowledge.collection.spiders.base import BaseDocSpider
from dorian.knowledge.collection.utils import fetch_documentation_url


class PlotlySpider(BaseDocSpider):
    """Plotly has no version listing — crawls stable only."""

    name = "plotly_spider"
    language = "python"
    allowed_domains = ["plotly.com"]
    start_urls = ["https://plotly.com/python/"]
    versions_url = None
    category_xpath = "//*[@class='toctree-wrapper compound']/ul/li/a"

    def parse(self, response):
        """Override: go straight to API reference with 'stable' version."""
        doc_url = fetch_documentation_url(response, self.allowed_domains[0])
        if doc_url:
            yield response.follow(
                doc_url,
                callback=self.parse_category_table,
                meta={"version": "stable"},
            )
