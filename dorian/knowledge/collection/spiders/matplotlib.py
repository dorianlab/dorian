from dorian.knowledge.collection.spiders.base import BaseDocSpider


class MatplotlibSpider(BaseDocSpider):
    name = "matplotlib_spider"
    language = "python"
    allowed_domains = ["matplotlib.org"]
    start_urls = ["https://matplotlib.org/"]
    versions_url = "https://matplotlib.org/devdocs/_static/switcher.json"
    category_xpath = "//*[@class='toctree-wrapper compound']/ul/li/a"
