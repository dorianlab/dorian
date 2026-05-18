from dorian.knowledge.collection.spiders.base import BaseDocSpider


class PandasSpider(BaseDocSpider):
    name = "pandas_spider"
    language = "python"
    allowed_domains = ["pandas.pydata.org"]
    start_urls = ["https://pandas.pydata.org"]
    versions_url = "https://pandas.pydata.org/versions.json"
    category_xpath = "//*[@class='toctree-wrapper compound']/ul/li/a"
