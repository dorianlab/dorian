import json
from dorian.knowledge.collection import constants
from dorian.knowledge.collection.utils import utils
import scrapy
from dorian.knowledge.collection.items import HyperParameter, LibraryItem

class PlotlySpider(scrapy.Spider):
    name = "plotly_spider"
    language = 'python'
    allowed_domains = ["plotly.com"]
    start_urls = ["https://plotly.com/python/"]

    def parse(self, response):
        yield response.follow(utils.fetchDocumentationUrl(response,self.allowed_domains[0]),self.getAllParentCategoryTable,meta={'version':"stable"})

    def parseDocumentation(self,response):
        #Working mentioned in sklearn_spider
        parameters_tables=response.xpath(constants.xPathConstants.PARAMTERS_PARENT_TABLE_LONGTABLE_CLASS)
        for table in parameters_tables:
            for parameters_in_tables_xpaths in constants.xPathConstants.PAREMETERS_TABLE_INSIDE_PARENT:
                parameters_in_tables = table.xpath(parameters_in_tables_xpaths)
                if len(parameters_in_tables)>0:
                    for param_page_url in parameters_in_tables:
                        yield response.follow(param_page_url.get(),callback=self.parseClassPage,meta=response.meta,dont_filter=True)
                    break
        
    def parseClassPage(self,response):
        libName=utils.fetchClassTitleWithUrl(response)
        libDocumentation=response.url
        libFunctions=utils.getAllLibraryFunctions(response)
        libHyperParams=utils.getHyperParametersFromTable(response)

        libraryItem = utils.createLibraryItem(libName,libDocumentation,libHyperParams,libFunctions,response.meta.get('version'))

        yield libraryItem
    
    def getAllParentCategoryTable(self,response):
        parentCatTable = response.xpath("//*[@class='toctree-wrapper compound']/ul/li/a")
        for category in parentCatTable:
            yield response.follow(category.xpath("@href").get(),self.parseDocumentation,meta=response.meta)
