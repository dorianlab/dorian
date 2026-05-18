from dorian.knowledge.collection.spiders.base import BaseDocSpider


class SklearnSpider(BaseDocSpider):
    name = "sklearn_spider"
    language = "python"
    allowed_domains = ["scikit-learn.org"]
    start_urls = ["https://scikit-learn.org"]
    versions_url = "https://scikit-learn.org/dev/_static/versions.json"
    version_filter = staticmethod(
        lambda v, url: "stable" in v.lower() or "stable" in url
    )
    category_xpath = None
