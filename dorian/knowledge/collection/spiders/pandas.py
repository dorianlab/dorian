import json
from dorian.knowledge.collection import constants
from dorian.knowledge.collection.utils import utils
import scrapy
from dorian.knowledge.collection.items import HyperParameter, LibraryItem

class PandasSpider(scrapy.Spider):
    name = "pandas_spider"
    language = 'python'
    allowed_domains = ["pandas.pydata.org"]
    start_urls = ["https://pandas.pydata.org"]

    def parse(self, response):
        yield response.follow("https://pandas.pydata.org/versions.json", self.parseAllVersions)        

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
      
    
    def parseAllVersions(self,response):
        versions = json.loads(response.text)
        for version in versions:
            versionHref = version.get('url')
            currVersion = version.get('name')
            yield response.follow(versionHref,callback=self.parseSelectedVersion,meta={'version':currVersion})

    def parseSelectedVersion(self,response):
        yield response.follow(utils.fetchDocumentationUrl(response,self.allowed_domains[0]),self.getAllParentCategoryTable,meta=response.meta)

    def getAllParentCategoryTable(self,response):
        parentCatTable = response.xpath("//*[@class='toctree-wrapper compound']/ul/li/a")
        for category in parentCatTable:
            yield response.follow(category.xpath("@href").get(),self.parseDocumentation,meta=response.meta)

    def getCurrVer(self,verText):
        return verText.replace("Scikit-learn ","").replace(" documentation","").split(' ')[0]
