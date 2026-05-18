"""
Extraction utilities for Sphinx-documented Python library pages.

All functions are module-level — no wrapper class.
"""
from __future__ import annotations

import re

from dorian.knowledge.collection.constants import utilConstants, xPathConstants
from dorian.knowledge.collection.items import (
    FunctionArgs,
    Functions,
    HyperParameter,
    LibraryItem,
)


# ── Page-level helpers ───────────────────────────────────────────────

def fetch_class_title_with_url(response) -> str:
    """Extract the class/function name from the URL path."""
    url = response.url
    slash = url.rfind("/")
    dot = url.find(".html")
    return url[slash + 1 : dot]


def fetch_documentation_url(response, domain: str) -> str | None:
    """Find the 'API' or 'Reference' link on a landing page."""
    for text in ("API", "Reference"):
        tag = _get_tag_with_text(response, text)
        if tag:
            url = tag.xpath(xPathConstants.HREF).get()
            if url and ("http" not in url or domain in url):
                return url
    return None


def determine_page_type(response) -> str:
    """Determine if a Sphinx page represents a Class, Function, Method, or Attribute."""
    if response.xpath('//dl[contains(@class, "class")]'):
        return "Class"
    if response.xpath('//dl[contains(@class, "function") and not(contains(@class, "method"))]'):
        return "Function"
    if response.xpath('//dl[contains(@class, "method")]'):
        return "Method"
    if response.xpath('//dl[contains(@class, "property")]'):
        return "Attribute"
    return "Class"


# ── Item construction ────────────────────────────────────────────────

def create_library_item(
    name: str,
    documentation: str,
    hyper_params: dict,
    functions: dict,
    version: str | None,
    outputs: list | None = None,
    attributes: list | None = None,
    item_type: str = "Class",
) -> LibraryItem:
    item = LibraryItem()
    item["name"] = name
    item["documentationUrl"] = documentation
    item["hyperParameters"] = hyper_params
    item["functions"] = functions
    item["version"] = version
    item["outputs"] = outputs
    item["attributes"] = attributes
    item["type"] = item_type
    return item


def create_hyper_parameter(dtype: str | None, default: str | None) -> dict:
    hp = HyperParameter()
    hp["type"] = dtype
    hp["default"] = default
    return dict(hp)


# ── Scoped extraction (used by all spiders via BaseDocSpider) ────────

def get_scoped_parameters(response, item_type: str) -> dict:
    """Extract constructor parameters scoped to the correct container."""
    container = _get_scoped_container(response, item_type)
    if not container:
        return {}

    if item_type == "Method":
        xpath_query = ".//*[contains(text(),'Parameters')]"
    else:
        xpath_query = ".//*[contains(text(),'Parameters') and not(ancestor::dl[contains(@class, 'method')])]"

    param_table = container.xpath(xpath_query)
    params: dict = {}
    if not param_table:
        return params

    header = param_table[0]
    details = header.xpath(xPathConstants.FOLLOWING_SIBLING_ALL + "[1]//strong")

    for parameter in details:
        name = _extract_param_name(parameter.xpath(xPathConstants.TEXT).get())
        obj = _extract_param_obj(name, parameter)
        type_val = _get_param_type_and_val(parameter, obj, name)
        if name is not None:
            params[name] = create_hyper_parameter(type_val["paramType"], type_val["paramVal"])
    return params


def get_scoped_methods(response, item_type: str) -> dict:
    """Extract methods only for Class pages, scoped to the class section."""
    if item_type != "Class":
        return {}

    class_dl = _get_scoped_container(response, item_type)
    if not class_dl:
        return {}

    container = class_dl.xpath('ancestor::div[contains(@class, "section")][1]')
    if not container:
        container = class_dl.xpath("parent::*")
    if not container:
        return {}

    openings = container.xpath('.//dl[contains(@class, "method")]//*[text()="("]')
    closings = container.xpath('.//dl[contains(@class, "method")]//*[text()=")"]')

    methods: dict = {}
    if len(openings) != len(closings):
        return methods

    for i in range(len(closings) - 1, -1, -1):
        func_el = openings[i]
        func_name = None

        dt = func_el.xpath("ancestor::dt[1]")
        if dt:
            desc_name = dt.xpath('string(.//span[contains(@class, "descname")])').get()
            if desc_name:
                func_name = desc_name.strip()

        if not func_name:
            text_els = func_el.xpath(xPathConstants.PARENT_TEXT)
            func_name = _extract_function_name_from_text(text_els)

        if func_name is None or func_name in methods:
            continue

        args = _extract_function_args(func_name, response)
        ctx = func_el.xpath("..")
        outputs = _extract_function_outputs(func_name, response, ctx)

        fn = Functions()
        fn["method"] = func_name
        fn["args"] = args
        fn["outputs"] = outputs
        methods[func_name] = fn

    return methods


def get_scoped_outputs(response, item_type: str) -> list:
    """Extract outputs scoped to the item type container."""
    if item_type == "Attribute":
        container = response.xpath(
            '//dl[contains(@class, "property")]/dd/dl[contains(@class, "field-list")]'
        )
        if container:
            return _get_outputs_from_element(container)
        desc = response.xpath('//dl[contains(@class, "property")]/dd/p[1]')
        if desc:
            text = "".join(desc.xpath(".//text()").getall()).strip()
            if text:
                return [{"name": "", "type": text}]
        return []

    xpath_map = {
        "Class": '//dl[contains(@class, "class")]/dd/dl[contains(@class, "field-list")]',
        "Function": '//dl[contains(@class, "function") and not(contains(@class, "method"))]/dd/dl[contains(@class, "field-list")]',
        "Method": '//dl[contains(@class, "method")]/dd/dl[contains(@class, "field-list")]',
    }
    xpath = xpath_map.get(item_type)
    if xpath:
        container = response.xpath(xpath)
        if container:
            return _get_outputs_from_element(container)
    return []


def get_scoped_attributes(response, item_type: str) -> list:
    """Extract attributes (Class only)."""
    if item_type != "Class":
        return []
    container = response.xpath(
        '//dl[contains(@class, "class")]/dd/dl[contains(@class, "field-list")]'
    )
    if container:
        return _get_descriptive_list(container, "Attributes")
    return []


# ── Internal helpers ─────────────────────────────────────────────────

def _get_tag_with_text(response, text: str):
    return response.xpath(f'//a[contains(text(),"{text}")]')


def _get_scoped_container(response, item_type: str):
    xpaths = {
        "Class": '//dl[contains(@class, "class")]',
        "Function": '//dl[contains(@class, "function") and not(contains(@class, "method"))]',
        "Method": '//dl[contains(@class, "method")]',
        "Attribute": '//dl[contains(@class, "property")]',
    }
    xpath = xpaths.get(item_type)
    return response.xpath(xpath) if xpath else None


def _extract_param_name(text: str) -> str | None:
    if text is None:
        return None
    colon = text.find(":")
    return text[:colon] if colon != -1 else text


def _extract_param_obj(name: str | None, parameter_detail):
    sibling = parameter_detail.xpath(xPathConstants.FOLLOWING_SIBLING_TEXT).get()
    sibling = _clean_text(sibling)
    if sibling is not None and sibling not in utilConstants.PARAM_OBJ_GARBAGE:
        return sibling

    all_text = parameter_detail.xpath(xPathConstants.PARENT + "//" + xPathConstants.TEXT)
    parent_text = ""
    for elem in all_text:
        if name == elem.get():
            continue
        parent_text += elem.get()
    parent_text = _clean_text(parent_text)
    return parent_text if parent_text else None


def _clean_text(text: str | None) -> str | None:
    """Strip leading/trailing special characters and balance parentheses."""
    if text is None:
        return None
    text = text.strip()

    leading = (":", ",", ")", "-", "\u2013")
    trailing = (":", ",", "(", "\u2013", "-")

    changed = True
    while changed:
        changed = False
        for ch in leading:
            if text.startswith(ch):
                text = text[1:].strip()
                changed = True
                break
        for ch in trailing:
            if text.endswith(ch):
                text = text[:-1].strip()
                changed = True
                break

    # Balance unmatched parentheses (max 20 iterations to prevent infinite loop)
    for _ in range(20):
        if text.count("(") == text.count(")"):
            break
        if text.startswith("(") and text.count("(") > text.count(")"):
            text = text[1:].strip()
        elif text.endswith(")") and text.count(")") > text.count("("):
            text = text[:-1].strip()
        else:
            break

    return text


def _get_param_type_and_val(parameter, param_obj, param_name):
    if param_obj is not None:
        dtype = _extract_param_type(param_obj)
        default = _extract_param_default(param_obj)
    elif param_name is not None:
        raw = parameter.xpath(xPathConstants.TEXT).get()
        dtype = _extract_param_type(raw)
        default = _extract_param_default(raw)
    else:
        dtype = None
        default = None

    if dtype is not None:
        dtype = _clean_text(dtype)
    if default is not None:
        default = _clean_text(default)
    return {"paramType": dtype, "paramVal": default}


def _extract_param_type(param_obj: str | None) -> str | None:
    if param_obj is None:
        return None
    eq = param_obj.find("=")
    default_idx = param_obj.find("default")

    modified = ""
    if eq != -1:
        modified = param_obj[:eq]
    elif default_idx != -1:
        modified = param_obj[:default_idx]

    if not modified:
        return param_obj

    last_comma = _get_last_comma_splitter(modified)
    return modified[:last_comma] if last_comma and last_comma != -1 else modified


def _get_last_comma_splitter(text: str) -> int | None:
    idx = text.rfind(",")
    if idx == -1:
        return None
    osq = text.rfind("[")
    csq = text.rfind("]")
    orn = text.rfind("(")
    crn = text.rfind(")")
    if (osq < idx < csq) or (orn < idx < crn):
        return None
    return idx


def _extract_param_default(param_obj: str | None) -> str | None:
    if param_obj is None:
        return None
    eq = param_obj.find("=")
    if eq != -1:
        return param_obj[eq + 1 :]
    default_idx = param_obj.find("default")
    if default_idx != -1:
        val = param_obj[default_idx + 7 + 1 :]
        hyph = val.find(" \u2013 ")
        if hyph != -1:
            val = val[:hyph]
        return val
    if "optional" in param_obj:
        return "None"
    return None


# ── Function/method extraction ───────────────────────────────────────

def _extract_function_args(func_name: str, response) -> list:
    max_depth = 4
    parent_xpath = "/" + xPathConstants.PARENT
    found = False

    for _ in range(max_depth):
        el = response.xpath(
            xPathConstants.FUNCS_PARAM_ELS
            .replace("<funcName>", func_name)
            .replace("<parentXPath>", parent_xpath)
        )
        if el is not None and len(el) == 1:
            found = True
            break
        parent_xpath += "/" + xPathConstants.PARENT

    params = []
    if found:
        params = _parse_parameter_table(
            response,
            xPathConstants.FUNCS_PARAM_ELS
            .replace("<funcName>", func_name)
            .replace("<parentXPath>", parent_xpath),
        )

    if params:
        response.xpath(
            xPathConstants.FUNCS_PARAM_ELS_PREFIX
            .replace("<funcName>", func_name)
            .replace("<parentXPath>", parent_xpath)
        ).drop()

    result = []
    for p in params:
        fa = FunctionArgs()
        fa["name"] = p["paramName"]
        fa["type"] = p["paramType"]
        fa["default"] = p["paramVal"]
        result.append(fa)
    return result


def _extract_function_outputs(func_name: str, response, context_element=None) -> list:
    # Strategy 0: context-based relative extraction
    if context_element:
        try:
            dt = context_element.xpath("ancestor-or-self::dt")
            if dt:
                dd = dt[0].xpath("following-sibling::dd[1]")
                if dd:
                    outputs = _get_outputs_from_element(dd)
                    if outputs:
                        return outputs
        except Exception:
            pass

    # Strategy 1: Sphinx ID lookup
    try:
        candidates = response.xpath(f"//*[contains(@id, '.{func_name}')]")
        for cand in candidates:
            cid = cand.attrib.get("id", "")
            if cid.endswith(f".{func_name}"):
                container = cand.xpath("parent::*")
                outputs = _get_outputs_from_element(container)
                if outputs:
                    return outputs
    except Exception:
        pass

    # Strategy 2: text-based ancestor traversal
    max_depth = 4
    parent_xpath = "/" + xPathConstants.PARENT
    for _ in range(max_depth):
        returns_xpath = (
            xPathConstants.FUNCS_PARAM_ELS_PREFIX
            .replace("<funcName>", func_name)
            .replace("<parentXPath>", parent_xpath)
            + "//*[contains(text(),'Returns')]"
        )
        if response.xpath(returns_xpath):
            container = response.xpath(
                xPathConstants.FUNCS_PARAM_ELS_PREFIX
                .replace("<funcName>", func_name)
                .replace("<parentXPath>", parent_xpath)
            )
            return _get_outputs_from_element(container)
        parent_xpath += "/" + xPathConstants.PARENT

    return []


def _extract_function_name_from_text(elements) -> str | None:
    name = ""
    for el in elements:
        if el.get() == "(":
            break
        name += el.get().strip()
    match = re.search(utilConstants.REGEX_FOR_FUNC_NAMES, name)
    if match and len(name) == match.end():
        return name
    return None


def _parse_parameter_table(response, xpath: str) -> list:
    result = []
    el = response.xpath(xpath)
    details = el.xpath(xPathConstants.FOLLOWING_SIBLING_ALL + "//strong")
    if details is not None and len(el) == 1:
        details = el.xpath(xPathConstants.FOLLOWING_SIBLING_ALL + "[1]//strong")
        for param in details:
            name = _extract_param_name(param.xpath(xPathConstants.TEXT).get())
            if name and name.startswith(">"):
                continue
            obj = _extract_param_obj(name, param)
            tv = _get_param_type_and_val(param, obj, name)
            if name is not None:
                result.append({
                    "paramName": name,
                    "paramType": tv["paramType"],
                    "paramVal": tv["paramVal"],
                })
    return result


# ── Descriptive list extraction (Returns, Attributes) ────────────────

def _extract_name_and_type(dt):
    raw = " ".join(dt.xpath(".//text()").getall()).replace("\n", " ").strip()
    while "  " in raw:
        raw = raw.replace("  ", " ")

    name = ""
    type_text = raw

    tag_name = dt.xpath("string(./strong)").get().strip()
    tag_type = dt.xpath("string(./span[@class='classifier'] | ./em)").get().strip()

    if tag_name:
        name = tag_name
        rest = raw.replace(name, "", 1).strip()
        if rest.startswith(":") or rest.startswith("-"):
            rest = rest[1:].strip()
        type_text = rest
    elif tag_type:
        type_text = tag_type
        rest = raw.replace(type_text, "", 1).strip()
        if rest.endswith(":") or rest.endswith("-"):
            rest = rest[:-1].strip()
        name = rest
    else:
        if ":" in raw:
            parts = raw.split(":", 1)
            name = parts[0].strip()
            type_text = parts[1].strip()
        else:
            name = raw
            type_text = ""

    return name, type_text


def _get_descriptive_list(response, header_name: str) -> list:
    """Extract name/type items from a definition list following a header."""
    items = []
    renamed_blacklist = set()

    header_dt = response.xpath(f".//dt[contains(., '{header_name}')]")
    if not header_dt:
        return items

    content_dd = header_dt.xpath("following-sibling::dd[1]")
    if not content_dd:
        return items

    sub_dls = content_dd.xpath(".//dl")
    if not sub_dls:
        return items

    main_dl = sub_dls[0]
    for dt in main_dl.xpath("./dt"):
        name, type_text = _extract_name_and_type(dt)

        parent_dd = dt.xpath("following-sibling::dd[1]")
        is_deprecated = False
        if parent_dd:
            dd_text = " ".join(parent_dd.xpath(".//p//text()").getall()).lower()
            if "deprecated" in dd_text:
                is_deprecated = True
            match = re.search(r"([\w_]+)\s+was renamed to", dd_text)
            if match:
                renamed_blacklist.add(match.group(1))

        if (type_text or name) and not is_deprecated:
            items.append({"name": name, "type": type_text})

    return [it for it in items if it["name"] not in renamed_blacklist]


def _get_outputs_from_element(response) -> list:
    return _get_descriptive_list(response, "Returns")


# ── Legacy aliases (backward compatibility for old imports) ───────────

class utils:
    """Deprecated: use module-level functions instead."""
    fetchClassTitleWithUrl = staticmethod(fetch_class_title_with_url)
    fetchDocumentationUrl = staticmethod(fetch_documentation_url)
    determinePageType = staticmethod(determine_page_type)
    createLibraryItem = staticmethod(
        lambda name, documentation, hyperUrl, functions, version, outputs=None, attributes=None, type="Class":
            create_library_item(name, documentation, hyperUrl, functions, version, outputs, attributes, type)
    )
    createHyperParameter = staticmethod(create_hyper_parameter)
    getHyperParametersFromTable = staticmethod(lambda response: get_scoped_parameters(response, "Class"))
    getAllLibraryFunctions = staticmethod(lambda response: get_scoped_methods(response, "Class"))
    getScopedParameters = staticmethod(get_scoped_parameters)
    getScopedMethods = staticmethod(get_scoped_methods)
    getScopedOutputs = staticmethod(get_scoped_outputs)
    getScopedAttributes = staticmethod(get_scoped_attributes)
    getOutputsFromElement = staticmethod(_get_outputs_from_element)
    getAttributesFromElement = staticmethod(lambda response: _get_descriptive_list(response, "Attributes"))
    parseParameterTable = staticmethod(_parse_parameter_table)
