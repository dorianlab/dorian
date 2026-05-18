from dorian.knowledge.collection.spiders.base import BaseDocSpider


class NumpySpider(BaseDocSpider):
    name = "numpy_spider"
    language = "python"
    allowed_domains = ["numpy.org"]
    start_urls = ["https://numpy.org/"]
    versions_url = "https://numpy.org/doc/_static/versions.json"
    category_xpath = (
        "//*[@class='toctree-wrapper compound']/ul/li/a"
        "/following-sibling::*/li/a"
    )
