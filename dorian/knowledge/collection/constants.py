class xPathConstants:
    FOLLOWING_SIBLING_ALL='following-sibling::*'
    PRECEDING_SIBLING_ALL='preceding-sibling::*'
    PARENT='parent::*'
    TEXT='text()'
    HREF='@href'
    CONTAINS_PARAMETER_TEXT='//*[contains(text(),"Parameters")]'
    CONTAINS_ROUND_BRACKETS='//*[contains(text(), "(") and contains(text(), ")")]'
    TAG_CONTAINS_TEXT='//<tag>[contains(text(),"<text>")]'
    ROUND_OPENING='//*[text()="("]'
    ROUND_CLOSING='//*[text()=")"]'
    PRECEDING_SIBLING_TEXT=PRECEDING_SIBLING_ALL+'/'+TEXT
    FOLLOWING_SIBLING_TEXT=FOLLOWING_SIBLING_ALL+'/'+TEXT
    PARENT_TEXT=PARENT+'//'+TEXT
    FUNCS_PARAM_ELS_PREFIX="//*[text()='<funcName>']<parentXPath>"
    FUNCS_PARAM_ELS=FUNCS_PARAM_ELS_PREFIX+"//*[contains(text(),'Parameters')]"
    PARAMTERS_PARENT_TABLE_LONGTABLE_CLASS="//table[contains(@class,'longtable')]"
    PAREMETERS_TABLE_INSIDE_PARENT=["tbody/tr/td[1]//@href","tbody/tr/td/a/@href"]


class utilConstants:
    REGEX_WITH_SPACE_AT_END=r'\ \w*\s*\([^)]+\)'
    REGEX_WITH_SPACE_AT_START=r'\w*\s*\([^)]+\)\ '
    REGEX_FOR_FUNC_NAMES=r'[a-zA-Z]+\w*[a-zA-Z]+'
    PARAM_OBJ_GARBAGE={"or"," or ",""}
