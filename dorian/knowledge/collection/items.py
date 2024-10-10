# Define here the models for your scraped items
#
# See documentation in:
# https://docs.scrapy.org/en/latest/topics/items.html

from scrapy import Item, Field


class DsaScraperItem(Item):
    # define the fields for your item here like:
    # name = Field()
    pass


class LibraryItem(Item):
    documentationUrl=Field()
    name=Field()
    hyperParameters=Field()
    functions=Field()
    version=Field()


class HyperParameter(Item):
    type=Field()
    default=Field()


class Functions(Item):
    method=Field()
    args=Field()


# TODO could be positional or keyword arguments, positional do not have name
class FunctionArgs(Item):
    name=Field()
    type=Field()