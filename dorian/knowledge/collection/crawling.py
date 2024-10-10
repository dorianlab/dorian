from scrapy.crawler import CrawlerProcess
from scrapy.settings import Settings

from dorian.knowledge.collection.spiders import (
    MatplotlibSpider,
    NumpySpider,
    PandasSpider,
    PlotlySpider,
    SklearnSpider,
    SeabornSpider
)

from dorian.knowledge.collection import settings


def main():
    crawler_settings = Settings()
    crawler_settings.setmodule(settings)
    process = CrawlerProcess(settings=crawler_settings)
    for spider in [SklearnSpider, PandasSpider]: #, NumpySpider, MatplotlibSpider, SeabornSpider, PlotlySpider]:
        process.crawl(spider)
    process.start()


if __name__ == '__main__':
    main()
    