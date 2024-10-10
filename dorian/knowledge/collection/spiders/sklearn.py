from dorian.knowledge.collection import constants
from dorian.knowledge.collection.utils import utils
import scrapy
from dorian.knowledge.collection.items import HyperParameter, LibraryItem

class SklearnSpider(scrapy.Spider):
    name = "sklearn_spider"
    language = 'python'
    allowed_domains = ["scikit-learn.org"]
    start_urls = ["https://scikit-learn.org"]

    def parse(self, response):
        yield response.follow(utils.fetchDocumentationUrl(response, self.allowed_domains[0]),callback=self.getAllVersionLinks)        

    def parseDocumentation(self,response):
        #Extracting parent table with class 'longtable' which is used in most libraries
        parameters_tables=response.xpath(constants.xPathConstants.PARAMTERS_PARENT_TABLE_LONGTABLE_CLASS)
        #Iterating the tables
        for table in parameters_tables:
            #Getting tables inside those category tables extracted above using possible xpaths from constant files
            for parameters_in_tables_xpaths in constants.xPathConstants.PAREMETERS_TABLE_INSIDE_PARENT:
                parameters_in_tables = table.xpath(parameters_in_tables_xpaths)
                if len(parameters_in_tables)>0:
                    #If parameters are found, they are iterated and finally, at the end xpath possibility loop break as params are found
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
        
    def getAllVersionLinks(self,response):
        otherVerEl = utils.getTagWithTextFromxPath(response,'Other versions')
        yield response.follow(otherVerEl.xpath('@href').get(),callback=self.parseAllVersions)
      
    
    def parseAllVersions(self,response):
        allVersions=response.xpath("//ul[@class='simple']/li/p/a[contains(text(),'documentation')]")
        for version in allVersions:
        # version = allVersions[1]
            versionHref = version.xpath('@href')
            currVersion = self.getCurrVer(version.xpath('text()').get())
            yield response.follow(versionHref.get(),callback=self.parseSelectedVersion,meta={'version':currVersion},dont_filter = True)
            

    def parseSelectedVersion(self,response):
        yield response.follow(utils.fetchDocumentationUrl(response,self.allowed_domains[0]),self.parseDocumentation,meta=response.meta,dont_filter=True)

    def getCurrVer(self,verText):
        return verText.replace("Scikit-learn ","").replace(" documentation","").split(' ')[0]
