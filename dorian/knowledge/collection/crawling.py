import sys
from twisted.internet import asyncioreactor
try:
    asyncioreactor.install()
except:
    pass

from scrapy.crawler import CrawlerProcess
from scrapy.settings import Settings
from twisted.internet import reactor

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
    
    if not hasattr(reactor, "_handleSignals"):
        reactor._handleSignals = lambda *args, **kwargs: None

    for spider in [PandasSpider, SklearnSpider]: #, PandasSpider, SklearnSpider, NumpySpider, MatplotlibSpider, SeabornSpider, PlotlySpider]:
        process.crawl(spider)
    process.start()


if __name__ == '__main__':
    main()
    