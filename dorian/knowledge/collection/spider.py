from dorian.collection.knowledge import constants
from dorian.collection.knowledge.utils import utils
from dorian.collection.knowledge.items import HyperParameter, LibraryItem
from scrapy import Spider
from dataclasses import dataclass
from typing import Callable

@dataclass
class Setup:
    name: str
    url: str
    get_version: Callable[[str], str]

def build(setup: Setup):
    def _parse(self, response):
        # TODO for the utils module, just keep functions separately and import them as necessary
        yield response.follow(utils.fetchDocumentationUrl(response, setup.url.replace('https://', '')), callback=self.getAllVersionLinks)

    return Spider(
        name=setup.name,
        start_urls=[setup.url],
        allowed_domains=[setup.url.replace('https://', '')],
        parse=_parse,
    )


get_version = lambda text: text.replace("Scikit-learn ","").replace(" documentation","")
SkleanSpider = build(Setup(name="sklearn_spider", url="https://scikit-learn.org", get_version=get_version))

def parseDocumentation(self, response):
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
    
def parseClassPage(self, response):
    libName=utils.fetchClassTitleWithUrl(response)
    libDocumentation=response.url
    libFunctions=utils.getAllLibraryFunctions(response)
    libHyperParams=utils.getHyperParametersFromTable(response)

    libraryItem = utils.createLibraryItem(libName,libDocumentation,libHyperParams,libFunctions,response.meta.get('version'))

    yield libraryItem
    
def getAllVersionLinks(self, response):
    otherVerEl = utils.getTagWithTextFromxPath(response,'Other versions')
    yield response.follow(otherVerEl.xpath('@href').get(),callback=self.parseAllVersions)
  

def parseAllVersions(self, response):
    allVersions=response.xpath("//ul[@class='simple']/li/p/a[contains(text(),'documentation')]")
    for version in allVersions:
    # version = allVersions[1]
        versionHref = version.xpath('@href')
        currVersion = self.getCurrVer(version.xpath('text()').get())
        yield response.follow(versionHref.get(),callback=self.parseSelectedVersion,meta={'version':currVersion},dont_filter = True)
        

def parseSelectedVersion(self, response):
    yield response.follow(utils.fetchDocumentationUrl(response,self.allowed_domains[0]),self.parseDocumentation,meta=response.meta,dont_filter=True)