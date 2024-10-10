from dorian.knowledge.collection.constants import utilConstants, xPathConstants
from dorian.knowledge.collection.items import HyperParameter, LibraryItem, FunctionArgs, Functions
import re


class utils:
    def fetchClassTitleWithUrl(response):
        url = response.url
        urlSlash = url.rfind("/")
        titleEnd = url.find(".html")
        return url[urlSlash+1:titleEnd]
    
    def fetchDocumentationUrl(response,domain):
        possibledDocumentationTexts = ['API','Reference']
        for docText in possibledDocumentationTexts:
            htmlTag = utils.getTagWithTextFromxPath(response,docText)
            if len(htmlTag)>0:
                url = htmlTag.xpath(xPathConstants.HREF).get()
                if url.find("http")!=-1 and url.find(domain)!=-1:
                    return htmlTag.xpath(xPathConstants.HREF).get()
                elif url.find("http")==-1:
                    return htmlTag.xpath(xPathConstants.HREF).get()


    def getTagWithTextFromxPath(response,text):
        return response.xpath('//a[contains(text(),"'+text+'")]')
    
    def createHyperParameter(type,defaultVal):
        hyperParameter = HyperParameter()
        hyperParameter["type"]=type
        hyperParameter["default"]=defaultVal
        return dict(hyperParameter)
    
    def parseParameterTable(response,elementContainsParameterTextxPath):
        #SIMILAR LOGIC TO getHyperParametersFromTable func
        resultList=[]
        elementContainsParameterText=response.xpath(elementContainsParameterTextxPath)
        parameterTableDetails = elementContainsParameterText.xpath(xPathConstants.FOLLOWING_SIBLING_ALL+'//strong')
        if parameterTableDetails is not None and len(elementContainsParameterText)==1:
            parameterTableDetails = elementContainsParameterText.xpath(xPathConstants.FOLLOWING_SIBLING_ALL+'[1]//strong')
            for parameter in parameterTableDetails:
                paramName = utils.__extractParamName(parameter.xpath(xPathConstants.TEXT).get())
                if(paramName.find(">")==0):
                    continue
                paramObj = utils._extractParamObj(paramName,parameter)
                paramValType=utils.__getParamTypeAndValFromObj(parameter,paramObj,paramName)
                if paramName is not None:
                    result={}
                    result["paramName"]=paramName
                    result["paramType"]=paramValType["paramType"]
                    result["paramVal"]=paramValType["paramVal"]
                    resultList.append(result)
        return resultList
    
    def getHyperParametersFromTable(response):
        hyperParameters={}
        #GET TABLE FOR PARAMETERS FROM PAGE BY FINDING 'PARAMETER' TEXT
        parameterTable = response.xpath(xPathConstants.CONTAINS_PARAMETER_TEXT)
        #GET ADJACENT TABLE OF 'PARAMETER' TEXT TO GET THE DETAILS
        parameterTableDetails = parameterTable.xpath(xPathConstants.FOLLOWING_SIBLING_ALL+'//strong')
        if parameterTableDetails is None:
            return hyperParameters
            #GET THE FIRST 'PARAMETER' TABLE FOUND AND LOOP ALL PARAMS WITH 'STRONG' ATTR (usually all params)
        parameterTableDetails = parameterTable.xpath(xPathConstants.FOLLOWING_SIBLING_ALL+'[1]//strong')
        for parameter in parameterTableDetails:
            paramName = utils.__extractParamName(parameter.xpath(xPathConstants.TEXT).get())
            paramObj = utils._extractParamObj(paramName,parameter)
            paramValType=utils.__getParamTypeAndValFromObj(parameter,paramObj,paramName)
            if paramName is not None:
                hyperParameters[paramName] = utils.createHyperParameter(paramValType["paramType"],paramValType["paramVal"])
        return hyperParameters
    

    def _extractParamObj(paramName,parameterDetail):
        siblingText = parameterDetail.xpath(xPathConstants.FOLLOWING_SIBLING_TEXT).get()
        siblingText = utils.__removeSpecialCharactersAndSpaces(siblingText)
        #Usually paramObj is a sibling of paramName. if this is the case, extract from there
        if siblingText is not None and siblingText not in utilConstants.PARAM_OBJ_GARBAGE:
            return siblingText
        #If not found in sibling / sibling has garbage, must be inside same parent tag, go to parent tag and extract all text
        AllElsUnderParentWithText = parameterDetail.xpath(xPathConstants.PARENT+'//'+xPathConstants.TEXT)
        parentText=""
        #concat all texts in this parent tag
        for elem in AllElsUnderParentWithText:
            #Making sure to skip the paramName itself, to get the value and type as part of paramObj as needed
            if paramName==elem.get():continue
            parentText+=elem.get()
        #Removing starting spaces and starting special characters for valid values
        parentText = utils.__removeSpecialCharactersAndSpaces(parentText)
        if parentText is not None and parentText!="":
            return parentText
        return None
    
    def __removeSpecialCharactersAndSpaces(text):
        if text is None: return None
        text=text.strip()
        startingCharsToRemove=[":",",",")","-","–"]
        endingCharsToRemove=[":",",","(","–","-"]
        restart = True
        while restart:
            restart = False
            for chars in startingCharsToRemove:   
                if text.find(chars)==0:
                    text=text[1:len(text)]
                    text=text.strip()
                    restart=True
                    break
        restart=True
        while restart:
            restart = False
            for chars in endingCharsToRemove:   
                if text.rfind(chars)==len(text)-1:
                    text=text[0:len(text)-1]
                    text=text.strip()
        allNotRemoved=True
        while(allNotRemoved):
            print("track")
            allNotRemoved=False
            if(text.count("(")!=text.count(")")):
                if(text.find("(")!=0 or text.find(")")!=len(text)-1):
                    if text.find("(")==0:
                        text=text[1:len(text)]
                        allNotRemoved=True
                    elif text.rfind(")")==len(text)-1:
                        text=text[0:len(text)-1]
                        allNotRemoved=True
                    text=text.strip()
        return text

    
    def __getParamTypeAndValFromObj(parameter,paramObj,paramName):
        # if(paramObj is None):
        #     #If paramObj is none, means must be part of parameter element itself and not in its siblings, 
        #     paramObj = utils.__extractParamObj(parameter)
        if paramObj is not None:
            #If ParamObj is there, extract its TYPE and default VALUE
            paramType=utils.__extractParamType(paramObj)
            paramVal=utils.__extractParamDefaultVal(paramObj)
        elif paramName is not None and paramObj is None:
            #If paramName is there and there is no Obj, must be contained inside name. need to extract
            rawParamName=parameter.xpath(xPathConstants.TEXT).get()
            paramType=utils.__extractParamType(rawParamName)
            paramVal=utils.__extractParamDefaultVal(rawParamName)
        if paramType is not None : paramType=utils.__removeSpecialCharactersAndSpaces(paramType)
        if paramVal is not None : paramVal=utils.__removeSpecialCharactersAndSpaces(paramVal)
        return {"paramType":paramType,"paramVal":paramVal}
    
    def createLibraryItem(name,documentation,hyperUrl,functions,version):
        libraryItem = LibraryItem()
        libraryItem["name"]=name
        libraryItem["documentationUrl"]=documentation
        libraryItem["hyperParameters"]=hyperUrl
        libraryItem["functions"]=functions
        libraryItem["version"]=version
        return libraryItem
    
    def __extractParamType(paramObj):
        ##extracting parameter type before , default=
        if paramObj is None:
            return paramObj
        equalsIndex = paramObj.find("=")
        defaultIndex = paramObj.find("default")
        modifiedResult=""
        #Find if there is equals sign, if yes, get text before that as after = is value, not type
        if equalsIndex!=-1:
            modifiedResult =paramObj[0:equalsIndex]
        #Find if there is text 'default' as after that will be the value, not the type
        elif defaultIndex!=-1:
            modifiedResult = paramObj[0:defaultIndex]
        #If modifiedText is empty, return the original back
        if modifiedResult=="":
            return paramObj
        #Remove the text after last comma as mostly contains the value, making sure last comma is not part of any brackets
        lastCommaSplitterIndex = utils.__getLastCommaSplitterIndex(modifiedResult)
        if lastCommaSplitterIndex !=-1:
            return modifiedResult[0:lastCommaSplitterIndex]
        else:
            return modifiedResult
        
    def __getLastCommaSplitterIndex(paramObj):
        #This functions check for last comma splitter and makes sure its not part of any brackets and a true seperator
        lastCommaSplitterIndex = paramObj.rfind(",")
        openingSqBracket = paramObj.rfind("[")
        closingSqBracket = paramObj.rfind("]")
        openingRouBracket = paramObj.rfind("(")
        closingRouBracket = paramObj.rfind(")")
        if lastCommaSplitterIndex is not None and not (lastCommaSplitterIndex>openingSqBracket and lastCommaSplitterIndex<closingSqBracket) and not (lastCommaSplitterIndex>openingRouBracket and lastCommaSplitterIndex<closingRouBracket):
            return lastCommaSplitterIndex
        else:
            return None
        
    def __extractParamName(paramNameText):
        colonIndex = paramNameText.find(":")
        if colonIndex ==-1:
            return paramNameText
        else:
            return paramNameText[0:colonIndex]
        
    def __extractParamObj(parameterEl):
        paramObj = parameterEl.xpath(xPathConstants.PARENT+"/"+xPathConstants.TEXT).get()
        if paramObj is not None and paramObj.strip()==':':
            return None
        return paramObj
    
    def __extractParamDefaultVal(paramObj):
        #get default value after = sign
        if paramObj is None:
            return paramObj
        equalsIndex = paramObj.find("=")
        defaultIndex=paramObj.find("default")
        #Find equals sign and return value after that if any
        if equalsIndex!=-1:
            return paramObj[equalsIndex+1:len(paramObj)]
        #Find text 'default' and return value after that
        elif paramObj.find("default")!=-1:
            defaultEndIndex=defaultIndex+7
            defaultValue = paramObj[defaultEndIndex+1:len(paramObj)]
            hyphIndex = defaultValue.find(" – ")
            if(hyphIndex!=-1):
                defaultValue=defaultValue[0:hyphIndex]
            return defaultValue
        return None

    def getAllLibraryFunctions(response):
        allFunctionsOpenings = response.xpath(xPathConstants.ROUND_OPENING) #get all with text ()
        allFunctionsClosing = response.xpath(xPathConstants.ROUND_CLOSING) #get all with text ()
        filteredLibraryFuncs={}
        if len(allFunctionsOpenings)==len(allFunctionsClosing):
            funcIndex = len(allFunctionsClosing)-1
            while(funcIndex>=0):
                function = allFunctionsOpenings[funcIndex]
                functionTextEls = function.xpath(xPathConstants.PARENT_TEXT)
                functionName = utils.__extractFunctionNameFromText(functionTextEls)
                if functionName is None:
                    funcIndex-=1
                    continue
                functionArgs=utils.__extractFunctionArgs(functionName,response)
                function=Functions()
                function["method"]=functionName
                function["args"]=functionArgs
                filteredLibraryFuncs[functionName]=function
                funcIndex-=1
        return filteredLibraryFuncs

    def __extractFunctionArgs(functionName,response):
        maxTest=4
        parentXpath="/"+xPathConstants.PARENT
        while(maxTest>0):
            #FIND functionName and go to parent nodes until 'Parameters' text is not found. max parents test = maxTest var
            parameterEl = response.xpath(xPathConstants.FUNCS_PARAM_ELS.replace("<funcName>",functionName).replace("<parentXPath>",parentXpath))
            if(parameterEl is not None and len(parameterEl)==1):
                #SET maxTest=999 as an indication that it is found
                maxTest=999
                break
            maxTest-=1
            parentXpath+='/'+xPathConstants.PARENT
        #once found, parse that table
        functionParams=[]
        if(maxTest==999):
            functionParams = utils.parseParameterTable(response,xPathConstants.FUNCS_PARAM_ELS.replace("<funcName>",functionName).replace("<parentXPath>",parentXpath))
        #after parsing, remove that table from response (so that only one parameter table left of main class if any)
        if(len(functionParams)>0):
            response.xpath(xPathConstants.FUNCS_PARAM_ELS_PREFIX.replace("<funcName>",functionName).replace("<parentXPath>",parentXpath)).drop()
        argsList=[]
        for functionParam in functionParams:
            funcArgs = FunctionArgs()
            funcArgs["name"]=functionParam["paramName"]
            funcArgs["type"]=functionParam["paramType"]
            argsList.append(funcArgs)
        return argsList
    
    def __extractFunctionNameFromText(elements):
        #extract function name before roundbrackets
        funcName=''
        for element in elements:
            if(element.get()=='('):
                break
            else:
                funcName+=element.get().strip()
        searchResult = re.search(utilConstants.REGEX_FOR_FUNC_NAMES,funcName)
        if searchResult is not None and len(funcName)==searchResult.end():
            return funcName
        return None
    


