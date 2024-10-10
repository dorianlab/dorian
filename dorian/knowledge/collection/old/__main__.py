import argparse
import os
import sys
import datetime
import test_scripts
import json
from rule_manager import RuleManager
from graph_extractor import GraphExtractor
from dsp_api import DSP_API
import utils


def print_info():
    print("type help to see help")


def get_hook(path):
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location("language_hook", path)
    hook_module = importlib.util.module_from_spec(spec)
    sys.modules["language_hook"] = hook_module
    spec.loader.exec_module(hook_module)
    return hook_module.get_hook()


def is_valid_file(parser, arg):
    if not os.path.exists(arg):
        parser.error("The file %s does not exist!" % arg)
    else:
        return open(arg, 'r')  # return an open file handle


def is_valid_rule_type(parser, arg):
    if arg not in [RuleManager.RULE_TYPE_SYNTACT, RuleManager.RULE_TYPE_SEMANT]:
        parser.error(f'{arg} is not a valid rule type!'
                     f' Use {RuleManager.RULE_TYPE_SYNTACT} or {RuleManager.RULE_TYPE_SEMANT} instead.')
    else:
        return arg


def is_date_valid(parser, arg):
    try:
        datetime.date.fromisoformat(arg)
    except ValueError:
        parser.error(f'{arg} is not a valid data format! Date should be in a format YYYY-MM-DD')
    return arg


def process_init_db(api, args):
    print(api.init_db())
    pass


def process_create_pipeline(api, args):
    # do return
    # get code from file
    path = args.path.name
    with open(path, 'r') as file:
        code = file.read()
    hook = get_hook(args.hpath.name)
    api.create_pipeline(code, args.language, hook, args.wtfile, args.opath)
    pass


def process_extract_rule(api, args):
    # get graphs from files
    f1 = open(args.g1.name)
    g1 = json.load(f1)
    f2 = open(args.g2.name)
    g2 = json.load(f2)
    G1 = utils.json_to_nxgraph_for_gdiff(g1)
    G2 = utils.json_to_nxgraph_for_gdiff(g2)

    pattern, result = api.extract_rule(G1, G2, args.type)

    with open("/home/xenia/Documents/demo/outputs/pattern.json", "w") as fp:
        json.dump(pattern, fp)

    with open("/home/xenia/Documents/demo/outputs/result.json", "w") as fp:
        json.dump(result, fp)

    pass


def process_confirm_rule(args, api):
    pattern_json = open(args.ppath.name)
    pattern = json.load(pattern_json)
    result_json = open(args.rpath.name)
    result = json.load(result_json)

    api.confirm_rule(pattern, result, args.name, args.description, args.language, args.rtype, args.priority)
    pass


def process_list_rules(api, args):
    rule_list = api.list_rules(args.language)
    print(rule_list)
    return


def process_visualize_rule(api, args):
    # do return
    api.visualize_rule(args.rule_name)
    pass


def process_delete_rule(api, args):
    api.delete_rule(args.rule_name)
    pass


def process_add_rule(api, args):
    f = open(args.rule_path, "r")
    rule_dict = json.loads(f.read())
    api.add_rule(rule_dict)
    pass


def process_add_module_name(api, args):
    api.add_module(args.module_name, args.module_version, args.module_date, args.module_language)
    pass


def process_add_function(api, args):
    api.add_function(args.module_name, args.function_title, description=args.description, link=args.link, language=args.language, data_science_task=args.data_science_task, args=args.args)
    pass


def process_add_ds_task(api, args):
    api.add_ds_task(args.module_name, args.function_title, args.language, args.ds_task)
    pass


def process_add_description(api, args):
    return api.add_description(args.module_name, args.function_title, args.language, args.description)



def process_cli(argv):
    api = DSP_API()
    parser = argparse.ArgumentParser()
    cmd_parser = parser.add_subparsers(dest='command')

    # creating command parsers
    cmd_init_db = cmd_parser.add_parser("init_db")
    cmd_create_pipeline = cmd_parser.add_parser("create_pipeline")
    cmd_extract_rule = cmd_parser.add_parser("extract_rule")
    cmd_confirm_rule = cmd_parser.add_parser("confirm_rule")
    cmd_list_rules = cmd_parser.add_parser("list_rules")
    cmd_visualize_rule = cmd_parser.add_parser("visualize_rule")
    cmd_delete_rule = cmd_parser.add_parser("delete_rule")
    cmd_add_pat_rule = cmd_parser.add_parser("add_rule")
    cmd_add_module = cmd_parser.add_parser("add_module")
    cmd_add_function = cmd_parser.add_parser("add_function")
    cmd_add_ds_task = cmd_parser.add_parser("add_ds_task")
    cmd_add_description = cmd_parser.add_parser("add_description")

    # configuring create pipeline
    cmd_create_pipeline.add_argument("path",
                                     help="path to a script to be transformed into a pipeline",
                                     metavar="path to script file",
                                     type=lambda x: is_valid_file(parser, x))
    cmd_create_pipeline.add_argument("language",
                                     help="script language", metavar="script language",
                                     type=str)
    cmd_create_pipeline.add_argument("--hpath", required=False,
                                     help="path to an external hook for pre and post transformations of the pipeline",
                                     metavar="hook",
                                     type=lambda x: is_valid_file(parser, x))
    cmd_create_pipeline.add_argument("--wtfile", required=False,
                                     help="should the graph be written to a json file?",
                                     metavar="w_to_file",
                                     type=str)

    cmd_create_pipeline.add_argument("--opath", required=False,
                                     help="path to the file with the output graph",
                                     metavar="out_path",
                                     type=str)

    # configuring extract rule
    cmd_extract_rule.add_argument("g1",
                                  help="path to first graph", metavar="graph_1",
                                  type=lambda x: is_valid_file(parser, x))
    cmd_extract_rule.add_argument("g2",
                                  help="path to second graph", metavar="graph_2",
                                  type=lambda x: is_valid_file(parser, x))
    cmd_extract_rule.add_argument("type",
                                  help="rule type to be created", metavar="rule type",
                                  type=lambda x: is_valid_rule_type(parser, x))

    # configuring confirm rule
    cmd_confirm_rule.add_argument("ppath",
                                  help="path to pattern", metavar="pattern",
                                  type=lambda x: is_valid_file(parser, x))

    cmd_confirm_rule.add_argument("rpath",
                                  help="path to result", metavar="result",
                                  type=lambda x: is_valid_file(parser, x))

    cmd_confirm_rule.add_argument("name",
                                  help="rule name", metavar="rule_name",
                                  type=str)

    cmd_confirm_rule.add_argument("description",
                                  help="rule description", metavar="rule_description",
                                  type=str)

    cmd_confirm_rule.add_argument("language",
                                  help="rule language", metavar="rule_language",
                                  type=str)

    cmd_confirm_rule.add_argument("rtype",
                                  help="rule type", metavar="rule_type",
                                  type=str)

    cmd_confirm_rule.add_argument("priority",
                                  help="rule priority", metavar="rule_priority",
                                  type=str)

    # configuring list rules
    cmd_list_rules.add_argument("language",
                                help="list rules by language", metavar="language",
                                type=str)

    # configuring visualize rule
    cmd_visualize_rule.add_argument("rule_name",
                                    help="creates a visualisation of a rule by rule name", metavar="rule_name",
                                    type=str)

    # configuring delete rule
    cmd_delete_rule.add_argument("rule_name",
                                 help="removes a rule by rule name", metavar="rule_name",
                                 type=str)

    # configuring add rule
    cmd_add_pat_rule.add_argument("rule_path",
                              help="path to PaT rule", metavar="rule_path",
                              type=lambda x: is_valid_file(parser, x))

    # configuring add module
    cmd_add_module.add_argument("module_name",
                                help="module name", metavar="module_name",
                                type=str)

    cmd_add_module.add_argument("module_version",
                                help="module version", metavar="module_version",
                                type=str)

    cmd_add_module.add_argument("module_date",
                                help="date when module was scraped in a format yyyy-mm-dd", metavar="date",
                                type=str)

    cmd_add_module.add_argument("module_language",
                                help="module language", metavar="module_language",
                                type=str)

    # configuring add function
    cmd_add_function.add_argument("module_name", type=str)
    cmd_add_function.add_argument("function_title", type=str)
    cmd_add_function.add_argument("function_description", type=str)
    cmd_add_function.add_argument("function_language", type=str)
    cmd_add_function.add_argument("ds_task", type=str)
    cmd_add_function.add_argument("--dlink", required=False,
                                  help="link to documentation", metavar="link",
                                  type=str)
    cmd_add_function.add_argument("--fargs", required=False,
                                  help="function arguments in a string format, comma separated", metavar="args",
                                  type=str)

    # configuring add ds task
    cmd_add_ds_task.add_argument("module_name", type=str)
    cmd_add_ds_task.add_argument("function_title", type=str)
    cmd_add_ds_task.add_argument("language", type=str)
    cmd_add_ds_task.add_argument("ds_task", type=str)

    # configuring add description
    cmd_add_description.add_argument("module_name", type=str)
    cmd_add_description.add_argument("function_title", type=str)
    cmd_add_description.add_argument("language", type=str)
    cmd_add_description.add_argument("description", type=str)

    args = parser.parse_args()
    if args.command == 'init_db':
        process_init_db(api, args)
    elif args.command == 'create_pipeline':
        process_create_pipeline(api, args)
    elif args.command == 'extract_rule':
        process_extract_rule(api, args)
    elif args.command == 'confirm_rule':
        process_confirm_rule(args, api)
    elif args.command == 'list_rules':
        process_list_rules(api, args)
    elif args.command == 'visualize_rule':
        process_visualize_rule(api, args)
    elif args.command == 'delete_rule':
        process_delete_rule(api, args)
    elif args.command == 'add_rule':
        process_add_rule(api, args)
    elif args.command == 'add_module':
        process_add_module_name(api, args)
    elif args.command == 'add_function':
        process_add_function(api, args)
    elif args.command == 'add_ds_task':
        process_add_ds_task(api, args)
    elif args.command == 'add_description':
        process_add_description(api, args)

    return


def extract_graph(external_language_extension=None):
    language = 'python'
    code = test_scripts.Python.code_0
    extractor = GraphExtractor()
    language_hook = None
    if external_language_extension is not None:
        language_hook = external_language_extension
    extractor.extract_pipeline(code, language, language_hook)


if __name__ == "__main__":
    process_cli(sys.argv)
