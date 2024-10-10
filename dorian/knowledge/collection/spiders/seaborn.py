import json
from dorian.knowledge.collection import constants
from dorian.knowledge.collection.utils import utils
import scrapy
from dorian.knowledge.collection.items import HyperParameter, LibraryItem

class SeabornSpider(scrapy.Spider):
    name = "seaborn_spider"
    language = 'python'
    allowed_domains = ["seaborn.pydata.org"]
    start_urls = ["https://seaborn.pydata.org/"]

    def parse(self, response):
        yield response.follow("https://seaborn.pydata.org",self.parseAllVersions)        

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

        versionsLinks = response.xpath('//*[@aria-labelledby="dropdownMenuLink"]/a')
        stableUrl=response.url
        stableVersion=response.xpath("//*[@id='version']/text()").get()
        for i in range(len(versionsLinks)+1):
            if (i<len(versionsLinks)):
                version = versionsLinks[i]
                versionHref = version.xpath('@href').get()
                currVersion = version.xpath('text()').get()
            else:
                versionHref=stableUrl
                currVersion=stableVersion
            yield response.follow(versionHref,callback=self.parseSelectedVersion,meta={'version':currVersion},dont_filter=True)

    def parseSelectedVersion(self,response):
        yield response.follow(utils.fetchDocumentationUrl(response,self.allowed_domains[0]),self.parseDocumentation,meta=response.meta,dont_filter=True)
